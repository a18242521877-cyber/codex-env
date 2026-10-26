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
from app.adapters.pdd_normalize import SAVED_LOGIN_NOTE
from app.config import LIVE_LOGIN_SECONDS, LOGIN_GATES, OFFICIAL_LOGIN_URL, SESSION_DAYS
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

READING_MESSAGE = SAVED_LOGIN_NOTE


def gate_by_id(gate: str) -> dict:
    for item in LOGIN_GATES:
        if item["id"] == gate:
            return item
    raise AppError("没有这个登录页", status=400, code="unknown_gate")


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
    global _driver
    if _driver is not None:
        return _driver
    from app.adapters.pdd_browser import PlaywrightLiveDriver

    _driver = PlaywrightLiveDriver()
    return _driver


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
    driver = _driver
    if driver is not None:
        try:
            driver.close_open_session()
        except Exception:
            logger.exception("close open browser")


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


def open_login_gate(conn: sqlite3.Connection, now: datetime, gate: str) -> dict:
    """只打开固定地址。不截二维码，不等待登录，不代点头像。"""
    del now
    chosen = gate_by_id(gate)
    db_path = Path(database_path(conn))
    driver = current_driver()
    driver.check_ready()
    driver.open_url_only(db_path, chosen["url"])
    from app.adapters.pdd_normalize import YINGXIAO_SYNC_HINT

    if "推广" in chosen["label"]:
        message = (
            f"已打开{chosen['label']}。这是干净的登录窗口，不会带上商家后台的旧登录，授权页不会反复刷新。"
            + YINGXIAO_SYNC_HINT
        )
    else:
        message = (
            f"已打开{chosen['label']}。请在弹出的 Edge 窗口里自己完成登录，停留在该页，再回到推广台点「同步」。"
            "程序不会代点，也不会等待扫码。"
        )
    log_op(conn, "登录", f"已打开{chosen['label']}")
    return {
        "ready": True,
        "opened": True,
        "mode": "live",
        "url": chosen["url"],
        "message": message,
        "hint": message,
        "gate": chosen,
    }


def sync_when_ready(conn: sqlite3.Connection, now: datetime) -> dict:
    """用户说已经登录后才收报表。没有打开的窗口、也没有已保存登录时，不启动浏览器。"""
    from app.services.auth import public_session
    from app.settings import load_settings

    if load_settings(conn).data_mode != "live":
        raise AppError("当前是模拟店铺。请用模拟确认进入，或先切换到真实店铺。", status=409, code="mode_mismatch")
    session = public_session(conn, now)
    if session.get("logged_in") and session.get("mode") == "live":
        from app.services.sync import sync_account

        return sync_account(conn, mode="live", now=now)
    if session.get("logged_in"):
        raise AppError(
            "当前登录和数据模式不一致。请退出后，打开推广中心自己完成登录，再点「同步」。",
            status=409,
            code="mode_mismatch",
        )
    from app.adapters.pdd_live import PddLiveAdapter
    from app.db import database_path

    payload = PddLiveAdapter(database_path(conn)).fetch(today=now.date(), now=now)
    result = finish_live_session(database_path(conn), payload, "", generation=current_generation())
    if result.get("skipped"):
        raise AppError(result.get("message") or "同步已取消", status=409, code="login_cancelled")
    return {"ok": True, **result}


def start_live_login(conn: sqlite3.Connection, now: datetime, *, gate: str = "mms", force_open: bool = False) -> dict:
    chosen = gate_by_id(gate)
    db_path = Path(database_path(conn))
    from app.services.auth import public_session

    session = public_session(conn, now)
    already = bool(session.get("logged_in") and session.get("mode") == "live")
    existing = _reusable_job(db_path, conn)
    if existing is not None and not force_open:
        return _job_payload(existing)
    if not force_open and storage_state_is_logged_in(storage_state_path(db_path)):
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
            args=(job, str(db_path), chosen["url"], not already),
            name="pdd-login",
            daemon=True,
        )
        thread.start()
    except Exception:
        release_browser()
        raise
    log_op(conn, "登录", f"已打开{chosen['label']}")
    body = _job_payload(job, expires=expires)
    body["gate"] = chosen
    body["message"] = f"已打开{chosen['label']}。请在弹出的浏览器里自己完成登录，回到推广台后再点「同步」。"
    body["hint"] = body["message"]
    return body


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
    message = snap["message"] or "请在弹出的浏览器里自己完成登录，回到推广台后再点「同步」。"
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


def _worker(job: LoginJob, db_path: str, open_url: str | None = None, establish_session: bool = True) -> None:
    try:
        current_driver().run(job, db_path, open_url=open_url, establish_session=establish_session)
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
        "message": "登录窗口已关闭。若刚才已经登录，点「用已保存登录进入」，再点「同步」。",
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
                if not isinstance(item, dict):
                    continue
                if item.get("kind") == "json" and "body" in item:
                    captures.append(
                        {
                            "kind": "json",
                            "body": item["body"],
                            "url": item.get("url") or "",
                            "path": item.get("path") or "",
                            "promo": bool(item.get("promo")),
                        }
                    )
                elif item.get("kind") == "note" and item.get("body"):
                    captures.append({"kind": "note", "body": item["body"]})
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
