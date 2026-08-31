from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from option_monitor.metrics import iv_high_alert
from option_monitor.models import (
    DailyOptionClose,
    DailyMarketClose,
    FlowSnapshot,
    FuturesChangeQuote,
    HourlyReport,
    MarketSnapshot,
    OptionAnalyticsSnapshot,
    OptionAnomaly,
    Trigger,
)
from option_monitor.option_analytics import (
    historical_volatility_10,
    implied_move,
    skew_change_alert,
    turnover_bias,
)


ZERO = Decimal("0")


def evaluate_option_anomaly(
    market: MarketSnapshot,
    option: OptionAnalyticsSnapshot,
    price_quote: FuturesChangeQuote | None,
    previous_market_closes: Sequence[DailyMarketClose],
    previous_iv_closes: Sequence[Decimal],
    previous_option_closes: Sequence[DailyOptionClose],
    iv_mean_multiplier: Decimal,
    skew_mean_multiplier: Decimal,
) -> OptionAnomaly | None:
    iv_values = tuple(previous_iv_closes[-10:])
    iv_triggered = iv_high_alert(
        market.atm_iv, list(iv_values), iv_mean_multiplier
    )
    selected_rr_values = tuple(
        close.rr25 for close in previous_option_closes[-11:]
    )
    rr_values = tuple(
        value for value in selected_rr_values if value is not None
    )
    previous_rr25 = (
        selected_rr_values[-1] if selected_rr_values else None
    )
    skew_triggered = (
        option.rr25 is not None
        and skew_change_alert(
            option.rr25, rr_values, skew_mean_multiplier
        )
    )
    if not iv_triggered and not skew_triggered:
        return None

    delta_iv = (
        market.atm_iv - iv_values[-1] if iv_values else None
    )
    delta_rr25 = (
        option.rr25 - previous_rr25
        if option.rr25 is not None and previous_rr25 is not None else None
    )
    side = _anomaly_side(option, skew_triggered, delta_rr25)
    evidence = _evidence(side, delta_iv, option, price_quote)
    triggers = tuple(
        name
        for name, active in (("iv", iv_triggered), ("skew", skew_triggered))
        if active
    )
    severity = (
        "important"
        if len(triggers) == 2 or evidence == ("price", "iv", "oi")
        else "warning"
    )

    price_closes = tuple(
        close.close_price for close in previous_market_closes[-11:]
    )
    hv10 = historical_volatility_10(price_closes)
    iv_mean = (
        sum(iv_values, ZERO) / Decimal(10)
        if len(iv_values) == 10 else None
    )
    iv_rank = (
        1 + sum(1 for value in iv_values if value > market.atm_iv)
        if len(iv_values) == 10 else None
    )
    skew_changes = [
        abs(current - previous)
        for previous, current in zip(rr_values, rr_values[1:])
    ]
    current_skew_change = abs(delta_rr25) if delta_rr25 is not None else None
    mean_abs_skew_change = (
        sum(skew_changes, ZERO) / Decimal(10)
        if len(skew_changes) == 10 else None
    )
    skew_rank = (
        1 + sum(
            1 for value in skew_changes if value > current_skew_change
        )
        if len(skew_changes) == 10 and current_skew_change is not None
        else None
    )
    days_to_expiry = _days_to_expiry(option.trading_day, option.expire)
    move = (
        implied_move(market.atm_iv, days_to_expiry, market.last_price)
        if days_to_expiry is not None else None
    )
    pin_risk = bool(
        days_to_expiry is not None
        and days_to_expiry <= 5
        and option.oi_concentrations
        and option.oi_concentrations[0].share >= Decimal("0.2")
    )
    return OptionAnomaly(
        run_at_ms=market.run_at_ms,
        severity=severity,
        triggers=triggers,
        product_code=market.product_code,
        product_name=market.product_name,
        underlying=market.underlying,
        side=side,
        price_change=(
            price_quote.change_pct if price_quote is not None else None
        ),
        atm_iv=market.atm_iv,
        delta_iv=delta_iv,
        hv10=hv10,
        iv_hv=(market.atm_iv - hv10 if hv10 is not None else None),
        iv_rank=iv_rank,
        iv_mean=iv_mean,
        rr25=option.rr25,
        delta_rr25=delta_rr25,
        skew_rank=skew_rank,
        mean_abs_skew_change=mean_abs_skew_change,
        option=option,
        implied_move_pct=(move[0] if move is not None else None),
        implied_move_amount=(move[1] if move is not None else None),
        evidence=evidence,
        pin_risk=pin_risk,
    )


def _anomaly_side(
    option: OptionAnalyticsSnapshot,
    skew_triggered: bool,
    delta_rr25: Decimal | None,
) -> str:
    if skew_triggered and delta_rr25 is not None:
        if delta_rr25 > ZERO:
            return "call"
        if delta_rr25 < ZERO:
            return "put"
        return "neutral"
    if not option.flow_baseline_ready:
        return "neutral"
    if option.call_turnover_delta > ZERO and option.put_turnover_delta == ZERO:
        return "call"
    if option.put_turnover_delta > ZERO and option.call_turnover_delta == ZERO:
        return "put"
    bias = turnover_bias(option.turnover_pcr)
    return bias if bias in ("call", "put") else "neutral"


