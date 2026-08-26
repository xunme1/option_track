from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from option_monitor.config import load_env_file
from option_monitor.models import ProductSpec


PRODUCTS = (
    ProductSpec("IO", "沪深300", "CFFEX"),
    ProductSpec("MO", "中证1000", "CFFEX"),
    ProductSpec("HO", "上证50", "CFFEX"),
    ProductSpec("au", "黄金", "SHFE"),
    ProductSpec("ag", "白银", "SHFE"),
    ProductSpec("cu", "铜", "SHFE"),
    ProductSpec("al", "铝", "SHFE"),
    ProductSpec("zn", "锌", "SHFE"),
    ProductSpec("ni", "镍", "SHFE"),
    ProductSpec("rb", "螺纹钢", "SHFE"),
    ProductSpec("ru", "天然橡胶", "SHFE"),
    ProductSpec("sc", "原油", "INE"),
    ProductSpec("i", "铁矿石", "DCE"),
    ProductSpec("jm", "焦煤", "DCE"),
    ProductSpec("jd", "鸡蛋", "DCE"),
    ProductSpec("m", "豆粕", "DCE"),
    ProductSpec("p", "棕榈油", "DCE"),
    ProductSpec("c", "玉米", "DCE"),
    ProductSpec("y", "豆油", "DCE"),
    ProductSpec("pp", "聚丙烯", "DCE"),
    ProductSpec("l", "聚乙烯", "DCE"),
    ProductSpec("v", "PVC", "DCE"),
    ProductSpec("eg", "乙二醇", "DCE"),
    ProductSpec("pg", "液化石油气", "DCE"),
    ProductSpec("TA", "PTA", "CZCE"),
    ProductSpec("MA", "甲醇", "CZCE"),
    ProductSpec("SR", "白糖", "CZCE"),
    ProductSpec("CF", "棉花", "CZCE"),
    ProductSpec("RM", "菜粕", "CZCE"),
    ProductSpec("OI", "菜油", "CZCE"),
    ProductSpec("SA", "纯碱", "CZCE"),
    ProductSpec("SH", "烧碱", "CZCE"),
    ProductSpec("si", "工业硅", "GFEX"),
    ProductSpec("lc", "碳酸锂", "GFEX"),
    ProductSpec("ps", "多晶硅", "GFEX"),
)

NIGHT_TO_0230 = frozenset({"au", "ag", "sc"})
NIGHT_TO_0100 = frozenset({"cu", "al", "zn", "ni"})
NIGHT_TO_2300 = frozenset({
    "rb", "ru", "i", "jm", "m", "p", "c", "y", "pp", "l", "v",
    "eg", "pg", "TA", "MA", "SR", "CF", "RM", "OI", "SA", "SH",
})
CFFEX = frozenset({"IO", "MO", "HO"})
DAY_ONLY = frozenset({"si", "lc", "ps"})
SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class MonitorSettings:
    root: Path
    orange_api_token: str = field(repr=False)
    dingtalk_secret: str = field(repr=False)
    dingtalk_webhook: str = field(repr=False)
    rqdata_api_key: str | None = field(default=None, repr=False)
    imgbb_api_key: str | None = field(default=None, repr=False)
    aliyun_oss_access_key_id: str | None = field(default=None, repr=False)
    aliyun_oss_access_key_secret: str | None = field(default=None, repr=False)
    aliyun_oss_region: str = "cn-guangzhou"
    aliyun_oss_bucket: str = "option-monitor-images"
    aliyun_oss_endpoint: str = "https://oss-cn-guangzhou.aliyuncs.com"
    aliyun_oss_prefix: str = "option-monitor/charts"
    price_alert_threshold: Decimal = Decimal("0.025")
    flow_general_threshold: Decimal = Decimal("10000000")
    flow_important_threshold: Decimal = Decimal("30000000")
    iv_mean_multiplier: Decimal = Decimal("1.1")
    skew_mean_multiplier: Decimal = Decimal("1.5")
    fresh_window_ms: int = 15 * 60 * 1000
    price_quote_max_age_ms: int = 5 * 60 * 1000
    cffex_price_quote_max_age_ms: int = 20 * 60 * 1000
    coverage_warning_ratio: Decimal = Decimal("0.8")
    retention_days: int = 90
    max_workers: int = 1
    max_collection_passes: int = 6

    @property
    def aliyun_oss_configured(self) -> bool:
        return bool(
            self.aliyun_oss_access_key_id
            and self.aliyun_oss_access_key_secret
        )

    @property
    def database_path(self) -> Path:
        return self.root / "state" / "option_monitor.sqlite3"

    @property
    def outbox_root(self) -> Path:
        return self.root / "state" / "outbox"

    @property
    def openvlab_profile_dir(self) -> Path:
        return self.root / "state" / "openvlab-browser-profile"

    @property
    def delivery_state_path(self) -> Path:
        return self.root / "state" / "dingtalk_delivery_state.json"


