from __future__ import annotations

from decimal import Decimal

from option_monitor.anomaly_interpretation import _extract_facts
from option_monitor.collector import ProductCollection
from option_monitor.models import (
    DailyMarketClose,
    DailyOptionClose,
    FuturesChangeQuote,
    MarketSnapshot,
    OptionAnalyticsSnapshot,
)


def make_collection() -> ProductCollection:
    market = MarketSnapshot(
        run_at_ms=1,
        data_time_ms=1,
        trading_day="2026-08-31",
        product_code="IO",
        product_name="沪深300",
        underlying="IF",
        last_price=Decimal("4000"),
        pre_settlement_price=Decimal("3980"),
        atm_iv=Decimal("0.20"),
    )
    option = OptionAnalyticsSnapshot(
        run_at_ms=1,
        data_time_ms=1,
        trading_day="2026-08-31",
        product_code="IO",
        product_name="沪深300",
        underlying="IF",
        expire="2026-09",
        rr25=Decimal("0.01"),
        call_volume_delta=0,
        put_volume_delta=0,
        call_turnover_delta=Decimal("0"),
        put_turnover_delta=Decimal("0"),
        call_open_interest=121000,
        put_open_interest=133000,
        call_pre_open_interest=120000,
        put_pre_open_interest=135000,
        volume_pcr=None,
        turnover_pcr=None,
        oi_pcr=Decimal("1.1"),
        oi_concentrations=(),
        flow_baseline_ready=False,
        oi_baseline_ready=True,
        call_oi_baseline_ready=True,
        put_oi_baseline_ready=True,
    )
    return ProductCollection(
        market=market,
        flow=None,
        contract_states=(),
        option_snapshot=option,
    )


def market_closes() -> tuple[DailyMarketClose, ...]:
    # 11 个收盘，日常环比波动 1%
    prices = (
        Decimal("3600"), Decimal("3636"), Decimal("3600"),
        Decimal("3564"), Decimal("3600"), Decimal("3636"),
        Decimal("3600"), Decimal("3564"), Decimal("3600"),
        Decimal("3636"), Decimal("3600"),
    )
    return tuple(
        DailyMarketClose(
            trading_day=f"2026-08-{day:02d}",
            product_code="IO",
            data_time_ms=day,
            close_price=price,
            atm_iv=Decimal("0.20"),
        )
        for day, price in enumerate(prices, start=1)
    )


def option_closes() -> tuple[DailyOptionClose, ...]:
    # 11 个收盘持仓快照，call 稳定 10 万，put 稳定 12 万
    return tuple(
        DailyOptionClose(
            trading_day=f"2026-08-{day:02d}",
            product_code="IO",
            data_time_ms=day,
            rr25=Decimal("0.01"),
            call_open_interest=100000 + (day % 2) * 1000,
            put_open_interest=120000 + (day % 2) * 1000,
        )
        for day in range(1, 12)
    )


def test_extract_facts_builds_price_history():
    facts = _extract_facts(
        make_collection(),
        FuturesChangeQuote(
            product_code="IO",
            underlying="IF",
            last_price=Decimal("3960"),
            change_pct=Decimal("-0.005"),
            source_time_ms=1,
        ),
        (),
        (),
        None,
        market_closes(),
    )
    # 11 个收盘 → 10 个环比 |变化|，都在 1% 附近
    assert len(facts.price_change_history) == 10
    assert all(
        Decimal("0.009") < value < Decimal("0.011")
        for value in facts.price_change_history
    )
    # 当前价 3960 vs 昨收 3600 → +10%，分位数打满
    assert facts.price_close_change == Decimal("0.1")


def test_extract_facts_builds_oi_and_pcr_history():
    facts = _extract_facts(
        make_collection(),
        None,
        (),
        option_closes(),
        None,
        (),
    )
    # 相邻快照 call/put 各变动 1000 张，分母约 22 万
    assert len(facts.oi_rate_history) == 10
    assert all(value > 0 for value in facts.oi_rate_history)
    # PCR 在 120/100 与 121/101 之间交替 → 变化非零
    assert len(facts.pcr_change_history) == 10
    assert all(value > 0 for value in facts.pcr_change_history)


def test_extract_facts_tolerates_missing_oi_in_history():
    legacy = tuple(
        DailyOptionClose(
            trading_day=f"2026-08-{day:02d}",
            product_code="IO",
            data_time_ms=day,
            rr25=Decimal("0.01"),
        )
        for day in range(1, 12)
    )
    facts = _extract_facts(
        make_collection(), None, (), legacy, None, ()
    )
    assert facts.oi_rate_history == ()
    assert facts.pcr_change_history == ()


def test_extract_facts_without_market_history():
    facts = _extract_facts(make_collection(), None, (), (), None, ())
    assert facts.price_change_history == ()
    assert facts.price_close_change is None
