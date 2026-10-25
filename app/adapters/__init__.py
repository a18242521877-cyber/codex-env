"""数据源适配器。"""

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


def get_adapter(mode: str) -> PromoAdapter:
    if mode == "live":
        return PddLiveAdapter()
    if mode == "mock":
        return MockAdapter()
    raise ValueError("未知数据模式")
