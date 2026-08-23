from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from option_monitor.collector import ProductCollection
from option_monitor.models import (
    AnomalyChartCard,
    AnomalyChartReport,
    DailyOptionClose,
    FuturesChangeQuote,
)


ZERO = Decimal("0")
Direction = Literal[
    "多头",
    "空头",
    "空转多",
    "多转空",
    "中性",
    "信号背离",
    "数据不足",
]
MANDATORY_CODES = ("IO", "MO", "HO", "au", "ag")


class AnomalyInterpretationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InterpretationFacts:
    product_code: str
    product_name: str
    underlying: str | None
    available: bool
    severity: Literal["important", "warning"]
    price: Decimal | None
    price_change: Decimal | None
    atm_iv: Decimal | None
    delta_iv: Decimal | None
    iv_triggered: bool
    rr25: Decimal | None
    delta_rr25: Decimal | None
    skew_triggered: bool
    call_oi_delta: int | None
    put_oi_delta: int | None
    oi_triggered: bool
    iv_rank: int | None = None
    iv_history_count: int = 0
    iv_history_mean: Decimal | None = None
    rr25_rank: int | None = None
    rr25_history_count: int = 0
    rr25_history_mean: Decimal | None = None


@dataclass(frozen=True)
class InterpretationResult:
    facts: InterpretationFacts
    direction: Direction
    important: bool
    judgment: str
    risk: str


def build_anomaly_interpretation(
    report: AnomalyChartReport,
    collections: Mapping[str, ProductCollection],
    price_quotes: Mapping[str, FuturesChangeQuote],
    iv_histories: Mapping[str, Sequence[Decimal]],
    option_histories: Mapping[str, Sequence[DailyOptionClose]],
    product_names: Mapping[str, str],
) -> str:
    try:
        return _build_anomaly_interpretation(
            report,
            collections,
            price_quotes,
            iv_histories,
            option_histories,
            product_names,
        )
    except AnomalyInterpretationError:
        raise
    except Exception:
        raise AnomalyInterpretationError(
            "anomaly interpretation failed"
        ) from None


def _build_anomaly_interpretation(
    report: AnomalyChartReport,
    collections: Mapping[str, ProductCollection],
    price_quotes: Mapping[str, FuturesChangeQuote],
    iv_histories: Mapping[str, Sequence[Decimal]],
    option_histories: Mapping[str, Sequence[DailyOptionClose]],
    product_names: Mapping[str, str],
) -> str:
    results_by_code = build_interpretation_results(
        report,
        collections,
        price_quotes,
        iv_histories,
        option_histories,
        product_names,
    )

    results: list[InterpretationResult] = []
    selected_codes: set[str] = set()
    for code in MANDATORY_CODES:
        selected_codes.add(code)
        results.append(results_by_code[code])

    for card in report.cards:
        code = card.product_code
        if code in selected_codes or code not in results_by_code:
            continue
        result = results_by_code[code]
        if card.severity == "important" or result.direction == "信号背离":
            selected_codes.add(code)
            results.append(result)

    return _render_markdown(results)


def build_interpretation_results(
    report: AnomalyChartReport,
    collections: Mapping[str, ProductCollection],
    price_quotes: Mapping[str, FuturesChangeQuote],
    iv_histories: Mapping[str, Sequence[Decimal]],
    option_histories: Mapping[str, Sequence[DailyOptionClose]],
    product_names: Mapping[str, str],
) -> dict[str, InterpretationResult]:
    try:
        return _build_interpretation_results(
            report,
            collections,
            price_quotes,
            iv_histories,
            option_histories,
            product_names,
        )
    except AnomalyInterpretationError:
        raise
    except Exception:
        raise AnomalyInterpretationError(
            "anomaly interpretation failed"
        ) from None


def _build_interpretation_results(
    report: AnomalyChartReport,
    collections: Mapping[str, ProductCollection],
    price_quotes: Mapping[str, FuturesChangeQuote],
    iv_histories: Mapping[str, Sequence[Decimal]],
    option_histories: Mapping[str, Sequence[DailyOptionClose]],
    product_names: Mapping[str, str],
) -> dict[str, InterpretationResult]:
    cards = {card.product_code: card for card in report.cards}
    facts_by_code = {
        code: _extract_facts(
            collection,
            price_quotes.get(code),
            iv_histories.get(code, ()),
            option_histories.get(code, ()),
            cards.get(code),
        )
        for code, collection in collections.items()
    }
    for code in MANDATORY_CODES:
        facts_by_code.setdefault(
            code,
            _missing_facts(code, product_names.get(code, code)),
        )
    return {
        code: interpret_facts(facts)
        for code, facts in facts_by_code.items()
    }


