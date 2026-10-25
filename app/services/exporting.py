"""导出日报。CSV 带 BOM，方便 Windows Excel 打开中文。"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, datetime

from app.db import log_op
from app.metrics import pct_text, roi_text
from app.services.readmodel import campaigns_for_day, goods_for_day, images_for_day, shop_name
from app.services.recommendations import build_plan
from app.services.summary import build_summary
from app.timeutil import now_local


def daily_markdown(conn: sqlite3.Connection, shop_id: str, day: date, now: datetime | None = None) -> str:
    now = now or now_local()
    summary = build_summary(conn, shop_id, day, now, record_log=False)
    plan = build_plan(conn, shop_id, day, now, record_log=False)
    lines = [
        f"# {summary['shop_name']} 推广日报",
        "",
        summary["headline"],
        "",
        "## 当日数字",
        "",
        "| 指标 | 数值 | 环比 |",
        "| --- | --- | --- |",
    ]
    for kpi in summary["kpis"]:
        lines.append(f"| {kpi['label']} | {_kpi_text(kpi)} | {_dod(kpi['dod'])} |")
    lines.extend(["", "## 总结", "", summary["narrative"], "", "## 亮点"])
    lines.extend(f"- {item}" for item in summary["highlights"])
    lines.extend(["", "## 风险"])
    lines.extend(f"- {item}" for item in summary["risks"])
    lines.extend(["", "## 调整方案", "", plan["overview"], ""])
    lines.append("| 优先级 | 类别 | 建议 | 状态 |")
    lines.append("| --- | --- | --- | --- |")
    status = {"pending": "待处理", "done": "已执行", "dismissed": "已忽略"}
    priority = {"high": "高", "medium": "中", "low": "低"}
    for item in plan["items"]:
        lines.append(
            f"| {priority[item['priority']]} | {item['category']} | {item['title']} | {status[item['status']]} |"
        )
    lines.extend(["", "### 怎么调", ""])
    for item in plan["items"]:
        if item["priority"] == "low":
            continue
        lines.append(f"**{item['title']}**")
        lines.append("")
        lines.append(item["reason"])
        lines.append("")
        lines.append(item["suggestion"])
        lines.append("")
    lines.append(plan["footnote"])
    lines.append("")
    text = "\n".join(lines)
    log_op(conn, "导出", f"Markdown 日报 {day.isoformat()}")
    return text


def metrics_csv(conn: sqlite3.Connection, shop_id: str, day: date) -> bytes:
    name = shop_name(conn, shop_id) or ""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["店铺", name, "日期", day.isoformat()])
    writer.writerow([])
    writer.writerow(["层级", "名称", "归属", "曝光", "点击", "花费(元)", "成交额(元)", "订单", "点击率", "转化率", "ROI", "加购", "日预算(元)"])
    for row in campaigns_for_day(conn, shop_id, day):
        writer.writerow(_csv_row("推广计划", row["name"], row["scene"], row, row["add_to_cart"], row["budget_yuan"]))
    for row in goods_for_day(conn, shop_id, day):
        writer.writerow(
            _csv_row("测款", row["name"], row["campaign_name"], row, row["add_to_cart"], row["budget_yuan"])
        )
    for row in images_for_day(conn, shop_id, day):
        writer.writerow(_csv_row("测图", row["label"], row["goods_name"], row, "", ""))
    log_op(conn, "导出", f"CSV 明细 {day.isoformat()}")
    return buffer.getvalue().encode("utf-8-sig")


def _csv_row(level, name, parent, row, cart, budget) -> list:
    return [
        level,
        name,
        parent,
        row["impressions"],
        row["clicks"],
        f"{row['spend_yuan']:.2f}",
        f"{row['gmv_yuan']:.2f}",
        row["orders"],
        pct_text(row["ctr"]),
        pct_text(row["cvr"]),
        roi_text(row["roi"]),
        cart,
        budget if budget == "" else f"{budget:.2f}",
    ]


def _kpi_text(kpi: dict) -> str:
    value = kpi["value"]
    if value is None:
        return "—"
    if kpi["format"] == "rate":
        return pct_text(value)
    if kpi["format"] == "roi":
        return roi_text(value)
    if kpi["format"] == "count":
        return str(value)
    unit = kpi["unit"]
    return f"{value:.2f}{unit}"


def _dod(value) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value * 100:.1f}%"
