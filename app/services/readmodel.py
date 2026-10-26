"""把某一天的报表整理成界面和日报都能用的结构。"""

from __future__ import annotations

import sqlite3
from datetime import date

from app.metrics import classify_pace, intraday_progress, pct_text, rates, safe_div, yuan
from app.settings import load_settings
from app.timeutil import day_span

_GOODS_SQL = """
SELECT g.goods_id, g.name, g.category, g.stage, g.price_fen, g.daily_budget_fen,
       g.campaign_id, c.name AS campaign_name, c.scene,
       COALESCE(d.impressions, 0) AS impressions,
       COALESCE(d.clicks, 0) AS clicks,
       COALESCE(d.spend_fen, 0) AS spend_fen,
       COALESCE(d.orders, 0) AS orders,
       COALESCE(d.gmv_fen, 0) AS gmv_fen,
       COALESCE(d.add_to_cart, 0) AS add_to_cart
FROM goods g
JOIN campaigns c ON c.campaign_id = g.campaign_id
LEFT JOIN goods_daily d ON d.goods_id = g.goods_id AND d.stat_date = ?
WHERE g.shop_id = ?
"""

_CAMPAIGN_SQL = """
SELECT c.campaign_id, c.name, c.scene, c.status, c.daily_budget_fen, c.bid_fen, c.pace,
       COALESCE(d.impressions, 0) AS impressions,
       COALESCE(d.clicks, 0) AS clicks,
       COALESCE(d.spend_fen, 0) AS spend_fen,
       COALESCE(d.orders, 0) AS orders,
       COALESCE(d.gmv_fen, 0) AS gmv_fen,
       COALESCE(d.add_to_cart, 0) AS add_to_cart
FROM campaigns c
LEFT JOIN campaign_daily d ON d.campaign_id = c.campaign_id AND d.stat_date = ?
WHERE c.shop_id = ?
"""

_IMAGE_SQL = """
SELECT i.image_id, i.label, i.tone, i.goods_id, g.name AS goods_name,
       COALESCE(d.impressions, 0) AS impressions,
       COALESCE(d.clicks, 0) AS clicks,
       COALESCE(d.spend_fen, 0) AS spend_fen,
       COALESCE(d.orders, 0) AS orders,
       COALESCE(d.gmv_fen, 0) AS gmv_fen
FROM image_tests i
JOIN goods g ON g.goods_id = i.goods_id
LEFT JOIN image_daily d ON d.image_id = i.image_id AND d.stat_date = ?
WHERE i.shop_id = ?
"""


def shop_name(conn: sqlite3.Connection, shop_id: str) -> str | None:
    row = conn.execute("SELECT name FROM shops WHERE shop_id = ?", (shop_id,)).fetchone()
    return None if row is None else row["name"]


