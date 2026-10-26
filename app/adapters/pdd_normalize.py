"""把商家后台页面自己返回的 JSON / CSV / xlsx 收成统一报表。

没有一份稳定的公开接口。这里只认「一行里同时有身份和指标」这种形状，
字段对不上就留空，不补造花费或 ROI。

金额启发式（后台改口径时可能差 100 倍，要对照页面核对）：

- CSV、xlsx 里的金额按「元」
- JSON 里的整数按「分」，小数按「元」
- JSON 字符串带小数点按「元」，纯数字按「分」

测图（创意 / 主图）不并进商品花费。只有商品、没有计划行时，按场景合成一条计划，
计划花费等于其下商品之和。已经有计划行时，不再把商品加进计划。
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from typing import Any

from app.adapters.base import (
    CampaignInfo,
    GoodsInfo,
    ImageInfo,
    MetricDay,
    ShopProfile,
    SyncPayload,
)

_DATE_KEYS = ("statDate", "stat_date", "reportDate", "日期", "统计日期", "date", "ds")
_GOODS_ID = ("goodsId", "goods_id", "商品ID")
_GOODS_NAME = ("goodsName", "goods_name", "商品名称", "商品名")
_PLAN_ID = ("planId", "adId", "campaignId", "unitId", "计划ID", "推广计划ID")
_PLAN_NAME = ("planName", "adName", "campaignName", "计划名称", "推广计划", "计划名")
_CREATIVE_ID = ("creativeId", "imageId", "创意ID")
_CREATIVE_NAME = ("creativeName", "创意名称", "主图名称", "主图")
_IMPR = ("impression", "impressions", "showCnt", "曝光量", "曝光", "展现量", "展现")
_CLICK = ("click", "clicks", "clickCnt", "点击量", "点击")
_SPEND = ("成交花费", "推广花费", "总花费", "花费", "消耗", "spend", "cost")
_ORDER = ("成交笔数", "支付订单数", "成交订单数", "订单数", "orderNum", "orders", "orderCnt")
_GMV = ("交易额", "成交额", "成交金额", "支付金额", "交易金额", "gmv")
_CART = ("addCart", "add_to_cart", "加购数", "加购")
_BUDGET = ("dailyBudget", "dayBudget", "日限额", "日预算", "预算")
_BID = ("bid", "出价", "点击出价")
_PRICE = ("goodsPrice", "price", "拼单价", "商品价格")
_STATUS = ("status", "adStatus", "状态")
_SCENE = ("scene", "推广场景", "推广类型", "场景", "计划类型")
_STAGE = ("stage", "阶段")
_MALL_ID = ("mallId", "mall_id", "shopId")
_MALL_NAME = ("mallName", "mall_name", "shopName", "店铺名称", "店铺名")


def money_to_fen(value: Any, *, source: str) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, str):
        text = value.replace(",", "").replace("元", "").replace("¥", "").replace("￥", "").strip()
        if text in {"", "-", "--"}:
            return 0
        try:
            number = float(text)
        except ValueError:
            return 0
        if source == "json" and "." not in text:
            return int(number)
        return int(round(number * 100))
    if isinstance(value, int):
        if source == "json":
            return value
        return int(round(value * 100))
    if isinstance(value, float):
        return int(round(value * 100))
    return 0


def normalize_captures(
    captures: list[dict],
    *,
    shop_hint: str = "",
    today: date,
) -> SyncPayload:
    rows: list[tuple[str, dict[str, Any]]] = []
    mall = {"id": "", "name": ""}
    notes: list[str] = []
    for item in captures:
        kind = item.get("kind")
        body = item.get("body")
        if kind == "note" and body:
            notes.append(str(body).strip()[:300])
            continue
        if kind == "debug":
            continue
        if kind == "json":
            data = _load_json(body)
            if data is None:
                continue
            _walk(data, rows, mall, "json")
        elif kind == "csv":
            text = _decode_table(body)
            if text is None:
                notes.append("有一份 CSV 无法识别编码")
                continue
            for raw in _csv_rows(text):
                mapped = _map_flat(raw)
                _absorb_mall(mapped, mall)
                row_kind = _kind(mapped)
                if row_kind and _has_metric(mapped):
                    rows.append((row_kind, {**mapped, "__source": "csv"}))
        elif kind == "xlsx":
            table, error = _xlsx_rows(body)
            if error:
                notes.append(error)
                continue
            for raw in table:
                mapped = _map_flat(raw)
                _absorb_mall(mapped, mall)
                row_kind = _kind(mapped)
                if row_kind and _has_metric(mapped):
                    rows.append((row_kind, {**mapped, "__source": "xlsx"}))
    return _assemble(rows, mall, shop_hint=shop_hint, today=today, notes=notes)


def _load_json(body: Any) -> Any:
    if isinstance(body, (dict, list)):
        return body
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None
    return None


def _walk(node: Any, rows: list[tuple[str, dict]], mall: dict[str, str], source: str, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, rows, mall, source, depth + 1)
        return
    if not isinstance(node, dict):
        return
    mapped = _map_flat(node)
    mapped["__source"] = source
    _absorb_mall(mapped, mall)
    nested = any(isinstance(value, (dict, list)) for value in node.values())
    row_kind = _kind(mapped)
    if row_kind and _has_metric(mapped) and (_scalar(mapped, _DATE_KEYS) not in (None, "") or not nested):
        rows.append((row_kind, mapped))
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk(value, rows, mall, source, depth + 1)


def _map_flat(raw: dict) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, (dict, list)):
            continue
        mapped[_norm(key)] = value
    return mapped


def _norm(key: Any) -> str:
    return str(key).strip().lower().replace("_", "").replace(" ", "")


def _scalar(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(_norm(key))
        if isinstance(value, (dict, list)) or value is None or value == "":
            continue
        return value
    return None


def _kind(row: dict[str, Any]) -> str | None:
    if _scalar(row, _CREATIVE_ID) is not None or _scalar(row, _CREATIVE_NAME) is not None:
        return "image"
    if _scalar(row, _GOODS_ID) is not None or _scalar(row, _GOODS_NAME) is not None:
        return "goods"
    if _scalar(row, _PLAN_ID) is not None or _scalar(row, _PLAN_NAME) is not None:
        return "campaign"
    return None


def _has_metric(row: dict[str, Any]) -> bool:
    for keys in (_IMPR, _CLICK, _SPEND, _ORDER, _GMV):
        if _scalar(row, keys) is not None:
            return True
    return False


def _absorb_mall(row: dict[str, Any], mall: dict[str, str]) -> None:
    if not mall["id"]:
        raw = _scalar(row, _MALL_ID)
        if raw is not None:
            mall["id"] = _id_token(raw)
    if not mall["name"]:
        raw = _scalar(row, _MALL_NAME)
        if raw is not None:
            mall["name"] = str(raw).strip()[:40]


def _decode_table(body: Any) -> str | None:
    if isinstance(body, str):
        return body
    if not isinstance(body, (bytes, bytearray)):
        return None
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return bytes(body).decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _csv_rows(text: str) -> list[dict[str, str]]:
    sample = text.lstrip("\ufeff")
    try:
        return list(csv.DictReader(io.StringIO(sample)))
    except csv.Error:
        return []


def _xlsx_rows(body: Any) -> tuple[list[dict], str]:
    if not isinstance(body, (bytes, bytearray)):
        return [], ""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return [], "导出了 xlsx，但本机没有 openpyxl，这份表格被跳过"
    try:
        book = load_workbook(io.BytesIO(bytes(body)), read_only=True, data_only=True)
    except Exception:
        return [], "有一份 xlsx 打不开"
    try:
        sheet = book.active
        grid = list(sheet.iter_rows(values_only=True))
    finally:
        book.close()
    if not grid:
        return [], ""
    headers = ["" if cell is None else str(cell).strip() for cell in grid[0]]
    rows = []
    for line in grid[1:]:
        if line is None or all(cell is None or cell == "" for cell in line):
            continue
        rows.append({headers[index]: line[index] for index in range(min(len(headers), len(line))) if headers[index]})
    return rows, ""


def _assemble(
    rows: list[tuple[str, dict]],
    mall: dict[str, str],
    *,
    shop_hint: str,
    today: date,
    notes: list[str],
) -> SyncPayload:
    goods_meta: dict[str, dict] = {}
    goods_days: dict[tuple[str, str], MetricDay] = {}
    goods_scene: dict[str, str] = {}
    goods_plan: dict[str, str] = {}
    plan_meta: dict[str, dict] = {}
    plan_days: dict[tuple[str, str], MetricDay] = {}
    plan_by_name: dict[str, str] = {}
    image_meta: dict[str, dict] = {}
    image_days: dict[tuple[str, str], MetricDay] = {}
    explicit_plan = False

    for kind, row in rows:
        source = str(row.get("__source") or "json")
        stat, metric = _metric(row, source, today)
        if kind == "campaign":
            explicit_plan = True
            plan_id = _entity_id("live-plan-", _scalar(row, _PLAN_ID), _scalar(row, _PLAN_NAME) or "计划")
            name = str(_scalar(row, _PLAN_NAME) or plan_id)
            plan_meta[plan_id] = {
                "name": name[:80],
                "scene": str(_scalar(row, _SCENE) or "商品推广")[:40],
                "status": _status(_scalar(row, _STATUS)),
                "budget": money_to_fen(_scalar(row, _BUDGET), source=source),
                "bid": _optional_money(row, source),
            }
            plan_by_name[name] = plan_id
            plan_days[(plan_id, stat)] = metric
            continue
        if kind == "goods":
            goods_id = _entity_id("live-goods-", _scalar(row, _GOODS_ID), _scalar(row, _GOODS_NAME) or "商品")
            scene = str(_scalar(row, _SCENE) or "商品推广")[:40]
            goods_meta[goods_id] = {
                "name": str(_scalar(row, _GOODS_NAME) or goods_id)[:80],
                "scene": scene,
                "price": money_to_fen(_scalar(row, _PRICE), source=source),
                "stage": _stage(_scalar(row, _STAGE)),
                "budget": money_to_fen(_scalar(row, _BUDGET), source=source),
            }
            goods_scene[goods_id] = scene
            plan_raw = _scalar(row, _PLAN_ID)
            plan_name = _scalar(row, _PLAN_NAME)
            if plan_raw is not None:
                goods_plan[goods_id] = _entity_id("live-plan-", plan_raw, str(plan_name or plan_raw))
            elif plan_name is not None:
                goods_plan[goods_id] = str(plan_name)
            goods_days[(goods_id, stat)] = metric
            continue
        image_id = _entity_id("live-img-", _scalar(row, _CREATIVE_ID), _scalar(row, _CREATIVE_NAME) or "主图")
        goods_raw = _scalar(row, _GOODS_ID)
        goods_name = _scalar(row, _GOODS_NAME)
        linked = _entity_id("live-goods-", goods_raw, str(goods_name or "未命名商品")) if goods_raw is not None or goods_name is not None else "live-goods-unknown"
        image_meta[image_id] = {
            "label": str(_scalar(row, _CREATIVE_NAME) or "主图")[:40],
            "goods_id": linked,
            "goods_name": str(goods_name or "")[:80],
        }
        image_days[(image_id, stat)] = metric

    for image in image_meta.values():
        gid = image["goods_id"]
        if gid not in goods_meta:
            goods_meta[gid] = {
                "name": image["goods_name"] or "未命名商品",
                "scene": "商品推广",
                "price": 0,
                "stage": "推广中",
                "budget": 0,
            }
            goods_scene[gid] = "商品推广"

    if not explicit_plan:
        _synthesize_plans(goods_meta, goods_days, goods_scene, plan_meta, plan_days)
        for gid in goods_meta:
            goods_plan[gid] = f"live-scene-{_slug(goods_scene.get(gid) or '商品推广')}"
    else:
        fallback = next(iter(plan_meta), "")
        for gid, meta in goods_meta.items():
            linked = goods_plan.get(gid, "")
            if linked in plan_meta:
                continue
            if linked and linked in plan_by_name:
                goods_plan[gid] = plan_by_name[linked]
                continue
            if fallback:
                goods_plan[gid] = fallback
                continue
            scene = goods_scene.get(gid) or "商品推广"
            plan_id = f"live-scene-{_slug(scene)}"
            plan_meta.setdefault(
                plan_id,
                {"name": scene, "scene": scene, "status": "投放中", "budget": 0, "bid": None},
            )
            goods_plan[gid] = plan_id

    mall_id = mall["id"]
    shop_id = f"live-{_slug(mall_id)}" if mall_id else "live-unknown"
    name = (mall["name"] or shop_hint or "拼多多店铺")[:40]
    note = " ".join(notes)
    if not goods_meta and not plan_meta:
        extra = (
            "已登录，但没有从报表里识别到推广商品或计划。"
            "请在弹出的窗口打开推广报表，或下载已经生成好的报表后再同步。"
            "测图只有报表带创意或主图时才会出现。"
        )
        note = f"{note} {extra}".strip()

    campaigns = [
        CampaignInfo(
            campaign_id=plan_id,
            name=meta["name"],
            scene=meta["scene"],
            status=meta["status"],
            daily_budget_fen=meta["budget"],
            bid_fen=meta["bid"],
            pace="normal",
        )
        for plan_id, meta in plan_meta.items()
    ]
    campaign_days = _group_days(plan_days)
    goods = [
        GoodsInfo(
            goods_id=gid,
            campaign_id=goods_plan[gid],
            name=meta["name"],
            category=meta["scene"],
            price_fen=meta["price"],
            stage=meta["stage"],
            daily_budget_fen=meta["budget"],
        )
        for gid, meta in goods_meta.items()
    ]
    images = [
        ImageInfo(image_id=image_id, goods_id=meta["goods_id"], label=meta["label"], tone="plain")
        for image_id, meta in image_meta.items()
    ]
    return SyncPayload(
        shop=ShopProfile(shop_id=shop_id, mall_id=mall_id, name=name),
        campaigns=campaigns,
        campaign_days=campaign_days,
        goods=goods,
        goods_days=_group_days(goods_days),
        images=images,
        image_days=_group_days(image_days),
        note=note,
    )


def _synthesize_plans(goods_meta, goods_days, goods_scene, plan_meta, plan_days) -> None:
    scenes = {goods_scene.get(gid) or "商品推广" for gid in goods_meta} or set()
    if not scenes and not goods_meta:
        return
    buckets: dict[tuple[str, str], list[MetricDay]] = {}
    budgets: dict[str, int] = {}
    for gid, meta in goods_meta.items():
        scene = goods_scene.get(gid) or "商品推广"
        budgets[scene] = budgets.get(scene, 0) + int(meta["budget"])
        for (owner, stat), metric in goods_days.items():
            if owner != gid:
                continue
            buckets.setdefault((scene, stat), []).append(metric)
    for scene in scenes:
        plan_id = f"live-scene-{_slug(scene)}"
        plan_meta[plan_id] = {
            "name": scene,
            "scene": scene,
            "status": "投放中",
            "budget": budgets.get(scene, 0),
            "bid": None,
        }
        for (bucket_scene, stat), metrics in buckets.items():
            if bucket_scene != scene:
                continue
            plan_days[(plan_id, stat)] = _sum_metrics(stat, metrics)


def _metric(row: dict[str, Any], source: str, today: date) -> tuple[str, MetricDay]:
    stat = _date_text(_scalar(row, _DATE_KEYS)) or today.isoformat()
    return stat, MetricDay(
        stat_date=stat,
        impressions=_count(_scalar(row, _IMPR)),
        clicks=_count(_scalar(row, _CLICK)),
        spend_fen=money_to_fen(_scalar(row, _SPEND), source=source),
        orders=_count(_scalar(row, _ORDER)),
        gmv_fen=money_to_fen(_scalar(row, _GMV), source=source),
        add_to_cart=_count(_scalar(row, _CART)),
    )


def _sum_metrics(stat: str, metrics: list[MetricDay]) -> MetricDay:
    return MetricDay(
        stat_date=stat,
        impressions=sum(item.impressions for item in metrics),
        clicks=sum(item.clicks for item in metrics),
        spend_fen=sum(item.spend_fen for item in metrics),
        orders=sum(item.orders for item in metrics),
        gmv_fen=sum(item.gmv_fen for item in metrics),
        add_to_cart=sum(item.add_to_cart for item in metrics),
    )


def _group_days(store: dict[tuple[str, str], MetricDay]) -> dict[str, list[MetricDay]]:
    grouped: dict[str, list[MetricDay]] = {}
    for (owner, _stat), metric in store.items():
        grouped.setdefault(owner, []).append(metric)
    for series in grouped.values():
        series.sort(key=lambda item: item.stat_date)
    return grouped


def _optional_money(row: dict[str, Any], source: str) -> int | None:
    raw = _scalar(row, _BID)
    if raw is None:
        return None
    return money_to_fen(raw, source=source)


def _status(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in {"1", "running", "online", "投放中", "推广中", "进行中", "启用"}:
        return "投放中"
    if lowered in {"2", "paused", "pause", "暂停", "已暂停", "停止", "下线"}:
        return "已暂停"
    if not text:
        return "投放中"
    return text[:20]


def _stage(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "推广中"
    if "积累" in text:
        return "数据积累期"
    if "智能" in text or "学习" in text:
        return "学习期"
    return text[:20]


def _count(value: Any) -> int:
    if value is None or isinstance(value, bool) or value == "":
        return 0
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if value in {"-", "--"}:
            return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _date_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip().replace("/", "-").replace(".", "-")
    if " " in text:
        text = text.split(" ", 1)[0]
    if "T" in text:
        text = text.split("T", 1)[0]
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return None


def _id_token(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return str(value).strip()


def _entity_id(prefix: str, raw: Any, fallback: str) -> str:
    token = _slug(_id_token(raw)) if raw not in (None, "") else _slug(str(fallback))
    return f"{prefix}{token}"


def _slug(text: str) -> str:
    cleaned = re.sub(r"\s+", "-", text.strip())
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "", cleaned)
    return cleaned[:40] or "item"
