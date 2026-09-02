from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from option_monitor.instant_product_chart import (
    InstantProductChartData,
    render_instant_product_chart,
)
from option_monitor.models import (
    ContractMapping,
    FuturesChangeQuote,
    ProductSpec,
)
from tests.test_extract_facts_history import make_collection

FONT_CANDIDATES = (
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _font() -> str:
    override = os.environ.get("OPTION_MONITOR_FONT_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    pytest.skip("no CJK font available for rendering test")


def make_data(**overrides) -> InstantProductChartData:
    base = dict(
        product=ProductSpec(code="IO", name="沪深300", exchange="CFFEX"),
        mapping=ContractMapping(
            trading_day="2026-08-31",
            product_code="IO",
            underlying="IF2609",
            expire="2026-09-18",
            multiplier=Decimal("300"),
            resolved_at_ms=1,
        ),
        collection=make_collection(),
        futures_quote=FuturesChangeQuote(
            product_code="IO",
            underlying="IF2609",
            last_price=Decimal("3960"),
            change_pct=Decimal("-0.005"),
            source_time_ms=1,
            data_source="rqdata",
        ),
        rendered_at_ms=1725000000000,
        rr25_change=Decimal("0.002"),
        rr25_baseline_trading_day="2026-08-28",
        iv_change=Decimal("0.005"),
        iv_baseline_trading_day="2026-08-28",
    )
    base.update(overrides)
    return InstantProductChartData(**base)


def test_render_five_panel_chart(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPTION_MONITOR_FONT_PATH", _font())
    output = render_instant_product_chart(
        make_data(), tmp_path / "instant.png"
    )
    from PIL import Image

    with Image.open(output) as image:
        assert image.size == (1200, 500)
        assert image.format == "PNG"


def test_render_chart_without_option_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPTION_MONITOR_FONT_PATH", _font())
    collection = make_collection()
    object.__setattr__(collection, "option_snapshot", None)
    output = render_instant_product_chart(
        make_data(collection=collection), tmp_path / "instant.png"
    )
    assert output.is_file()


def test_render_chart_with_missing_pcr(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPTION_MONITOR_FONT_PATH", _font())
    collection = make_collection()
    option = collection.option_snapshot
    object.__setattr__(option, "oi_pcr", None)
    output = render_instant_product_chart(
        make_data(collection=collection), tmp_path / "instant.png"
    )
    assert output.is_file()
