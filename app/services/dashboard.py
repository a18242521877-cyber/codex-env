from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from app.errors import AppError
from app.metrics import change
from app.services.readmodel import (
    campaigns_for_day,
    data_bounds,
    day_metrics,
    goods_for_day,
    images_for_day,
    last_sync,
    trend,
    unread_count,
)
from app.services.recommendations import build_plan
from app.settings import load_settings
from app.timeutil import iso


def build_dashboard(conn: sqlite3.Connection, shop_id: str, day: date, now: datetime) -> dict:
    settings = load_settings(conn)
    current = day_metrics(conn, "goods_daily", shop_id, day)
    previous = day_metrics(conn, "goods_daily", shop_id, day - timedelta(days=1))
    plan_day = day if day < now.date() else now.date() - timedelta(days=1)
    plan_preview: list[dict] = []
    plan_date = None
    try:
        plan = build_plan(conn, shop_id, plan_day, now, record_log=False)
        plan_date = plan["date"]
        plan_preview = [
            item
            for item in plan["items"]
            if item["priority"] in {"high", "medium"} and item["status"] == "pending"
        ][:3]
    except AppError:
        plan_preview = []
    return {
        "date": day.isoformat(),
        "complete": day < now.date(),
        "as_of": iso(now),
        "target_roi": settings.target_roi,
        "bounds": data_bounds(conn, shop_id),
        "last_sync": last_sync(conn),
        "unread_alerts": unread_count(conn, shop_id),
        "kpis": _kpis(current, previous),
        "trend": trend(conn, shop_id, day, 14),
        "campaigns": campaigns_for_day(conn, shop_id, day, now),
        "goods": goods_for_day(conn, shop_id, day),
        "images": images_for_day(conn, shop_id, day),
        "plan_date": plan_date,
        "plan_preview": plan_preview,
    }


def _kpis(current: dict, previous: dict) -> list[dict]:
    specs = [
        ("spend_yuan", "推广花费", "元", "yuan"),
        ("gmv_yuan", "成交额", "元", "yuan"),
        ("roi", "ROI", "", "roi"),
        ("orders", "订单", "笔", "count"),
        ("ctr", "点击率", "", "rate"),
        ("cvr", "转化率", "", "rate"),
    ]
    return [
        {
            "key": key,
            "label": label,
            "unit": unit,
            "format": kind,
            "value": current[key],
            "dod": change(current[key], previous[key]),
        }
        for key, label, unit, kind in specs
    ]
