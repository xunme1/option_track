from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None

from option_monitor.models import (
    AnomalyChartCard,
    AnomalyChartReport,
    AnomalyMetric,
    ContractOiChange,
)
from option_monitor.settings import SHANGHAI


CHART_WIDTH = 1200
HEADER_HEIGHT = 170
RANKING_HEIGHT = 320
CARD_HEIGHT = 330
CARD_GAP = 18
FOOTER_MARGIN = 32
BACKGROUND_COLOR = (247, 248, 250)
CARD_COLOR = (255, 255, 255)
TEXT_COLOR = (31, 41, 55)
MUTED_COLOR = (107, 114, 128)
GRID_COLOR = (222, 226, 232)
BLUE_COLOR = (37, 99, 235)
PURPLE_COLOR = (147, 51, 234)
POSITIVE_COLOR = (211, 47, 47)
NEGATIVE_COLOR = (0, 137, 123)
WARNING_COLOR = (180, 110, 0)
TRIGGER_BACKGROUNDS = {
    "price": (255, 241, 242),
    "iv": (239, 246, 255),
    "oi": (255, 247, 237),
    "skew": (250, 245, 255),
}
NEUTRAL_BACKGROUND = (246, 247, 249)
FONT_PATHS = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
)


class AnomalyChartError(RuntimeError):
    """A safe-to-log anomaly chart rendering failure."""


def render_anomaly_chart(
    report: AnomalyChartReport, output_path: Path
) -> Path:
    if Image is None or ImageDraw is None or ImageFont is None:
        raise AnomalyChartError("Pillow is unavailable")
    if len(report.cards) > 30:
        raise AnomalyChartError("anomaly chart has too many cards")
    font_path = _font_path()
    if font_path is None:
        raise AnomalyChartError("anomaly chart font is unavailable")

    height = (
        HEADER_HEIGHT
        + RANKING_HEIGHT
        + len(report.cards) * (CARD_HEIGHT + CARD_GAP)
        + FOOTER_MARGIN
    )
    try:
        fonts = _load_fonts(font_path)
        image = Image.new(
            "RGB", (CHART_WIDTH, height), BACKGROUND_COLOR
        )
        draw = ImageDraw.Draw(image)
        _draw_header(draw, report, fonts)
        _draw_rankings(draw, report, fonts, font_path)
        top = HEADER_HEIGHT + RANKING_HEIGHT
        for card in report.cards:
            _draw_card(draw, card, top, fonts, font_path)
            top += CARD_HEIGHT + CARD_GAP
    except (OSError, TypeError, ValueError):
        raise AnomalyChartError("anomaly chart rendering failed") from None

    destination = Path(output_path).resolve()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(temporary, format="PNG", optimize=True)
        with Image.open(temporary) as verification:
            if (
                verification.format != "PNG"
                or verification.width != CHART_WIDTH
                or verification.height != height
            ):
                raise ValueError("invalid rendered image")
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError):
        raise AnomalyChartError("anomaly chart rendering failed") from None
    finally:
        temporary.unlink(missing_ok=True)
        image.close()
    return destination


def _load_fonts(font_path: Path) -> dict[str, object]:
    try:
        return {
            "title": ImageFont.truetype(str(font_path), 40),
            "subtitle": ImageFont.truetype(str(font_path), 19),
            "section": ImageFont.truetype(str(font_path), 25),
            "card_title": ImageFont.truetype(str(font_path), 25),
            "label": ImageFont.truetype(str(font_path), 19),
            "value": ImageFont.truetype(str(font_path), 19),
            "small": ImageFont.truetype(str(font_path), 16),
            "tiny": ImageFont.truetype(str(font_path), 14),
        }
    except (OSError, ValueError):
        raise AnomalyChartError("anomaly chart font is unavailable") from None


