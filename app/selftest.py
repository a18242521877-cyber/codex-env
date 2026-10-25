"""通过本机 HTTP 走一遍登录、报表、日报和调整方案。"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from app.server import AppServer


def _keepalive_ok(port: int) -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Content-Type": "application/json", "X-Pdd-Monitor": "1"}
        conn.request("POST", "/api/auth/qr", body=b"{}", headers=headers)
        first = conn.getresponse()
        first.read()
        if first.status != 200:
            return False
        conn.request("GET", "/api/session", headers={"X-Pdd-Monitor": "1"})
        second = conn.getresponse()
        payload = second.read()
        if second.status != 200:
            return False
        return b"logged_in" in payload
    finally:
        conn.close()


def run_self_test() -> int:
    os.environ["PDD_MONITOR_DISABLE_SCHEDULER"] = "1"
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "selftest.sqlite"
    httpd = AppServer(("127.0.0.1", 0), str(db_path))
    thread = threading.Thread(target=httpd.serve_forever, name="selftest-http", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if ok:
            print(f"[通过] {name}")
            return
        failures.append(name)
        print(f"[失败] {name} {detail}".rstrip())

    def request(method: str, path: str, body: dict | None = None, *, header: bool = True):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method=method)
        if header:
            req.add_header("X-Pdd-Monitor", "1")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return resp.status, json.loads(raw.decode("utf-8"))
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                payload = raw
            return exc.code, payload

    try:
        status, health = request("GET", "/api/health")
        check("健康检查", status == 200 and health.get("ok") is True, str(health))

        status, page = request("GET", "/")
        text = page.decode("utf-8") if isinstance(page, bytes) else str(page)
        status_js, script = request("GET", "/app.js")
        js = script.decode("utf-8") if isinstance(script, bytes) else str(script)
        check(
            "首页是中文界面",
            status == 200 and status_js == 200 and "推广台" in text and "测图" in js and "每日总结" in js,
            f"html={status} js={status_js}",
        )

        status, denied = request("POST", "/api/auth/qr", {}, header=False)
        check("缺少本地请求头会被拒绝", status == 403, str(denied))
        check("连续请求不会粘包", _keepalive_ok(httpd.server_address[1]), "POST 之后的 GET 应仍是 200")

        status, live = request("PUT", "/api/settings", {"data_mode": "live"})
        check("可以切换到真实模式", status == 200 and live["values"]["data_mode"] == "live", str(live))
        status, qr = request("POST", "/api/auth/qr", {})
        check(
            "真实模式不伪造扫码",
            status == 200 and qr.get("ready") is False and "mms.pinduoduo.com" in qr.get("official_login_url", ""),
            str(qr),
        )

        status, bad = request("PUT", "/api/settings", {"pause_roi": "9", "target_roi": "2"})
        check("非法阈值会被拒绝", status == 400, str(bad))
        status, mock = request("PUT", "/api/settings", {"data_mode": "mock"})
        check("切回模拟模式", status == 200 and mock["values"]["data_mode"] == "mock")

        status, qr = request("POST", "/api/auth/qr", {})
        check("模拟二维码", status == 200 and qr.get("ready") is True and "<svg" in qr.get("qr_svg", ""), str(qr)[:180])
        ticket = qr.get("ticket", "")
        status, polled = request("GET", f"/api/auth/qr/{ticket}")
        check("扫码前是待确认", status == 200 and polled.get("status") == "pending", str(polled))
        status, session = request("POST", f"/api/auth/qr/{ticket}/confirm", {})
        check(
            "模拟确认后进入店铺",
            status == 200 and session.get("logged_in") is True and session.get("shop", {}).get("name") == "云岭家居旗舰店",
            str(session),
        )

        status, dash = request("GET", "/api/dashboard")
        check(
            "总览有花费和计划",
            status == 200 and len(dash.get("kpis", [])) >= 6 and len(dash.get("campaigns", [])) == 4,
            str(dash)[:240],
        )
        status, images = request("GET", "/api/image-tests")
        check("测图有三张主图", status == 200 and len(images.get("items", [])) == 3, str(images)[:180])
        status, goods = request("GET", "/api/goods-tests")
        check("测款有五个商品", status == 200 and len(goods.get("items", [])) == 5, str(goods)[:180])

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        status, summary = request("GET", f"/api/summary?date={yesterday}")
        check(
            "昨日总结",
            status == 200 and summary.get("complete") is True and "云岭家居旗舰店" in summary.get("narrative", ""),
            str(summary)[:240],
        )
        status, today = request("GET", "/api/summary")
        check("今日总结标明未完结", status == 200 and today.get("complete") is False, str(today)[:180])

        status, plan = request("GET", f"/api/plans?date={yesterday}")
        actions = {item["action"] for item in plan.get("items", [])} if isinstance(plan, dict) else set()
        check(
            "调整方案含放量、暂停和换主图",
            status == 200 and {"goods_scale", "goods_pause", "image_set_primary"} <= actions,
            str(sorted(actions)),
        )
        pause = next((item for item in plan.get("items", []) if item["action"] == "goods_pause"), None)
        status, updated = request(
            "POST",
            "/api/plans/status",
            {"date": yesterday, "item_key": pause["item_key"] if pause else "", "status": "done"},
        )
        check("建议可以标记已执行", status == 200 and updated.get("status") == "done", str(updated)[:180])
        status, plan_again = request("GET", f"/api/plans?date={yesterday}")
        kept = next((item for item in plan_again.get("items", []) if item["action"] == "goods_pause"), None)
        check("重新计算后仍保留执行状态", kept is not None and kept["status"] == "done", str(kept))

        status, alerts = request("GET", "/api/alerts")
        check("预警已生成", status == 200 and alerts.get("unread", 0) >= 1, str(alerts)[:180])

        status, csv_body = request("GET", f"/api/export/metrics.csv?date={yesterday}")
        csv_ok = isinstance(csv_body, bytes) and csv_body.startswith(b"\xef\xbb\xbf") and "无痕衣架".encode() in csv_body
        check("CSV 带 BOM 且含测款", status == 200 and csv_ok)
        status, markdown = request("GET", f"/api/export/daily.md?date={yesterday}")
        md = markdown.decode("utf-8") if isinstance(markdown, bytes) else str(markdown)
        check("Markdown 日报含调整方案", status == 200 and "调整方案" in md and "云岭家居旗舰店" in md)

        status, logs = request("GET", "/api/logs")
        actions_logged = {row["action"] for row in logs.get("items", [])} if isinstance(logs, dict) else set()
        check("操作日志记下登录", status == 200 and "登录" in actions_logged, str(actions_logged))

        status, logged_out = request("POST", "/api/auth/logout", {})
        check("退出登录", status == 200 and logged_out.get("logged_in") is False, str(logged_out))
        status, blocked = request("GET", "/api/dashboard")
        check("退出后不能看报表", status == 401, str(blocked))

        status, qr = request("POST", "/api/auth/qr", {})
        status, session = request("POST", f"/api/auth/qr/{qr.get('ticket')}/confirm", {})
        status, goods = request("GET", "/api/goods-tests")
        check("退出后历史仍在", status == 200 and len(goods.get("items", [])) == 5, str(goods)[:160])

        status, reset = request("POST", "/api/data/reset", {"confirm": "清空"})
        check("清空本地数据", status == 200 and reset.get("ok") is True, str(reset))
        status, session = request("GET", "/api/session")
        check("清空后需要重新登录", status == 200 and session.get("logged_in") is False, str(session))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        tmp.cleanup()

    if failures:
        print(f"自检未通过：{len(failures)} 项")
        return 1
    print("自检通过")
    return 0
