"""数据源适配器。"""

from pathlib import Path

from app.adapters.base import MetricDay, PromoAdapter, SyncPayload
from app.adapters.mock import MockAdapter
from app.adapters.pdd_live import PddLiveAdapter

__all__ = [
    "MetricDay",
    "MockAdapter",
    "PddLiveAdapter",
    "PromoAdapter",
    "SyncPayload",
    "get_adapter",
]


def get_adapter(mode: str, db_path: str | Path | None = None) -> PromoAdapter:
    if mode == "live":
        if not db_path:
            raise ValueError("真实店铺需要数据库路径")
        return PddLiveAdapter(db_path)
    if mode == "mock":
        return MockAdapter()
    raise ValueError("未知数据模式")
