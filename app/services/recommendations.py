"""调整方案。本地规则，可复现，不调用外部模型。"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from app.config import RULE_DAYS
from app.db import log_op
from app.errors import AppError
from app.metrics import (
    adjust_budget_fen,
    classify_pace,
    pct_text,
    rates,
    roi_text,
    safe_div,
    yuan_text,
)
from app.settings import Settings, load_settings
from app.timeutil import iso, now_local

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def build_plan(
    conn: sqlite3.Connection,
    shop_id: str,
    selected: date,
    now: datetime | None = None,
    *,
    record_log: bool = False,
) -> dict:
    now = now or now_local()
    today = now.date()
    if selected > today:
        raise AppError("不能生成未来日期的调整方案")
    settings = load_settings(conn)
    window_end = today - timedelta(days=1) if selected >= today else selected
    window_start = window_end - timedelta(days=RULE_DAYS - 1)

    goods_aggs = _aggregate_goods(conn, shop_id, window_start, window_end)
    if not goods_aggs:
        raise AppError("没有可分析的数据，请先同步", status=409, code="no_data")
    image_aggs = _aggregate_images(conn, shop_id, window_start, window_end)
    campaign_aggs = _aggregate_campaigns(conn, shop_id, window_start, window_end)

    goods_items = goods_recommendations(goods_aggs, settings)
    suppressed = {
        item["campaign_id"]
        for item in goods_items
        if item["action"] in {"goods_pause", "goods_fix_listing"}
    }
    items = [
        *goods_items,
        *image_recommendations(image_aggs, settings),
        *campaign_recommendations(campaign_aggs, suppressed, settings),
    ]
    if selected >= today:
        items.extend(pacing_recommendations(_today_campaigns(conn, shop_id, today), now, settings))

    items.sort(key=lambda item: (PRIORITY_RANK[item["priority"]], item["category"], item["target_id"]))
    _store_items(conn, shop_id, selected, items, now)
    _store_alerts(conn, shop_id, selected, items, now)
    stored = _load_items(conn, shop_id, selected)
    overview = _overview(stored, selected, today, window_start, window_end)
    if record_log:
        log_op(conn, "生成调整方案", f"{selected.isoformat()} {overview}")
    counts = {"pending": 0, "done": 0, "dismissed": 0, "high": 0}
    for item in stored:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        if item["priority"] == "high":
            counts["high"] += 1
    return {
        "date": selected.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "includes_today_in_rules": False,
        "overview": overview,
        "footnote": (
            "测款和测图使用最近 7 个完整日，避免上午的低量被当成下滑。"
            "预算节奏只在查看今天时，按当前时刻外推全天花费。"
        ),
        "generated_at": iso(now),
        "counts": counts,
        "items": stored,
    }


def set_item_status(
    conn: sqlite3.Connection,
    shop_id: str,
    selected: date,
    item_key: str,
    status: str,
) -> dict:
    if status not in {"pending", "done", "dismissed"}:
        raise AppError("状态只能是待处理、已执行或已忽略")
    now = iso(now_local())
    cur = conn.execute(
        """
        UPDATE adjustment_items
        SET status = ?, updated_at = ?
        WHERE shop_id = ? AND stat_date = ? AND item_key = ?
        """,
        (status, now, shop_id, selected.isoformat(), item_key),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise AppError("这条建议不存在", status=404, code="missing_item")
    label = {"pending": "待处理", "done": "已执行", "dismissed": "已忽略"}[status]
    row = conn.execute(
        "SELECT title FROM adjustment_items WHERE shop_id = ? AND stat_date = ? AND item_key = ?",
        (shop_id, selected.isoformat(), item_key),
    ).fetchone()
    log_op(conn, "更新建议状态", f"{row['title']} → {label}")
    return _load_items(conn, shop_id, selected, item_key)[0]


def goods_recommendations(aggs: list[dict], settings: Settings) -> list[dict]:
    items = []
    for agg in aggs:
        item = evaluate_goods(agg, settings)
        if item:
            items.append(item)
    return items


def evaluate_goods(agg: dict, settings: Settings) -> dict | None:
    stat = rates(agg["impressions"], agg["clicks"], agg["orders"], agg["spend_fen"], agg["gmv_fen"])
    util = safe_div(agg["spend_fen"], agg["daily_budget_fen"] * agg["days"]) if agg["days"] else None
    spend_yuan = agg["spend_fen"] / 100
    cap = (agg["daily_budget_fen"] / 100) * max(agg["days"], 1) * settings.observe_budget_ratio
    insufficient = (
        agg["days"] == 0
        or agg["impressions"] < settings.goods_min_impressions
        or agg["orders"] < settings.goods_min_orders
    )
    common = {
        "category": "测款",
        "target_id": agg["goods_id"],
        "target_name": agg["name"],
        "campaign_id": agg["campaign_id"],
        "window_label": f"近{agg['days']}个完整日",
    }
    chips = _chips(
        [
            ("花费", f"{spend_yuan:.2f} 元"),
            ("ROI", roi_text(stat["roi"])),
            ("订单", str(agg["orders"])),
            ("点击率", pct_text(stat["ctr"])),
            ("转化率", pct_text(stat["cvr"])),
            ("预算使用率", pct_text(util)),
        ]
    )
    raw = {**stat, "budget_util": None if util is None else round(util, 4), "stage": agg.get("stage")}

    if insufficient and spend_yuan < cap:
        return _item(
            common,
            priority="low",
            action="goods_observe",
            title=f"观察：「{agg['name']}」样本还不够",
            reason=(
                f"累计曝光 {agg['impressions']}，订单 {agg['orders']}。"
                "花费还不高，现在暂停或放量都容易误判。"
            ),
            suggestion="维持小预算，等曝光和订单积累后再决定。",
            chips=chips,
            raw=raw,
        )

    ctr = stat["ctr"] or 0
    cvr = stat["cvr"] or 0
    roi = stat["roi"]
    if ctr >= settings.high_ctr and cvr < settings.low_cvr:
        return _item(
            common,
            priority="high",
            action="goods_fix_listing",
            title=f"优化：「{agg['name']}」先改详情再加预算",
            reason=f"点击率 {pct_text(ctr)} 已经不低，转化率只有 {pct_text(cvr)}，流量没有变成订单。",
            suggestion="检查价格、评价、详情和 SKU。先不要加预算，避免把无效点击放大。",
            chips=chips,
            raw=raw,
        )
    if roi is not None and roi < settings.pause_roi and spend_yuan >= settings.pause_min_spend_yuan:
        return _item(
            common,
            priority="high",
            action="goods_pause",
            title=f"暂停：「{agg['name']}」先停掉投放",
            reason=(
                f"ROI {roi_text(roi)}，低于暂停线 {settings.pause_roi:g}，"
                f"花费 {spend_yuan:.2f} 元。"
            ),
            suggestion="在推广里暂停该商品。链接先留着，改完主图、价格或评价后用小预算重开。",
            chips=chips,
            raw=raw,
        )
    rising = (
        agg["orders_recent"] is not None
        and agg["orders_prior"] is not None
        and agg["orders_recent"] >= agg["orders_prior"] + 4
    )
    if (
        roi is not None
        and roi >= settings.scale_roi
        and agg["orders"] >= settings.scale_min_orders
        and util is not None
        and util >= settings.scale_min_budget_util
    ):
        new_budget = adjust_budget_fen(agg["daily_budget_fen"], settings.budget_up_ratio)
        return _item(
            common,
            priority="high",
            action="goods_scale",
            title=f"放量：上调「{agg['name']}」日预算",
            reason=(
                f"ROI {roi_text(roi)}，订单 {agg['orders']}，"
                f"预算使用率 {pct_text(util)}。效率和消耗都支撑加预算。"
            ),
            suggestion=(
                f"日预算从 {yuan_text(agg['daily_budget_fen'])} 元调到 {yuan_text(new_budget)} 元"
                f"（+{settings.budget_up_ratio * 100:.0f}%）。"
            ),
            chips=chips,
            raw=raw,
        )
    if (
        roi is not None
        and roi >= settings.target_roi
        and rising
        and util is not None
        and util >= settings.mild_min_budget_util
    ):
        new_budget = adjust_budget_fen(agg["daily_budget_fen"], settings.budget_mild_ratio)
        return _item(
            common,
            priority="medium",
            action="goods_mild_up",
            title=f"小幅放量：「{agg['name']}」日预算上调",
            reason=(
                f"ROI {roi_text(roi)} 达到目标，近 3 日订单 {agg['orders_recent']}，"
                f"前 3 日 {agg['orders_prior']}，预算使用率 {pct_text(util)}。"
            ),
            suggestion=(
                f"日预算从 {yuan_text(agg['daily_budget_fen'])} 元调到 {yuan_text(new_budget)} 元"
                f"（+{settings.budget_mild_ratio * 100:.0f}%）。先看一天再决定是否继续加。"
            ),
            chips=chips,
            raw=raw,
        )
    return None


def image_recommendations(aggs: list[dict], settings: Settings) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for agg in aggs:
        grouped[agg["goods_id"]].append(agg)
    items: list[dict] = []
    for group in grouped.values():
        items.extend(_images_for_goods(group, settings))
    return items


def _images_for_goods(group: list[dict], settings: Settings) -> list[dict]:
    eligible = []
    for agg in group:
        if agg["impressions"] < settings.image_min_impressions or agg["clicks"] <= 0:
            continue
        stat = rates(agg["impressions"], agg["clicks"], agg["orders"], agg["spend_fen"], agg["gmv_fen"])
        eligible.append({**agg, **stat})
    if len(eligible) < 2:
        return []
    ranked = sorted(eligible, key=lambda row: (row["ctr"] or 0, row["impressions"]), reverse=True)
    best, second = ranked[0], ranked[1]
    best_ctr = best["ctr"] or 0
    second_ctr = second["ctr"] or 0
    goods_name = best["goods_name"]
    if best_ctr < settings.min_ctr_image:
        return [
            _item(
                {
                    "category": "测图",
                    "target_id": best["goods_id"],
                    "target_name": goods_name,
                    "window_label": "近7个完整日",
                },
                priority="high",
                action="image_redesign",
                title=f"重做创意：「{goods_name}」",
                reason=f"样本够了，最好的图点击率也只有 {pct_text(best_ctr)}。",
                suggestion="停掉现有测试图，换一批白底、场景和卖点图，重新小预算开测。",
                chips=_chips(
                    [(row["label"], pct_text(row["ctr"])) for row in ranked]
                ),
                raw={"best_ctr": best_ctr},
            )
        ]
    if second_ctr <= 0 or best_ctr < second_ctr * settings.image_ctr_gap:
        return []
    best_cvr = best["cvr"] or 0
    second_cvr = second["cvr"] or 0
    if best_cvr < second_cvr * 0.8:
        return []
    items = [
        _item(
            {
                "category": "测图",
                "target_id": best["image_id"],
                "target_name": best["label"],
                "window_label": "近7个完整日",
            },
            priority="high",
            action="image_set_primary",
            title=f"换主图：把「{best['label']}」设为首图",
            reason=(
                f"「{goods_name}」里，这张图点击率 {pct_text(best_ctr)}，"
                f"次优是「{second['label']}」{pct_text(second_ctr)}。"
                "转化率没有明显更差。"
            ),
            suggestion=f"到商品编辑页，把「{best['label']}」设为商品主图。",
            chips=_chips(
                [
                    ("点击率", pct_text(best_ctr)),
                    ("转化率", pct_text(best_cvr)),
                    ("曝光", str(best["impressions"])),
                ]
            ),
            raw={"ctr": best_ctr, "cvr": best_cvr, "impressions": best["impressions"]},
        )
    ]
    cutoff = best_ctr * 0.6
    for other in ranked[1:]:
        other_ctr = other["ctr"] or 0
        if other_ctr >= cutoff:
            continue
        items.append(
            _item(
                {
                    "category": "测图",
                    "target_id": other["image_id"],
                    "target_name": other["label"],
                    "window_label": "近7个完整日",
                },
                priority="high",
                action="image_pause",
                title=f"停测图：「{other['label']}」",
                reason=f"点击率 {pct_text(other_ctr)}，不到胜出图的 60%，继续测会分走曝光。",
                suggestion="停止这张图的测试，把量让给胜出图。",
                chips=_chips(
                    [
                        ("点击率", pct_text(other_ctr)),
                        ("曝光", str(other["impressions"])),
                    ]
                ),
                raw={"ctr": other_ctr, "impressions": other["impressions"]},
            )
        )
    return items


def campaign_recommendations(aggs: list[dict], suppressed: set[str], settings: Settings) -> list[dict]:
    items = []
    for agg in aggs:
        if agg["campaign_id"] in suppressed:
            continue
        if agg["impressions"] < settings.goods_min_impressions or agg["days"] <= 0:
            continue
        stat = rates(agg["impressions"], agg["clicks"], agg["orders"], agg["spend_fen"], agg["gmv_fen"])
        util = safe_div(agg["spend_fen"], agg["daily_budget_fen"] * agg["days"])
        roi = stat["roi"]
        if roi is None or agg["spend_fen"] < 10000:
            continue
        common = {
            "category": "推广",
            "target_id": agg["campaign_id"],
            "target_name": agg["name"],
            "window_label": f"近{agg['days']}个完整日",
        }
        chips = _chips(
            [
                ("花费", f"{yuan_text(agg['spend_fen'])} 元"),
                ("ROI", roi_text(roi)),
                ("预算使用率", pct_text(util)),
            ]
        )
        if roi < settings.target_roi:
            if agg["scene"] == "全站推广" or not agg.get("bid_fen"):
                new_budget = adjust_budget_fen(agg["daily_budget_fen"], -0.20)
                items.append(
                    _item(
                        common,
                        priority="medium",
                        action="campaign_cut_budget",
                        title=f"收预算：「{agg['name']}」",
                        reason=f"计划 ROI {roi_text(roi)}，低于目标 {settings.target_roi:g}。全站推广没有单独点击出价。",
                        suggestion=(
                            f"日预算从 {yuan_text(agg['daily_budget_fen'])} 元收到 {yuan_text(new_budget)} 元。"
                        ),
                        chips=chips,
                        raw={**stat, "budget_util": util},
                    )
                )
            else:
                new_bid = max(1, int(round(agg["bid_fen"] * 0.9)))
                items.append(
                    _item(
                        common,
                        priority="medium",
                        action="campaign_lower_bid",
                        title=f"降出价：「{agg['name']}」",
                        reason=f"计划 ROI {roi_text(roi)}，低于目标 {settings.target_roi:g}。",
                        suggestion=f"出价从 {yuan_text(agg['bid_fen'])} 元降到 {yuan_text(new_bid)} 元（约 -10%）。",
                        chips=chips,
                        raw={**stat, "bid_fen": agg["bid_fen"], "new_bid_fen": new_bid},
                    )
                )
        elif util is not None and roi >= settings.scale_roi and util >= settings.campaign_raise_util:
            new_budget = adjust_budget_fen(agg["daily_budget_fen"], settings.budget_up_ratio)
            items.append(
                _item(
                    common,
                    priority="medium",
                    action="campaign_raise_budget",
                    title=f"加预算：「{agg['name']}」",
                    reason=(
                        f"计划 ROI {roi_text(roi)} 达到放量线，预算使用率 {pct_text(util)}，"
                        "再限预算会丢掉还能赚钱的量。"
                    ),
                    suggestion=(
                        f"日预算从 {yuan_text(agg['daily_budget_fen'])} 元调到 {yuan_text(new_budget)} 元。"
                    ),
                    chips=chips,
                    raw={**stat, "budget_util": util},
                )
            )
    return items


def pacing_recommendations(rows: list[dict], now: datetime, settings: Settings) -> list[dict]:
    items = []
    for row in rows:
        util = safe_div(row["spend_fen"], row["daily_budget_fen"])
        roi = safe_div(row["gmv_fen"], row["spend_fen"])
        flag = classify_pace(None if util is None else util, roi, now, settings)
        if flag is None:
            continue
        common = {
            "category": "预算",
            "target_id": row["campaign_id"],
            "target_name": row["name"],
            "window_label": "今天",
        }
        chips = _chips(
            [
                ("今日花费", f"{yuan_text(row['spend_fen'])} 元"),
                ("日预算", f"{yuan_text(row['daily_budget_fen'])} 元"),
                ("使用率", pct_text(util)),
                ("当前 ROI", roi_text(None if roi is None else round(roi, 4))),
            ]
        )
        if flag == "fast":
            items.append(
                _item(
                    common,
                    priority="medium",
                    action="pacing_fast",
                    title=f"节奏过快：「{row['name']}」",
                    reason="按现在的速度外推，日预算会明显提前花完。",
                    suggestion="临时降低出价，或把日限额收紧，给晚上留一点量。",
                    chips=chips,
                    raw={"budget_util": util, "roi": roi},
                )
            )
        else:
            items.append(
                _item(
                    common,
                    priority="medium",
                    action="pacing_slow",
                    title=f"节奏偏慢：「{row['name']}」",
                    reason="ROI 还在目标之上，但按当前速度预算花不完。",
                    suggestion="出价上调 5%～10%，两小时后再看花费和 ROI。",
                    chips=chips,
                    raw={"budget_util": util, "roi": roi},
                )
            )
    return items


def _item(common: dict, **fields) -> dict:
    raw = fields.pop("raw")
    chips = fields.pop("chips")
    return {
        **common,
        **fields,
        "evidence": {"chips": chips, "raw": _jsonable(raw)},
    }


def _chips(pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"label": label, "value": value} for label, value in pairs]


def _jsonable(raw: dict) -> dict:
    cleaned = {}
    for key, value in raw.items():
        if isinstance(value, float):
            cleaned[key] = round(value, 6)
        else:
            cleaned[key] = value
    return cleaned


def _sum_orders(series: list[sqlite3.Row]) -> tuple[int | None, int | None]:
    orders = [int(row["orders"]) for row in series]
    if len(orders) < 6:
        return None, None
    return sum(orders[-6:-3]), sum(orders[-3:])


def _aggregate_goods(conn, shop_id: str, start: date, end: date) -> list[dict]:
    goods = conn.execute("SELECT * FROM goods WHERE shop_id = ? ORDER BY name", (shop_id,)).fetchall()
    grouped = _grouped_days(
        conn,
        """
        SELECT * FROM goods_daily
        WHERE shop_id = ? AND stat_date BETWEEN ? AND ?
        ORDER BY goods_id, stat_date
        """,
        (shop_id, start.isoformat(), end.isoformat()),
        "goods_id",
    )
    aggs = []
    for row in goods:
        series = grouped.get(row["goods_id"], [])
        prior, recent = _sum_orders(series)
        aggs.append(
            {
                "goods_id": row["goods_id"],
                "campaign_id": row["campaign_id"],
                "name": row["name"],
                "stage": row["stage"],
                "daily_budget_fen": row["daily_budget_fen"],
                "days": len(series),
                "orders_prior": prior,
                "orders_recent": recent,
                **_sum_metrics(series, with_cart=True),
            }
        )
    return aggs


def _aggregate_images(conn, shop_id: str, start: date, end: date) -> list[dict]:
    images = conn.execute(
        """
        SELECT i.*, g.name AS goods_name, g.price_fen
        FROM image_tests i
        JOIN goods g ON g.goods_id = i.goods_id
        WHERE i.shop_id = ?
        ORDER BY i.label
        """,
        (shop_id,),
    ).fetchall()
    grouped = _grouped_days(
        conn,
        """
        SELECT * FROM image_daily
        WHERE shop_id = ? AND stat_date BETWEEN ? AND ?
        ORDER BY image_id, stat_date
        """,
        (shop_id, start.isoformat(), end.isoformat()),
        "image_id",
    )
    aggs = []
    for row in images:
        series = grouped.get(row["image_id"], [])
        aggs.append(
            {
                "image_id": row["image_id"],
                "goods_id": row["goods_id"],
                "goods_name": row["goods_name"],
                "label": row["label"],
                "days": len(series),
                **_sum_metrics(series, with_cart=False),
            }
        )
    return aggs


def _aggregate_campaigns(conn, shop_id: str, start: date, end: date) -> list[dict]:
    campaigns = conn.execute(
        "SELECT * FROM campaigns WHERE shop_id = ? ORDER BY name",
        (shop_id,),
    ).fetchall()
    grouped = _grouped_days(
        conn,
        """
        SELECT * FROM campaign_daily
        WHERE shop_id = ? AND stat_date BETWEEN ? AND ?
        ORDER BY campaign_id, stat_date
        """,
        (shop_id, start.isoformat(), end.isoformat()),
        "campaign_id",
    )
    aggs = []
    for row in campaigns:
        series = grouped.get(row["campaign_id"], [])
        aggs.append(
            {
                "campaign_id": row["campaign_id"],
                "name": row["name"],
                "scene": row["scene"],
                "bid_fen": row["bid_fen"],
                "daily_budget_fen": row["daily_budget_fen"],
                "days": len(series),
                **_sum_metrics(series, with_cart=True),
            }
        )
    return aggs


def _today_campaigns(conn, shop_id: str, today: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.campaign_id, c.name, c.daily_budget_fen,
               COALESCE(d.spend_fen, 0) AS spend_fen,
               COALESCE(d.gmv_fen, 0) AS gmv_fen,
               COALESCE(d.impressions, 0) AS impressions
        FROM campaigns c
        LEFT JOIN campaign_daily d
          ON d.campaign_id = c.campaign_id AND d.stat_date = ?
        WHERE c.shop_id = ?
        """,
        (today.isoformat(), shop_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _grouped_days(conn, sql: str, params: tuple, key: str) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(sql, params):
        grouped[row[key]].append(row)
    return grouped


def _sum_metrics(series: list[sqlite3.Row], *, with_cart: bool) -> dict:
    total = {
        "impressions": 0,
        "clicks": 0,
        "spend_fen": 0,
        "orders": 0,
        "gmv_fen": 0,
    }
    if with_cart:
        total["add_to_cart"] = 0
    for row in series:
        for field in total:
            total[field] += int(row[field])
    return total


def _store_items(conn, shop_id: str, selected: date, items: list[dict], now: datetime) -> None:
    existing = {
        row["item_key"]: row["status"]
        for row in conn.execute(
            "SELECT item_key, status FROM adjustment_items WHERE shop_id = ? AND stat_date = ?",
            (shop_id, selected.isoformat()),
        )
    }
    keys: list[str] = []
    stamp = iso(now)
    with conn:
        for item in items:
            key = f"{item['action']}:{item['target_id']}"
            keys.append(key)
            status = existing.get(key, "pending")
            if status not in {"pending", "done", "dismissed"}:
                status = "pending"
            conn.execute(
                """
                INSERT INTO adjustment_items(
                  shop_id, stat_date, item_key, priority, category, target_id, target_name,
                  action, title, reason, suggestion, evidence_json, status, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shop_id, stat_date, item_key) DO UPDATE SET
                  priority = excluded.priority,
                  category = excluded.category,
                  target_id = excluded.target_id,
                  target_name = excluded.target_name,
                  action = excluded.action,
                  title = excluded.title,
                  reason = excluded.reason,
                  suggestion = excluded.suggestion,
                  evidence_json = excluded.evidence_json,
                  status = excluded.status,
                  updated_at = excluded.updated_at
                """,
                (
                    shop_id,
                    selected.isoformat(),
                    key,
                    item["priority"],
                    item["category"],
                    item["target_id"],
                    item["target_name"],
                    item["action"],
                    item["title"],
                    item["reason"],
                    item["suggestion"],
                    json.dumps(item["evidence"], ensure_ascii=False),
                    status,
                    stamp,
                ),
            )
        if keys:
            marks = ",".join("?" for _ in keys)
            conn.execute(
                f"""
                DELETE FROM adjustment_items
                WHERE shop_id = ? AND stat_date = ? AND item_key NOT IN ({marks})
                """,
                (shop_id, selected.isoformat(), *keys),
            )
        else:
            conn.execute(
                "DELETE FROM adjustment_items WHERE shop_id = ? AND stat_date = ?",
                (shop_id, selected.isoformat()),
            )


def _store_alerts(conn, shop_id: str, selected: date, items: list[dict], now: datetime) -> None:
    stamp = iso(now)
    codes = []
    with conn:
        for item in items:
            if item["priority"] == "low":
                continue
            code = f"{item['action']}:{item['target_id']}"
            codes.append(code)
            level = "warning" if item["priority"] == "high" else "info"
            conn.execute(
                """
                INSERT INTO alerts(shop_id, stat_date, code, level, title, body, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shop_id, stat_date, code) DO UPDATE SET
                  level = excluded.level,
                  title = excluded.title,
                  body = excluded.body
                """,
                (
                    shop_id,
                    selected.isoformat(),
                    code,
                    level,
                    item["title"],
                    item["reason"] + item["suggestion"],
                    stamp,
                ),
            )
        if codes:
            marks = ",".join("?" for _ in codes)
            conn.execute(
                f"DELETE FROM alerts WHERE shop_id = ? AND stat_date = ? AND code NOT IN ({marks})",
                (shop_id, selected.isoformat(), *codes),
            )
        else:
            conn.execute(
                "DELETE FROM alerts WHERE shop_id = ? AND stat_date = ?",
                (shop_id, selected.isoformat()),
            )


def _load_items(conn, shop_id: str, selected: date, item_key: str | None = None) -> list[dict]:
    sql = """
      SELECT * FROM adjustment_items
      WHERE shop_id = ? AND stat_date = ?
    """
    params: list = [shop_id, selected.isoformat()]
    if item_key:
        sql += " AND item_key = ?"
        params.append(item_key)
    sql += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, category, target_id"
    rows = []
    for row in conn.execute(sql, params):
        evidence = json.loads(row["evidence_json"])
        rows.append(
            {
                "item_key": row["item_key"],
                "priority": row["priority"],
                "category": row["category"],
                "target_id": row["target_id"],
                "target_name": row["target_name"],
                "action": row["action"],
                "title": row["title"],
                "reason": row["reason"],
                "suggestion": row["suggestion"],
                "chips": evidence.get("chips", []),
                "status": row["status"],
                "updated_at": row["updated_at"],
            }
        )
    return rows


def _overview(items: list[dict], selected: date, today: date, start: date, end: date) -> str:
    pending_high = [item for item in items if item["priority"] == "high" and item["status"] == "pending"]
    text = (
        f"统计窗口 {start.isoformat()} 至 {end.isoformat()}（7 个完整日），"
        f"共 {len(items)} 条建议。"
    )
    if selected >= today:
        text += "查看的是今天，测款和测图不含今天的未完结数据；预算节奏按当前时刻计算。"
    if pending_high:
        text += f"优先处理 {len(pending_high)} 条，第一条是：{pending_high[0]['title']}。"
    else:
        text += "没有待处理的高优先级建议。"
    return text
