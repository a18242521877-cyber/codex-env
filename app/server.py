"""只监听本机的 HTTP 服务。页面和接口都从这里出。"""

from __future__ import annotations

import json
import logging
import mimetypes
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import OFFICIAL_LOGIN_URL
from app.db import connect, init_db
from app.errors import AppError
from app.services.auth import (
    confirm_ticket,
    create_login_ticket,
    logout,
    poll_ticket,
    public_session,
    require_session,
)
from app.services.dashboard import build_dashboard
from app.services.exporting import daily_markdown, metrics_csv
from app.services.maintenance import reset_local_data
from app.services.readmodel import (
    campaigns_for_day,
    data_bounds,
    goods_for_day,
    images_for_day,
    last_sync,
    list_alerts,
    list_logs,
    mark_alerts_read,
    unread_count,
)
from app.services.recommendations import build_plan, set_item_status
from app.services.summary import build_summary
from app.services.sync import sync_account
from app.settings import load_settings, settings_payload, update_settings
from app.timeutil import iso, now_local, parse_day
from app.version import __version__

logger = logging.getLogger(__name__)
UI_DIR = Path(__file__).resolve().parent / "ui"
STATIC = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, db_path: str):
        super().__init__(server_address, Handler)
        self.db_path = db_path


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        conn = connect(self.server.db_path)
        try:
            # 先读完 body。否则 keep-alive 下，没读掉的 `{}` 会粘到下一个请求行上，
            # 浏览器就会看到一闪而过的「请求失败」。
            self._raw_body = self._read_raw_body()
            init_db(conn)
            if not self._host_allowed():
                self._json(403, {"error": "推广台只接受本机访问", "code": "forbidden_host"})
                return
            if not self._origin_allowed():
                self._json(403, {"error": "拒绝来自其他网站的请求", "code": "forbidden_origin"})
                return
            if method in {"POST", "PUT"} and self.headers.get("X-Pdd-Monitor") != "1":
                self._json(403, {"error": "缺少本地请求头", "code": "forbidden_client"})
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            self._route(method, path, query, conn)
        except AppError as exc:
            self._json(exc.status, {"error": str(exc), "code": exc.code})
        except ValueError as exc:
            self._json(400, {"error": str(exc), "code": "bad_request"})
        except Exception:
            logger.exception("请求处理失败 %s %s", method, self.path)
            self._json(500, {"error": "内部错误", "code": "internal"})
        finally:
            conn.close()

    def _route(self, method: str, path: str, query: dict, conn) -> None:
        now = self._clock()
        if method == "GET" and path in STATIC:
            self._static(STATIC[path])
            return
        if method == "GET" and path == "/api/health":
            self._json(200, {"ok": True, "version": __version__, "app": "推广台"})
            return
        if method == "GET" and path == "/api/settings":
            self._json(200, settings_payload(conn))
            return
        if method == "PUT" and path == "/api/settings":
            self._json(200, update_settings(conn, self._body()))
            return
        if method == "GET" and path == "/api/session":
            self._json(200, public_session(conn, now))
            return
        if method == "POST" and path == "/api/auth/qr":
            mode = load_settings(conn).data_mode
            self._json(200, create_login_ticket(conn, mode, now))
            return
        if method == "POST" and path == "/api/auth/open":
            from app.services.live_login import open_login_gate

            gate = str((self._body() or {}).get("gate") or "yingxiao")
            self._json(200, open_login_gate(conn, now, gate))
            return
        if method == "POST" and path == "/api/auth/sync-ready":
            from app.services.live_login import sync_when_ready

            self._json(200, sync_when_ready(conn, now))
            return
        if method == "POST" and path == "/api/auth/logout":
            self._json(200, logout(conn))
            return
        if method == "POST" and path == "/api/data/reset":
            body = self._body()
            self._json(200, reset_local_data(conn, str(body.get("confirm", ""))))
            return

        parts = [part for part in path.split("/") if part]
        if method == "GET" and parts[:3] == ["api", "auth", "qr"] and len(parts) == 4:
            self._json(200, poll_ticket(conn, parts[3], now))
            return
        if method == "POST" and parts[:3] == ["api", "auth", "qr"] and len(parts) == 5 and parts[4] == "confirm":
            self._json(200, confirm_ticket(conn, parts[3], now))
            return

        session = require_session(conn, now)
        shop_id = session["shop"]["shop_id"]
        if method == "GET" and path == "/api/bootstrap":
            self._json(200, self._bootstrap(conn, session))
            return
        if method == "POST" and path == "/api/sync":
            mode = load_settings(conn).data_mode
            if session["mode"] != mode:
                raise AppError(
                    "当前登录和数据模式不一致。请退出后，打开对应登录页自己完成登录，再点「同步」。",
                    status=409,
                    code="mode_mismatch",
                )
            self._json(200, sync_account(conn, mode=session["mode"], now=now))
            return
        if method == "GET" and path == "/api/dashboard":
            self._json(200, build_dashboard(conn, shop_id, self._day(query), now))
            return
        if method == "GET" and path == "/api/campaigns":
            self._json(200, {"date": self._day(query).isoformat(), "items": campaigns_for_day(conn, shop_id, self._day(query), now)})
            return
        if method == "GET" and path == "/api/goods-tests":
            day = self._day(query)
            self._json(200, {"date": day.isoformat(), "items": goods_for_day(conn, shop_id, day)})
            return
        if method == "GET" and path == "/api/image-tests":
            day = self._day(query)
            self._json(200, {"date": day.isoformat(), "items": images_for_day(conn, shop_id, day)})
            return
        if method == "GET" and path == "/api/summary":
            day = self._day(query)
            self._json(200, build_summary(conn, shop_id, day, now, record_log=False))
            return
        if method == "POST" and path == "/api/summary/refresh":
            day = self._day(query, self._body())
            self._json(200, build_summary(conn, shop_id, day, now, record_log=True))
            return
        if method == "GET" and path == "/api/plans":
            day = self._day(query)
            self._json(200, build_plan(conn, shop_id, day, now, record_log=False))
            return
        if method == "POST" and path == "/api/plans/refresh":
            day = self._day(query, self._body())
            self._json(200, build_plan(conn, shop_id, day, now, record_log=True))
            return
        if method == "POST" and path == "/api/plans/status":
            body = self._body()
            day = parse_day(str(body.get("date", "")))
            item = set_item_status(conn, shop_id, day, str(body.get("item_key", "")), str(body.get("status", "")))
            self._json(200, item)
            return
        if method == "GET" and path == "/api/alerts":
            self._json(200, {"unread": unread_count(conn, shop_id), "items": list_alerts(conn, shop_id)})
            return
        if method == "POST" and path == "/api/alerts/read":
            body = self._body()
            updated = mark_alerts_read(
                conn,
                shop_id,
                day=body.get("date"),
                code=body.get("code"),
                mark_all=bool(body.get("all")),
                stamp=iso(now),
            )
            self._json(200, {"updated": updated, "unread": unread_count(conn, shop_id)})
            return
        if method == "GET" and path == "/api/logs":
            self._json(200, {"items": list_logs(conn)})
            return
        if method == "GET" and path == "/api/export/daily.md":
            day = self._day(query)
            text = daily_markdown(conn, shop_id, day, now).encode("utf-8")
            self._bytes(
                200,
                text,
                "text/markdown; charset=utf-8",
                f"attachment; filename=daily-{day.isoformat()}.md",
            )
            return
        if method == "GET" and path == "/api/export/metrics.csv":
            day = self._day(query)
            payload = metrics_csv(conn, shop_id, day)
            self._bytes(
                200,
                payload,
                "text/csv; charset=utf-8",
                f"attachment; filename=metrics-{day.isoformat()}.csv",
            )
            return
        if method == "GET" and path == "/api/meta":
            self._json(
                200,
                {
                    "official_login_url": OFFICIAL_LOGIN_URL,
                    "version": __version__,
                    "bounds": data_bounds(conn, shop_id),
                    "last_sync": last_sync(conn),
                },
            )
            return
        self._json(404, {"error": "没有这个接口", "code": "not_found"})

    def _bootstrap(self, conn, session: dict) -> dict:
        shop_id = session["shop"]["shop_id"]
        return {
            "session": session,
            "today": date.today().isoformat(),
            "bounds": data_bounds(conn, shop_id),
            "unread_alerts": unread_count(conn, shop_id),
            "last_sync": last_sync(conn),
            "version": __version__,
            "official_login_url": OFFICIAL_LOGIN_URL,
        }

    def _day(self, query: dict, body: dict | None = None) -> date:
        raw = None
        if body and body.get("date"):
            raw = str(body["date"])
        elif query.get("date"):
            raw = query["date"][0]
        if not raw:
            return date.today()
        return parse_day(raw)

    def _clock(self) -> datetime:
        import os

        if os.environ.get("PDD_MONITOR_TEST_CLOCK") != "1":
            return now_local()
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        raw = (query.get("now") or [None])[0]
        if not raw:
            return now_local()
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None:
            value = value.replace(tzinfo=now_local().tzinfo)
        return value.replace(microsecond=0)

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_000_000:
            raise AppError("请求体过大")
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _body(self) -> dict:
        raw = getattr(self, "_raw_body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError("JSON 无法解析") from exc
        if not isinstance(data, dict):
            raise AppError("JSON 必须是对象")
        return data

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in {"127.0.0.1", "localhost"}

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        port = self.server.server_address[1]
        return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port in {port, None}

    def _static(self, name: str) -> None:
        path = (UI_DIR / name).resolve()
        if UI_DIR.resolve() not in path.parents and path != UI_DIR.resolve():
            self._json(404, {"error": "文件不存在", "code": "not_found"})
            return
        if not path.is_file():
            self._json(404, {"error": "文件不存在", "code": "not_found"})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".html", ".css", ".js"}:
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
            }[path.suffix]
        self._bytes(200, path.read_bytes(), content_type, cache="no-store")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._bytes(code, body, "application/json; charset=utf-8")

    def _bytes(self, code: int, body: bytes, content_type: str, disposition: str | None = None, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)
