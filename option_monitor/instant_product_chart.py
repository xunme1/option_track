from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None

from option_monitor.collector import ProductCollection
from option_monitor.models import ContractMapping, FuturesChangeQuote, ProductSpec
from option_monitor.settings import SHANGHAI


WIDTH = 1200
HEIGHT = 500
BACKGROUND = (247, 248, 250)
PANEL = (255, 255, 255)
MUTED_PANEL = (246, 247, 249)
PRICE_PANEL = (255, 241, 242)
IV_PANEL = (239, 246, 255)
OI_PANEL = (255, 247, 237)
PCR_PANEL = (240, 253, 250)
VPCR_PANEL = (254, 252, 232)
SKEW_PANEL = (250, 245, 255)
TEXT = (31, 41, 55)
MUTED = (107, 114, 128)
GRID = (222, 226, 232)
BLUE = (37, 99, 235)
PURPLE = (147, 51, 234)
TEAL = (13, 148, 136)
RED = (211, 47, 47)
GREEN = (0, 137, 123)
ORANGE = (180, 110, 0)
FONT_PATHS = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
)


class InstantProductChartError(RuntimeError):
    """A safe-to-log instant product chart rendering failure."""


@dataclass(frozen=True)
class InstantProductChartData:
    product: ProductSpec
    mapping: ContractMapping
    collection: ProductCollection
    futures_quote: FuturesChangeQuote | None
    rendered_at_ms: int
    rr25_change: Decimal | None = None
    rr25_baseline_trading_day: str | None = None
    iv_change: Decimal | None = None
    iv_baseline_trading_day: str | None = None


def render_instant_product_chart(
    data: InstantProductChartData, output_path: Path
) -> Path:
    """Render one product's current price, IV, OI, PCR, and RR25 into a PNG."""
    if Image is None or ImageDraw is None or ImageFont is None:
        raise InstantProductChartError("Pillow is unavailable")
    font_path = _font_path()
    if font_path is None:
        raise InstantProductChartError("instant product chart font is unavailable")

    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.casefold() != ".png":
        raise InstantProductChartError("instant product chart output must be a PNG")
    temporary = destination.with_suffix(".png.tmp")
    image = None
    try:
        fonts = _load_fonts(font_path)
        image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
        draw = ImageDraw.Draw(image)
        _draw_header(draw, data, fonts)
        _draw_metrics(draw, data, fonts)
        _draw_footer(draw, data, fonts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(temporary, format="PNG", optimize=True)
        with Image.open(temporary) as verification:
            if verification.format != "PNG" or verification.size != (WIDTH, HEIGHT):
                raise ValueError("invalid rendered image")
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError):
        raise InstantProductChartError("instant product chart rendering failed") from None
    finally:
        temporary.unlink(missing_ok=True)
        if image is not None:
            image.close()
    return destination


