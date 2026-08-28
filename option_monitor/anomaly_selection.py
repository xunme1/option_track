from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from option_monitor.anomaly_interpretation import InterpretationResult
from option_monitor.models import AnomalyChartCard, AnomalyChartReport


ZERO = Decimal("0")
ONE = Decimal("1")
PRICE_THRESHOLD = Decimal("0.025")
IMPORTANT_BONUS = Decimal("0.05")
CONFLICT_BONUS = Decimal("0.05")
MAX_CONFLICTS = 3
TREND_DIRECTIONS = frozenset(("多头", "空头", "空转多", "多转空"))
INDEX_CODES = ("IO", "MO", "HO")
METAL_CODES = ("au", "ag")


@dataclass(frozen=True)
class InternalAnomalyScore:
    base: Decimal
    important_bonus: Decimal
    divergence_bonus: Decimal

    @property
    def total(self) -> Decimal:
        return self.base + self.important_bonus + self.divergence_bonus


@dataclass(frozen=True)
class AnomalyDeliverySelection:
    image_cards: tuple[AnomalyChartCard, ...]
    text_codes: tuple[str, ...]


def score_interpretations(
    results: Mapping[str, InterpretationResult],
) -> dict[str, InternalAnomalyScore]:
    oi_totals = {
        code: _oi_total(result)
        for code, result in results.items()
    }
    max_oi = max(oi_totals.values(), default=ZERO)
    return {
        code: _score(result, oi_totals[code], max_oi)
        for code, result in results.items()
    }


def select_anomaly_delivery(
    report: AnomalyChartReport,
    results: Mapping[str, InterpretationResult],
) -> AnomalyDeliverySelection:
    scores = score_interpretations(results)
    cards_by_code = {
        card.product_code: card for card in report.cards
    }
    trend_codes = _rank_codes(
        (
            code for code in cards_by_code
            if code in results
            and results[code].facts.available
            and results[code].direction in TREND_DIRECTIONS
        ),
        scores,
    )
    divergence_codes = _rank_codes(
        (
            code for code in cards_by_code
            if code in results
            and results[code].facts.available
            and results[code].direction == "信号背离"
        ),
        scores,
    )
    image_codes = trend_codes[:5] + divergence_codes[:3]

    selected: list[str] = []
    selected_set: set[str] = set()

    def add(code: str) -> None:
        if code not in selected_set:
            selected.append(code)
            selected_set.add(code)

    for group in (INDEX_CODES, METAL_CODES):
        candidates = _rank_codes(
            (
                code for code in group
                if code in results and results[code].facts.available
            ),
            scores,
        )
        if candidates:
            add(candidates[0])

    added_trends = 0
    for code in trend_codes:
        if code in selected_set:
            continue
        add(code)
        added_trends += 1
        if added_trends == 2:
            break

    for code in divergence_codes:
        if code not in selected_set:
            add(code)
            break

    return AnomalyDeliverySelection(
        image_cards=tuple(cards_by_code[code] for code in image_codes),
        text_codes=tuple(selected),
    )


def _score(
    result: InterpretationResult,
    oi_total: Decimal,
    max_oi: Decimal,
) -> InternalAnomalyScore:
    facts = result.facts
    if not facts.available:
        return InternalAnomalyScore(ZERO, ZERO, ZERO)
    price = _price_strength(facts.price_change)
    iv = _iv_strength(result)
    rr25 = _rr25_strength(result)
    oi = oi_total / max_oi if max_oi > ZERO else ZERO
    base = (
        price * Decimal("0.15")
        + iv * Decimal("0.25")
        + rr25 * Decimal("0.25")
        + oi * Decimal("0.35")
    )
    important = (
        IMPORTANT_BONUS
        if facts.severity == "important" else ZERO
    )
    divergence = (
        CONFLICT_BONUS
        * Decimal(min(_conflict_count(result), MAX_CONFLICTS))
    )
    return InternalAnomalyScore(base, important, divergence)


def _price_strength(value: Decimal | None) -> Decimal:
    if value is None:
        return ZERO
    return min(abs(value) / PRICE_THRESHOLD, Decimal("2")) / Decimal("2")


def _iv_strength(result: InterpretationResult) -> Decimal:
    facts = result.facts
    rank = _rank_strength(facts.iv_rank, facts.iv_history_count)
    mean = facts.iv_history_mean
    level = ZERO
    change = ZERO
    if facts.atm_iv is not None and mean is not None and mean > ZERO:
        excess = max(facts.atm_iv / mean - ONE, ZERO)
        level = min(excess / Decimal("0.10"), ONE)
        if facts.delta_iv is not None:
            relative_change = abs(facts.delta_iv) / mean
            change = min(relative_change / Decimal("0.10"), ONE)
    return (
        rank * Decimal("0.40")
        + level * Decimal("0.30")
        + change * Decimal("0.30")
    )


def _rr25_strength(result: InterpretationResult) -> Decimal:
    facts = result.facts
    rank = _rank_strength(
        facts.rr25_rank, facts.rr25_history_count
    )
    relative = ZERO
    if (
        facts.delta_rr25 is not None
        and facts.rr25_history_mean is not None
        and facts.rr25_history_mean > ZERO
    ):
        relative = min(
            abs(facts.delta_rr25)
            / facts.rr25_history_mean
            / Decimal("2"),
            ONE,
        )
    return relative * Decimal("0.70") + rank * Decimal("0.30")


def _rank_strength(rank: int | None, count: int) -> Decimal:
    if rank is None or count <= 0 or rank < 1 or rank > count:
        return ZERO
    return Decimal(count - rank + 1) / Decimal(count)


def _oi_total(result: InterpretationResult) -> Decimal:
    facts = result.facts
    if not facts.available:
        return ZERO
    return Decimal(
        abs(facts.call_oi_delta or 0) + abs(facts.put_oi_delta or 0)
    )


def _conflict_count(result: InterpretationResult) -> int:
    if result.direction != "信号背离":
        return 0
    facts = result.facts
    change = facts.price_change
    conflicts = 0
    if change is not None and change > ZERO:
        if (
            facts.oi_triggered
            and facts.put_oi_delta is not None
            and facts.put_oi_delta > 0
        ):
            conflicts += 1
        if (
            facts.skew_triggered
            and facts.delta_rr25 is not None
            and facts.delta_rr25 < ZERO
        ):
            conflicts += 1
    elif change is not None and change < ZERO:
        if (
            facts.oi_triggered
            and facts.call_oi_delta is not None
            and facts.call_oi_delta > 0
        ):
            conflicts += 1
        if (
            facts.skew_triggered
            and facts.delta_rr25 is not None
            and facts.delta_rr25 > ZERO
        ):
            conflicts += 1
    elif (
        facts.iv_triggered
        and facts.delta_iv is not None
        and facts.delta_iv > ZERO
    ):
        conflicts += 1
    return conflicts


def _rank_codes(
    codes,
    scores: Mapping[str, InternalAnomalyScore],
) -> tuple[str, ...]:
    return tuple(sorted(
        codes,
        key=lambda code: (-scores[code].total, code),
    ))
