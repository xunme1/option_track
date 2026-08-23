from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from option_monitor.collector import ProductCollection
from option_monitor.models import (
    AnomalyChartCard,
    AnomalyChartReport,
    AnomalyMetric,
    ContractOiChange,
    DailyOptionClose,
    FuturesChangeQuote,
    OptionAnomaly,
    Trigger,
)


ZERO = Decimal("0")
CATEGORY_ORDER = ("price", "iv", "oi", "skew")


def rank_contract_oi_changes(
    rows: Iterable[ContractOiChange],
    limit: int = 5,
) -> tuple[tuple[ContractOiChange, ...], tuple[ContractOiChange, ...]]:
    selected = tuple(rows)
    increases = tuple(sorted(
        (row for row in selected if row.delta_open_interest > 0),
        key=lambda row: (-row.delta_open_interest, row.symbol),
    )[:limit])
    decreases = tuple(sorted(
        (row for row in selected if row.delta_open_interest < 0),
        key=lambda row: (row.delta_open_interest, row.symbol),
    )[:limit])
    return increases, decreases


def rank_contract_capital_flows(
    rows: Iterable[ContractOiChange],
    limit: int = 5,
) -> tuple[tuple[ContractOiChange, ...], tuple[ContractOiChange, ...]]:
    selected = tuple(
        row
        for row in rows
        if row.oi_capital_flow is not None
        and row.oi_capital_flow != ZERO
    )
    increases = tuple(sorted(
        (row for row in selected if row.oi_capital_flow > ZERO),
        key=lambda row: (-row.oi_capital_flow, row.symbol),
    )[:limit])
    decreases = tuple(sorted(
        (row for row in selected if row.oi_capital_flow < ZERO),
        key=lambda row: (row.oi_capital_flow, row.symbol),
    )[:limit])
    return increases, decreases


def build_anomaly_chart_report(
    run_at_ms: int,
    collections: Mapping[str, ProductCollection],
    price_quotes: Mapping[str, FuturesChangeQuote],
    triggers: Sequence[Trigger],
    anomalies: Sequence[OptionAnomaly],
    iv_histories: Mapping[str, Sequence[Decimal]],
    option_histories: Mapping[str, Sequence[DailyOptionClose]],
    expected_count: int,
) -> AnomalyChartReport | None:
    all_oi_rows = tuple(
        row
        for collection in collections.values()
        for row in collection.oi_changes
    )
    top_increases, top_decreases = rank_contract_oi_changes(all_oi_rows)
    top_capital_increases, top_capital_decreases = (
        rank_contract_capital_flows(all_oi_rows)
    )
    ranked_rows = top_increases + top_decreases
    ranked_codes = {row.product_code for row in ranked_rows}

    categories: dict[str, set[str]] = {}
    severities: dict[str, list[str]] = {}
    for trigger in triggers:
        if trigger.product_code not in collections:
            continue
        if trigger.category not in ("price", "iv"):
            continue
        categories.setdefault(trigger.product_code, set()).add(
            trigger.category
        )
        severities.setdefault(trigger.product_code, []).append(
            trigger.severity
        )
    for anomaly in anomalies:
        if anomaly.product_code not in collections:
            continue
        categories.setdefault(anomaly.product_code, set()).update(
            anomaly.triggers
        )
        severities.setdefault(anomaly.product_code, []).append(
            anomaly.severity
        )
    for product_code in ranked_codes:
        if product_code in collections:
            categories.setdefault(product_code, set()).add("oi")
            severities.setdefault(product_code, []).append("warning")

    active_codes = tuple(
        code for code in categories if code in collections
    )
    if not active_codes:
        return None

    cards = [
        _build_card(
            collections[code],
            price_quotes.get(code),
            categories[code],
            severities.get(code, ()),
            iv_histories.get(code, ()),
            option_histories.get(code, ()),
            tuple(row for row in ranked_rows if row.product_code == code),
        )
        for code in active_codes
    ]
    cards.sort(key=lambda card: (
        0 if card.severity == "important" else 1,
        -len(card.trigger_categories),
        card.product_code,
    ))
    return AnomalyChartReport(
        run_at_ms=run_at_ms,
        collected_count=len(collections),
        expected_count=expected_count,
        top_increases=top_increases,
        top_decreases=top_decreases,
        cards=tuple(cards[:30]),
        top_capital_increases=top_capital_increases,
        top_capital_decreases=top_capital_decreases,
    )