def _font_path() -> Path | None:
    override = os.environ.get("OPTION_MONITOR_FONT_PATH", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    for path in FONT_PATHS:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _load_fonts(font_path: Path) -> dict[str, object]:
    try:
        return {
            "title": ImageFont.truetype(str(font_path), 40),
            "subtitle": ImageFont.truetype(str(font_path), 19),
            "heading": ImageFont.truetype(str(font_path), 25),
            "value": ImageFont.truetype(str(font_path), 25),
            "body": ImageFont.truetype(str(font_path), 19),
            "small": ImageFont.truetype(str(font_path), 16),
        }
    except (OSError, ValueError):
        raise InstantProductChartError("instant product chart font is unavailable") from None


def _draw_header(draw, data: InstantProductChartData, fonts) -> None:
    timestamp = datetime.fromtimestamp(data.rendered_at_ms / 1000, tz=SHANGHAI)
    draw.text((42, 28), "期权即时信息", fill=BLUE, font=fonts["title"])
    draw.text(
        (44, 88),
        f"{data.product.name} ({data.product.code})  {data.mapping.underlying}",
        fill=TEXT,
        font=fonts["heading"],
    )
    draw.text(
        (44, 124),
        f"采集时间  {timestamp:%Y-%m-%d %H:%M:%S}  |  到期日 {data.mapping.expire}",
        fill=MUTED,
        font=fonts["subtitle"],
    )
    draw.line((42, 165, WIDTH - 42, 165), fill=GRID, width=2)


def _draw_metrics(draw, data: InstantProductChartData, fonts) -> None:
    top = 195
    left = 42
    gap = 16
    width = (WIDTH - 84 - gap * 5) // 6
    boxes = []
    for index in range(6):
        x1 = left + index * (width + gap)
        boxes.append((x1, top, x1 + width, 399))

    _draw_price(draw, boxes[0], data, fonts)
    _draw_iv(draw, boxes[1], data, fonts)
    _draw_oi(draw, boxes[2], data, fonts)
    _draw_pcr(draw, boxes[3], data, fonts)
    _draw_volume_pcr(draw, boxes[4], data, fonts)
    _draw_skew(draw, boxes[5], data, fonts)


def _metric_box(draw, box, label: str, fill, color, fonts) -> tuple[int, int]:
    draw.rounded_rectangle(box, radius=8, fill=fill)
    draw.text((box[0] + 16, box[1] + 16), label, fill=color, font=fonts["body"])
    return box[0] + 16, box[1] + 62


def _draw_price(draw, box, data: InstantProductChartData, fonts) -> None:
    x, y = _metric_box(draw, box, "期货价格", PRICE_PANEL, RED, fonts)
    quote = data.futures_quote
    last_price = quote.last_price if quote is not None else data.collection.market.last_price
    change = quote.change_pct if quote is not None else None
    draw.text((x, y), f"现价  {last_price}", fill=TEXT, font=fonts["value"])
    if change is None:
        draw.text((x, y + 45), "日内涨跌  数据暂不可用", fill=MUTED, font=fonts["body"])
        draw.text((x, y + 84), "计算： (最新价 - 开盘价) / 开盘价", fill=MUTED, font=fonts["small"])
        return
    draw.text(
        (x, y + 45),
        f"日内  {change * Decimal('100'):+.2f}%",
        fill=_direction_color(change),
        font=fonts["body"],
    )
    source = "RQData" if quote.data_source == "rqdata" else "东方财富"
    draw.text((x, y + 84), f"数据源  {source}", fill=MUTED, font=fonts["small"])


def _draw_iv(draw, box, data: InstantProductChartData, fonts) -> None:
    x, y = _metric_box(draw, box, "ATM IV", IV_PANEL, BLUE, fonts)
    iv = data.collection.market.atm_iv
    draw.text(
        (x, y), f"当前  {iv * Decimal('100'):.2f}%", fill=TEXT, font=fonts["value"]
    )
    change = data.iv_change
    change_text = "——" if change is None else f"{change * Decimal('100'):+.2f} pp"
    draw.text(
        (x, y + 45),
        f"ΔIV  {change_text}",
        fill=MUTED if change is None else _direction_color(change),
        font=fonts["body"],
    )
    baseline_text = (
        f"基线  {data.iv_baseline_trading_day} 收盘"
        if data.iv_baseline_trading_day is not None
        else "基线  暂无该合约收盘"
    )
    draw.text((x, y + 84), baseline_text, fill=MUTED, font=fonts["small"])


def _draw_oi(draw, box, data: InstantProductChartData, fonts) -> None:
    x, y = _metric_box(draw, box, "Call / Put 持仓", OI_PANEL, ORANGE, fonts)
    option = data.collection.option_snapshot
    if option is None:
        draw.text((x, y), "数据暂不可用", fill=MUTED, font=fonts["body"])
        return
    call_delta = _oi_delta(option.call_open_interest, option.call_pre_open_interest, option.call_oi_baseline_ready)
    put_delta = _oi_delta(option.put_open_interest, option.put_pre_open_interest, option.put_oi_baseline_ready)
    draw.text(
        (x, y), f"Call  {_oi_text(call_delta)}", fill=_oi_color(call_delta), font=fonts["body"]
    )
    draw.text(
        (x, y + 45), f"Put   {_oi_text(put_delta)}", fill=_oi_color(put_delta), font=fonts["body"]
    )
    draw.text((x, y + 84), "较昨持仓变化", fill=MUTED, font=fonts["small"])


def _draw_pcr(draw, box, data: InstantProductChartData, fonts) -> None:
    x, y = _metric_box(draw, box, "OI PCR", PCR_PANEL, TEAL, fonts)
    option = data.collection.option_snapshot
    current = (
        option.oi_pcr
        if option is not None and option.call_open_interest > 0
        else None
    )
    previous = (
        Decimal(option.put_pre_open_interest)
        / Decimal(option.call_pre_open_interest)
        if option is not None
        and option.call_oi_baseline_ready
        and option.put_oi_baseline_ready
        and option.call_pre_open_interest > 0
        else None
    )
    change = (
        current / previous - Decimal("1")
        if current is not None and previous is not None and previous > 0
        else None
    )
    current_text = "--" if current is None else f"{current:.2f}"
    previous_text = "--" if previous is None else f"{previous:.2f}"
    draw.text((x, y), f"当前  {current_text}", fill=TEXT, font=fonts["value"])
    draw.text((x, y + 45), f"昨收  {previous_text}", fill=MUTED, font=fonts["body"])
    change_text = "--" if change is None else f"{change * Decimal('100'):+.2f}%"
    draw.text(
        (x, y + 84),
        f"变化  {change_text}",
        fill=MUTED,
        font=fonts["small"],
    )


def _draw_volume_pcr(draw, box, data: InstantProductChartData, fonts) -> None:
    x, y = _metric_box(draw, box, "Volume PCR", VPCR_PANEL, ORANGE, fonts)
    option = data.collection.option_snapshot
    value = option.session_volume_pcr if option is not None else None
    current_text = "--" if value is None else f"{value:.2f}"
    draw.text((x, y), f"当前  {current_text}", fill=TEXT, font=fonts["value"])
    if value is None:
        bias = "数据暂不可用"
    elif value >= Decimal("1.25"):
        bias = "put 成交活跃"
    elif value <= Decimal("0.8"):
        bias = "call 成交活跃"
    else:
        bias = "成交相对均衡"
    draw.text((x, y + 45), bias, fill=MUTED, font=fonts["body"])
    draw.text((x, y + 84), "当日累计 Put÷Call", fill=MUTED, font=fonts["small"])


def _draw_skew(draw, box, data: InstantProductChartData, fonts) -> None:
    x, y = _metric_box(draw, box, "RR25 偏度", SKEW_PANEL, PURPLE, fonts)
    option = data.collection.option_snapshot
    if option is None or option.rr25 is None:
        draw.text((x, y), "当前  数据暂不可用", fill=MUTED, font=fonts["body"])
    else:
        draw.text(
            (x, y),
            f"当前  {option.rr25 * Decimal('100'):+.2f} pp",
            fill=TEXT,
            font=fonts["value"],
        )
    change = data.rr25_change
    change_text = "——" if change is None else f"{change * Decimal('100'):+.2f} pp"
    draw.text(
        (x, y + 45),
        f"ΔRR25  {change_text}",
        fill=MUTED if change is None else _direction_color(change),
        font=fonts["body"],
    )
    baseline_text = (
        f"基线  {data.rr25_baseline_trading_day} 收盘"
        if data.rr25_baseline_trading_day is not None
        else "基线  暂无该合约收盘"
    )
    draw.text((x, y + 84), baseline_text, fill=MUTED, font=fonts["small"])


def _draw_footer(draw, data: InstantProductChartData, fonts) -> None:
    market_time = datetime.fromtimestamp(
        data.collection.market.data_time_ms / 1000, tz=SHANGHAI
    )
    draw.text(
        (44, 430),
        f"期权快照时间  {market_time:%Y-%m-%d %H:%M:%S}  |  ΔIV/ΔRR25 基线=同合约昨收  |  OI PCR = Put / Call 总持仓（昨收=昨持仓比）  |  Volume PCR = Put / Call 当日累计成交量",
        fill=MUTED,
        font=fonts["small"],
    )


def _oi_delta(current: int, previous: int, ready: bool) -> int | None:
    return current - previous if ready else None


def _oi_text(value: int | None) -> str:
    if value is None:
        return "基线不足"
    return f"{value:+d} 张"


def _oi_color(value: int | None) -> tuple[int, int, int]:
    return MUTED if value is None else _direction_color(Decimal(value))


def _direction_color(value: Decimal) -> tuple[int, int, int]:
    return RED if value > 0 else GREEN if value < 0 else MUTED
