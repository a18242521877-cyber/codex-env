from __future__ import annotations

from datetime import date, datetime, timedelta

WEEKDAYS = "一二三四五六日"


def now_local() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now_local().tzinfo)
    return dt.isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now_local().tzinfo)
    return dt


def parse_day(value: str) -> date:
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期格式应为 YYYY-MM-DD") from exc
    if day.year < 2020 or day.year > 2100:
        raise ValueError("日期超出可查询范围")
    return day


def pretty_day(day: date) -> str:
    return f"{day.year}年{day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}"


def day_span(end: date, days: int) -> list[date]:
    start = end - timedelta(days=days - 1)
    return [start + timedelta(days=i) for i in range(days)]