def day_metrics(conn: sqlite3.Connection, table: str, shop_id: str, day: date) -> dict:
    if table not in {"goods_daily", "campaign_daily"}:
        raise ValueError(table)
    row = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(impressions), 0) AS impressions,
          COALESCE(SUM(clicks), 0) AS clicks,
          COALESCE(SUM(orders), 0) AS orders,
          COALESCE(SUM(spend_fen), 0) AS spend_fen,
          COALESCE(SUM(gmv_fen), 0) AS gmv_fen,
          COALESCE(SUM(add_to_cart), 0) AS add_to_cart
        FROM {table}
        WHERE shop_id = ? AND stat_date = ?
        """,
        (shop_id, day.isoformat()),
    ).fetchone()
    packed = rates(row["impressions"], row["clicks"], row["orders"], row["spend_fen"], row["gmv_fen"])
    packed["add_to_cart"] = row["add_to_cart"]
    return packed


def goods_for_day(conn: sqlite3.Connection, shop_id: str, day: date) -> list[dict]:
    rows = []
    for row in conn.execute(_GOODS_SQL, (day.isoformat(), shop_id)):
        item = _decorate(dict(row), with_cart=True)
        item["price_yuan"] = yuan(row["price_fen"])
        item["budget_yuan"] = yuan(row["daily_budget_fen"])
        item["budget_util"] = safe_div(row["spend_fen"], row["daily_budget_fen"])
        rows.append(item)
    rows.sort(key=lambda item: item["spend_fen"], reverse=True)
    return rows


def campaigns_for_day(conn: sqlite3.Connection, shop_id: str, day: date, now=None) -> list[dict]:
    settings = load_settings(conn) if now is not None else None
    rows = []
    for row in conn.execute(_CAMPAIGN_SQL, (day.isoformat(), shop_id)):
        item = _decorate(dict(row), with_cart=True)
        item["budget_yuan"] = yuan(row["daily_budget_fen"])
        item["bid_yuan"] = None if row["bid_fen"] is None else yuan(row["bid_fen"])
        util = safe_div(row["spend_fen"], row["daily_budget_fen"])
        item["budget_util"] = None if util is None else round(util, 4)
        item["pace_flag"] = None
        item["projected_util"] = None
        if now is not None and day == now.date() and util is not None and settings is not None:
            item["pace_flag"] = classify_pace(util, item["roi"], now, settings)
            item["projected_util"] = round(util / intraday_progress(now), 4)
        rows.append(item)
    rows.sort(key=lambda item: item["spend_fen"], reverse=True)
    return rows


def images_for_day(conn: sqlite3.Connection, shop_id: str, day: date) -> list[dict]:
    rows = [_decorate(dict(row), with_cart=False) for row in conn.execute(_IMAGE_SQL, (day.isoformat(), shop_id))]
    rows.sort(key=lambda item: (item["ctr"] is not None, item["ctr"] or 0), reverse=True)
    return rows


def ranked_goods(conn, shop_id: str, day: date) -> list[dict]:
    return goods_for_day(conn, shop_id, day)


def ranked_images(conn, shop_id: str, day: date) -> list[dict]:
    usable = [row for row in images_for_day(conn, shop_id, day) if row["impressions"] > 0]
    usable.sort(key=lambda row: row["ctr"] or 0, reverse=True)
    return usable


def data_bounds(conn, shop_id: str) -> dict | None:
    row = conn.execute(
        "SELECT MIN(stat_date) AS min_day, MAX(stat_date) AS max_day FROM goods_daily WHERE shop_id = ?",
        (shop_id,),
    ).fetchone()
    if row is None or row["min_day"] is None:
        return None
    return {"min": row["min_day"], "max": row["max_day"]}


def last_sync(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT mode, status, message, started_at, finished_at
        FROM sync_runs
        WHERE status = 'success'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else dict(row)


def list_alerts(conn, shop_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT stat_date, code, level, title, body, created_at, read_at
        FROM alerts
        WHERE shop_id = ?
        ORDER BY stat_date DESC,
                 CASE level WHEN 'warning' THEN 0 ELSE 1 END,
                 code
        LIMIT 100
        """,
        (shop_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def unread_count(conn, shop_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE shop_id = ? AND read_at IS NULL",
        (shop_id,),
    ).fetchone()
    return int(row["n"])


def mark_alerts_read(conn, shop_id: str, *, day: str | None, code: str | None, mark_all: bool, stamp: str) -> int:
    if mark_all:
        cur = conn.execute(
            "UPDATE alerts SET read_at = ? WHERE shop_id = ? AND read_at IS NULL",
            (stamp, shop_id),
        )
    elif day and code:
        cur = conn.execute(
            """
            UPDATE alerts SET read_at = ?
            WHERE shop_id = ? AND stat_date = ? AND code = ? AND read_at IS NULL
            """,
            (stamp, shop_id, day, code),
        )
    else:
        return 0
    conn.commit()
    return int(cur.rowcount)


def list_logs(conn, limit: int = 80) -> list[dict]:
    rows = conn.execute(
        "SELECT id, created_at, action, detail FROM operation_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def trend(conn, shop_id: str, end: date, days: int = 14) -> list[dict]:
    points = []
    for day in day_span(end, days):
        metrics = day_metrics(conn, "goods_daily", shop_id, day)
        points.append(
            {
                "date": day.isoformat(),
                "spend_yuan": metrics["spend_yuan"],
                "gmv_yuan": metrics["gmv_yuan"],
                "roi": metrics["roi"],
                "orders": metrics["orders"],
            }
        )
    return points


def _decorate(row: dict, *, with_cart: bool) -> dict:
    packed = rates(row["impressions"], row["clicks"], row["orders"], row["spend_fen"], row["gmv_fen"])
    row.update(packed)
    if with_cart:
        row["cart_rate"] = safe_div(row.get("add_to_cart", 0), row["clicks"])
    row["ctr_text"] = pct_text(row["ctr"])
    row["cvr_text"] = pct_text(row["cvr"])
    return row

