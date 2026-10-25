"""指标口径。金额在库里用分，展示用元。"""

from __future__ import annotations

from datetime import datetime


def yuan(fen: int | float) -> float:
    return round(float(fen) / 100.0, 2)


def yuan_text(fen: int | float) -> str:
    return f"{yuan(fen):.2f}"


def pct_text(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def roi_text(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def safe_div(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return num / den


def change(curr: float | None, prev: float | None) -> float | None:
    if curr is None or prev is None or prev == 0:
        return None
    return (curr - prev) / prev


def rates(impressions: int, clicks: int, orders: int, spend_fen: int, gmv_fen: int) -> dict:
    ctr = safe_div(clicks, impressions)
    cvr = safe_div(orders, clicks)
    roi = safe_div(gmv_fen, spend_fen)
    cpa = safe_div(spend_fen, orders)
    cpc = safe_div(spend_fen, clicks)
    return {
        "impressions": impressions,
        "clicks": clicks,
        "orders": orders,
        "spend_fen": spend_fen,
        "gmv_fen": gmv_fen,
        "spend_yuan": yuan(spend_fen),
        "gmv_yuan": yuan(gmv_fen),
        "ctr": None if ctr is None else round(ctr, 6),
        "cvr": None if cvr is None else round(cvr, 6),
        "roi": None if roi is None else round(roi, 4),
        "cpa_yuan": None if cpa is None else yuan(cpa),
        "cpc_yuan": None if cpc is None else yuan(cpc),
    }


def intraday_progress(now: datetime) -> float:
    minutes = now.hour * 60 + now.minute
    return max(0.02, min(0.999, minutes / 1440))


def classify_pace(util: float | None, roi: float | None, now: datetime, settings) -> str | None:
    """返回 fast / slow / None。只对「今天」有意义，调用方不要拿历史日来问。"""
    if util is None:
        return None
    hour = now.hour + now.minute / 60
    if not (10 <= hour <= 20):
        return None
    projected = util / intraday_progress(now)
    if util >= 0.5 and projected >= settings.pacing_fast_projected:
        return "fast"
    if (
        hour >= 14
        and roi is not None
        and roi >= settings.target_roi
        and projected < settings.pacing_slow_projected
    ):
        return "slow"
    return None


def adjust_budget_fen(budget_fen: int, ratio: float) -> int:
    target = int(round(budget_fen * (1 + ratio) / 100.0) * 100)
    if ratio > 0:
        return max(target, budget_fen + 100)
    return max(1000, min(target, budget_fen - 100))