def _evidence(
    side: str,
    delta_iv: Decimal | None,
    option: OptionAnalyticsSnapshot,
    price_quote: FuturesChangeQuote | None,
) -> tuple[str, ...]:
    facts: list[str] = []
    if price_quote is not None and (
        (side == "call" and price_quote.change_pct > ZERO)
        or (side == "put" and price_quote.change_pct < ZERO)
    ):
        facts.append("price")
    if delta_iv is not None and delta_iv > ZERO:
        facts.append("iv")
    if option.oi_baseline_ready and (
        (
            side == "call"
            and option.call_open_interest > option.call_pre_open_interest
        )
        or (
            side == "put"
            and option.put_open_interest > option.put_pre_open_interest
        )
    ):
        facts.append("oi")
    return tuple(facts)


def _days_to_expiry(trading_day: str, expire: str) -> int | None:
    try:
        start = datetime.strptime(trading_day, "%Y%m%d").date()
        end = datetime.strptime(expire, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None
    days = (end - start).days
    return days if days > 0 else None


def evaluate_triggers(
    market: MarketSnapshot,
    flow: FlowSnapshot,
    price_quote: FuturesChangeQuote | None,
    previous_iv_closes: list[Decimal],
    price_threshold: Decimal,
    iv_mean_multiplier: Decimal,
) -> tuple[Trigger, ...]:
    """Evaluate one product in the delivery order used by the monitor."""
    triggers: list[Trigger] = []

    if (
        price_quote is not None
        and abs(price_quote.change_pct) > price_threshold
    ):
        triggers.append(Trigger(
            severity="warning",
            category="price",
            product_code=market.product_code,
            product_name=market.product_name,
            direction="上涨" if price_quote.change_pct >= ZERO else "下跌",
            value=price_quote.change_pct,
            details={"underlying": price_quote.underlying},
        ))

    if iv_high_alert(market.atm_iv, previous_iv_closes, iv_mean_multiplier):
        triggers.append(Trigger(
            severity="warning",
            category="iv",
            product_code=market.product_code,
            product_name=market.product_name,
            direction="上升",
            value=market.atm_iv,
            details={"underlying": market.underlying},
        ))

    return tuple(triggers)


def build_hourly_report(
    run_at_ms: int,
    current_markets: Iterable[MarketSnapshot],
    price_quotes: Mapping[str, FuturesChangeQuote],
    previous_closes: Mapping[str, DailyMarketClose | None],
    current_flows: Mapping[str, FlowSnapshot | None],
    coverage_ratio: Decimal,
    missing_products: tuple[str, ...],
    missing_price_products: tuple[str, ...],
    missing_close_products: tuple[str, ...],
    incomplete_flow_products: tuple[str, ...],
) -> HourlyReport:
    """Rank current observations against the previous trading-day close."""
    price_entries: list[dict[str, object]] = []
    flow_entries: list[dict[str, object]] = []
    iv_entries: list[dict[str, object]] = []
    iv_change_entries: list[dict[str, object]] = []
    iv_level_entries: list[dict[str, object]] = []

    for market in sorted(current_markets, key=lambda item: item.product_code):
        iv_level_entry = {
            "product_code": market.product_code,
            "product_name": market.product_name,
            "underlying": market.underlying,
            "atm_iv": market.atm_iv,
        }
        iv_level_entries.append(iv_level_entry)

        price_quote = price_quotes.get(market.product_code)
        if price_quote is not None:
            price_entries.append({
                "product_code": market.product_code,
                "product_name": market.product_name,
                "underlying": price_quote.underlying,
                "last_price": price_quote.last_price,
                "price_change": price_quote.change_pct,
            })

        previous = previous_closes.get(market.product_code)
        if previous is not None:
            iv_change_entry = {
                **iv_level_entry,
                "delta_iv": market.atm_iv - previous.atm_iv,
            }
            iv_entries.append(iv_change_entry)
            iv_change_entries.append(iv_change_entry)

    return HourlyReport(
        run_at_ms=run_at_ms,
        coverage_ratio=coverage_ratio,
        missing_products=missing_products,
        price_entries=tuple(_rank_entries(price_entries, "price_change", 5)),
        flow_entries=tuple(_rank_entries(flow_entries, "net_inflow", 8)),
        iv_entries=tuple(_rank_entries(iv_entries, "delta_iv", 5)),
        iv_change_chart_entries=tuple(
            _rank_entries(iv_change_entries, "delta_iv", 10)
        ),
        iv_level_chart_entries=tuple(
            _rank_descending_entries(iv_level_entries, "atm_iv", 10)
        ),
        missing_price_products=missing_price_products,
        missing_close_products=missing_close_products,
        incomplete_flow_products=incomplete_flow_products,
    )
def _rank_entries(
    entries: list[dict[str, object]],
    value_key: str,
    limit: int,
) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda entry: (
            -abs(Decimal(entry[value_key])),
            str(entry["product_code"]),
        ),
    )[:limit]


def _rank_descending_entries(
    entries: list[dict[str, object]],
    value_key: str,
    limit: int,
) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda entry: (
            -Decimal(entry[value_key]),
            str(entry["product_code"]),
        ),
    )[:limit]
