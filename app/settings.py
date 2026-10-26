from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config import LOGIN_GATES, SETTING_FIELDS
from app.db import load_setting_map, log_op, save_setting_map
from app.errors import AppError

_FIELDS = {field["key"]: field for field in SETTING_FIELDS}


@dataclass(frozen=True)
class Settings:
    data_mode: str
    refresh_interval_minutes: int
    target_roi: float
    pause_roi: float
    scale_roi: float
    pause_min_spend_yuan: float
    goods_min_impressions: int
    goods_min_orders: int
    scale_min_orders: int
    scale_min_budget_util: float
    mild_min_budget_util: float
    budget_up_ratio: float
    budget_mild_ratio: float
    high_ctr: float
    low_cvr: float
    observe_budget_ratio: float
    image_min_impressions: int
    image_ctr_gap: float
    min_ctr_image: float
    campaign_raise_util: float
    pacing_fast_projected: float
    pacing_slow_projected: float


def _as_settings(raw: dict[str, str]) -> Settings:
    return Settings(
        data_mode=raw["data_mode"],
        refresh_interval_minutes=int(raw["refresh_interval_minutes"]),
        target_roi=float(raw["target_roi"]),
        pause_roi=float(raw["pause_roi"]),
        scale_roi=float(raw["scale_roi"]),
        pause_min_spend_yuan=float(raw["pause_min_spend_yuan"]),
        goods_min_impressions=int(raw["goods_min_impressions"]),
        goods_min_orders=int(raw["goods_min_orders"]),
        scale_min_orders=int(raw["scale_min_orders"]),
        scale_min_budget_util=float(raw["scale_min_budget_util"]),
        mild_min_budget_util=float(raw["mild_min_budget_util"]),
        budget_up_ratio=float(raw["budget_up_ratio"]),
        budget_mild_ratio=float(raw["budget_mild_ratio"]),
        high_ctr=float(raw["high_ctr"]),
        low_cvr=float(raw["low_cvr"]),
        observe_budget_ratio=float(raw["observe_budget_ratio"]),
        image_min_impressions=int(raw["image_min_impressions"]),
        image_ctr_gap=float(raw["image_ctr_gap"]),
        min_ctr_image=float(raw["min_ctr_image"]),
        campaign_raise_util=float(raw["campaign_raise_util"]),
        pacing_fast_projected=float(raw["pacing_fast_projected"]),
        pacing_slow_projected=float(raw["pacing_slow_projected"]),
    )


def load_settings(conn: sqlite3.Connection) -> Settings:
    return _as_settings(load_setting_map(conn))


def settings_payload(conn: sqlite3.Connection) -> dict:
    values = load_setting_map(conn)
    return {"values": values, "fields": SETTING_FIELDS, "login_gates": list(LOGIN_GATES)}


def _normalize(field: dict, raw) -> str:
    kind = field["type"]
    if kind == "enum":
        value = str(raw)
        allowed = {choice["value"] for choice in field["choices"]}
        if value not in allowed:
            raise AppError(f"{field['label']}的取值无效")
        return value
    if kind == "int":
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise AppError(f"{field['label']}需要整数") from exc
        if not number.is_integer():
            raise AppError(f"{field['label']}需要整数")
        value = int(number)
        if value < field["min"] or value > field["max"]:
            raise AppError(f"{field['label']}需在 {field['min']} 到 {field['max']} 之间")
        return str(value)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise AppError(f"{field['label']}需要数字") from exc
    if value < field["min"] or value > field["max"]:
        raise AppError(f"{field['label']}需在 {field['min']} 到 {field['max']} 之间")
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def update_settings(conn: sqlite3.Connection, patch: dict) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise AppError("请提供要保存的设置")
    current = load_setting_map(conn)
    unknown = [key for key in patch if key not in _FIELDS]
    if unknown:
        raise AppError("包含无法识别的设置项")
    merged = dict(current)
    for key, raw in patch.items():
        merged[key] = _normalize(_FIELDS[key], raw)
    if float(merged["pause_roi"]) >= float(merged["target_roi"]):
        raise AppError("暂停 ROI 必须小于目标 ROI")
    if float(merged["target_roi"]) > float(merged["scale_roi"]):
        raise AppError("目标 ROI 不能大于放量 ROI")
    if float(merged["mild_min_budget_util"]) > float(merged["scale_min_budget_util"]):
        raise AppError("小幅上调的预算使用率不能高于放量使用率")
    save_setting_map(conn, merged)
    log_op(conn, "修改设置", f"数据模式 {merged['data_mode']}，目标 ROI {merged['target_roi']}")
    return settings_payload(conn)
