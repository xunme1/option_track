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

from option_monitor.models import HourlyReport
from option_monitor.settings import SHANGHAI


CHART_WIDTH = 1200
CHART_HEIGHT = 1500
BACKGROUND_COLOR = (255, 255, 255)
TEXT_COLOR = (31, 41, 55)
GRID_COLOR = (220, 225, 230)
POSITIVE_COLOR = (211, 47, 47)
NEGATIVE_COLOR = (0, 137, 123)
LEVEL_COLOR = (41, 98, 255)
FONT_PATHS = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
)


class IvChartError(RuntimeError):
    """A safe-to-log IV chart rendering failure."""


def render_iv_chart(report: HourlyReport, output_path: Path) -> Path:
    if Image is None or ImageDraw is None or ImageFont is None:
        raise IvChartError("Pillow is unavailable")
    if (
        not report.iv_change_chart_entries
        and not report.iv_level_chart_entries
    ):
        raise IvChartError("IV chart data is unavailable")

    font_path = _font_path()
    if font_path is None:
        raise IvChartError("IV chart font is unavailable")

    try:
        fonts = _load_fonts(font_path)
        image = Image.new(
            "RGB", (CHART_WIDTH, CHART_HEIGHT), BACKGROUND_COLOR
        )
        draw = ImageDraw.Draw(image)
        _draw_header(draw, report, fonts)
        _draw_change_panel(
            draw,
            report.iv_change_chart_entries,
            fonts,
            top=175,
            bottom=805,
        )
        _draw_level_panel(
            draw,
            report.iv_level_chart_entries,
            fonts,
            top=840,
            bottom=1470,
        )
    except (OSError, ValueError):
        raise IvChartError("IV chart rendering failed") from None

    destination = Path(output_path).resolve()
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, destination)
    except (OSError, ValueError):
        raise IvChartError("IV chart rendering failed") from None
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _load_fonts(font_path: Path) -> dict[str, object]:
    try:
        return {
            "title": ImageFont.truetype(str(font_path), 44),
            "panel": ImageFont.truetype(str(font_path), 32),
            "label": ImageFont.truetype(str(font_path), 25),
            "value": ImageFont.truetype(str(font_path), 23),
            "axis": ImageFont.truetype(str(font_path), 20),
        }
    except (OSError, ValueError):
        raise IvChartError("IV chart font is unavailable") from None


