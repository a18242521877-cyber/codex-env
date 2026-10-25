from __future__ import annotations

import sqlite3

from app.db import log_op
from app.errors import AppError

_TABLES = [
    "image_daily",
    "image_tests",
    "goods_daily",
    "goods",
    "campaign_daily",
    "campaigns",
    "daily_summaries",
    "adjustment_items",
    "alerts",
    "sessions",
    "login_tickets",
    "sync_runs",
    "operation_logs",
    "shops",
]


def reset_local_data(conn: sqlite3.Connection, confirm: str) -> dict:
    if confirm != "清空":
        raise AppError("请输入「清空」以确认。设置项会保留。")
    with conn:
        for table in _TABLES:
            conn.execute(f"DELETE FROM {table}")
    log_op(conn, "清空数据", "已删除本地店铺、报表、日报和建议，设置已保留")
    return {"ok": True, "kept_settings": True}
