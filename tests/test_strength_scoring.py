from __future__ import annotations

from decimal import Decimal

from option_monitor.anomaly_interpretation import (
    COMPONENT_CAPS,
    MIN_HISTORY,
    InterpretationFacts,
    _alert_level,
    _normalized_strength,
    interpret_facts,
)


def make_facts(**overrides) -> InterpretationFacts:
    base = dict(
        product_code="IO",
        product_name="沪深300",
        underlying="IF",
        available=True,
        severity="warning",
        price=Decimal("4000"),
        price_change=Decimal("0.01"),
        atm_iv=Decimal("0.20"),
        delta_iv=None,
        iv_triggered=False,
        rr25=Decimal("0.01"),
        delta_rr25=None,
        skew_triggered=False,
        call_oi_delta=None,
        put_oi_delta=None,
        oi_triggered=False,
    )
    base.update(overrides)
    return InterpretationFacts(**base)


def component(result, name):
    return dict(result.component_scores)[name]


class TestNormalizedStrength:
    def test_fallback_when_history_insufficient(self):
        history = (Decimal("0.01"),) * (MIN_HISTORY - 1)
        value = _normalized_strength(
            Decimal("0.05"), history, fallback=Decimal("0.42")
        )
        assert value == Decimal("0.42")

    def test_fallback_when_current_missing(self):
        history = (Decimal("0.01"),) * MIN_HISTORY
        value = _normalized_strength(None, history, fallback=Decimal("0"))
        assert value == Decimal("0")

    def test_percentile_median(self):
        history = tuple(
            Decimal(i) / Decimal("100") for i in range(1, MIN_HISTORY + 1)
        )
        # 当前值 0.055，大于 0.01~0.05 共 5 个样本 → 5/10
        value = _normalized_strength(
            Decimal("0.055"), history, fallback=Decimal("0")
        )
        assert value == Decimal("0.5")

    def test_percentile_extreme(self):
        history = tuple(Decimal("0.01") for _ in range(MIN_HISTORY))
        value = _normalized_strength(
            Decimal("0.10"), history, fallback=Decimal("0")
        )
        assert value == Decimal("1")

    def test_percentile_zero_when_smallest(self):
        history = tuple(
            Decimal(i) / Decimal("100") for i in range(1, MIN_HISTORY + 1)
        )
        value = _normalized_strength(
            Decimal("0.001"), history, fallback=Decimal("0")
        )
        assert value == Decimal("0")


class TestPriceStrengthNormalization:
    def test_uses_history_when_available(self):
        # 历史日常波动都在 1%，今天涨 5% → 分位数打满 → 价格项 20 分
        facts = make_facts(
            price_change=Decimal("0.05"),
            price_close_change=Decimal("0.05"),
            price_change_history=(
                tuple(Decimal("0.01") for _ in range(MIN_HISTORY))
            ),
        )
        result = interpret_facts(facts)
        assert component(result, "价格") == 20

    def test_falls_back_to_fixed_threshold_without_history(self):
        # 无历史：5% 达到 PRICE_FULL → 同样 20 分，路径不同结果一致
        facts = make_facts(price_change=Decimal("0.05"))
        result = interpret_facts(facts)
        assert component(result, "价格") == 20

    def test_history_makes_calm_product_score_lower(self):
        # 低波动品种：历史波动 0.2%，今天 1% 的固定阈值分只有 4 分，
        # 但分位数视角已是极端 → 20 分
        facts = make_facts(
            price_change=Decimal("0.01"),
            price_close_change=Decimal("0.01"),
            price_change_history=(
                tuple(Decimal("0.002") for _ in range(MIN_HISTORY))
            ),
        )
        result = interpret_facts(facts)
        assert component(result, "价格") == 20
        calm = interpret_facts(
            make_facts(
                price_change=Decimal("0.01"),
                price_close_change=Decimal("0.002"),
                price_change_history=(
                    tuple(Decimal("0.01") for _ in range(MIN_HISTORY))
                ),
            )
        )
        assert component(calm, "价格") == 0


class TestMaxedDimensionFloor:
    def test_maxed_price_alone_is_warning_not_observation(self):
        # 价格单边暴涨 8%，无其他维度配合：旧逻辑 observation，新逻辑 warning
        facts = make_facts(
            price_change=Decimal("0.08"),
            price_close_change=None,
        )
        result = interpret_facts(facts)
        assert result.level == "warning"
        # 只有价格一个确认维度，不构成方向确认
        assert result.direction == "方向未确认"

    def test_calm_market_stays_observation(self):
        facts = make_facts(price_change=Decimal("0.003"))
        result = interpret_facts(facts)
        assert result.level == "observation"

    def test_alert_level_backward_compatible(self):
        assert _alert_level(30, 1, 0, 0) == "observation"
        assert _alert_level(30, 1, 0, 0, True) == "warning"
        assert _alert_level(50, 2, 1, 0) == "warning"
        assert _alert_level(75, 3, 2, 0) == "important"
        assert _alert_level(65, 2, 0, 2) == "important"
        assert _alert_level(39, 3, 2, 0, True) == "warning"


class TestOiPcrNormalization:
    def test_oi_rate_percentile(self):
        facts = make_facts(
            call_oi_delta=6000,
            put_oi_delta=4000,
            call_pre_open_interest=100000,
            put_pre_open_interest=100000,
            oi_triggered=True,
            # 当前 oi_rate = 5%；历史日常只有 1% → 分位数打满 → 20 分
            oi_rate_history=tuple(
                Decimal("0.01") for _ in range(MIN_HISTORY)
            ),
        )
        result = interpret_facts(facts)
        assert component(result, "持仓") == 20

    def test_pcr_change_percentile(self):
        facts = make_facts(
            oi_pcr=Decimal("0.9"),
            previous_oi_pcr=Decimal("1.0"),
            oi_pcr_change=Decimal("-0.10"),
            pcr_change_history=tuple(
                Decimal("0.02") for _ in range(MIN_HISTORY)
            ),
        )
        result = interpret_facts(facts)
        assert component(result, "OI PCR") == 15

    def test_pcr_fallback_fixed_threshold(self):
        facts = make_facts(
            oi_pcr=Decimal("0.9"),
            previous_oi_pcr=Decimal("1.0"),
            oi_pcr_change=Decimal("-0.125"),
        )
        result = interpret_facts(facts)
        # 0.125 / PCR_FULL(0.25) = 0.5 → 15 * 0.5 = 7.5 → 8（HALF_UP）
        assert component(result, "OI PCR") == 8


class TestComponentCaps:
    def test_caps_match_weighted_dimensions(self):
        assert COMPONENT_CAPS == (20, 25, 20, 20, 15)
        facts = make_facts(price_change=Decimal("0.08"))
        result = interpret_facts(facts)
        assert len(result.component_scores) == len(COMPONENT_CAPS)
