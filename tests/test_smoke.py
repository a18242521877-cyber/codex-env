"""规则和本机流程自检。不访问拼多多。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.adapters.pdd_live import PddLiveAdapter
from app.db import connect, init_db
from app.errors import AdapterNotReady, AppError
from app.metrics import classify_pace
from app.selftest import run_self_test
from app.services.auth import confirm_ticket, create_login_ticket, logout, public_session
from app.services.recommendations import (
    build_plan,
    campaign_recommendations,
    set_item_status,
)
from app.services.summary import build_summary
from app.settings import load_settings, update_settings
from app.timeutil import now_local


def _afternoon():
    return now_local().replace(hour=15, minute=0, second=0, microsecond=0)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "rules.sqlite")
        init_db(self.conn)
        self.now = _afternoon()
        ticket = create_login_ticket(self.conn, "mock", self.now)
        confirm_ticket(self.conn, ticket["ticket"], self.now)
        self.shop_id = public_session(self.conn, self.now)["shop"]["shop_id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_yesterday_actions(self):
        yesterday = self.now.date() - timedelta(days=1)
        plan = build_plan(self.conn, self.shop_id, yesterday, self.now)
        actions = {(item["action"], item["target_id"]) for item in plan["items"]}
        expected = {
            ("goods_scale", "goods-hanger-20"),
            ("goods_fix_listing", "goods-wide-hanger"),
            ("goods_pause", "goods-drawer-box"),
            ("goods_mild_up", "goods-quilt"),
            ("goods_observe", "goods-shelf"),
            ("image_set_primary", "img-hanger-a"),
            ("image_pause", "img-hanger-c"),
        }
        self.assertTrue(expected <= actions, actions)
        present = {action for action, _target in actions}
        self.assertNotIn("campaign_cut_budget", present)
        self.assertNotIn("campaign_lower_bid", present)
        self.assertNotIn("pacing_fast", present)
        self.assertNotIn("pacing_slow", present)

    def test_today_adds_pacing_without_dropping_goods_rules(self):
        plan = build_plan(self.conn, self.shop_id, self.now.date(), self.now)
        actions = {(item["action"], item["target_id"]) for item in plan["items"]}
        self.assertIn(("pacing_fast", "camp-search-hanger"), actions)
        self.assertIn(("pacing_slow", "camp-scene-quilt"), actions)
        self.assertIn(("goods_scale", "goods-hanger-20"), actions)
        self.assertIn(("goods_mild_up", "goods-quilt"), actions)

    def test_status_survives_rebuild(self):
        yesterday = self.now.date() - timedelta(days=1)
        plan = build_plan(self.conn, self.shop_id, yesterday, self.now)
        key = next(item["item_key"] for item in plan["items"] if item["action"] == "goods_pause")
        set_item_status(self.conn, self.shop_id, yesterday, key, "done")
        again = build_plan(self.conn, self.shop_id, yesterday, self.now)
        kept = next(item for item in again["items"] if item["item_key"] == key)
        self.assertEqual(kept["status"], "done")

    def test_summary_mentions_shop_and_incomplete_today(self):
        yesterday = self.now.date() - timedelta(days=1)
        summary = build_summary(self.conn, self.shop_id, yesterday, self.now)
        self.assertTrue(summary["complete"])
        self.assertIn("云岭家居旗舰店", summary["narrative"])
        self.assertGreaterEqual(len(summary["kpis"]), 6)
        today = build_summary(self.conn, self.shop_id, self.now.date(), self.now)
        self.assertFalse(today["complete"])
        self.assertIn("尚未结束", today["narrative"])

    def test_logout_keeps_rows(self):
        logout(self.conn)
        self.assertFalse(public_session(self.conn)["logged_in"])
        count = self.conn.execute("SELECT COUNT(*) AS n FROM goods").fetchone()["n"]
        self.assertEqual(count, 5)

    def test_invalid_thresholds(self):
        with self.assertRaises(AppError):
            update_settings(self.conn, {"pause_roi": "5", "target_roi": "2"})
        self.assertEqual(load_settings(self.conn).pause_roi, 1)

    def test_live_adapter_is_explicitly_not_ready(self):
        with self.assertRaises(AdapterNotReady):
            PddLiveAdapter().fetch(today=self.now.date(), now=self.now)

    def test_pace_classifier(self):
        settings = load_settings(self.conn)
        self.assertEqual(classify_pace(0.92, 1.2, self.now, settings), "fast")
        self.assertEqual(classify_pace(0.25, 2.5, self.now, settings), "slow")
        self.assertIsNone(classify_pace(0.92, 1.2, self.now.replace(hour=9), settings))
        self.assertIsNone(classify_pace(0.25, 2.5, self.now.replace(hour=11), settings))

    def test_campaign_rules_are_pure(self):
        settings = load_settings(self.conn)
        raised = campaign_recommendations(
            [
                {
                    "campaign_id": "c-raise",
                    "name": "搜索高ROI",
                    "scene": "搜索推广",
                    "bid_fen": 100,
                    "daily_budget_fen": 10000,
                    "days": 7,
                    "impressions": 20000,
                    "clicks": 800,
                    "orders": 80,
                    "spend_fen": 65000,
                    "gmv_fen": 260000,
                }
            ],
            set(),
            settings,
        )
        self.assertEqual(raised[0]["action"], "campaign_raise_budget")
        cut = campaign_recommendations(
            [
                {
                    "campaign_id": "c-cut",
                    "name": "全站低ROI",
                    "scene": "全站推广",
                    "bid_fen": None,
                    "daily_budget_fen": 20000,
                    "days": 7,
                    "impressions": 9000,
                    "clicks": 300,
                    "orders": 10,
                    "spend_fen": 50000,
                    "gmv_fen": 20000,
                }
            ],
            set(),
            settings,
        )
        self.assertEqual(cut[0]["action"], "campaign_cut_budget")
        low_roi = {
            "campaign_id": "c-cut",
            "name": "全站低ROI",
            "scene": "全站推广",
            "bid_fen": None,
            "daily_budget_fen": 20000,
            "days": 7,
            "impressions": 9000,
            "clicks": 300,
            "orders": 10,
            "spend_fen": 50000,
            "gmv_fen": 20000,
        }
        self.assertEqual(campaign_recommendations([low_roi], {"c-cut"}, settings), [])


class HttpSelfTest(unittest.TestCase):
    def test_http_self_test(self):
        self.assertEqual(run_self_test(), 0)


if __name__ == "__main__":
    unittest.main()
