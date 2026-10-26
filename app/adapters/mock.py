"""虚构店铺「云岭家居旗舰店」。

数字是按规则反推的，用来稳定演示，不是真实经营数据。
改这些常量前先跑 `python run.py --self-test`，避免把测图/测款结论改没。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.adapters.base import (
    CampaignInfo,
    GoodsInfo,
    ImageInfo,
    MetricDay,
    ShopProfile,
    SyncPayload,
)
from app.config import HISTORY_DAYS

SHOP = ShopProfile(
    shop_id="mock-mall-10086001",
    mall_id="10086001",
    name="云岭家居旗舰店",
)

# pace: fast 今天消耗偏快；slow 今天消耗偏慢；normal 大致跟着时间走。
_CAMPAIGN_META = [
    {
        "campaign_id": "camp-search-hanger",
        "name": "搜索推广 · 衣架测款",
        "scene": "搜索推广",
        "status": "投放中",
        "bid_fen": 85,
        "pace": "fast",
    },
    {
        "campaign_id": "camp-scene-quilt",
        "name": "场景推广 · 夏凉被",
        "scene": "场景推广",
        "status": "投放中",
        "bid_fen": 120,
        "pace": "slow",
    },
    {
        "campaign_id": "camp-fullsite-storage",
        "name": "全站推广 · 收纳专区",
        "scene": "全站推广",
        "status": "投放中",
        "bid_fen": None,
        "pace": "normal",
    },
    {
        "campaign_id": "camp-search-shelf",
        "name": "搜索推广 · 置物架冷启动",
        "scene": "搜索推广",
        "status": "学习期",
        "bid_fen": 70,
        "pace": "normal",
    },
]

# 期望结论（默认阈值、近 7 个完整日）：
# hanger-20 放量；wide-hanger 先改详情；drawer-box 暂停；quilt 小幅上调；shelf 继续观察。
_GOODS_META = [
    {
        "goods_id": "goods-hanger-20",
        "campaign_id": "camp-search-hanger",
        "name": "无痕衣架 20只装",
        "category": "衣架",
        "price_fen": 1990,
        "stage": "放量",
        "daily_budget_fen": 22000,
        "impr": 10000,
        "clicks": 460,
        "orders": 39,
        "spend_fen": 21000,
        "rising": False,
    },
    {
        "goods_id": "goods-wide-hanger",
        "campaign_id": "camp-search-hanger",
        "name": "防滑宽肩衣架 10只",
        "category": "衣架",
        "price_fen": 1580,
        "stage": "观察",
        "daily_budget_fen": 8600,
        "impr": 8000,
        "clicks": 480,
        "orders": 6,
        "spend_fen": 8200,
        "rising": False,
    },
    {
        "goods_id": "goods-drawer-box",
        "campaign_id": "camp-fullsite-storage",
        "name": "抽屉收纳盒三件套",
        "category": "收纳",
        "price_fen": 2990,
        "stage": "观察",
        "daily_budget_fen": 40000,
        "impr": 6500,
        "clicks": 180,
        "orders": 7,
        "spend_fen": 33700,
        "rising": False,
    },
    {
        "goods_id": "goods-quilt",
        "campaign_id": "camp-scene-quilt",
        "name": "水洗棉夏凉被",
        "category": "床品",
        "price_fen": 5900,
        "stage": "稳定",
        "daily_budget_fen": 45000,
        "impr": 5200,
        "clicks": 180,
        "orders": 8,
        "spend_fen": 21000,
        "rising": True,
    },
    {
        "goods_id": "goods-shelf",
        "campaign_id": "camp-search-shelf",
        "name": "桌面置物架",
        "category": "收纳",
        "price_fen": 3600,
        "stage": "冷启动",
        "daily_budget_fen": 8000,
        "impr": 320,
        "clicks": 13,
        "orders": 0,
        "spend_fen": 1200,
        "rising": False,
    },
]

# 期望：主图 A 胜出，主图 C 停止。成交额按所属商品标价估算，不与商品日报相加。
_IMAGE_META = [
    {
        "image_id": "img-hanger-a",
        "goods_id": "goods-hanger-20",
        "label": "主图A · 白底平铺",
        "tone": "plain",
        "impr": 1800,
        "clicks": 94,
        "orders": 8,
        "spend_fen": 4200,
    },
    {
        "image_id": "img-hanger-b",
        "goods_id": "goods-hanger-20",
        "label": "主图B · 衣柜场景",
        "tone": "scene",
        "impr": 1600,
        "clicks": 64,
        "orders": 5,
        "spend_fen": 3600,
    },
    {
        "image_id": "img-hanger-c",
        "goods_id": "goods-hanger-20",
        "label": "主图C · 促销大字",
        "tone": "promo",
        "impr": 1500,
        "clicks": 26,
        "orders": 1,
        "spend_fen": 2800,
    },
]


def _pace_factor(pace: str, day: date, today: date, now: datetime) -> float:
    if day != today:
        return 1.0
    progress = max(0.02, min(0.999, (now.hour * 60 + now.minute) / 1440))
    if pace == "fast":
        return min(0.96, max(0.2, progress * 2.2))
    if pace == "slow":
        return min(1.0, max(0.05, progress * 0.45))
    return min(1.0, max(0.05, progress * 0.92))


def _scale(impr: int, clicks: int, orders: int, spend: int, price_fen: int, factor: float) -> MetricDay:
    impr_s = int(round(impr * factor))
    clicks_s = min(impr_s, int(round(clicks * factor)))
    orders_s = min(clicks_s, int(round(orders * factor)))
    spend_s = int(round(spend * factor))
    carts = min(clicks_s, int(round(clicks * 0.2 * factor)))
    return impr_s, clicks_s, orders_s, spend_s, orders_s * price_fen, carts


class MockAdapter:
    mode = "mock"

    def fetch(self, *, today: date, now: datetime) -> SyncPayload:
        pace_of = {row["campaign_id"]: row["pace"] for row in _CAMPAIGN_META}
        price_of = {row["goods_id"]: row["price_fen"] for row in _GOODS_META}
        goods: list[GoodsInfo] = []
        goods_days: dict[str, list[MetricDay]] = {}
        campaign_bucket: dict[str, dict[str, list[int]]] = {
            row["campaign_id"]: {} for row in _CAMPAIGN_META
        }

        dates = [today - timedelta(days=HISTORY_DAYS - 1 - index) for index in range(HISTORY_DAYS)]
        for spec in _GOODS_META:
            goods.append(
                GoodsInfo(
                    goods_id=spec["goods_id"],
                    campaign_id=spec["campaign_id"],
                    name=spec["name"],
                    category=spec["category"],
                    price_fen=spec["price_fen"],
                    stage=spec["stage"],
                    daily_budget_fen=spec["daily_budget_fen"],
                )
            )
            series: list[MetricDay] = []
            pace = pace_of[spec["campaign_id"]]
            for index, day in enumerate(dates):
                orders = spec["orders"] + (index // 2 if spec["rising"] else 0)
                spend = spec["spend_fen"]
                if spec["orders"]:
                    spend = int(round(spec["spend_fen"] * orders / spec["orders"]))
                factor = _pace_factor(pace, day, today, now)
                impr_s, clicks_s, orders_s, spend_s, gmv_s, carts = _scale(
                    spec["impr"], spec["clicks"], orders, spend, spec["price_fen"], factor
                )
                stat = day.isoformat()
                series.append(
                    MetricDay(stat, impr_s, clicks_s, spend_s, orders_s, gmv_s, carts)
                )
                bucket = campaign_bucket[spec["campaign_id"]].setdefault(stat, [0, 0, 0, 0, 0, 0])
                bucket[0] += impr_s
                bucket[1] += clicks_s
                bucket[2] += spend_s
                bucket[3] += orders_s
                bucket[4] += gmv_s
                bucket[5] += carts
            goods_days[spec["goods_id"]] = series

        budget_of: dict[str, int] = {row["campaign_id"]: 0 for row in _CAMPAIGN_META}
        for spec in _GOODS_META:
            budget_of[spec["campaign_id"]] += spec["daily_budget_fen"]

        campaigns: list[CampaignInfo] = []
        campaign_days: dict[str, list[MetricDay]] = {}
        for meta in _CAMPAIGN_META:
            cid = meta["campaign_id"]
            campaigns.append(
                CampaignInfo(
                    campaign_id=cid,
                    name=meta["name"],
                    scene=meta["scene"],
                    status=meta["status"],
                    daily_budget_fen=budget_of[cid],
                    bid_fen=meta["bid_fen"],
                    pace=meta["pace"],
                )
            )
            campaign_days[cid] = [
                MetricDay(stat, *vals)
                for stat, vals in sorted(campaign_bucket[cid].items())
            ]

        images: list[ImageInfo] = []
        image_days: dict[str, list[MetricDay]] = {}
        goods_pace = {spec["goods_id"]: pace_of[spec["campaign_id"]] for spec in _GOODS_META}
        for spec in _IMAGE_META:
            images.append(
                ImageInfo(
                    image_id=spec["image_id"],
                    goods_id=spec["goods_id"],
                    label=spec["label"],
                    tone=spec["tone"],
                )
            )
            price = price_of[spec["goods_id"]]
            pace = goods_pace[spec["goods_id"]]
            series = []
            for day in dates:
                factor = _pace_factor(pace, day, today, now)
                impr_s, clicks_s, orders_s, spend_s, gmv_s, _carts = _scale(
                    spec["impr"], spec["clicks"], spec["orders"], spec["spend_fen"], price, factor
                )
                series.append(
                    MetricDay(day.isoformat(), impr_s, clicks_s, spend_s, orders_s, gmv_s, 0)
                )
            image_days[spec["image_id"]] = series

        return SyncPayload(
            shop=SHOP,
            campaigns=campaigns,
            campaign_days=campaign_days,
            goods=goods,
            goods_days=goods_days,
            images=images,
            image_days=image_days,
        )