def interpret_facts(facts: InterpretationFacts) -> InterpretationResult:
    if not facts.available:
        return InterpretationResult(
            facts=facts,
            direction="数据不足",
            important=False,
            judgment="本时段休市或数据不足，暂不判断，不沿用旧信号。",
            risk="等待下一次有效快照。",
        )

    divergence = _divergence_reasons(facts)
    if divergence:
        return InterpretationResult(
            facts=facts,
            direction="信号背离",
            important=True,
            judgment="；".join(divergence) + "，信号存在背离。",
            risk="方向可能正在切换，需等待价格、波动率和持仓进一步确认。",
        )

    price_change = facts.price_change
    direction: Direction
    if price_change is not None and price_change > ZERO:
        if facts.put_oi_delta is not None and facts.put_oi_delta < 0:
            direction = "空转多"
        elif (
            (facts.call_oi_delta is not None and facts.call_oi_delta > 0)
            or (
                facts.delta_rr25 is not None
                and facts.delta_rr25 > ZERO
            )
        ):
            direction = "多头"
        else:
            direction = "中性"
    elif price_change is not None and price_change < ZERO:
        if facts.call_oi_delta is not None and facts.call_oi_delta < 0:
            direction = "多转空"
        elif (
            (facts.put_oi_delta is not None and facts.put_oi_delta > 0)
            or (
                facts.delta_rr25 is not None
                and facts.delta_rr25 < ZERO
            )
        ):
            direction = "空头"
        else:
            direction = "中性"
    else:
        direction = "中性"

    return InterpretationResult(
        facts=facts,
        direction=direction,
        important=facts.severity == "important",
        judgment=_judgment(direction),
        risk=_risk(facts, direction),
    )


def _divergence_reasons(facts: InterpretationFacts) -> tuple[str, ...]:
    change = facts.price_change
    reasons: list[str] = []
    if change is not None and change > ZERO:
        if (
            facts.oi_triggered
            and facts.put_oi_delta is not None
            and facts.put_oi_delta > 0
        ):
            reasons.append("价格上涨但 Put 增仓")
        if (
            facts.skew_triggered
            and facts.delta_rr25 is not None
            and facts.delta_rr25 < ZERO
        ):
            reasons.append("价格上涨但 RR25 走弱")
    elif change is not None and change < ZERO:
        if (
            facts.oi_triggered
            and facts.call_oi_delta is not None
            and facts.call_oi_delta > 0
        ):
            reasons.append("价格下跌但 Call 增仓")
        if (
            facts.skew_triggered
            and facts.delta_rr25 is not None
            and facts.delta_rr25 > ZERO
        ):
            reasons.append("价格下跌但 RR25 走强")
    elif (
        facts.iv_triggered
        and facts.delta_iv is not None
        and facts.delta_iv > ZERO
    ):
        reasons.append("价格未明显变化但 ATM IV 上升")
    return tuple(reasons)


def _judgment(direction: Direction) -> str:
    messages = {
        "多头": "价格走强并获得 Call 持仓或偏度确认，趋势倾向多头。",
        "空头": "价格走弱并获得 Put 持仓或偏度确认，趋势倾向空头。",
        "空转多": "价格走强且 Put 持仓下降，可能为空头或保护盘撤退，倾向空转多。",
        "多转空": "价格走弱且 Call 持仓下降，可能为多头撤退，倾向多转空。",
        "中性": "价格与确认指标尚未形成一致方向，当前倾向中性。",
    }
    return messages[direction]


def _risk(facts: InterpretationFacts, direction: Direction) -> str:
    if facts.delta_iv is not None and facts.delta_iv > ZERO:
        return "ATM IV 同步上升，市场分歧或事件风险可能扩大。"
    if direction == "中性":
        return "等待价格、RR25 与持仓形成一致确认。"
    return "仍需后续价格、RR25 与持仓变化确认趋势延续性。"


