from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta

from app.config import QR_TTL_SECONDS, SESSION_DAYS
from app.db import log_op
from app.errors import AppError, AuthError
from app.qrsvg import make_qr_svg
from app.services.sync import sync_account
from app.timeutil import iso, now_local, parse_dt


def _purge_expired_session(conn: sqlite3.Connection, now: datetime) -> None:
    row = conn.execute("SELECT expires_at FROM sessions WHERE id = 1").fetchone()
    if row and parse_dt(row["expires_at"]) <= now:
        conn.execute("DELETE FROM sessions WHERE id = 1")
        conn.commit()


def public_session(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or now_local()
    _purge_expired_session(conn, now)
    row = conn.execute(
        """
        SELECT s.mode, s.created_at, s.expires_at, shops.shop_id, shops.mall_id, shops.name
        FROM sessions s
        JOIN shops ON shops.shop_id = s.shop_id
        WHERE s.id = 1
        """
    ).fetchone()
    if row is None:
        from app.adapters.pdd_session import storage_state_is_logged_in, storage_state_path
        from app.db import database_path

        return {
            "logged_in": False,
            "resume_available": storage_state_is_logged_in(storage_state_path(database_path(conn))),
        }
    return {
        "logged_in": True,
        "mode": row["mode"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "shop": {
            "shop_id": row["shop_id"],
            "mall_id": row["mall_id"],
            "name": row["name"],
        },
    }


def require_session(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    session = public_session(conn, now)
    if not session["logged_in"]:
        from app.adapters.pdd_normalize import YINGXIAO_SYNC_HINT

        raise AuthError("推广中心尚未登录，测图测款未写入。" + YINGXIAO_SYNC_HINT)
    return session


def create_login_ticket(conn: sqlite3.Connection, mode: str, now: datetime | None = None) -> dict:
    if mode not in {"mock", "live"}:
        raise AppError("未知数据模式")
    now = now or now_local()
    if mode == "live":
        from app.services.live_login import start_live_login

        return start_live_login(conn, now)
    from app.services.live_login import cancel_jobs

    cancel_jobs(force=True)
    ticket = secrets.token_urlsafe(18)
    expires = now + timedelta(seconds=QR_TTL_SECONDS)
    payload = f"pddmon://mock-confirm?ticket={ticket}"
    svg, scannable = make_qr_svg(payload)
    with conn:
        conn.execute("DELETE FROM login_tickets WHERE expires_at < ?", (iso(now - timedelta(days=1)),))
        conn.execute(
            "INSERT INTO login_tickets(ticket, mode, status, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
            (ticket, mode, "pending", iso(now), iso(expires)),
        )
    return {
        "ready": True,
        "mode": "mock",
        "ticket": ticket,
        "expires_at": iso(expires),
        "ttl_seconds": QR_TTL_SECONDS,
        "qr_svg": svg,
        "scannable": scannable,
        "hint": "这是本机演示二维码，手机扫描不会登录真实拼多多。请点击「模拟已在手机确认」。",
    }


def poll_ticket(conn: sqlite3.Connection, ticket: str, now: datetime | None = None) -> dict:
    now = now or now_local()
    row = conn.execute("SELECT * FROM login_tickets WHERE ticket = ?", (ticket,)).fetchone()
    if row is None:
        raise AppError("登录票据不存在", status=404, code="ticket_missing")
    if row["mode"] == "live":
        from app.services.live_login import poll_live_job

        return poll_live_job(conn, ticket, row, now)
    status = row["status"]
    if status == "pending" and parse_dt(row["expires_at"]) <= now:
        conn.execute("UPDATE login_tickets SET status = 'expired' WHERE ticket = ?", (ticket,))
        conn.commit()
        status = "expired"
    return {"ticket": ticket, "status": status, "mode": row["mode"]}


def confirm_ticket(conn: sqlite3.Connection, ticket: str, now: datetime | None = None) -> dict:
    now = now or now_local()
    row = conn.execute("SELECT * FROM login_tickets WHERE ticket = ?", (ticket,)).fetchone()
    if row is None:
        raise AppError("登录票据不存在", status=404, code="ticket_missing")
    if row["mode"] != "mock":
        raise AppError("真实店铺不能用模拟确认", status=409, code="live_unconfirmed")
    if row["status"] == "confirmed":
        session = public_session(conn, now)
        if session["logged_in"]:
            return session
        raise AppError("登录票据已使用", status=409, code="ticket_used")
    if row["status"] != "pending":
        raise AppError("二维码已失效，请刷新后再试", status=410, code="ticket_expired")
    if parse_dt(row["expires_at"]) <= now:
        conn.execute("UPDATE login_tickets SET status = 'expired' WHERE ticket = ?", (ticket,))
        conn.commit()
        raise AppError("二维码已过期，请刷新后再试", status=410, code="ticket_expired")

    synced = sync_account(conn, mode="mock", now=now)
    token = secrets.token_urlsafe(24)
    expires = now + timedelta(days=SESSION_DAYS)
    with conn:
        conn.execute(
            """
            INSERT INTO sessions(id, shop_id, mode, token, created_at, expires_at)
            VALUES(1, ?, 'mock', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              shop_id = excluded.shop_id,
              mode = excluded.mode,
              token = excluded.token,
              created_at = excluded.created_at,
              expires_at = excluded.expires_at
            """,
            (synced["shop_id"], token, iso(now), iso(expires)),
        )
        conn.execute("UPDATE login_tickets SET status = 'confirmed' WHERE ticket = ?", (ticket,))
        log_op(conn, "登录", f"模拟登录 {synced['shop_name']}")
    return public_session(conn, now)


def logout(conn: sqlite3.Connection) -> dict:
    from app.adapters.pdd_session import remove_session_files
    from app.db import database_path
    from app.services.live_login import begin_logout

    begin_logout()
    remove_session_files(database_path(conn))
    session = public_session(conn)
    with conn:
        conn.execute("DELETE FROM sessions WHERE id = 1")
    if session["logged_in"]:
        log_op(conn, "退出", f"已退出 {session['shop']['name']}。本地历史仍保留。")
    else:
        log_op(conn, "退出", "当前没有登录会话")
    return {"logged_in": False, "kept_history": True}
