"""真实店铺扫码。浏览器只在本机打开，会话文件不进数据库。"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.adapters.pdd_session import (
    capture_path,
    remove_session_files,
    storage_state_is_logged_in,
    storage_state_path,
)
from app.config import LIVE_LOGIN_SECONDS, OFFICIAL_LOGIN_URL, SESSION_DAYS
from app.db import connect, database_path, init_db, log_op
from app.errors import AppError
from app.timeutil import iso

logger = logging.getLogger(__name__)

_jobs: dict[str, "LoginJob"] = {}
_jobs_guard = threading.Lock()
_browser_lock = threading.Lock()
_auth_lock = threading.Lock()
_auth_generation = 0
_driver = None

READING_MESSAGE = "已扫码，正在读取推广报表。请只扫弹出的浏览器窗口，先不要点「重新打开登录」。"


class LoginJob:
    def __init__(self, ticket: str, *, db_path: str = "", generation: int | None = None):
        self.ticket = ticket
        self.db_path = str(db_path)
        self.generation = generation
        self._status = "pending"
        self._message = "正在打开拼多多商家登录页…"
        self._qr_image: str | None = None
        self._cancelled = False
        self._cookies_saved = False
        self._finished = False
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
            if "cookies_saved" in kwargs:
                self._cookies_saved = bool(kwargs["cookies_saved"])
            if "finished" in kwargs:
                self._finished = bool(kwargs["finished"])

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "message": self._message,
                "qr_image": self._qr_image,
                "cancelled": self._cancelled,
                "cookies_saved": self._cookies_saved,
                "finished": self._finished,
            }


def use_driver(driver) -> None:
    global _driver
    _driver = driver


def current_driver():
    if _driver is not None:
        return _driver
    from app.adapters.pdd_browser import PlaywrightLiveDriver

    return PlaywrightLiveDriver()


def current_generation() -> int:
    with _auth_lock:
        return _auth_generation


def generation_is_current(generation: int | None) -> bool:
    if generation is None:
        return True
    return generation == current_generation()


def begin_logout() -> None:
    """退出登录。进行中的回写看到代数变化后不再改会话。"""
    global _auth_generation
    with _auth_lock:
        _auth_generation += 1
    cancel_jobs(force=True)


def cancel_jobs(*, force: bool = False) -> None:
    """普通取消不动已经扫完或正在收报表的任务。退出登录才强制停浏览器。"""
    with _jobs_guard:
        jobs = list(_jobs.values())
    for job in jobs:
        snap = job.snapshot()
        if not force and (snap["finished"] or snap["status"] == "confirmed" or snap["cookies_saved"]):
            continue
        job.update(cancelled=True)


def remember_job(job: LoginJob) -> None:
    with _jobs_guard:
        _jobs[job.ticket] = job


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


def retain_saved_login(db_path: str | Path, job: LoginJob | None = None) -> None:
    """扫码已经落盘或会话已经写入时，取消不得删掉它们。"""
    del db_path, job


def start_live_login(conn: sqlite3.Connection, now: datetime) -> dict:
    db_path = Path(database_path(conn))
    existing = _reusable_job(db_path, conn)
    if existing is not None:
        return _job_payload(existing)
    if storage_state_is_logged_in(storage_state_path(db_path)):
        return _resume_saved_login(conn, now, db_path)
    driver = current_driver()
    driver.check_ready()
    cancel_jobs()
    if not _browser_lock.acquire(timeout=8):
        raise AppError("浏览器正忙，请稍后再试", status=409, code="browser_busy")
    ticket = secrets.token_urlsafe(18)
    expires = now + timedelta(seconds=LIVE_LOGIN_SECONDS)
    job = LoginJob(ticket, db_path=str(db_path.resolve()), generation=current_generation())
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
        remember_job(job)
        thread = threading.Thread(
            target=_worker,
            args=(job, str(db_path)),
            name="pdd-login",
            daemon=True,
        )
        thread.start()
    except Exception:
        release_browser()
        raise
    log_op(conn, "登录", "已打开真实店铺登录窗口")
    return _job_payload(job, expires=expires)


def _reusable_job(db_path: Path, conn: sqlite3.Connection) -> LoginJob | None:
    from app.services.auth import public_session

    key = str(db_path.resolve())
    session = public_session(conn)
    logged_live = bool(session.get("logged_in") and session.get("mode") == "live")
    with _jobs_guard:
        jobs = list(_jobs.values())
    chosen: LoginJob | None = None
    for job in jobs:
        if not job.db_path:
            continue
        try:
            same_db = str(Path(job.db_path).resolve()) == key
        except OSError:
            same_db = False
        if not same_db:
            continue
        snap = job.snapshot()
        if snap["cookies_saved"] and not snap["finished"] and snap["status"] != "confirmed":
            chosen = job
            continue
        if (snap["finished"] or snap["status"] == "confirmed") and logged_live:
            chosen = job
    return chosen


def _resume_saved_login(conn: sqlite3.Connection, now: datetime, db_path: Path) -> dict:
    ticket = secrets.token_urlsafe(18)
    expires = now + timedelta(seconds=LIVE_LOGIN_SECONDS)
    job = LoginJob(ticket, db_path=str(db_path.resolve()), generation=current_generation())
    with conn:
        conn.execute(
            "INSERT INTO login_tickets(ticket, mode, status, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
            (ticket, "live", "pending", iso(now), iso(expires)),
        )
    payload = payload_from_saved_capture(db_path)
    result = finish_live_session(db_path, payload, ticket, generation=job.generation)
    if result.get("skipped"):
        job.update(status="failed", message=result.get("message") or "登录已取消")
    else:
        job.update(
            status="confirmed",
            finished=True,
            cookies_saved=True,
            message=result.get("message") or "已使用本机保存的登录",
        )
    remember_job(job)
    log_op(conn, "登录", "使用本机已保存的商家登录")
    body = _job_payload(job, expires=expires)
    body["resumed"] = True
    return body


def _job_payload(job: LoginJob, *, expires: datetime | None = None) -> dict:
    snap = job.snapshot()
    message = snap["message"] or "请用「拼多多商家版」App 扫描弹出窗口里的二维码。"
    body = {
        "ready": True,
        "mode": "live",
        "ticket": job.ticket,
        "official_login_url": OFFICIAL_LOGIN_URL,
        "qr_image": snap["qr_image"],
        "hint": message,
        "message": message,
        "resumed": snap["status"] == "confirmed",
        "resume_available": False,
    }
    if expires is not None:
        body["expires_at"] = iso(expires)
        body["ttl_seconds"] = LIVE_LOGIN_SECONDS
    return body


def _worker(job: LoginJob, db_path: str) -> None:
    try:
        current_driver().run(job, db_path)
        snap = job.snapshot()
        if snap["finished"] or snap["status"] == "confirmed":
            return
        if snap["cancelled"]:
            return
        if snap["status"] == "pending":
            job.update(status="failed", message="登录未完成")
    except AppError as exc:
        if job.snapshot()["status"] != "confirmed" and not job.snapshot()["finished"]:
            job.update(status="failed", message=str(exc))
    except Exception:
        logger.exception("live login failed")
        if job.snapshot()["status"] != "confirmed" and not job.snapshot()["finished"]:
            job.update(status="failed", message="浏览器操作失败，请重试。")
    finally:
        release_browser()


def poll_live_job(conn: sqlite3.Connection, ticket: str, row, now: datetime) -> dict:
    from app.services.auth import public_session

    session = public_session(conn, now)
    logged_live = bool(session.get("logged_in") and session.get("mode") == "live")
    resume_available = storage_state_is_logged_in(storage_state_path(database_path(conn))) and not logged_live
    with _jobs_guard:
        job = _jobs.get(ticket)
    if job is not None:
        snap = job.snapshot()
        if snap["finished"] or snap["status"] == "confirmed" or logged_live:
            status = "confirmed"
        elif snap["cancelled"]:
            status = "expired"
        else:
            status = snap["status"]
        return {
            "ticket": ticket,
            "status": status,
            "mode": "live",
            "message": snap["message"],
            "qr_image": snap["qr_image"],
            "resume_available": resume_available and status != "confirmed",
        }
    if logged_live:
        return {
            "ticket": ticket,
            "status": "confirmed",
            "mode": "live",
            "message": "已登录",
            "qr_image": None,
            "resume_available": False,
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
        "message": "登录窗口已关闭。若刚才已经扫过码，点「用已保存登录进入」。",
        "qr_image": None,
        "resume_available": resume_available,
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


def payload_from_saved_capture(db_path: str | Path):
    from datetime import date

    from app.adapters.pdd_normalize import normalize_captures

    captures: list[dict] = []
    path = capture_path(db_path)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("kind") == "json" and "body" in item:
                    captures.append({"kind": "json", "body": item["body"]})
    return normalize_captures(captures, shop_hint="拼多多店铺", today=date.today())


def finish_live_session(db_path: str | Path, payload, ticket: str, generation: int | None = None) -> dict:
    from app.services.sync import describe_sync, finish_run, persist_payload
    from app.timeutil import now_local

    with _auth_lock:
        if generation is not None and generation != _auth_generation:
            return {"skipped": True, "message": "登录已取消"}
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
            if generation is not None and generation != _auth_generation:
                return {"skipped": True, "message": "登录已取消"}
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


def update_live_payload(db_path: str | Path, payload, generation: int | None = None) -> dict | None:
    """报表晚到时补写数字。退出或已经换成模拟登录后不再改当前会话。"""
    from app.services.sync import describe_sync, persist_payload
    from app.timeutil import now_local

    with _auth_lock:
        if generation is not None and generation != _auth_generation:
            return None
        conn = connect(db_path)
        try:
            init_db(conn)
            row = conn.execute("SELECT mode FROM sessions WHERE id = 1").fetchone()
            if row is not None and row["mode"] != "live":
                return None
            counts = persist_payload(conn, payload, synced_at=iso(now_local()))
            if row is not None:
                conn.execute("UPDATE sessions SET shop_id = ? WHERE id = 1", (payload.shop.shop_id,))
                conn.commit()
            message = describe_sync(payload, counts)
            log_op(conn, "同步", message)
            return {"message": message, "shop_id": payload.shop.shop_id, **counts}
        finally:
            conn.close()


def drop_live_login(db_path: str | Path) -> None:
    """仅用于同步时发现 Cookie 已经失效，或用户明确退出。登录成功后的取消不能走这里。"""
    remove_session_files(db_path)
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM sessions WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