def _extract_facts(
    collection: ProductCollection,
    quote: FuturesChangeQuote | None,
    iv_history: Sequence[Decimal],
    option_history: Sequence[DailyOptionClose],
    card: AnomalyChartCard | None,
) -> InterpretationFacts:
    market = collection.market
    option = collection.option_snapshot
    if option is None:
        return _missing_facts(market.product_code, market.product_name)
    selected_iv = tuple(iv_history[-10:])
    previous_iv = selected_iv[-1] if selected_iv else None
    complete_iv = len(selected_iv) == 10
    selected_rr25 = tuple(
        item.rr25 for item in option_history[-11:]
    )
    previous_rr25 = selected_rr25[-1] if selected_rr25 else None
    rr25_changes = tuple(
        abs(current - previous)
        for previous, current in zip(selected_rr25, selected_rr25[1:])
    )
    complete_rr25 = len(rr25_changes) == 10
    current_rr25_change = (
        abs(option.rr25 - previous_rr25)
        if option.rr25 is not None and previous_rr25 is not None
        else None
    )
    call_delta = (
        option.call_open_interest - option.call_pre_open_interest
        if option.call_oi_baseline_ready else None
    )
    put_delta = (
        option.put_open_interest - option.put_pre_open_interest
        if option.put_oi_baseline_ready else None
    )
    categories = frozenset(card.trigger_categories if card else ())
    return InterpretationFacts(
        product_code=market.product_code,
        product_name=market.product_name,
        underlying=market.underlying,
        available=True,
        severity=card.severity if card else "warning",
        price=quote.last_price if quote else None,
        price_change=quote.change_pct if quote else None,
        atm_iv=market.atm_iv,
        delta_iv=(
            market.atm_iv - previous_iv
            if previous_iv is not None else None
        ),
        iv_triggered="iv" in categories,
        rr25=option.rr25,
        delta_rr25=(
            option.rr25 - previous_rr25
            if option.rr25 is not None and previous_rr25 is not None
            else None
        ),
        skew_triggered="skew" in categories,
        call_oi_delta=call_delta,
        put_oi_delta=put_delta,
        oi_triggered="oi" in categories,
        iv_rank=(
            1 + sum(1 for value in selected_iv if value > market.atm_iv)
            if complete_iv else None
        ),
        iv_history_count=len(selected_iv),
        iv_history_mean=(
            sum(selected_iv, ZERO) / Decimal(10)
            if complete_iv else None
        ),
        rr25_rank=(
            1 + sum(
                1 for value in rr25_changes
                if value > current_rr25_change
            )
            if complete_rr25 and current_rr25_change is not None
            else None
        ),
        rr25_history_count=len(rr25_changes),
        rr25_history_mean=(
            sum(rr25_changes, ZERO) / Decimal(10)
            if complete_rr25 else None
        ),
    )


def _missing_facts(code: str, name: str) -> InterpretationFacts:
    return InterpretationFacts(
        product_code=code,
        product_name=name,
        underlying=None,
        available=False,
        severity="warning",
        price=None,
        price_change=None,
        atm_iv=None,
        delta_iv=None,
        iv_triggered=False,
        rr25=None,
        delta_rr25=None,
        skew_triggered=False,
        call_oi_delta=None,
        put_oi_delta=None,
        oi_triggered=False,
    )


def render_anomaly_interpretation(
    results: Mapping[str, InterpretationResult],
    selected_codes: Sequence[str],
) -> str:
    try:
        selected: list[InterpretationResult] = []
        seen: set[str] = set()
        for code in selected_codes:
            if code in seen or code not in results:
                continue
            selected.append(results[code])
            seen.add(code)
        return _render_markdown(selected)
    except AnomalyInterpretationError:
        raise
    except Exception:
        raise AnomalyInterpretationError(
            "anomaly interpretation failed"
        ) from None


def _render_markdown(results: Sequence[InterpretationResult]) -> str:
    blocks = ["## 异常解读"]
    for result in results:
        facts = result.facts
        level = "重要" if result.important else "常规"
        blocks.append(
            f"### {_escape(facts.product_name)}（{_escape(facts.product_code)}）"
            f"｜{result.direction}｜{level}"
        )
        if not facts.available:
            blocks.append(result.judgment)
            continue
        blocks.extend((
            _market_line(facts),
            _oi_line(facts),
            f"判断：{result.judgment}",
            f"风险：{result.risk}",
        ))
    blocks.append(
        "口径：ATM IV 变化为平值隐含波动率相对前一交易日收盘的变化；"
        "RR25 = 25Delta Call IV - 25Delta Put IV。"
    )
    return "\n\n".join(blocks)


def _market_line(facts: InterpretationFacts) -> str:
    price = _decimal(facts.price, 2)
    change = _percent(facts.price_change)
    atm_iv = _percent(facts.atm_iv, signed=False)
    delta_iv = _points(facts.delta_iv)
    rr25 = _points(facts.rr25)
    delta_rr25 = _points(facts.delta_rr25)
    return (
        f"价格 {price}（{change}）｜ATM IV {atm_iv}（变化 {delta_iv}）｜"
        f"RR25 {rr25}（变化 {delta_rr25}）"
    )


def _oi_line(facts: InterpretationFacts) -> str:
    return (
        f"Call {_oi_change(facts.call_oi_delta)}｜"
        f"Put {_oi_change(facts.put_oi_delta)}"
    )


def _oi_change(value: int | None) -> str:
    if value is None:
        return "持仓 --"
    if value > 0:
        return f"增仓 {value:,} 张"
    if value < 0:
        return f"减仓 {abs(value):,} 张"
    return "持仓不变"


def _decimal(value: Decimal | None, places: int) -> str:
    if value is None:
        return "--"
    return f"{value:.{places}f}"


def _percent(value: Decimal | None, signed: bool = True) -> str:
    if value is None:
        return "--"
    sign = "+" if signed else ""
    return f"{value * Decimal('100'):{sign}.2f}%"


def _points(value: Decimal | None) -> str:
    if value is None:
        return "--"
    return f"{value * Decimal('100'):+.2f}pp"


def _escape(value: str) -> str:
    single_line = " ".join(str(value).splitlines())
    markdown_characters = frozenset("\\*_~`#+-.![]()<>|{}")
    return "".join(
        f"\\{character}" if character in markdown_characters else character
        for character in single_line
    )
