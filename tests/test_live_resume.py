"""扫码回传被取消时不能删掉已经写下的登录。不访问拼多多。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.adapters.pdd_normalize import normalize_captures
from app.adapters.pdd_session import (
    capture_path,
    page_looks_logged_in,
    storage_state_is_logged_in,
    storage_state_path,
    write_private_text,
)
from app.adapters.scripted_driver import ScriptedLiveDriver
from app.db import connect, init_db
from app.services.auth import poll_ticket, public_session
from app.services.live_login import (
    LoginJob,
    begin_logout,
    cancel_jobs,
    current_generation,
    finish_live_session,
    remember_job,
    start_live_login,
    update_live_payload,
    use_driver,
)
from app.services.maintenance import reset_local_data
from app.timeutil import now_local


def _db():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "monitor.sqlite"
    conn = connect(path)
    init_db(conn)
    return tmp, path, conn


def _pass_state(value: str = "fixture-pass") -> str:
    return json.dumps(
        {
            "cookies": [
                {
                    "name": "PASS_ID",
                    "value": value,
                    "domain": ".pinduoduo.com",
                    "path": "/",
                }
            ],
            "origins": [],
        },
        ensure_ascii=False,
    )


def _shop_capture() -> str:
    return json.dumps(
        [
            {
                "kind": "json",
                "body": {"mallId": 753391861, "mallName": "御品渔获专营店"},
            }
        ],
        ensure_ascii=False,
    )


class LoginDetectionTests(unittest.TestCase):
    def test_page_login_signals(self):
        self.assertFalse(page_looks_logged_in({"api_uid"}, "https://mms.pinduoduo.com/login/"))
        self.assertTrue(page_looks_logged_in({"api_uid"}, "https://mms.pinduoduo.com/home/"))
        self.assertTrue(page_looks_logged_in({"PASS_ID"}, "https://mms.pinduoduo.com/home/"))
        self.assertFalse(page_looks_logged_in({"PASS_ID"}, "https://mms.pinduoduo.com/login/"))
        self.assertTrue(page_looks_logged_in({"windows_app_shop_token_23"}, "https://mms.pinduoduo.com/home/"))
        self.assertFalse(page_looks_logged_in(set(), "https://mms.pinduoduo.com/home/"))

    def test_storage_resume_ignores_api_uid_alone(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "storage_state.json"
        write_private_text(path, json.dumps({"cookies": [{"name": "api_uid", "value": "x"}], "origins": []}))
        self.assertFalse(storage_state_is_logged_in(path))
        write_private_text(path, _pass_state())
        self.assertTrue(storage_state_is_logged_in(path))
        write_private_text(
            path,
            json.dumps({"cookies": [{"name": "windows_app_shop_token_9", "value": "x"}], "origins": []}),
        )
        self.assertTrue(storage_state_is_logged_in(path))


class FinishSurvivesCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.path, self.conn = _db()
        self.addCleanup(self.conn.close)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: use_driver(None))

    def test_finish_then_cancel_keeps_session_and_storage(self):
        write_private_text(storage_state_path(self.path), _pass_state())
        payload = normalize_captures(
            [{"kind": "json", "body": {"mallId": 753391861, "mallName": "御品渔获专营店"}}],
            shop_hint="御品渔获专营店",
            today=date.today(),
        )
        job = LoginJob("finish-then-cancel", db_path=str(self.path), generation=current_generation())
        remember_job(job)
        result = finish_live_session(self.path, payload, job.ticket, generation=job.generation)
        self.assertFalse(result.get("skipped"))
        job.update(status="confirmed", finished=True, cookies_saved=True)
        cancel_jobs()
        self.assertFalse(job.snapshot()["cancelled"])
        cancel_jobs(force=True)
        self.assertTrue(job.snapshot()["cancelled"])
        session = public_session(self.conn)
        self.assertTrue(session["logged_in"])
        self.assertEqual(session["shop"]["shop_id"], "live-753391861")
        self.assertEqual(session["shop"]["name"], "御品渔获专营店")
        self.assertTrue(storage_state_path(self.path).is_file())
        goods = self.conn.execute("SELECT COUNT(*) AS n FROM goods").fetchone()["n"]
        self.assertEqual(goods, 0)

    def test_cancel_during_harvest_does_not_delete_saved_cookies(self):
        state = storage_state_path(self.path)
        write_private_text(state, _pass_state())
        payload = normalize_captures([], shop_hint="拼多多店铺", today=date.today())
        job = LoginJob("harvest-cancel", db_path=str(self.path), generation=current_generation())
        job.update(cookies_saved=True, message="已扫码，正在读取推广报表")
        remember_job(job)
        finish_live_session(self.path, payload, job.ticket, generation=job.generation)
        cancel_jobs()
        self.assertFalse(job.snapshot()["cancelled"])
        self.assertTrue(state.is_file())
        self.assertTrue(public_session(self.conn)["logged_in"])
        job.update(cancelled=True)
        from app.services.live_login import retain_saved_login

        retain_saved_login(self.path, job)
        self.assertTrue(state.is_file())
        self.assertEqual(public_session(self.conn)["mode"], "live")

    def test_scripted_login_then_cancel_keeps_file(self):
        job = LoginJob("scripted", db_path=str(self.path), generation=current_generation())
        ScriptedLiveDriver().run(job, self.path)
        self.assertEqual(job.snapshot()["status"], "confirmed")
        cancel_jobs(force=True)
        self.assertTrue(storage_state_path(self.path).is_file())
        self.assertTrue(public_session(self.conn)["logged_in"])

    def test_cancel_before_cookies_leaves_existing_storage(self):
        state = storage_state_path(self.path)
        write_private_text(state, _pass_state())
        job = LoginJob("early-cancel", db_path=str(self.path), generation=current_generation())
        job.update(cancelled=True)
        ScriptedLiveDriver().run(job, self.path)
        self.assertTrue(state.is_file())
        self.assertIn("PASS_ID", state.read_text(encoding="utf-8"))
        self.assertFalse(public_session(self.conn)["logged_in"])

    def test_empty_metrics_still_confirms(self):
        payload = normalize_captures([], shop_hint="御品渔获专营店", today=date.today())
        self.assertEqual(payload.goods, [])
        self.assertEqual(payload.campaigns, [])
        self.assertIn("没有从报表", payload.note)
        result = finish_live_session(self.path, payload, "empty-metrics", generation=current_generation())
        self.assertEqual(result["shop_name"], "御品渔获专营店")
        self.assertEqual(result["goods"], 0)
        session = public_session(self.conn)
        self.assertTrue(session["logged_in"])
        self.assertEqual(session["mode"], "live")
        self.assertEqual(session["shop"]["name"], "御品渔获专营店")

    def test_saved_pass_id_resumes_without_browser(self):
        class ExplodingDriver:
            def check_ready(self):
                raise AssertionError("已有登录文件时不应再打开浏览器")

            def run(self, job, db_path):
                raise AssertionError("已有登录文件时不应再打开浏览器")

        write_private_text(storage_state_path(self.path), _pass_state())
        write_private_text(capture_path(self.path), _shop_capture())
        use_driver(ExplodingDriver())
        started = start_live_login(self.conn, now_local())
        self.assertTrue(started["resumed"])
        polled = poll_ticket(self.conn, started["ticket"])
        self.assertEqual(polled["status"], "confirmed")
        session = public_session(self.conn)
        self.assertEqual(session["shop"]["shop_id"], "live-753391861")
        self.assertEqual(session["shop"]["name"], "御品渔获专营店")
        again = start_live_login(self.conn, now_local())
        self.assertEqual(again["ticket"], started["ticket"])

    def test_poll_confirmed_when_session_exists_even_if_job_cancelled(self):
        payload = normalize_captures(
            [{"kind": "json", "body": {"mallId": 753391861, "mallName": "御品渔获专营店"}}],
            today=date.today(),
        )
        now = now_local()
        ticket = "poll-confirmed"
        self.conn.execute(
            "INSERT INTO login_tickets(ticket, mode, status, created_at, expires_at) VALUES(?, 'live', 'pending', ?, ?)",
            (ticket, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        finish_live_session(self.path, payload, ticket, generation=current_generation())
        job = LoginJob(ticket, db_path=str(self.path), generation=current_generation())
        job.update(status="pending", cancelled=True, message="窗口被关掉了")
        remember_job(job)
        polled = poll_ticket(self.conn, ticket)
        self.assertEqual(polled["status"], "confirmed")

    def test_failed_poll_offers_resume_when_storage_exists(self):
        write_private_text(storage_state_path(self.path), _pass_state())
        now = now_local()
        ticket = "poll-failed"
        self.conn.execute(
            "INSERT INTO login_tickets(ticket, mode, status, created_at, expires_at) VALUES(?, 'live', 'pending', ?, ?)",
            (ticket, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        job = LoginJob(ticket, db_path=str(self.path))
        job.update(status="failed", message="浏览器操作失败，请重试。")
        remember_job(job)
        polled = poll_ticket(self.conn, ticket)
        self.assertEqual(polled["status"], "failed")
        self.assertEqual(polled["message"], "浏览器操作失败，请重试。")
        self.assertTrue(polled["resume_available"])

    def test_logout_generation_skips_late_finish_and_reset_drops_session(self):
        payload = normalize_captures([], shop_hint="御品渔获专营店", today=date.today())
        generation = current_generation()
        finish_live_session(self.path, payload, "before-reset", generation=generation)
        self.assertTrue(public_session(self.conn)["logged_in"])
        reset_local_data(self.conn, "清空")
        self.assertFalse(public_session(self.conn)["logged_in"])
        self.assertFalse(storage_state_path(self.path).exists())
        late = finish_live_session(self.path, payload, "after-reset", generation=generation)
        self.assertTrue(late.get("skipped"))
        self.assertFalse(public_session(self.conn)["logged_in"])
        begin_logout()
        self.assertIsNone(update_live_payload(self.path, payload, generation=current_generation() - 1))
