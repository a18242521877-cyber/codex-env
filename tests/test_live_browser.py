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
MMS_HOME_HTML = """<!doctype html><html><body>
<div class="shop-name">夹具店铺</div>
<a href="/goods/promotion/report">商品推广</a>
<script>fetch("/api/mms/profile.json");</script>
</body></html>"""
PROMO_PAGE_HTML = """<!doctype html><html><body>
<button type="button" id="week">近7天</button>
<button type="button" id="gen">生成报表</button>
<script>
document.getElementById("gen").onclick = function () { fetch("/api/generated-report.json"); };
document.getElementById("week").onclick = function () {
  fetch("/api/goods/promotion/list.json?access_token=should-not-store");
};
</script>
</body></html>"""
PROFILE_JSON = """{
  "mall_id": 753391861,
  "mall_name": "御品渔获专营店",
  "permissionList": [],
  "packList": [],
  "mikasaErrorCode": 54001,
  "result": null
}"""
PROMO_LIST_JSON = """{
  "mallId": 753391861,
  "mallName": "御品渔获专营店",
  "result": {"list": [
    {"statDate": "2026-10-24", "goodsId": 9001, "goodsName": "渔获单品",
     "impression": 10, "click": 2, "spend": 3.5, "gmv": 8, "orderNum": 1}
  ]}
}"""
GENERATED_JSON = """{
  "mallId": 1,
  "result": {"list": [
    {"statDate": "2026-10-24", "goodsId": 1, "goodsName": "不应出现",
     "impression": 1, "click": 1, "spend": 1, "gmv": 1, "orderNum": 1}
  ]}
}"""
EFFECT_HTML = """<!doctype html><html><body>
<div class="shop-name">夹具店铺</div>
<script>fetch("/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms");</script>
</body></html>"""
SSO_HTML = """<!doctype html><html><body><p>推广中心授权</p></body></html>"""
SYDNEY_JSON = """{
  "success": true,
  "result": {
    "mallId": 753391861,
    "mallName": "御品渔获专营店",
    "yesData": {"payOrdrAmt": "5538.6", "payOrdrCnt": "122", "gpv": "2722", "payUvRto": "6.23%"}
  }
}"""
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
            "/mms/home": ("text/html; charset=utf-8", MMS_HOME_HTML.encode()),
            "/mms/home/": ("text/html; charset=utf-8", MMS_HOME_HTML.encode()),
            "/goods/promotion/report": ("text/html; charset=utf-8", PROMO_PAGE_HTML.encode()),
            "/goods/promotion/report/": ("text/html; charset=utf-8", PROMO_PAGE_HTML.encode()),
            "/api/mms/profile.json": ("application/json", PROFILE_JSON.encode()),
            "/api/goods/promotion/list.json": ("application/json", PROMO_LIST_JSON.encode()),
            "/api/generated-report.json": ("application/json", GENERATED_JSON.encode()),
            "/sycm/goods_effect/detail": ("text/html; charset=utf-8", EFFECT_HTML.encode()),
            "/login/sso": ("text/html; charset=utf-8", SSO_HTML.encode()),
            "/sydney/api/goodsDataShow/queryGoodsPageOverviewForMms": ("application/json", SYDNEY_JSON.encode()),
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

    def test_home_json_is_not_enough_until_promo_page_loads(self):
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
                ("promo-ticket", iso(now), iso(now)),
            )
            conn.commit()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            driver = PlaywrightLiveDriver(
                login_url=base + "/login/",
                report_urls=[base + "/mms/home/"],
                headless=True,
            )
            job = LoginJob("promo-ticket")
            driver.run(job, db_path)
            self.assertEqual(job.snapshot()["status"], "confirmed", job.snapshot())
            names = [row["name"] for row in conn.execute("SELECT name FROM goods")]
            self.assertEqual(names, ["渔获单品"])
            spend = conn.execute(
                "SELECT spend_fen FROM goods_daily WHERE stat_date = '2026-10-24'"
            ).fetchone()
            self.assertEqual(spend["spend_fen"], 350)
            shop = conn.execute(
                """
                SELECT shops.shop_id, shops.name
                FROM sessions s
                JOIN shops ON shops.shop_id = s.shop_id
                """
            ).fetchone()
            self.assertEqual(shop["shop_id"], "live-753391861")
            self.assertEqual(shop["name"], "御品渔获专营店")
            from app.adapters.pdd_session import capture_path

            raw = capture_path(db_path).read_text(encoding="utf-8")
            self.assertNotIn("should-not-store", raw)
            self.assertIn("/api/goods/promotion/list.json", raw)
            self.assertIn("/api/mms/profile.json", raw)
            self.assertNotIn("不应出现", raw)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_sso_stuck_still_keeps_yesterday_goods_effect(self):
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
                ("sydney-ticket", iso(now), iso(now)),
            )
            conn.commit()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            driver = PlaywrightLiveDriver(
                login_url=base + "/login/",
                report_urls=[base + "/sycm/goods_effect/detail", base + "/login/sso"],
                headless=True,
                sso_probe_seconds=0.4,
                sso_wait_seconds=0,
            )
            job = LoginJob("sydney-ticket")
            driver.run(job, db_path)
            self.assertEqual(job.snapshot()["status"], "confirmed", job.snapshot())
            row = conn.execute(
                """
                SELECT g.stat_date, g.spend_fen, g.gmv_fen, g.orders
                FROM goods_daily g
                JOIN sessions s ON s.shop_id = g.shop_id
                """
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["spend_fen"], 0)
            self.assertEqual(row["gmv_fen"], 553860)
            self.assertEqual(row["orders"], 122)
            details = " ".join(item["detail"] for item in conn.execute("SELECT detail FROM operation_logs"))
            self.assertIn("商品数据", details)
            self.assertIn("没有写入推广花费", details)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
