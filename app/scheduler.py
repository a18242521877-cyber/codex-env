"""登录后按设置间隔重拉数据。真实店铺会弹出本机浏览器。"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.db import connect, init_db
from app.errors import AppError
from app.services.auth import public_session
from app.services.sync import sync_account
from app.settings import load_settings
from app.timeutil import parse_dt
from app.services.readmodel import last_sync

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="pdd-sync", daemon=True)

    def start(self) -> None:
        if os.environ.get("PDD_MONITOR_DISABLE_SCHEDULER") == "1":
            return
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(20):
            try:
                self.tick()
            except Exception:
                logger.exception("自动同步失败")

    def tick(self) -> None:
        conn = connect(self.db_path)
        try:
            init_db(conn)
            settings = load_settings(conn)
            interval = settings.refresh_interval_minutes
            if interval <= 0:
                return
            session = public_session(conn)
            if not session["logged_in"] or session["mode"] != settings.data_mode:
                return
            if session["mode"] not in {"mock", "live"}:
                return
            previous = last_sync(conn)
            if previous and previous.get("finished_at"):
                due = parse_dt(previous["finished_at"]) + timedelta(minutes=interval)
                if due > datetime.now().astimezone():
                    return
            if session["mode"] == "live":
                from app.services.live_login import browser_is_busy

                if browser_is_busy():
                    return
            sync_account(conn, mode=session["mode"])
        except AppError as exc:
            logger.info("跳过自动同步：%s", exc)
        finally:
            conn.close()