def load_monitor_settings(root: Path) -> MonitorSettings:
    load_env_file(root / ".env")
    return MonitorSettings(
        root=root,
        orange_api_token=os.environ["ORANGE_API_TOKEN"],
        rqdata_api_key=os.environ.get("RQDATA_API_KEY") or None,
        dingtalk_webhook=os.environ["DINGTALK_WEBHOOK"],
        dingtalk_secret=os.environ["DINGTALK_SECRET"],
        imgbb_api_key=os.environ.get("IMGBB_API_KEY") or None,
        aliyun_oss_access_key_id=(
            os.environ.get("ALIYUN_OSS_ACCESS_KEY_ID") or None
        ),
        aliyun_oss_access_key_secret=(
            os.environ.get("ALIYUN_OSS_ACCESS_KEY_SECRET") or None
        ),
        aliyun_oss_region=(
            os.environ.get("ALIYUN_OSS_REGION") or "cn-guangzhou"
        ),
        aliyun_oss_bucket=(
            os.environ.get("ALIYUN_OSS_BUCKET") or "option-monitor-images"
        ),
        aliyun_oss_endpoint=(
            os.environ.get("ALIYUN_OSS_ENDPOINT")
            or "https://oss-cn-guangzhou.aliyuncs.com"
        ),
        aliyun_oss_prefix=(
            os.environ.get("ALIYUN_OSS_PREFIX") or "option-monitor/charts"
        ),
    )


def expected_open_products(now: datetime) -> tuple[str, ...]:
    if now.tzinfo is not None and now.utcoffset() is not None:
        now = now.astimezone(SHANGHAI)

    current_time = now.timetz().replace(tzinfo=None)
    codes: set[str] = set()

    if now.weekday() < 5:
        if _in_window(current_time, time(9, 30), time(11, 30)) or _in_window(
            current_time, time(13), time(15)
        ):
            codes.update(CFFEX)
        if _in_window(current_time, time(9), time(10, 15)) or _in_window(
            current_time, time(10, 30), time(11, 30)
        ) or _in_window(current_time, time(13, 30), time(15)):
            codes.update(code for code in (product.code for product in PRODUCTS) if code not in CFFEX)
        if _in_window(current_time, time(21), time(23)):
            codes.update(NIGHT_TO_0100 | NIGHT_TO_0230 | NIGHT_TO_2300)
        if current_time >= time(23):
            codes.update(NIGHT_TO_0100 | NIGHT_TO_0230)

    if 1 <= now.weekday() <= 5:
        if _in_window(current_time, time(0), time(1)):
            codes.update(NIGHT_TO_0100 | NIGHT_TO_0230)
        if _in_window(current_time, time(1), time(2, 30)):
            codes.update(NIGHT_TO_0230)

    return tuple(product.code for product in PRODUCTS if product.code in codes)


def _in_window(value: time, start: time, end: time) -> bool:
    return start <= value <= end
