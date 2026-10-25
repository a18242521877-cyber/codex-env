"""用本地夹具页走一遍 Playwright 登录和报表捕获。不访问拼多多。"""

from __future__ import annotations

import base64
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

from app.adapters.pdd_browser import PlaywrightLiveDriver
from app.adapters.pdd_session import storage_state_path
from app.db import connect, init_db
from app.services.live_login import LoginJob
from app.timeutil import iso, now_local

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
LOGIN_HTML = """<!doctype html><html><body>
<img class="qrcode" src="/qr.png" alt="qr" />
<script>
setTimeout(function () {
  document.cookie = "PASS_ID=fixture-session; path=/";
  location.href = "/home/";
}, 300);
</script>
</body></html>"""
HOME_HTML = """<!doctype html><html><body><div class="shop-name">夹具店铺</div></body></html>"""
REPORT_HTML = """<!doctype html><html><body>
<div id="report"></div>
<a download href="/goods.csv">下载</a>
<script>
fetch("/api/report.json").then(function (r) { return r.json(); }).then(function () {
  document.getElementById("report").textContent = "ok";
});
</script>
</body></html>"""
REPORT_JSON = """{
  "mallId": 10001,
  "mallName": "夹具店铺",
  "result": {"list": [
    {"statDate": "2026-10-24", "goodsId": 9001, "goodsName": "夹具衣架", "scene": "商品推广",
     "impression": 1000, "click": 50, "spend": 12.5, "gmv": 40.0, "orderNum": 4, "dailyBudget": 100.0},
    {"statDate": "2026-10-24", "creativeId": "c1", "creativeName": "主图A", "goodsId": 9001,
     "impression": 400, "click": 30, "spend": 5.0, "gmv": 20.0, "orderNum": 2}
  ]}
}"""
CSV = "日期,商品名称,商品ID,推广计划,曝光,点击,花费,成交笔数,交易额,日预算\n2026-10-23,夹具衣架,9001,商品推广,800,40,10.00,3,30.00,100\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        routes = {
            "/login": ("text/html; charset=utf-8", LOGIN_HTML.encode()),
            "/login/": ("text/html; charset=utf-8", LOGIN_HTML.encode()),
            "/qr.png": ("image/png", PNG),
            "/home": ("text/html; charset=utf-8", HOME_HTML.encode()),
            "/home/": ("text/html; charset=utf-8", HOME_HTML.encode()),
            "/report": ("text/html; charset=utf-8", REPORT_HTML.encode()),
            "/report/": ("text/html; charset=utf-8", REPORT_HTML.encode()),
            "/api/report.json": ("application/json", REPORT_JSON.encode()),
            "/goods.csv": ("text/csv; charset=utf-8", CSV.encode("utf-8")),
        }
        if path not in routes:
            body = b"no"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        ctype, body = routes[path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _chromium_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception:
        return False
    return True


@unittest.skipUnless(_chromium_ready(), "本机没有 Playwright Chromium，跳过浏览器夹具")
class BrowserFixtureTests(unittest.TestCase):
    def test_login_and_report_capture(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "browser.sqlite"
        conn = connect(db_path)
        self.addCleanup(conn.close)
        try:
            init_db(conn)
            now = now_local()
            conn.execute(
                "INSERT INTO login_tickets(ticket, mode, status, created_at, expires_at) VALUES(?, 'live', 'pending', ?, ?)",
                ("fixture-ticket", iso(now), iso(now)),
            )
            conn.commit()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            driver = PlaywrightLiveDriver(
                login_url=base + "/login/",
                report_urls=[base + "/report/"],
                headless=True,
            )
            job = LoginJob("fixture-ticket")
            driver.run(job, db_path)
            self.assertEqual(job.snapshot()["status"], "confirmed", job.snapshot())
            storage = storage_state_path(db_path)
            self.assertTrue(storage.is_file())
            self.assertEqual(storage.stat().st_mode & 0o777, 0o600)
            row = conn.execute("SELECT mode, token FROM sessions WHERE id = 1").fetchone()
            self.assertEqual(row["mode"], "live")
            self.assertEqual(row["token"], "storage")
            goods = conn.execute("SELECT name FROM goods").fetchone()
            self.assertEqual(goods["name"], "夹具衣架")
            spend = conn.execute(
                "SELECT spend_fen FROM goods_daily WHERE stat_date = '2026-10-24'"
            ).fetchone()
            self.assertEqual(spend["spend_fen"], 1250)
            label = conn.execute("SELECT label FROM image_tests").fetchone()
            self.assertEqual(label["label"], "主图A")
            extra = conn.execute(
                "SELECT spend_fen FROM goods_daily WHERE stat_date = '2026-10-23'"
            ).fetchone()
            self.assertIsNotNone(extra)
            self.assertEqual(extra["spend_fen"], 1000)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
