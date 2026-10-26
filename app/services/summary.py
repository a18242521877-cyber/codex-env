"""每日总结。由本地数据直接写中文，不上传到外部模型。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from app.db import log_op
from app.errors import AppError
from app.metrics import change, pct_text, roi_text
from app.services.readmodel import day_metrics, ranked_goods, ranked_images, shop_name
from app.timeutil import iso, now_local, pretty_day


def build_summary(
    conn: sqlite3.Connection,
    shop_id: str,
    day: date,
    now: datetime | None = None,
    *,
    record_log: bool = False,
) -> dict:
    now = now or now_local()
    today = now.date()
    if day > today:
        raise AppError("不能总结未来的日期")
    name = shop_name(conn, shop_id)
    if not name:
        raise AppError("没有可总结的数据，请先同步", status=409, code="no_data")
    current = day_metrics(conn, "goods_daily", shop_id, day)
    previous = day_metrics(conn, "goods_daily", shop_id, day - timedelta(days=1))
    goods = ranked_goods(conn, shop_id, day)
    images = ranked_images(conn, shop_id, day)
    payload = _compose(name, day, today, current, previous, goods, images, iso(now))
    with conn:
        conn.execute(
            """
            INSERT INTO daily_summaries(shop_id, stat_date, payload_json, generated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(shop_id, stat_date) DO UPDATE SET
              payload_json = excluded.payload_json,
              generated_at = excluded.generated_at
            """,
            (shop_id, day.isoformat(), json.dumps(payload, ensure_ascii=False), payload["generated_at"]),
        )
    if record_log:
        log_op(conn, "生成日报", f"{day.isoformat()} {name}")
    return payload


def _compose(name, day, today, current, previous, goods, images, generated_at) -> dict:
    complete = day < today
    kpis = _kpis(current, previous)
    qualified = [row for row in goods if row["spend_fen"] >= 2000]
    top = sorted(qualified, key=lambda row: (row["roi"] is not None, row["roi"] or -1), reverse=True)[:3]
    weak = sorted(
        [row for row in qualified if row["roi"] is not None],
        key=lambda row: row["roi"],
    )[:3]
    best_image = images[0] if images else None
    worst_image = images[-1] if len(images) > 1 else None

    highlights = []
    risks = []
    if current["roi"] is not None and current["roi"] >= 2:
        highlights.append(f"当日 ROI {roi_text(current['roi'])}，达到默认目标 2。")
    if top:
        best = top[0]
        highlights.append(
            f"测款效率最好的是「{best['name']}」，ROI {roi_text(best['roi'])}，订单 {best['orders']} 笔。"
        )
    if best_image and worst_image and (best_image["ctr"] or 0) >= (worst_image["ctr"] or 0) * 1.2:
        highlights.append(
            f"测图「{best_image['label']}」点击率 {pct_text(best_image['ctr'])}，"
            f"高于「{worst_image['label']}」的 {pct_text(worst_image['ctr'])}。"
        )
    if not complete:
        risks.append("今天还没结束，花费和订单都会继续变，不要把这份总结当成全天结论。")
    for row in weak:
        if row["roi"] is not None and row["roi"] < 1:
            risks.append(f"「{row['name']}」当日 ROI 只有 {roi_text(row['roi'])}，低于暂停线。")
    for row in goods:
        ctr = row["ctr"] or 0
        cvr = row["cvr"] or 0
        if row["clicks"] >= 50 and ctr >= 0.03 and cvr < 0.02:
            risks.append(f"「{row['name']}」点击率 {pct_text(ctr)}，转化率 {pct_text(cvr)}，详情页可能接不住流量。")
    spend_change = change(current["spend_yuan"], previous["spend_yuan"])
    if complete and spend_change is not None and spend_change > 0.3:
        risks.append("花费比前一日高出 30% 以上，确认一下是不是预算放得太开。")
    if not highlights:
        highlights.append("这一天没有特别突出的测款，维持观察即可。")
    if not risks:
        risks.append("没有触发花费、ROI 或转化的明显风险。")

    shop_data_only = bool(goods) and not images and all(row.get("category") == "商品数据" for row in goods)
    narrative = _narrative(
        name,
        day,
        complete,
        current,
        previous,
        top,
        weak,
        best_image,
        worst_image,
        shop_data_only=shop_data_only,
    )
    return {
        "date": day.isoformat(),
        "shop_name": name,
        "complete": complete,
        "headline": pretty_day(day),
        "kpis": kpis,
        "highlights": highlights,
        "risks": risks,
        "top_goods": [_public_goods(row) for row in top],
        "weak_goods": [_public_goods(row) for row in weak if row["roi"] is not None and row["roi"] < 2],
        "best_image": _public_image(best_image),
        "worst_image": _public_image(worst_image) if worst_image and worst_image is not best_image else None,
        "narrative": narrative,
        "generated_at": generated_at,
    }


def _narrative(name, day, complete, current, previous, top, weak, best_image, worst_image, shop_data_only: bool = False) -> str:
    head = pretty_day(day)
    if complete:
        opening = f"{head}，{name} 的推广复盘如下。"
    else:
        opening = f"{head} 尚未结束。下面是 {name} 截至当前的推广表现，只能作日内参考。"
    spend_move = _movement("花费", current["spend_yuan"], previous["spend_yuan"])
    roi_move = _movement("ROI", current["roi"], previous["roi"])
    cpa = "—" if current["cpa_yuan"] is None else f"{current['cpa_yuan']:.2f} 元"
    body = (
        f"推广花费 {current['spend_yuan']:.2f} 元，成交额 {current['gmv_yuan']:.2f} 元，"
        f"ROI {roi_text(current['roi'])}，订单 {current['orders']} 笔。"
        f"点击率 {pct_text(current['ctr'])}，转化率 {pct_text(current['cvr'])}，"
        f"单笔成本 {cpa}。"
        f"{spend_move}，{roi_move}。"
    )
    parts = [opening, body]
    if shop_data_only and current["spend_yuan"] > 0:
        parts.append("推广花费来自商家后台首页汇总，不是测款或测图。请先在浏览器登录推广中心，再到本应用点同步。")
    elif shop_data_only:
        parts.append("成交额和订单来自商家后台「商品数据」，不是推广测款，也没有测图。请先在浏览器登录推广中心，再到本应用点同步。")
    if top:
        best = top[0]
        parts.append(
            f"测款里效率最好的是「{best['name']}」，ROI {roi_text(best['roi'])}，订单 {best['orders']} 笔。"
        )
    weak_line = [row for row in weak if row["roi"] is not None and row["roi"] < 1]
    if weak_line:
        row = weak_line[0]
        parts.append(f"需要盯住的是「{row['name']}」，当日 ROI 只有 {roi_text(row['roi'])}。")
    if best_image and worst_image and best_image is not worst_image:
        parts.append(
            f"测图方面，「{best_image['label']}」点击率 {pct_text(best_image['ctr'])}，"
            f"「{worst_image['label']}」点击率 {pct_text(worst_image['ctr'])}。"
        )
    parts.append("加减预算、换图和暂停以「调整方案」为准。那里看的是近 7 个完整日，不只看这一天。")
    return "\n\n".join(parts)


def _movement(label: str, curr, prev) -> str:
    delta = change(curr, prev)
    if delta is None:
        return f"{label}没有前一日可对比"
    if abs(delta) < 0.005:
        return f"{label}与前一日基本持平"
    word = "上升" if delta > 0 else "下降"
    return f"{label}较前一日{word} {abs(delta) * 100:.1f}%"


def _kpis(current: dict, previous: dict) -> list[dict]:
    specs = [
        ("spend_yuan", "推广花费", "元", "yuan"),
        ("gmv_yuan", "成交额", "元", "yuan"),
        ("roi", "ROI", "", "roi"),
        ("orders", "订单", "笔", "count"),
        ("ctr", "点击率", "", "rate"),
        ("cvr", "转化率", "", "rate"),
        ("cpa_yuan", "单笔成本", "元", "yuan"),
    ]
    rows = []
    for key, label, unit, kind in specs:
        rows.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "format": kind,
                "value": current[key],
                "dod": change(current[key], previous[key]),
            }
        )
    return rows


def _public_goods(row: dict) -> dict:
    return {
        "goods_id": row["goods_id"],
        "name": row["name"],
        "stage": row["stage"],
        "spend_yuan": row["spend_yuan"],
        "gmv_yuan": row["gmv_yuan"],
        "orders": row["orders"],
        "roi": row["roi"],
        "ctr": row["ctr"],
        "cvr": row["cvr"],
    }


def _public_image(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "image_id": row["image_id"],
        "label": row["label"],
        "goods_name": row["goods_name"],
        "ctr": row["ctr"],
        "cvr": row["cvr"],
        "impressions": row["impressions"],
        "orders": row["orders"],
    }
