from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from option_monitor.anomaly_chart import render_anomaly_chart
from option_monitor.models import (
    AnomalyChartCard,
    AnomalyChartReport,
    AnomalyMetric,
)
from tests.test_instant_product_chart import _font


def make_card() -> AnomalyChartCard:
    def metric(current, change, rank=3):
        return AnomalyMetric(
            current=current,
            change=change,
            rank=rank,
            history_count=10,
            history_mean=Decimal("0.18"),
            triggered=True,
            available=True,
        )

    return AnomalyChartCard(
        product_code="sc",
        product_name="原油",
        underlying="sc2610",
        severity="important",
        trigger_categories=("price", "iv", "oi", "skew"),
        data_time_ms=1788271253500,
        futures_price=Decimal("660.8"),
        futures_change_percent=Decimal("0.037"),
        price_triggered=True,
        atm_iv=metric(Decimal("0.4965"), Decimal("0.005")),
        rr25=metric(Decimal("-0.0279"), Decimal("-0.0378")),
        call_oi_delta=-714,
        put_oi_delta=2450,
        call_oi_baseline_ready=True,
        put_oi_baseline_ready=True,
        ranked_contracts=(),
        evidence="价格上涨且 RR25 走强，Call 减仓",
        oi_pcr=Decimal("2.24"),
        previous_oi_pcr=Decimal("2.09"),
        oi_pcr_change=Decimal("0.0712"),
        session_volume_pcr=Decimal("1.42"),
        pcr_state="confirm",
        strength_score=78,
        direction_label="偏多确认",
    )


def make_report(*cards: AnomalyChartCard) -> AnomalyChartReport:
    return AnomalyChartReport(
        run_at_ms=1788271253500,
        collected_count=35,
        expected_count=35,
        top_increases=(),
        top_decreases=(),
        cards=cards,
    )


def test_render_anomaly_chart_with_volume_pcr_box(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPTION_MONITOR_FONT_PATH", _font())
    output = render_anomaly_chart(
        make_report(make_card()), tmp_path / "anomaly.png"
    )
    from PIL import Image

    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == 1200


def test_render_anomaly_chart_volume_pcr_missing(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPTION_MONITOR_FONT_PATH", _font())
    card = make_card()
    object.__setattr__(card, "session_volume_pcr", None)
    output = render_anomaly_chart(make_report(card), tmp_path / "anomaly.png")
    assert output.is_file()
