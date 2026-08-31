from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Mapping, Sequence

from option_monitor.collector import ProductCollection
from option_monitor.models import (
    AlertLevel,
    AnomalyChartCard,
    AnomalyChartReport,
    DailyMarketClose,
    DailyOptionClose,
    FuturesChangeQuote,
)


ZERO = Decimal("0")
Direction = Literal[
    "偏多确认",
    "偏空确认",
    "信号背离",
    "方向未确认",
    "数据不足",
]
MANDATORY_CODES = ("IO", "MO", "HO", "au", "ag")
PRICE_ANCHOR = Decimal("0.025")
PRICE_FULL = Decimal("0.05")
OI_FULL = Decimal("0.05")
PCR_EFFECTIVE = Decimal("0.10")
PCR_FULL = Decimal("0.25")
ONE = Decimal("1")
# 各维度强度改用品种自身历史分位数所需的最小历史样本数；
# 样本不足时回落到上面的固定阈值线性打分。
MIN_HISTORY = 10
COMPONENT_CAPS = (20, 25, 20, 20, 15)


class AnomalyInterpretationError(RuntimeError):
    pass


@dataclass(frozen=True)
class InterpretationFacts:
    product_code: str
    product_name: str
    underlying: str | None
    available: bool
    severity: AlertLevel
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
    call_pre_open_interest: int | None = None
    put_pre_open_interest: int | None = None
    oi_pcr: Decimal | None = None
    previous_oi_pcr: Decimal | None = None
    oi_pcr_change: Decimal | None = None
    # 品种自身历史分布（用于分位数强度归一化，样本不足时为空）
    price_close_change: Decimal | None = None
    price_change_history: tuple[Decimal, ...] = ()
    oi_rate_history: tuple[Decimal, ...] = ()
    pcr_change_history: tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class InterpretationResult:
    facts: InterpretationFacts
    direction: Direction
    important: bool
    judgment: str
    risk: str
    strength_score: int = 0
    level: AlertLevel = "observation"
    component_scores: tuple[tuple[str, int], ...] = ()
    effective_dimensions: tuple[str, ...] = ()
    confirmations: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    pcr_state: Literal[
        "confirm", "conflict", "neutral", "unavailable"
    ] = "unavailable"


@dataclass(frozen=True)
class _StrengthAssessment:
    score: int
    level: AlertLevel
    component_scores: tuple[tuple[str, int], ...]
    effective_dimensions: tuple[str, ...]
    confirmations: tuple[str, ...]
    conflicts: tuple[str, ...]
    direction: Direction
    pcr_state: Literal[
        "confirm", "conflict", "neutral", "unavailable"
    ]