def _font_path() -> Path | None:
    override = os.environ.get("OPTION_MONITOR_FONT_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    for path in FONT_PATHS:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _draw_header(draw, report: HourlyReport, fonts: dict[str, object]) -> None:
    report_time = datetime.fromtimestamp(
        report.run_at_ms / 1000, tz=SHANGHAI
    )
    coverage = report.coverage_ratio * Decimal("100")
    draw.text(
        (48, 30),
        "期权监控 IV 柱状图",
        fill=TEXT_COLOR,
        font=fonts["title"],
    )
    draw.text(
        (50, 102),
        (
            f"报告时间：{report_time:%Y-%m-%d %H:%M}  "
            f"数据覆盖率：{coverage:.1f}%"
        ),
        fill=TEXT_COLOR,
        font=fonts["value"],
    )
    draw.line((48, 150, CHART_WIDTH - 48, 150), fill=GRID_COLOR, width=2)


def _draw_change_panel(
    draw,
    entries: tuple[dict[str, object], ...],
    fonts: dict[str, object],
    *,
    top: int,
    bottom: int,
) -> None:
    draw.text(
        (48, top),
        "|ΔIV| Top 10（相比昨日收盘）",
        fill=TEXT_COLOR,
        font=fonts["panel"],
    )
    if not entries:
        _draw_empty_panel(draw, top, fonts)
        return

    plot_left = 60
    plot_right = 1140
    plot_top = 250
    zero_y = 465
    slot_width = (plot_right - plot_left) // 10
    bar_width = 54
    maximum = max(abs(Decimal(entry["delta_iv"])) for entry in entries)
    start_x = (CHART_WIDTH - slot_width * len(entries)) // 2

    draw.line(
        (plot_left, zero_y, plot_right, zero_y),
        fill=GRID_COLOR,
        width=2,
    )
    for index, entry in enumerate(entries):
        center_x = (
            start_x + index * slot_width + slot_width // 2
        )
        delta = Decimal(entry["delta_iv"])
        bar_height = (
            int(abs(delta) / maximum * (zero_y - plot_top))
            if maximum
            else 0
        )

        if delta >= 0:
            bar_box = (
                center_x - bar_width // 2,
                zero_y - bar_height,
                center_x + bar_width // 2,
                zero_y,
            )
            color = POSITIVE_COLOR
            value_y = max(zero_y - bar_height - 30, 220)
        else:
            bar_box = (
                center_x - bar_width // 2,
                zero_y,
                center_x + bar_width // 2,
                zero_y + bar_height,
            )
            color = NEGATIVE_COLOR
            value_y = min(zero_y + bar_height + 4, 686)
        if bar_height:
            draw.rectangle(bar_box, fill=color)
        _draw_centered_text(
            draw,
            f"{delta * Decimal('100'):+.2f} pp",
            center_x,
            value_y,
            fonts["value"],
            color,
        )
        _draw_axis_labels(
            draw,
            entry,
            center_x,
            718,
            fonts,
            slot_width - 8,
        )


def _draw_level_panel(
    draw,
    entries: tuple[dict[str, object], ...],
    fonts: dict[str, object],
    *,
    top: int,
    bottom: int,
) -> None:
    draw.text(
        (48, top),
        "ATM IV 绝对值 Top 10",
        fill=TEXT_COLOR,
        font=fonts["panel"],
    )
    if not entries:
        _draw_empty_panel(draw, top, fonts)
        return

    plot_left = 60
    plot_right = 1140
    plot_top = 920
    plot_bottom = 1325
    slot_width = (plot_right - plot_left) // 10
    bar_width = 54
    maximum = max(Decimal(entry["atm_iv"]) for entry in entries)
    start_x = (CHART_WIDTH - slot_width * len(entries)) // 2

    draw.line(
        (plot_left, plot_bottom, plot_right, plot_bottom),
        fill=GRID_COLOR,
        width=2,
    )
    for index, entry in enumerate(entries):
        center_x = (
            start_x + index * slot_width + slot_width // 2
        )
        atm_iv = Decimal(entry["atm_iv"])
        bar_height = (
            int(atm_iv / maximum * (plot_bottom - plot_top))
            if maximum
            else 0
        )
        if bar_height:
            draw.rectangle(
                (
                    center_x - bar_width // 2,
                    plot_bottom - bar_height,
                    center_x + bar_width // 2,
                    plot_bottom,
                ),
                fill=LEVEL_COLOR,
            )
        _draw_centered_text(
            draw,
            f"{atm_iv * Decimal('100'):.2f}%",
            center_x,
            max(plot_bottom - bar_height - 28, 890),
            fonts["value"],
            LEVEL_COLOR,
        )
        _draw_axis_labels(
            draw,
            entry,
            center_x,
            1340,
            fonts,
            slot_width - 8,
        )


def _draw_empty_panel(draw, top: int, fonts: dict[str, object]) -> None:
    draw.text(
        (48, top + 82),
        "暂无可用数据",
        fill=TEXT_COLOR,
        font=fonts["label"],
    )


def _draw_axis_labels(
    draw,
    entry: dict[str, object],
    center_x: int,
    first_y: int,
    fonts: dict[str, object],
    maximum_width: int,
) -> None:
    product_name = _fit_label(
        draw,
        str(entry["product_name"]),
        fonts["axis"],
        maximum_width,
    )
    product_code = _fit_label(
        draw,
        str(entry["product_code"]),
        fonts["axis"],
        maximum_width,
    )
    _draw_centered_text(
        draw,
        product_name,
        center_x,
        first_y,
        fonts["axis"],
        TEXT_COLOR,
    )
    _draw_centered_text(
        draw,
        product_code,
        center_x,
        first_y + 26,
        fonts["axis"],
        TEXT_COLOR,
    )


def _draw_centered_text(
    draw,
    text: str,
    center_x: int,
    y: int,
    font,
    color: tuple[int, int, int],
) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    draw.text(
        (center_x - width // 2, y),
        text,
        fill=color,
        font=font,
    )


def _fit_label(draw, label: str, font, maximum_width: int) -> str:
    if draw.textbbox((0, 0), label, font=font)[2] <= maximum_width:
        return label
    candidate = label
    while candidate:
        candidate = candidate[:-1]
        shortened = f"{candidate}…"
        if draw.textbbox((0, 0), shortened, font=font)[2] <= maximum_width:
            return shortened
    return ""
