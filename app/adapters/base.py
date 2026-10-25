from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True)
class ShopProfile:
    shop_id: str
    mall_id: str
    name: str


@dataclass(frozen=True)
class CampaignInfo:
    campaign_id: str
    name: str
    scene: str
    status: str
    daily_budget_fen: int
    bid_fen: int | None
    pace: str


@dataclass(frozen=True)
class GoodsInfo:
    goods_id: str
    campaign_id: str
    name: str
    category: str
    price_fen: int
    stage: str
    daily_budget_fen: int


@dataclass(frozen=True)
class ImageInfo:
    image_id: str
    goods_id: str
    label: str
    tone: str


@dataclass(frozen=True)
class MetricDay:
    stat_date: str
    impressions: int
    clicks: int
    spend_fen: int
    orders: int
    gmv_fen: int
    add_to_cart: int = 0


@dataclass
class SyncPayload:
    shop: ShopProfile
    campaigns: list[CampaignInfo] = field(default_factory=list)
    campaign_days: dict[str, list[MetricDay]] = field(default_factory=dict)
    goods: list[GoodsInfo] = field(default_factory=list)
    goods_days: dict[str, list[MetricDay]] = field(default_factory=dict)
    images: list[ImageInfo] = field(default_factory=list)
    image_days: dict[str, list[MetricDay]] = field(default_factory=dict)
    note: str = ""


class PromoAdapter(Protocol):
    mode: str

    def fetch(self, *, today: date, now: datetime) -> SyncPayload:
        """拉取店铺在 today 之前（含 today）的推广、测图、测款。"""