def build_anomaly_interpretation(
    report: AnomalyChartReport,
    collections: Mapping[str, ProductCollection],
    price_quotes: Mapping[str, FuturesChangeQuote],
    iv_histories: Mapping[str, Sequence[Decimal]],
    option_histories: Mapping[str, Sequence[DailyOptionClose]],
    product_names: Mapping[str, str],
    market_histories: Mapping[str, Sequence[DailyMarketClose]] | None = None,
) -> str:
    try:
        return _build_anomaly_interpretation(
            report,
            collections,
            price_quotes,
            iv_histories,
            option_histories,
            product_names,
            market_histories,
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
    market_histories: Mapping[str, Sequence[DailyMarketClose]] | None = None,
) -> str:
    results_by_code = build_interpretation_results(
        report,
        collections,
        price_quotes,
        iv_histories,
        option_histories,
        product_names,
        market_histories,
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
        if result.level == "important" or result.direction == "信号背离":
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
    market_histories: Mapping[str, Sequence[DailyMarketClose]] | None = None,
) -> dict[str, InterpretationResult]:
    try:
        return _build_interpretation_results(
            report,
            collections,
            price_quotes,
            iv_histories,
            option_histories,
            product_names,
            market_histories,
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
    market_histories: Mapping[str, Sequence[DailyMarketClose]] | None = None,
) -> dict[str, InterpretationResult]:
    market_histories = market_histories or {}
    cards = {card.product_code: card for card in report.cards}
    facts_by_code = {
        code: _extract_facts(
            collection,
            price_quotes.get(code),
            iv_histories.get(code, ()),
            option_histories.get(code, ()),
            cards.get(code),
            market_histories.get(code, ()),
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
    assessment = _assess_strength(facts)
    return InterpretationResult(
        facts=facts,
        direction=assessment.direction,
        important=assessment.level == "important",
        judgment=_judgment(assessment),
        risk=_risk(assessment),
        strength_score=assessment.score,
        level=assessment.level,
        component_scores=assessment.component_scores,
        effective_dimensions=assessment.effective_dimensions,
        confirmations=assessment.confirmations,
        conflicts=assessment.conflicts,
        pcr_state=assessment.pcr_state,
    )


def _assess_strength(facts: InterpretationFacts) -> _StrengthAssessment:
    price = _normalized_strength(
        (
            abs(facts.price_close_change)
            if facts.price_close_change is not None else None
        ),
        facts.price_change_history,
        fallback=(
            min(abs(facts.price_change or ZERO) / PRICE_FULL, ONE)
        ),
    )
    iv = _iv_strength(facts)
    rr25 = _rr25_strength(facts)
    oi_rate: Decimal | None = None
    previous_total = (
        (facts.call_pre_open_interest or 0)
        + (facts.put_pre_open_interest or 0)
    )
    if (
        facts.call_oi_delta is not None
        and facts.put_oi_delta is not None
        and previous_total > 0
    ):
        oi_rate = Decimal(
            abs(facts.call_oi_delta) + abs(facts.put_oi_delta)
        ) / Decimal(previous_total)
    oi = _normalized_strength(
        oi_rate,
        facts.oi_rate_history,
        fallback=(
            min(oi_rate / OI_FULL, ONE) if oi_rate is not None else ZERO
        ),
    )
    pcr_change_abs = (
        abs(facts.oi_pcr_change)
        if facts.oi_pcr_change is not None else None
    )
    pcr = _normalized_strength(
        pcr_change_abs,
        facts.pcr_change_history,
        fallback=(
            min(pcr_change_abs / PCR_FULL, ONE)
            if pcr_change_abs is not None else ZERO
        ),
    )
    weighted = (
        ("价格", price * Decimal("20")),
        ("ATM IV", iv * Decimal("25")),
        ("RR25", rr25 * Decimal("20")),
        ("持仓", oi * Decimal("20")),
        ("OI PCR", pcr * Decimal("15")),
    )
    components = tuple(
        (name, int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        for name, value in weighted
    )
    score = min(100, sum(value for _, value in components))
    has_maxed_dimension = any(
        value >= Decimal(cap)
        for (_, value), cap in zip(weighted, COMPONENT_CAPS)
    )

    effective: list[str] = []
    price_direction: Literal["bullish", "bearish"] | None = None
    if facts.price_change is not None and abs(facts.price_change) >= PRICE_ANCHOR:
        effective.append("价格")
        price_direction = "bullish" if facts.price_change > ZERO else "bearish"
    if facts.iv_triggered:
        effective.append("ATM IV")
    if facts.skew_triggered:
        effective.append("RR25")
    if facts.oi_triggered:
        effective.append("持仓")
    pcr_effective = (
        facts.oi_pcr_change is not None
        and abs(facts.oi_pcr_change) >= PCR_EFFECTIVE
    )
    if pcr_effective:
        effective.append("OI PCR")

    confirmations: list[str] = []
    conflicts: list[str] = []
    pcr_state: Literal[
        "confirm", "conflict", "neutral", "unavailable"
    ] = "unavailable" if facts.oi_pcr is None else "neutral"
    if price_direction is not None:
        confirmations.append("价格")
        signals: list[tuple[str, Literal["bullish", "bearish"]]] = []
        if (
            facts.oi_triggered
            and facts.call_oi_delta is not None
            and facts.put_oi_delta is not None
        ):
            oi_bias = facts.call_oi_delta - facts.put_oi_delta
            if oi_bias != 0:
                signals.append((
                    "持仓", "bullish" if oi_bias > 0 else "bearish"
                ))
        if (
            facts.skew_triggered
            and facts.delta_rr25 is not None
            and facts.delta_rr25 != ZERO
        ):
            signals.append((
                "RR25", "bullish" if facts.delta_rr25 > ZERO else "bearish"
            ))
        if pcr_effective:
            signals.append((
                "OI PCR",
                "bullish" if facts.oi_pcr_change < ZERO else "bearish",
            ))
        for name, signal_direction in signals:
            if signal_direction == price_direction:
                confirmations.append(name)
                if name == "OI PCR":
                    pcr_state = "confirm"
            else:
                conflicts.append(name)
                if name == "OI PCR":
                    pcr_state = "conflict"

    if conflicts:
        direction: Direction = "信号背离"
    elif len(confirmations) >= 2 and price_direction == "bullish":
        direction = "偏多确认"
    elif len(confirmations) >= 2 and price_direction == "bearish":
        direction = "偏空确认"
    else:
        direction = "方向未确认"

    level = _alert_level(
        score,
        len(effective),
        len(confirmations),
        len(conflicts),
        has_maxed_dimension,
    )
    return _StrengthAssessment(
        score=score,
        level=level,
        component_scores=components,
        effective_dimensions=tuple(effective),
        confirmations=tuple(confirmations),
        conflicts=tuple(conflicts),
        direction=direction,
        pcr_state=pcr_state,
    )


def _alert_level(
    score: int,
    effective_count: int,
    confirmation_count: int,
    conflict_count: int,
    has_maxed_dimension: bool = False,
) -> AlertLevel:
    if effective_count < 2 or score < 40:
        # 单一维度打满（如价格暴涨暴跌超 5%）即便缺少其他维度配合，
        # 也至少提到预警，避免极端行情被埋进“观察”。
        return "warning" if has_maxed_dimension else "observation"
    if (
        score >= 70
        and effective_count >= 3
        and confirmation_count >= 2
    ) or (score >= 60 and conflict_count >= 2):
        return "important"
    return "warning"


def _normalized_strength(
    current: Decimal | None,
    history: Sequence[Decimal],
    *,
    fallback: Decimal,
) -> Decimal:
    """品种自身历史分位数强度；样本不足时回落到固定阈值打分。

    分位数 = 历史样本中严格小于当前值的比例，取值 [0, 1]。
    """
    if current is None or len(history) < MIN_HISTORY:
        return fallback
    below = sum(1 for value in history if value < current)
    return Decimal(below) / Decimal(len(history))


def _iv_strength(facts: InterpretationFacts) -> Decimal:
    rank = _rank_strength(facts.iv_rank, facts.iv_history_count)
    level = ZERO
    change = ZERO
    mean = facts.iv_history_mean
    if facts.atm_iv is not None and mean is not None and mean > ZERO:
        level = min(max(facts.atm_iv / mean - ONE, ZERO) / Decimal("0.10"), ONE)
        if facts.delta_iv is not None:
            change = min(abs(facts.delta_iv) / mean / Decimal("0.10"), ONE)
    return rank * Decimal("0.40") + level * Decimal("0.30") + change * Decimal("0.30")


def _rr25_strength(facts: InterpretationFacts) -> Decimal:
    rank = _rank_strength(facts.rr25_rank, facts.rr25_history_count)
    relative = ZERO
    if facts.delta_rr25 is not None and facts.rr25_history_mean not in (None, ZERO):
        relative = min(
            abs(facts.delta_rr25) / facts.rr25_history_mean / Decimal("2"),
            ONE,
        )
    return relative * Decimal("0.70") + rank * Decimal("0.30")


def _rank_strength(rank: int | None, count: int) -> Decimal:
    if rank is None or count <= 0 or rank < 1 or rank > count:
        return ZERO
    return Decimal(count - rank + 1) / Decimal(count)


def _judgment(assessment: _StrengthAssessment) -> str:
    if assessment.direction == "信号背离":
        names = "、".join(assessment.conflicts)
        return f"价格与{names}方向不一致，当前方向置信度下降，但波动风险正在上升。"
    if assessment.direction == "偏多确认":
        names = "、".join(assessment.confirmations[1:])
        return f"价格走强并获得{names}同向确认，偏多信号得到多项支持。"
    if assessment.direction == "偏空确认":
        names = "、".join(assessment.confirmations[1:])
        return f"价格走弱并获得{names}同向确认，下行风险得到多项支持。"
    return "当前异常尚未获得足够的方向指标确认，暂不作单边判断。"


def _risk(assessment: _StrengthAssessment) -> str:
    if assessment.direction == "偏多确认":
        return "OI PCR 可能包含套保头寸；若价格回落且 PCR 转升，偏多确认减弱。"
    if assessment.direction == "偏空确认":
        return "OI PCR 可能包含保护性套保，不等同于净看空；若价格反弹且 PCR 回落，偏空确认减弱。"
    if assessment.direction == "信号背离":
        return "等待价格、RR25 和持仓重新形成同向确认。"
    return "继续观察价格、RR25、持仓与 OI PCR 是否形成一致方向。"


def _extract_facts(
    collection: ProductCollection,
    quote: FuturesChangeQuote | None,
    iv_history: Sequence[Decimal],
    option_history: Sequence[DailyOptionClose],
    card: AnomalyChartCard | None,
    market_history: Sequence[DailyMarketClose] = (),
) -> InterpretationFacts:
    market = collection.market
    option = collection.option_snapshot
    if option is None:
        return _missing_facts(market.product_code, market.product_name)
    selected_iv = tuple(iv_history[-10:])
    previous_iv = selected_iv[-1] if selected_iv else None
    complete_iv = len(selected_iv) == 10
    selected_rr25_values = tuple(
        item.rr25 for item in option_history[-11:]
    )
    previous_rr25 = (
        selected_rr25_values[-1] if selected_rr25_values else None
    )
    selected_rr25 = tuple(
        value for value in selected_rr25_values if value is not None
    )
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
    current_pcr = (
        option.oi_pcr
        if option.call_open_interest > 0
        else None
    )
    previous_pcr = (
        Decimal(option.put_pre_open_interest)
        / Decimal(option.call_pre_open_interest)
        if option.call_oi_baseline_ready
        and option.put_oi_baseline_ready
        and option.call_pre_open_interest > 0
        else None
    )
    pcr_change = (
        current_pcr / previous_pcr - ONE
        if current_pcr is not None
        and previous_pcr is not None
        and previous_pcr > ZERO
        else None
    )
    # 品种自身历史分布：价格用收盘价环比，持仓/PCR 用连续两个收盘快照推导，
    # 与盘中“相对昨仓”的口径一致。
    selected_market = tuple(market_history[-11:])
    price_change_history = tuple(
        abs(current.close_price / previous.close_price - ONE)
        for previous, current in zip(selected_market, selected_market[1:])
        if previous.close_price > ZERO
    )
    previous_close = (
        selected_market[-1].close_price if selected_market else None
    )
    price_close_change = (
        quote.last_price / previous_close - ONE
        if quote is not None
        and previous_close is not None
        and previous_close > ZERO
        else None
    )
    oi_closes = tuple(
        (close.call_open_interest, close.put_open_interest)
        for close in option_history[-11:]
    )
    oi_rate_history = tuple(
        Decimal(
            abs(call - previous_call) + abs(put - previous_put)
        ) / Decimal(previous_call + previous_put)
        for (previous_call, previous_put), (call, put) in zip(
            oi_closes, oi_closes[1:]
        )
        if None not in (previous_call, previous_put, call, put)
        and previous_call + previous_put > 0
    )
    pcr_closes = tuple(
        Decimal(put) / Decimal(call)
        for call, put in oi_closes
        if call is not None and put is not None and call > 0
    )
    pcr_change_history = tuple(
        abs(current / previous - ONE)
        for previous, current in zip(pcr_closes, pcr_closes[1:])
        if previous > ZERO
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
        call_pre_open_interest=(
            option.call_pre_open_interest
            if option.call_oi_baseline_ready else None
        ),
        put_pre_open_interest=(
            option.put_pre_open_interest
            if option.put_oi_baseline_ready else None
        ),
        oi_pcr=current_pcr,
        previous_oi_pcr=previous_pcr,
        oi_pcr_change=pcr_change,
        price_close_change=price_close_change,
        price_change_history=price_change_history,
        oi_rate_history=oi_rate_history,
        pcr_change_history=pcr_change_history,
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
        level = _level_text(result.level)
        blocks.append(
            f"### {_escape(facts.product_name)}（{_escape(facts.product_code)}）"
            f"｜{level} {result.strength_score}/100｜{result.direction}"
        )
        if not facts.available:
            blocks.append(result.judgment)
            continue
        blocks.extend((
            f"异动：{_market_line(facts)}",
            f"持仓：{_oi_line(facts)}",
            _strength_line(result),
            f"判断与关注：{result.judgment}{result.risk}",
        ))
    blocks.append(
        "口径：ATM IV 变化为平值隐含波动率相对前一交易日收盘的变化；"
        "RR25 = 25Delta Call IV - 25Delta Put IV；"
        "OI PCR = Put 总持仓 / Call 总持仓，仅用于确认或背离，"
        "可能包含套保头寸，不等同于净多空方向。"
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
        f"Put {_oi_change(facts.put_oi_delta)}｜"
        f"OI PCR {_pcr_text(facts)}"
    )


def _strength_line(result: InterpretationResult) -> str:
    components = "｜".join(
        f"{name} {score}/{maximum}"
        for (name, score), maximum in zip(
            result.component_scores, (20, 25, 20, 20, 15)
        )
    ) or "数据不足"
    details = []
    if len(result.confirmations) > 1:
        details.append("确认 " + "、".join(result.confirmations[1:]))
    if result.conflicts:
        details.append("背离 " + "、".join(result.conflicts))
    suffix = "；" + "；".join(details) if details else ""
    return f"强度：{components}{suffix}"


def _pcr_text(facts: InterpretationFacts) -> str:
    current = _decimal(facts.oi_pcr, 2)
    previous = _decimal(facts.previous_oi_pcr, 2)
    change = _percent(facts.oi_pcr_change)
    return f"{current}（昨 {previous}，{change}）"


def _level_text(level: AlertLevel) -> str:
    return {
        "observation": "观察",
        "warning": "预警",
        "important": "重要",
    }[level]


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
