"""本机配置。不存放店铺密钥或 Cookie。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "推广台"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
OFFICIAL_LOGIN_URL = "https://mms.pinduoduo.com/login/"
OFFICIAL_PROMO_URL = "https://yingxiao.pinduoduo.com/mains/promotionOverview"
QR_TTL_SECONDS = 120
LIVE_LOGIN_SECONDS = 300
SESSION_DAYS = 7
HISTORY_DAYS = 15
RULE_DAYS = 7

# 分组顺序即设置页展示顺序。
SETTING_FIELDS: list[dict] = [
    {
        "key": "data_mode",
        "group": "数据来源",
        "label": "数据模式",
        "type": "enum",
        "choices": [
            {"value": "live", "label": "真实店铺"},
            {"value": "mock", "label": "模拟店铺"},
        ],
        "help": "真实店铺会在本机弹出浏览器，打开拼多多商家后台扫码。Cookie 只留在这台电脑。模拟店铺不联网，用来演示。",
    },
    {
        "key": "refresh_interval_minutes",
        "group": "数据来源",
        "label": "自动同步间隔（分钟）",
        "type": "int",
        "min": 0,
        "max": 240,
        "help": "0 表示只在你点击同步时更新。真实店铺自动同步会弹出浏览器，间隔建议设为 0，需要时再手动同步。",
    },
    {
        "key": "target_roi",
        "group": "目标",
        "label": "目标 ROI",
        "type": "float",
        "min": 0.2,
        "max": 30,
        "help": "低于该值的计划会建议降价或收预算。ROI = 成交额 / 花费。",
    },
    {
        "key": "pause_roi",
        "group": "目标",
        "label": "暂停 ROI",
        "type": "float",
        "min": 0,
        "max": 30,
        "help": "近 7 个完整日低于该值，且花费达到阈值，建议暂停。必须小于目标 ROI。",
    },
    {
        "key": "scale_roi",
        "group": "目标",
        "label": "放量 ROI",
        "type": "float",
        "min": 0.2,
        "max": 30,
        "help": "达到该值且订单、预算使用率够了，建议上调日预算。不能小于目标 ROI。",
    },
    {
        "key": "pause_min_spend_yuan",
        "group": "测款",
        "label": "暂停最少花费（7日，元）",
        "type": "float",
        "min": 1,
        "max": 1_000_000,
        "help": "花费太少时不因为单日 ROI 波动就建议暂停。",
    },
    {
        "key": "goods_min_impressions",
        "group": "测款",
        "label": "测款最低累计曝光（7日）",
        "type": "int",
        "min": 100,
        "max": 5_000_000,
        "help": "曝光不够且花费不高时，建议继续观察，不要过早否定新品。",
    },
    {
        "key": "goods_min_orders",
        "group": "测款",
        "label": "测款最少订单（7日）",
        "type": "int",
        "min": 1,
        "max": 100_000,
        "help": "订单低于该值且花费不高，视为样本不足。",
    },
    {
        "key": "scale_min_orders",
        "group": "测款",
        "label": "放量最少订单（7日）",
        "type": "int",
        "min": 1,
        "max": 100_000,
        "help": "订单不够不建议直接放大预算。",
    },
    {
        "key": "scale_min_budget_util",
        "group": "测款",
        "label": "放量最低预算使用率",
        "type": "float",
        "min": 0.1,
        "max": 1,
        "help": "预算还花不完时，优先检查出价，而不是继续加预算。",
    },
    {
        "key": "mild_min_budget_util",
        "group": "测款",
        "label": "小幅上调最低预算使用率",
        "type": "float",
        "min": 0.1,
        "max": 1,
        "help": "ROI 达标、订单在涨、预算吃得比较满时，建议小幅加预算。",
    },
    {
        "key": "budget_up_ratio",
        "group": "测款",
        "label": "放量上调幅度",
        "type": "float",
        "min": 0.05,
        "max": 1,
        "help": "0.30 表示在现有日预算上加 30%。",
    },
    {
        "key": "budget_mild_ratio",
        "group": "测款",
        "label": "小幅上调幅度",
        "type": "float",
        "min": 0.05,
        "max": 1,
        "help": "0.10 表示加 10%。",
    },
    {
        "key": "high_ctr",
        "group": "测款",
        "label": "高点击率门槛",
        "type": "float",
        "min": 0.005,
        "max": 0.5,
        "help": "点击率高于该值且转化率很低时，先改详情、价格或评价，不加预算。",
    },
    {
        "key": "low_cvr",
        "group": "测款",
        "label": "低转化率门槛",
        "type": "float",
        "min": 0.001,
        "max": 0.5,
        "help": "与高点击率搭配使用。转化率 = 订单 / 点击。",
    },
    {
        "key": "observe_budget_ratio",
        "group": "测款",
        "label": "冷启动花费上限（相对预算）",
        "type": "float",
        "min": 0.05,
        "max": 1,
        "help": "样本不足但花费已超过「日预算 × 天数 × 该比例」时，不再建议干等。",
    },
    {
        "key": "image_min_impressions",
        "group": "测图",
        "label": "测图最低累计曝光（7日，单张）",
        "type": "int",
        "min": 100,
        "max": 5_000_000,
        "help": "两张图都达到该曝光后才比较点击率。",
    },
    {
        "key": "image_ctr_gap",
        "group": "测图",
        "label": "胜出点击率倍数",
        "type": "float",
        "min": 1.05,
        "max": 5,
        "help": "1.25 表示最优图点击率至少是次优图的 1.25 倍，才建议换主图。",
    },
    {
        "key": "min_ctr_image",
        "group": "测图",
        "label": "创意重做点击率",
        "type": "float",
        "min": 0.001,
        "max": 0.2,
        "help": "样本够了但最好的图仍低于该点击率，建议重做主图。",
    },
    {
        "key": "campaign_raise_util",
        "group": "推广计划",
        "label": "计划加预算使用率",
        "type": "float",
        "min": 0.3,
        "max": 1,
        "help": "计划 ROI 达到放量线且使用率超过该值时，建议上调计划日预算。",
    },
    {
        "key": "pacing_fast_projected",
        "group": "预算节奏",
        "label": "消耗过快（预计全天 / 预算）",
        "type": "float",
        "min": 1.05,
        "max": 3,
        "help": "按当前时刻线性外推，预计全天花费超过日预算的该倍数则提示过快。仅看今天。",
    },
    {
        "key": "pacing_slow_projected",
        "group": "预算节奏",
        "label": "消耗偏慢（预计全天 / 预算）",
        "type": "float",
        "min": 0.05,
        "max": 0.95,
        "help": "14 点之后，ROI 仍达标但预计全天花费低于该比例，建议小幅提价。",
    },
]

DEFAULT_SETTINGS: dict[str, str] = {
    "data_mode": "live",
    "refresh_interval_minutes": "30",
    "target_roi": "2",
    "pause_roi": "1",
    "scale_roi": "3",
    "pause_min_spend_yuan": "200",
    "goods_min_impressions": "3000",
    "goods_min_orders": "5",
    "scale_min_orders": "30",
    "scale_min_budget_util": "0.75",
    "mild_min_budget_util": "0.60",
    "budget_up_ratio": "0.30",
    "budget_mild_ratio": "0.10",
    "high_ctr": "0.03",
    "low_cvr": "0.02",
    "observe_budget_ratio": "0.5",
    "image_min_impressions": "3000",
    "image_ctr_gap": "1.25",
    "min_ctr_image": "0.015",
    "campaign_raise_util": "0.90",
    "pacing_fast_projected": "1.35",
    "pacing_slow_projected": "0.60",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    override = os.environ.get("PDD_MONITOR_DB")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "pdd-promo-monitor" / "monitor.sqlite"


def default_port() -> int:
    raw = os.environ.get("PDD_MONITOR_PORT")
    if not raw:
        return DEFAULT_PORT
    return int(raw)
