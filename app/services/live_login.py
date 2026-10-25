"""真实店铺扫码。浏览器只在本机打开，会话文件不进数据库。"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.adapters.pdd_session import remove_session_files
from app.config import LIVE_LOGIN_SECONDS, OFFICIAL_LOGIN_URL, SESSION_DAYS
from app.db import connect, database_path, init_db, log_op
from app.errors import AppError
from app.timeutil import iso

logger = logging.getLogger(__name__)

_jobs: dict[str, "LoginJob"] = {}
_jobs_guard = threading.Lock()
_browser_lock = threading.Lock()
_driver = None


class LoginJob:
    def __init__(self, ticket: str):
        self.ticket = ticket
        self._status = "pending"
        self._message = "正在打开拼多多商家登录页…"
        self._qr_image: str | None = None
        self._cancelled = False
        self._lock = threading.Lock()

    def update(self, **kwargs) -> None:
        with self._lock:
            if "status" in kwargs:
                self._status = kwargs["status"]
            if "message" in kwargs:
                self._message = kwargs["message"]
            if "qr_image" in kwargs:
                self._qr_image = kwargs["qr_image"]
            if "cancelled" in kwargs:
                self._cancelled = bool(kwargs["cancelled"])

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "message": self._message,
                "qr_image": self._qr_image,
                "cancelled": self._cancelled,
            }


def use_driver(driver) -> None:
    global _driver
    _driver = driver


def current_driver():
    if _driver is not None:
        return _driver
    from app.adapters.pdd_browser import PlaywrightLiveDriver

    return PlaywrightLiveDriver()


def cancel_jobs() -> None:
    with _jobs_guard:
        jobs = list(_jobs.values())
    for job in jobs:
        job.update(cancelled=True)


def browser_is_busy() -> bool:
    acquired = _browser_lock.acquire(blocking=False)
    if acquired:
        _browser_lock.release()
        return False
    return True


def acquire_browser(timeout: float) -> bool:
    return _browser_lock.acquire(timeout=timeout)


def release_browser() -> None:
    try:
        _browser_lock.release()
    except RuntimeError:
        pass


def start_live_login(conn: sqlite3.Connection, now: datetime) -> dict:
    driver = current_driver()
    driver.check_ready()
    cancel_jobs()
    if not _browser_lock.acquire(timeout=8):
        raise AppError("浏览器正忙，请稍后再试", status=409, code="browser_busy")
    ticket = secrets.token_urlsafe(18)
    expires = now + timedelta(seconds=LIVE_LOGIN_SECONDS)
    job = LoginJob(ticket)
    try:
        with conn:
            conn.execute(
                "DELETE FROM login_tickets WHERE expires_at < ?",
                (iso(now - timedelta(days=1)),),
            )
            conn.execute(
                "INSERT INTO login_tickets(ticket, mode, status, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
                (ticket, "live", "pending", iso(now), iso(expires)),
            )
        with _jobs_guard:
            _jobs[ticket] = job
        thread = threading.Thread(
            target=_worker,
            args=(job, str(database_path(conn))),
            name="pdd-login",
            daemon=True,
        )
        thread.start()
    except Exception:
        release_browser()
        raise
    log_op(conn, "登录", "已打开真实店铺登录窗口")
    return {
        "ready": True,
        "mode": "live",
        "ticket": ticket,
        "expires_at": iso(expires),
        "ttl_seconds": LIVE_LOGIN_SECONDS,
        "official_login_url": OFFICIAL_LOGIN_URL,
        "qr_image": None,
        "hint": "请用「拼多多商家版」App 扫描弹出窗口里的二维码。个人版拼多多扫不了商家后台。",
        "message": "正在打开拼多多商家登录页…",
    }


def _worker(job: LoginJob, db_path: str) -> None:
    try:
        current_driver().run(job, db_path)
        snap = job.snapshot()
        if snap["cancelled"]:
            return
        if snap["status"] == "pending":
            job.update(status="failed", message="登录未完成")
    except AppError as exc:
        job.update(status="failed", message=str(exc))
    except Exception:
        logger.exception("live login failed")
        job.update(status="failed", message="浏览器操作失败，请重试。")
    finally:
        release_browser()


def poll_live_job(conn: sqlite3.Connection, ticket: str, row, now: datetime) -> dict:
    with _jobs_guard:
        job = _jobs.get(ticket)
    if job is not None:
        snap = job.snapshot()
        status = "expired" if snap["cancelled"] else snap["status"]
        return {
            "ticket": ticket,
            "status": status,
            "mode": "live",
            "message": snap["message"],
            "qr_image": snap["qr_image"],
        }
    from app.timeutil import parse_dt

    status = row["status"]
    if status == "pending" and parse_dt(row["expires_at"]) <= now:
        conn.execute("UPDATE login_tickets SET status = 'expired' WHERE ticket = ?", (ticket,))
        conn.commit()
        status = "expired"
    return {
        "ticket": ticket,
        "status": status,
        "mode": "live",
        "message": "登录窗口已关闭，请重新打开。",
        "qr_image": None,
    }


def mark_ticket(db_path: str | Path, ticket: str, status: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE login_tickets SET status = ? WHERE ticket = ? AND status = 'pending'",
            (status, ticket),
        )
        conn.commit()
    finally:
        conn.close()


def finish_live_session(db_path: str | Path, payload, ticket: str) -> dict:
    from app.services.sync import describe_sync, finish_run, persist_payload
    from app.timeutil import now_local

    conn = connect(db_path)
    try:
        init_db(conn)
        now = now_local()
        cur = conn.execute(
            "INSERT INTO sync_runs(shop_id, mode, status, message, started_at) VALUES(?, ?, ?, ?, ?)",
            (None, "live", "running", "开始同步", iso(now)),
        )
        conn.commit()
        run_id = int(cur.lastrowid)
        try:
            counts = persist_payload(conn, payload, synced_at=iso(now))
            message = describe_sync(payload, counts)
            finish_run(conn, run_id, payload.shop.shop_id, "success", message)
            log_op(conn, "同步", message)
        except Exception:
            finish_run(conn, run_id, None, "failed", "同步失败")
            raise
        expires = now + timedelta(days=SESSION_DAYS)
        with conn:
            conn.execute(
                """
                INSERT INTO sessions(id, shop_id, mode, token, created_at, expires_at)
                VALUES(1, ?, 'live', 'storage', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  shop_id = excluded.shop_id,
                  mode = excluded.mode,
                  token = excluded.token,
                  created_at = excluded.created_at,
                  expires_at = excluded.expires_at
                """,
                (payload.shop.shop_id, iso(now), iso(expires)),
            )
            conn.execute("UPDATE login_tickets SET status = 'confirmed' WHERE ticket = ?", (ticket,))
            log_op(conn, "登录", f"真实店铺登录 {payload.shop.name}")
        return {"message": message, "shop_id": payload.shop.shop_id, "shop_name": payload.shop.name, **counts}
    finally:
        conn.close()


def drop_live_login(db_path: str | Path) -> None:
    remove_session_files(db_path)
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM sessions WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
