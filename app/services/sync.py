from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from app.adapters import get_adapter
from app.adapters.base import SyncPayload
from app.db import database_path, log_op
from app.errors import AdapterNotReady, AppError
from app.timeutil import iso, now_local

logger = logging.getLogger(__name__)


def sync_account(conn: sqlite3.Connection, *, mode: str, now: datetime | None = None) -> dict:
    now = now or now_local()
    started = iso(now)
    cur = conn.execute(
        "INSERT INTO sync_runs(shop_id, mode, status, message, started_at) VALUES(?, ?, ?, ?, ?)",
        (None, mode, "running", "开始同步", started),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    try:
        payload = get_adapter(mode, database_path(conn)).fetch(today=now.date(), now=now)
        counts = persist_payload(conn, payload, synced_at=iso(now_local()))
        conn.execute("UPDATE sessions SET shop_id = ? WHERE id = 1", (payload.shop.shop_id,))
        conn.commit()
        message = describe_sync(payload, counts)
        finish_run(conn, run_id, payload.shop.shop_id, "success", message)
        log_op(conn, "同步", message)
        return {
            "ok": True,
            "shop_id": payload.shop.shop_id,
            "shop_name": payload.shop.name,
            "message": message,
            **counts,
        }
    except AdapterNotReady as exc:
        finish_run(conn, run_id, None, "failed", str(exc))
        raise
    except AppError as exc:
        finish_run(conn, run_id, None, "failed", str(exc))
        raise
    except Exception as exc:
        logger.exception("sync failed")
        finish_run(conn, run_id, None, "failed", "同步失败")
        raise AppError("同步失败", status=500, code="sync_failed") from exc


def persist_payload(conn: sqlite3.Connection, payload: SyncPayload, *, synced_at: str) -> dict:
    return _persist(conn, payload, synced_at=synced_at)


def _shop_data_goods(goods) -> bool:
    return goods.category == "商品数据" or "sydney-shop" in goods.goods_id


def _shop_data_campaign(campaign) -> bool:
    return campaign.scene == "商品数据" or campaign.name == "商品数据"


def _shop_data_trade(payload: SyncPayload) -> bool:
    for item in payload.goods:
        if not _shop_data_goods(item):
            continue
        for day in payload.goods_days.get(item.goods_id, []):
            if day.gmv_fen or day.orders:
                return True
    return False


def describe_sync(payload: SyncPayload, counts: dict) -> str:
    """测款、测图只数推广报表。商品数据兜底和授权失败都不能写成「已同步，测款 0」。"""
    from app.adapters.pdd_normalize import note_says_auth_failed

    campaigns = [item for item in payload.campaigns if not _shop_data_campaign(item)]
    goods = [item for item in payload.goods if not _shop_data_goods(item)]
    images = list(payload.images)
    note = (payload.note or "").strip()
    sydney = any(_shop_data_goods(item) for item in payload.goods)
    if "登录已保存" in note and not goods and not images:
        return f"{payload.shop.name}：{note}"
    if note_says_auth_failed(note) and not goods and not images:
        message = f"{payload.shop.name}：{note}" if note else f"{payload.shop.name}：推广中心未授权 / 登录态为空，测图测款未写入"
        if _shop_data_trade(payload) and "不是推广测款" not in message:
            message += " 昨日成交来自商品数据，不是推广测款。"
        return message
    message = (
        f"已同步 {payload.shop.name}：计划 {len(campaigns)}，"
        f"测款 {len(goods)}，测图 {len(images)}，"
        f"覆盖 {counts['days']} 天"
    )
    if sydney and not goods:
        message += "。测款未计入商品数据"
    if note:
        message = f"{message}。{note}"
    return message


def finish_run(conn, run_id: int, shop_id: str | None, status: str, message: str) -> None:
    _finish(conn, run_id, shop_id, status, message)


def _finish(conn, run_id: int, shop_id: str | None, status: str, message: str) -> None:
    conn.execute(
        "UPDATE sync_runs SET shop_id = ?, status = ?, message = ?, finished_at = ? WHERE id = ?",
        (shop_id, status, message, iso(now_local()), run_id),
    )
    conn.commit()


def _persist(conn: sqlite3.Connection, payload: SyncPayload, synced_at: str) -> dict:
    shop = payload.shop
    with conn:
        conn.execute(
            """
            INSERT INTO shops(shop_id, mall_id, name, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(shop_id) DO UPDATE SET
              mall_id = excluded.mall_id,
              name = excluded.name,
              updated_at = excluded.updated_at
            """,
            (shop.shop_id, shop.mall_id, shop.name, synced_at, synced_at),
        )
        for camp in payload.campaigns:
            conn.execute(
                """
                INSERT INTO campaigns(
                  campaign_id, shop_id, name, scene, status, daily_budget_fen, bid_fen, pace, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                  name = excluded.name,
                  scene = excluded.scene,
                  status = excluded.status,
                  daily_budget_fen = excluded.daily_budget_fen,
                  bid_fen = excluded.bid_fen,
                  pace = excluded.pace,
                  updated_at = excluded.updated_at
                """,
                (
                    camp.campaign_id,
                    shop.shop_id,
                    camp.name,
                    camp.scene,
                    camp.status,
                    camp.daily_budget_fen,
                    camp.bid_fen,
                    camp.pace,
                    synced_at,
                ),
            )
            for day in payload.campaign_days.get(camp.campaign_id, []):
                _upsert_daily(
                    conn,
                    "campaign_daily",
                    "campaign_id",
                    camp.campaign_id,
                    shop.shop_id,
                    day,
                    synced_at,
                    with_cart=True,
                )
        for goods in payload.goods:
            conn.execute(
                """
                INSERT INTO goods(
                  goods_id, shop_id, campaign_id, name, category, price_fen, stage, daily_budget_fen, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goods_id) DO UPDATE SET
                  campaign_id = excluded.campaign_id,
                  name = excluded.name,
                  category = excluded.category,
                  price_fen = excluded.price_fen,
                  stage = excluded.stage,
                  daily_budget_fen = excluded.daily_budget_fen,
                  updated_at = excluded.updated_at
                """,
                (
                    goods.goods_id,
                    shop.shop_id,
                    goods.campaign_id,
                    goods.name,
                    goods.category,
                    goods.price_fen,
                    goods.stage,
                    goods.daily_budget_fen,
                    synced_at,
                ),
            )
            for day in payload.goods_days.get(goods.goods_id, []):
                _upsert_daily(
                    conn,
                    "goods_daily",
                    "goods_id",
                    goods.goods_id,
                    shop.shop_id,
                    day,
                    synced_at,
                    with_cart=True,
                )
        for image in payload.images:
            conn.execute(
                """
                INSERT INTO image_tests(image_id, shop_id, goods_id, label, tone, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                  goods_id = excluded.goods_id,
                  label = excluded.label,
                  tone = excluded.tone,
                  updated_at = excluded.updated_at
                """,
                (image.image_id, shop.shop_id, image.goods_id, image.label, image.tone, synced_at),
            )
            for day in payload.image_days.get(image.image_id, []):
                _upsert_daily(
                    conn,
                    "image_daily",
                    "image_id",
                    image.image_id,
                    shop.shop_id,
                    day,
                    synced_at,
                    with_cart=False,
                )
    day_count = 0
    if payload.goods_days:
        day_count = max(len(series) for series in payload.goods_days.values())
    return {
        "campaigns": len(payload.campaigns),
        "goods": len(payload.goods),
        "images": len(payload.images),
        "days": day_count,
    }


def _upsert_daily(conn, table, id_col, entity_id, shop_id, day, synced_at, *, with_cart: bool) -> None:
    cart_col = ", add_to_cart" if with_cart else ""
    cart_val = ", ?" if with_cart else ""
    params = [
        entity_id,
        shop_id,
        day.stat_date,
        day.impressions,
        day.clicks,
        day.spend_fen,
        day.orders,
        day.gmv_fen,
    ]
    if with_cart:
        params.append(day.add_to_cart)
    params.append(synced_at)
    assignments = """
      impressions = excluded.impressions,
      clicks = excluded.clicks,
      spend_fen = excluded.spend_fen,
      orders = excluded.orders,
      gmv_fen = excluded.gmv_fen,
    """
    if with_cart:
        assignments += "add_to_cart = excluded.add_to_cart,\n"
    assignments += "synced_at = excluded.synced_at"
    conn.execute(
        f"""
        INSERT INTO {table}(
          {id_col}, shop_id, stat_date, impressions, clicks, spend_fen, orders, gmv_fen{cart_col}, synced_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?{cart_val}, ?)
        ON CONFLICT({id_col}, stat_date) DO UPDATE SET
          {assignments}
        """,
        params,
    )