def _build_card(
    collection: ProductCollection,
    price_quote: FuturesChangeQuote | None,
    active_categories: set[str],
    severity_values: Sequence[str],
    iv_history: Sequence[Decimal],
    option_history: Sequence[DailyOptionClose],
    ranked_contracts: tuple[ContractOiChange, ...],
) -> AnomalyChartCard:
    market = collection.market
    option = collection.option_snapshot
    if option is None:
        raise ValueError("option snapshot required for anomaly chart")
    ordered_categories = tuple(
        category
        for category in CATEGORY_ORDER
        if category in active_categories
    )
    severity = (
        "important" if "important" in severity_values else "warning"
    )
    call_delta = (
        option.call_open_interest - option.call_pre_open_interest
        if option.call_oi_baseline_ready else None
    )
    put_delta = (
        option.put_open_interest - option.put_pre_open_interest
        if option.put_oi_baseline_ready else None
    )
    data_times = [market.data_time_ms, option.data_time_ms]
    if price_quote is not None:
        data_times.append(price_quote.source_time_ms)
    data_times.extend(row.data_time_ms for row in ranked_contracts)
    atm_iv = _iv_metric(
        market.atm_iv, iv_history, "iv" in active_categories
    )
    rr25 = _rr_metric(
        option.rr25, option_history, "skew" in active_categories
    )
    return AnomalyChartCard(
        product_code=market.product_code,
        product_name=market.product_name,
        underlying=market.underlying,
        severity=severity,
        trigger_categories=ordered_categories,
        data_time_ms=max(data_times),
        futures_price=(
            price_quote.last_price if price_quote is not None else None
        ),
        futures_change_percent=(
            price_quote.change_pct if price_quote is not None else None
        ),
        price_triggered="price" in active_categories,
        atm_iv=atm_iv,
        rr25=rr25,
        call_oi_delta=call_delta,
        put_oi_delta=put_delta,
        call_oi_baseline_ready=option.call_oi_baseline_ready,
        put_oi_baseline_ready=option.put_oi_baseline_ready,
        ranked_contracts=ranked_contracts,
        evidence=_evidence_text(
            price_quote, atm_iv.change, call_delta, put_delta
        ),
    )


def _iv_metric(
    current: Decimal,
    history: Sequence[Decimal],
    triggered: bool,
) -> AnomalyMetric:
    selected = tuple(history[-10:])
    change = current - selected[-1] if selected else None
    complete = len(selected) == 10
    return AnomalyMetric(
        current=current,
        change=change,
        rank=(
            1 + sum(1 for value in selected if value > current)
            if complete else None
        ),
        history_count=len(selected),
        history_mean=(
            sum(selected, ZERO) / Decimal(10) if complete else None
        ),
        triggered=triggered,
        available=True,
    )


def _rr_metric(
    current: Decimal | None,
    history: Sequence[DailyOptionClose],
    triggered: bool,
) -> AnomalyMetric:
    selected = tuple(close.rr25 for close in history[-11:])
    change = (
        current - selected[-1]
        if current is not None and selected else None
    )
    changes = tuple(
        abs(later - earlier)
        for earlier, later in zip(selected, selected[1:])
    )
    complete = len(changes) == 10
    absolute_change = abs(change) if change is not None else None
    return AnomalyMetric(
        current=current,
        change=change,
        rank=(
            1 + sum(
                1 for value in changes if value > absolute_change
            )
            if complete and absolute_change is not None else None
        ),
        history_count=len(changes),
        history_mean=(
            sum(changes, ZERO) / Decimal(10) if complete else None
        ),
        triggered=triggered,
        available=current is not None,
    )


def _evidence_text(
    quote: FuturesChangeQuote | None,
    delta_iv: Decimal | None,
    call_oi_delta: int | None,
    put_oi_delta: int | None,
) -> str:
    facts: list[str] = []
    if quote is not None:
        facts.append(
            "price up" if quote.change_pct > ZERO
            else "price down" if quote.change_pct < ZERO
            else "price flat"
        )
    if delta_iv is not None:
        facts.append(
            "IV up" if delta_iv > ZERO
            else "IV down" if delta_iv < ZERO
            else "IV flat"
        )
    if call_oi_delta is not None:
        facts.append(f"Call OI {call_oi_delta:+d}")
    if put_oi_delta is not None:
        facts.append(f"Put OI {put_oi_delta:+d}")
    return " | ".join(facts) if facts else "context unavailable"
