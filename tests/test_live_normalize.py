"""真实报表归一化。不访问拼多多。"""

from __future__ import annotations

import io
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

from app.adapters.pdd_browser import is_export_label
from app.adapters.pdd_normalize import money_to_fen, normalize_captures
from app.adapters.scripted_driver import ScriptedLiveDriver, scripted_payload
from app.db import connect, init_db
from app.scheduler import Scheduler
from app.services.auth import create_login_ticket, poll_ticket, public_session
from app.services.live_login import use_driver
from app.services.recommendations import build_plan
from app.services.summary import build_summary
from app.services.sync import persist_payload
from app.settings import update_settings
from app.timeutil import iso, now_local


TODAY = date(2026, 10, 25)


class NormalizeTests(unittest.TestCase):
    def test_money_units(self):
        self.assertEqual(money_to_fen(1250, source="json"), 1250)
        self.assertEqual(money_to_fen(12.5, source="json"), 1250)
        self.assertEqual(money_to_fen("12.50", source="json"), 1250)
        self.assertEqual(money_to_fen("1250", source="json"), 1250)
        self.assertEqual(money_to_fen("12.50", source="csv"), 1250)
        self.assertEqual(money_to_fen(12, source="xlsx"), 1200)

    def test_json_keeps_image_spend_out_of_goods(self):
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "body": {
                        "mallId": 10001,
                        "mallName": "夹具店铺",
                        "result": {
                            "list": [
                                {
                                    "statDate": "2026-10-24",
                                    "goodsId": 9001,
                                    "goodsName": "夹具衣架",
                                    "scene": "商品推广",
                                    "impression": 1000,
                                    "click": 50,
                                    "spend": 12.5,
                                    "gmv": 40.0,
                                    "orderNum": 4,
                                    "dailyBudget": 100.0,
                                },
                                {
                                    "statDate": "2026-10-24",
                                    "creativeId": "c1",
                                    "creativeName": "主图A",
                                    "goodsId": 9001,
                                    "impression": 400,
                                    "click": 30,
                                    "spend": 5.0,
                                    "gmv": 20.0,
                                    "orderNum": 2,
                                },
                            ]
                        },
                    },
                }
            ],
            today=TODAY,
        )
        self.assertEqual(payload.shop.name, "夹具店铺")
        self.assertEqual(payload.shop.shop_id, "live-10001")
        goods = payload.goods[0]
        self.assertEqual(payload.goods_days[goods.goods_id][0].spend_fen, 1250)
        image = payload.images[0]
        self.assertEqual(image.label, "主图A")
        self.assertEqual(payload.image_days[image.image_id][0].spend_fen, 500)
        campaign_spend = payload.campaign_days[goods.campaign_id][0].spend_fen
        self.assertEqual(campaign_spend, 1250)

    def test_explicit_campaign_is_not_summed_from_goods(self):
        payload = normalize_captures(
            [
                {
                    "kind": "json",
                    "body": [
                        {
                            "planId": 3,
                            "planName": "搜索计划",
                            "scene": "搜索推广",
                            "statDate": "2026-10-24",
                            "spend": 5000,
                            "impression": 100,
                        },
                        {
                            "goodsId": 8,
                            "goodsName": "抽屉盒",
                            "planId": 3,
                            "statDate": "2026-10-24",
                            "spend": 2000,
                            "impression": 40,
                        },
                    ],
                }
            ],
            today=TODAY,
        )
        self.assertEqual(payload.campaigns[0].name, "搜索计划")
        self.assertEqual(payload.campaign_days[payload.campaigns[0].campaign_id][0].spend_fen, 5000)
        self.assertEqual(payload.goods_days[payload.goods[0].goods_id][0].spend_fen, 2000)
        self.assertEqual(payload.goods[0].campaign_id, payload.campaigns[0].campaign_id)

    def test_csv_gbk_amounts_are_yuan(self):
        text = "日期,商品名称,商品ID,推广计划,曝光,点击,花费,成交笔数,交易额,日预算\n2026-10-23,国标衣架,7,商品推广,800,40,10.00,3,30.00,100\n"
        payload = normalize_captures([{"kind": "csv", "body": text.encode("gbk")}], today=TODAY)
        day = payload.goods_days[payload.goods[0].goods_id][0]
        self.assertEqual(day.stat_date, "2026-10-23")
        self.assertEqual(day.spend_fen, 1000)
        self.assertEqual(day.gmv_fen, 3000)
        self.assertEqual(payload.campaign_days[payload.goods[0].campaign_id][0].spend_fen, 1000)

    def test_xlsx_amounts_are_yuan(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["日期", "商品名称", "商品ID", "花费", "交易额", "曝光", "点击", "成交笔数"])
        sheet.append([date(2026, 10, 24), "表格款", 42, 8.5, 20, 100, 10, 1])
        buffer = io.BytesIO()
        book.save(buffer)
        payload = normalize_captures([{"kind": "xlsx", "body": buffer.getvalue()}], today=TODAY)
        day = payload.goods_days[payload.goods[0].goods_id][0]
        self.assertEqual(day.spend_fen, 850)
        self.assertEqual(day.stat_date, "2026-10-24")

    def test_empty_capture_does_not_invent_numbers(self):
        payload = normalize_captures([{"kind": "json", "body": {"ok": True}}], shop_hint="空店", today=TODAY)
        self.assertEqual(payload.goods, [])
        self.assertEqual(payload.campaigns, [])
        self.assertIn("没有从报表", payload.note)
        self.assertEqual(payload.shop.name, "空店")

    def test_export_label_skips_generate(self):
        self.assertTrue(is_export_label("下载"))
        self.assertTrue(is_export_label("导出报表"))
        self.assertFalse(is_export_label("生成报表"))
        self.assertFalse(is_export_label("请先生成报表再下载今天的全店明细"))

    def test_summary_and_plan_use_fetched_numbers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = connect(Path(tmp.name) / "live.sqlite")
        self.addCleanup(conn.close)
        init_db(conn)
        payload = scripted_payload(date.today())
        persist_payload(conn, payload, synced_at=iso(now_local()))
        yesterday = date.today() - timedelta(days=1)
        summary = build_summary(conn, payload.shop.shop_id, yesterday, now_local())
        self.assertIn("夹具真实店", summary["narrative"])
        self.assertIn("500.00", summary["narrative"])
        plan = build_plan(conn, payload.shop.shop_id, yesterday, now_local())
        actions = {item["action"] for item in plan["items"]}
        self.assertIn("goods_pause", actions)


class LiveLoginStructureTests(unittest.TestCase):
    def test_scheduler_refreshes_a_live_session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "sched.sqlite"
        conn = connect(db_path)
        self.addCleanup(conn.close)
        init_db(conn)
        update_settings(conn, {"data_mode": "live", "refresh_interval_minutes": "30"})
        use_driver(ScriptedLiveDriver())
        self.addCleanup(lambda: use_driver(None))
        now = now_local()
        ticket = create_login_ticket(conn, "live", now)
        polled = {}
        for _ in range(40):
            polled = poll_ticket(conn, ticket["ticket"], now)
            if polled["status"] in {"confirmed", "failed", "expired"}:
                break
            time.sleep(0.05)
        self.assertEqual(polled["status"], "confirmed", polled)
        stale = iso(now - timedelta(hours=2))
        conn.execute("UPDATE sync_runs SET finished_at = ? WHERE status = 'success'", (stale,))
        conn.commit()
        time.sleep(0.2)
        Scheduler(db_path).tick()
        session = public_session(conn)
        self.assertEqual(session["mode"], "live")
        count = conn.execute("SELECT COUNT(*) AS n FROM goods_daily").fetchone()["n"]
        self.assertGreaterEqual(count, 1)
