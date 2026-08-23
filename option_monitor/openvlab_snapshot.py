from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Sequence

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None


UNIT_FACTORS = {
    None: Decimal("1"),
    "万": Decimal("10000"),
    "亿": Decimal("100000000"),
}
DISPLAY_AMOUNT = re.compile(r"^([+-]?\d+(?:\.\d+)?)(万|亿)?$")
HEADER_HEIGHT = 72
MIN_PANEL_WIDTH = 800
MIN_PANEL_HEIGHT = 200
FONT_PATHS = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
)


class OpenVlabSnapshotError(RuntimeError):
    """A safe-to-log OpenVLab snapshot failure."""


@dataclass(frozen=True)
class RankingEntry:
    symbol: str
    amount: Decimal


def parse_display_amount(text: str) -> Decimal:
    normalized = re.sub(r"[,\s￥¥元]", "", text)
    match = DISPLAY_AMOUNT.fullmatch(normalized)
    if match is None:
        raise OpenVlabSnapshotError("OpenVLab ranking amount is invalid")
    try:
        return Decimal(match.group(1)) * UNIT_FACTORS[match.group(2)]
    except (InvalidOperation, KeyError):
        raise OpenVlabSnapshotError(
            "OpenVLab ranking amount is invalid"
        ) from None


def validate_top_eight(
    entries: Sequence[RankingEntry],
    expected_sign: Literal["positive", "negative"],
) -> tuple[RankingEntry, ...]:
    selected = tuple(entries[:8])
    if len(selected) != 8:
        raise OpenVlabSnapshotError(
            "OpenVLab ranking has fewer than eight rows"
        )
    if any(not entry.symbol.strip() for entry in selected):
        raise OpenVlabSnapshotError("OpenVLab ranking symbol is missing")
    if len({entry.symbol for entry in selected}) != 8:
        raise OpenVlabSnapshotError(
            "OpenVLab ranking symbols are duplicated"
        )
    if expected_sign == "positive":
        sign_is_valid = all(entry.amount > 0 for entry in selected)
    elif expected_sign == "negative":
        sign_is_valid = all(entry.amount < 0 for entry in selected)
    else:
        raise OpenVlabSnapshotError("OpenVLab ranking sign is invalid")
    if not sign_is_valid:
        raise OpenVlabSnapshotError("OpenVLab ranking sign does not match")
    magnitudes = tuple(abs(entry.amount) for entry in selected)
    if any(
        left < right for left, right in zip(magnitudes, magnitudes[1:])
    ):
        raise OpenVlabSnapshotError("OpenVLab ranking is not descending")
    return selected


def compose_openvlab_rankings(
    increase_path: Path,
    decrease_path: Path,
    output_path: Path,
    captured_at: datetime,
) -> Path:
    if Image is None or ImageDraw is None or ImageFont is None:
        raise OpenVlabSnapshotError("Pillow is unavailable")
    font_path = _font_path()
    if font_path is None:
        raise OpenVlabSnapshotError("OpenVLab ranking font is unavailable")

    destination = Path(output_path)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        increase = _read_panel(Path(increase_path))
        decrease = _read_panel(Path(decrease_path))
        width = max(increase.width, decrease.width)
        height = (
            increase.height + decrease.height + 2 * HEADER_HEIGHT
        )
        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.truetype(str(font_path), 24)
        timestamp_font = ImageFont.truetype(str(font_path), 18)

        _draw_panel_header(
            draw,
            "增仓额 Top 8",
            captured_at,
            0,
            font,
            timestamp_font,
        )
        canvas.paste(increase, (0, HEADER_HEIGHT))
        second_header_y = HEADER_HEIGHT + increase.height
        _draw_panel_header(
            draw,
            "减仓额 Top 8",
            captured_at,
            second_header_y,
            font,
            timestamp_font,
        )
        canvas.paste(
            decrease, (0, second_header_y + HEADER_HEIGHT)
        )
        canvas.save(temporary, format="PNG")
        with Image.open(temporary) as rendered:
            if rendered.format != "PNG" or rendered.size != canvas.size:
                raise OpenVlabSnapshotError(
                    "OpenVLab ranking output is invalid"
                )
        temporary.replace(destination)
        return destination
    except OpenVlabSnapshotError:
        raise
    except (OSError, ValueError, TypeError):
        raise OpenVlabSnapshotError(
            "OpenVLab ranking panel is invalid"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _font_path() -> Path | None:
    override = (
        os.environ.get("OPENVLAB_FONT_PATH", "").strip()
        or os.environ.get("OPTION_MONITOR_FONT_PATH", "").strip()
    )
    if override:
        return Path(override).expanduser()
    return next((path for path in FONT_PATHS if path.is_file()), None)


def _read_panel(path: Path):
    try:
        with Image.open(path) as source:
            if (
                source.format != "PNG"
                or source.width < MIN_PANEL_WIDTH
                or source.height < MIN_PANEL_HEIGHT
                or source.mode not in {"RGB", "RGBA"}
            ):
                raise OpenVlabSnapshotError(
                    "OpenVLab ranking panel is invalid"
                )
            source.load()
            return source.convert("RGB")
    except OpenVlabSnapshotError:
        raise
    except (OSError, ValueError, TypeError):
        raise OpenVlabSnapshotError(
            "OpenVLab ranking panel is invalid"
        ) from None


def _draw_panel_header(
    draw,
    title: str,
    captured_at: datetime,
    top: int,
    title_font,
    timestamp_font,
) -> None:
    draw.text((24, top + 12), title, fill=(31, 41, 55), font=title_font)
    draw.text(
        (240, top + 18),
        captured_at.strftime("北京时间 %Y-%m-%d %H:%M:%S"),
        fill=(107, 114, 128),
        font=timestamp_font,
    )