def _font_path() -> Path | None:
    override = os.environ.get("OPTION_MONITOR_FONT_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return next((path for path in FONT_PATHS if path.is_file()), None)


def _draw_header(draw, report, fonts) -> None:
    report_time = datetime.fromtimestamp(
        report.run_at_ms / 1000, tz=SHANGHAI
    )
    draw.text(
        (42, 25),
        "期权异常监控",
        fill=BLUE_COLOR,
        font=fonts["title"],
    )
    draw.text(
        (44, 84),
        f"快照时间  {report_time:%Y-%m-%d %H:%M}  |  "
        f"已采集 {report.collected_count}/{report.expected_count}",
        fill=TEXT_COLOR,
        font=fonts["subtitle"],
    )
    draw.text(
        (44, 118),
        "数据源  期货日内：米筐优先，东方财富兜底  |  期权：Orange Hitick",
        fill=MUTED_COLOR,
        font=fonts["subtitle"],
    )
    draw.line(
        (42, HEADER_HEIGHT - 16, CHART_WIDTH - 42, HEADER_HEIGHT - 16),
        fill=GRID_COLOR,
        width=2,
    )


def _draw_rankings(draw, report, fonts, font_path: Path) -> None:
    top = HEADER_HEIGHT
    panel_height = RANKING_HEIGHT - 18
    left = (42, top, 584, top + panel_height)
    right = (616, top, 1158, top + panel_height)
    _draw_ranking_panel(
        draw,
        left,
        "增仓额 Top 5（资金流近似值）",
        report.top_capital_increases,
        "本轮无有效增仓资金流",
        fonts,
        font_path,
        value_mode="capital_flow",
    )
    _draw_ranking_panel(
        draw,
        right,
        "减仓额 Top 5（资金流近似值）",
        report.top_capital_decreases,
        "本轮无有效减仓资金流",
        fonts,
        font_path,
        value_mode="capital_flow",
    )


def _draw_ranking_panel(
    draw,
    box: tuple[int, int, int, int],
    title: str,
    rows: tuple[ContractOiChange, ...],
    empty_text: str,
    fonts,
    font_path: Path,
    value_mode: str = "quantity",
) -> None:
    draw.rounded_rectangle(
        box, radius=8, fill=CARD_COLOR, outline=GRID_COLOR, width=1
    )
    x1, y1, x2, _ = box
    draw.text((x1 + 18, y1 + 14), title, fill=TEXT_COLOR, font=fonts["section"])
    column_header = (
        "排名 / 合约 / C-P / 资金流近似值 / 变化"
        if value_mode == "capital_flow"
        else "排名 / 合约 / C-P / 当前 / 昨持仓 / 变化"
    )
    draw.text(
        (x1 + 18, y1 + 54),
        column_header,
        fill=MUTED_COLOR,
        font=fonts["tiny"],
    )
    if not rows:
        draw.text(
            (x1 + 18, y1 + 110),
            empty_text,
            fill=MUTED_COLOR,
            font=fonts["label"],
        )
        return
    for index, row in enumerate(rows[:5], start=1):
        row_y = y1 + 88 + (index - 1) * 38
        draw.line(
            (x1 + 16, row_y - 7, x2 - 16, row_y - 7),
            fill=(238, 240, 243),
            width=1,
        )
        color = (
            POSITIVE_COLOR
            if row.delta_open_interest > 0 else NEGATIVE_COLOR
        )
        prefix = f"{index}.  {row.symbol}  [{row.side}]"
        _draw_fitted_text(
            draw,
            (x1 + 18, row_y),
            prefix,
            TEXT_COLOR,
            font_path,
            max_width=285,
            max_size=16,
            min_size=11,
        )
        if value_mode == "capital_flow":
            amount = row.oi_capital_flow
            values = (
                "数据不足" if amount is None
                else f"{_compact_money(abs(amount))} / "
                f"{row.delta_open_interest:+d} 张"
            )
        else:
            values = (
                f"{row.open_interest} / {row.pre_open_interest} / "
                f"{row.delta_open_interest:+d} 张"
            )
        _draw_right_text(
            draw,
            values,
            x2 - 18,
            row_y,
            fonts["small"],
            color,
        )


def _draw_card(
    draw,
    card: AnomalyChartCard,
    top: int,
    fonts,
    font_path: Path,
) -> None:
    box = (42, top, CHART_WIDTH - 42, top + CARD_HEIGHT)
    draw.rounded_rectangle(
        box, radius=8, fill=CARD_COLOR, outline=GRID_COLOR, width=1
    )
    title = f"{card.product_name} ({card.product_code})  {card.underlying}"
    _draw_fitted_text(
        draw,
        (60, top + 16),
        title,
        TEXT_COLOR,
        font_path,
        max_width=760,
        max_size=25,
        min_size=16,
    )
    severity_text = "重要警报" if card.severity == "important" else "一般预警"
    severity_color = (
        POSITIVE_COLOR if card.severity == "important" else WARNING_COLOR
    )
    _draw_right_text(
        draw,
        severity_text,
        CHART_WIDTH - 60,
        top + 19,
        fonts["label"],
        severity_color,
    )

    left = 60
    gap = 12
    widths = (258, 258, 278, 266)
    boxes = []
    for width in widths:
        boxes.append((left, top + 62, left + width, top + 215))
        left += width + gap
    _draw_price_metric(draw, boxes[0], card, fonts)
    _draw_iv_metric(draw, boxes[1], card.atm_iv, fonts)
    _draw_oi_metric(draw, boxes[2], card, fonts)
    _draw_skew_metric(draw, boxes[3], card.rr25, fonts)

    category_names = {
        "price": "价格",
        "iv": "ATM IV",
        "oi": "持仓",
        "skew": "偏度",
    }
    trigger_text = "触发：" + " / ".join(
        category_names[item] for item in card.trigger_categories
    )
    draw.text(
        (60, top + 234),
        trigger_text,
        fill=severity_color,
        font=fonts["small"],
    )
    _draw_fitted_text(
        draw,
        (60, top + 266),
        f"配合判断：{card.evidence}",
        TEXT_COLOR,
        font_path,
        max_width=1080,
        max_size=16,
        min_size=12,
    )


def _compact_money(value: Decimal) -> str:
    if value >= Decimal("100000000"):
        return f"{value / Decimal('100000000'):,.2f} 亿"
    if value >= Decimal("10000"):
        return f"{value / Decimal('10000'):,.2f} 万"
    return f"{value:,.2f} 元"


def _metric_box(draw, box, label, active, color, fonts) -> None:
    fill = TRIGGER_BACKGROUNDS[label] if active else NEUTRAL_BACKGROUND
    draw.rounded_rectangle(box, radius=6, fill=fill)
    labels = {
        "price": "期货价格",
        "iv": "ATM IV",
        "oi": "Call / Put 持仓",
        "skew": "RR25 偏度",
    }
    draw.text(
        (box[0] + 14, box[1] + 12),
        labels[label],
        fill=color if active else MUTED_COLOR,
        font=fonts["label"],
    )


def _draw_price_metric(draw, box, card, fonts) -> None:
    _metric_box(
        draw, box, "price", card.price_triggered, POSITIVE_COLOR, fonts
    )
    x, y = box[0] + 14, box[1] + 53
    if card.futures_price is None or card.futures_change_percent is None:
        draw.text((x, y), "数据不足", fill=MUTED_COLOR, font=fonts["value"])
        return
    change = card.futures_change_percent
    color = _direction_color(change)
    draw.text(
        (x, y), f"现价  {card.futures_price}", fill=TEXT_COLOR, font=fonts["value"]
    )
    draw.text(
        (x, y + 36),
        f"日内涨跌  {change * Decimal('100'):+.2f}%  {_direction_word(change)}",
        fill=color,
        font=fonts["value"],
    )
    draw.text(
        (x, y + 73),
        "阈值  |日内涨跌幅| > 2.50%",
        fill=MUTED_COLOR,
        font=fonts["small"],
    )


def _draw_iv_metric(draw, box, metric: AnomalyMetric, fonts) -> None:
    _metric_box(draw, box, "iv", metric.triggered, BLUE_COLOR, fonts)
    x, y = box[0] + 14, box[1] + 53
    if not metric.available or metric.current is None:
        draw.text((x, y), "数据不足", fill=MUTED_COLOR, font=fonts["value"])
        return
    draw.text(
        (x, y),
        f"当前  {metric.current * Decimal('100'):.2f}%",
        fill=TEXT_COLOR,
        font=fonts["value"],
    )
    change_text = "--" if metric.change is None else (
        f"{metric.change * Decimal('100'):+.2f} pp"
    )
    change_color = (
        MUTED_COLOR if metric.change is None
        else _direction_color(metric.change)
    )
    draw.text(
        (x, y + 36),
        f"ΔIV  {change_text}",
        fill=change_color,
        font=fonts["value"],
    )
    rank = "--" if metric.rank is None else str(metric.rank)
    mean = "--" if metric.history_mean is None else (
        f"{metric.history_mean * Decimal('100'):.2f}%"
    )
    draw.text(
        (x, y + 73),
        f"十日排名 {rank}  均值 {mean}",
        fill=MUTED_COLOR,
        font=fonts["small"],
    )


def _draw_oi_metric(draw, box, card, fonts) -> None:
    _metric_box(
        draw,
        box,
        "oi",
        "oi" in card.trigger_categories,
        WARNING_COLOR,
        fonts,
    )
    x, y = box[0] + 14, box[1] + 53
    call_text = (
        "基线不足" if card.call_oi_delta is None
        else f"{card.call_oi_delta:+d} 张 {_oi_word(card.call_oi_delta)}"
    )
    put_text = (
        "基线不足" if card.put_oi_delta is None
        else f"{card.put_oi_delta:+d} 张 {_oi_word(card.put_oi_delta)}"
    )
    draw.text(
        (x, y),
        f"Call  {call_text}",
        fill=(
            MUTED_COLOR if card.call_oi_delta is None
            else _direction_color(Decimal(card.call_oi_delta))
        ),
        font=fonts["value"],
    )
    draw.text(
        (x, y + 36),
        f"Put   {put_text}",
        fill=(
            MUTED_COLOR if card.put_oi_delta is None
            else _direction_color(Decimal(card.put_oi_delta))
        ),
        font=fonts["value"],
    )
def _draw_skew_metric(draw, box, metric: AnomalyMetric, fonts) -> None:
    _metric_box(draw, box, "skew", metric.triggered, PURPLE_COLOR, fonts)
    x, y = box[0] + 14, box[1] + 53
    if not metric.available or metric.current is None:
        draw.text((x, y), "数据不足", fill=MUTED_COLOR, font=fonts["value"])
        return
    draw.text(
        (x, y),
        f"当前  {metric.current * Decimal('100'):+.2f} pp",
        fill=TEXT_COLOR,
        font=fonts["value"],
    )
    change_text = "--" if metric.change is None else (
        f"{metric.change * Decimal('100'):+.2f} pp"
    )
    draw.text(
        (x, y + 36),
        f"ΔRR25  {change_text}",
        fill=(
            MUTED_COLOR if metric.change is None
            else _direction_color(metric.change)
        ),
        font=fonts["value"],
    )
    rank = "--" if metric.rank is None else str(metric.rank)
    mean = "--" if metric.history_mean is None else (
        f"{metric.history_mean * Decimal('100'):.2f} pp"
    )
    draw.text(
        (x, y + 73),
        f"变化排名 {rank}  均值 {mean}",
        fill=MUTED_COLOR,
        font=fonts["small"],
    )


def _draw_fitted_text(
    draw,
    position: tuple[int, int],
    text: str,
    color: tuple[int, int, int],
    font_path: Path,
    *,
    max_width: int,
    max_size: int,
    min_size: int,
) -> None:
    font = _fit_font(
        draw, text, font_path, max_width, max_size, min_size
    )
    draw.text(position, text, fill=color, font=font)


def _fit_font(draw, text, font_path, max_width, max_size, min_size):
    for size in range(max_size, min_size - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        bounds = draw.textbbox((0, 0), text, font=font)
        if bounds[2] - bounds[0] <= max_width:
            return font
    font = ImageFont.truetype(str(font_path), min_size)
    bounds = draw.textbbox((0, 0), text, font=font)
    if bounds[2] - bounds[0] > max_width:
        raise ValueError("text does not fit chart")
    return font


def _draw_right_text(draw, text, right, y, font, color) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (right - (bounds[2] - bounds[0]), y),
        text,
        fill=color,
        font=font,
    )


def _direction_color(value: Decimal) -> tuple[int, int, int]:
    if value > 0:
        return POSITIVE_COLOR
    if value < 0:
        return NEGATIVE_COLOR
    return MUTED_COLOR


def _direction_word(value: Decimal) -> str:
    if value > 0:
        return "上涨"
    if value < 0:
        return "下跌"
    return "持平"


def _oi_word(value: int) -> str:
    if value > 0:
        return "增仓"
    if value < 0:
        return "减仓"
    return "持平"
