from __future__ import annotations

from decimal import Decimal

from option_monitor.anomaly_interpretation import (
    InterpretationFacts,
    InterpretationResult,
)
from option_monitor.anomaly_selection import select_anomaly_delivery
from option_monitor.models import (
    AnomalyChartCard,
    AnomalyChartReport,
    AnomalyMetric,
)


def _metric() -> AnomalyMetric:
    return AnomalyMetric(
        current=None,
        change=None,
        rank=None,
        history_count=0,
        history_mean=None,
        triggered=False,
        available=False,
    )


def _card(code: str) -> AnomalyChartCard:
    return AnomalyChartCard(
        product_code=code,
        product_name=code,
        underlying=code,
        severity="warning",
        trigger_categories=("iv",),
        data_time_ms=1,
        futures_price=None,
        futures_change_percent=None,
        price_triggered=False,
        atm_iv=_metric(),
        rr25=_metric(),
        call_oi_delta=None,
        put_oi_delta=None,
        call_oi_baseline_ready=False,
        put_oi_baseline_ready=False,
        ranked_contracts=(),
        evidence="",
    )


def _result(
    code: str,
    direction: str,
    score: int,
    level: str = "warning",
) -> InterpretationResult:
    return InterpretationResult(
        facts=InterpretationFacts(
            product_code=code,
            product_name=code,
            underlying=code,
            available=True,
            severity=level,
            price=None,
            price_change=None,
            atm_iv=None,
            delta_iv=None,
            iv_triggered=True,
            rr25=None,
            delta_rr25=None,
            skew_triggered=False,
            call_oi_delta=None,
            put_oi_delta=None,
            oi_triggered=False,
        ),
        direction=direction,
        important=False,
        judgment="",
        risk="",
        strength_score=score,
        level=level,
        effective_dimensions=("iv",),
    )


def _selection(codes_results):
    codes = [code for code, _ in codes_results]
    report = AnomalyChartReport(
        run_at_ms=1,
        collected_count=len(codes),
        expected_count=len(codes),
        top_increases=(),
        top_decreases=(),
        cards=tuple(_card(code) for code in codes),
    )
    results = {code: result for code, result in codes_results}
    return select_anomaly_delivery(report, results)


def test_text_codes_cover_up_to_four_confirmed_commodities():
    selection = _selection([
        ("cu", _result("cu", "偏多确认", 90)),
        ("al", _result("al", "偏空确认", 80)),
        ("zn", _result("zn", "偏多确认", 70)),
        ("rb", _result("rb", "偏多确认", 60)),
        ("ru", _result("ru", "偏空确认", 50)),  # 第 5 个确认方向，超出上限
    ])
    assert selection.text_codes == ("cu", "al", "zn", "rb")


def test_text_codes_include_two_per_index_and_metal_groups():
    selection = _selection([
        ("IO", _result("IO", "偏多确认", 90)),
        ("MO", _result("MO", "偏多确认", 85)),
        ("HO", _result("HO", "偏多确认", 80)),  # 指数组第 3 个，从确认池回填
        ("au", _result("au", "偏空确认", 75)),
        ("ag", _result("ag", "偏空确认", 70)),
        ("cu", _result("cu", "偏多确认", 65)),
    ])
    assert selection.text_codes == ("IO", "MO", "au", "ag", "HO", "cu")


def test_text_codes_include_up_to_two_divergences():
    selection = _selection([
        ("cu", _result("cu", "偏多确认", 90)),
        ("al", _result("al", "信号背离", 60)),
        ("zn", _result("zn", "信号背离", 55)),
        ("rb", _result("rb", "信号背离", 50)),  # 第 3 个背离，超出上限
    ])
    assert selection.text_codes == ("cu", "al", "zn")


def test_text_codes_capped_at_eight():
    selection = _selection([
        ("IO", _result("IO", "偏多确认", 99)),
        ("MO", _result("MO", "偏多确认", 98)),
        ("au", _result("au", "偏多确认", 97)),
        ("ag", _result("ag", "偏多确认", 96)),
        ("cu", _result("cu", "偏多确认", 95)),
        ("al", _result("al", "偏多确认", 94)),
        ("zn", _result("zn", "偏多确认", 93)),
        ("rb", _result("rb", "偏多确认", 92)),
        ("ru", _result("ru", "偏多确认", 91)),  # 总封顶 8，超出
        ("sc", _result("sc", "偏多确认", 90)),
    ])
    assert len(selection.text_codes) == 8
    assert "ru" not in selection.text_codes


def test_unavailable_facts_are_never_selected():
    result = _result("cu", "偏多确认", 90)
    result = type(result)(
        **{**result.__dict__, "facts": type(result.facts)(
            **{**result.facts.__dict__, "available": False}
        )}
    )
    selection = _selection([("cu", result)])
    assert selection.text_codes == ()
    assert selection.image_cards == ()
