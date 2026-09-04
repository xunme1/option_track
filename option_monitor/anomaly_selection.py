from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from option_monitor.anomaly_interpretation import InterpretationResult
from option_monitor.models import AlertLevel, AnomalyChartCard, AnomalyChartReport


CONFIRMED_DIRECTIONS = frozenset(("偏多确认", "偏空确认"))
INDEX_CODES = ("IO", "MO", "HO")
METAL_CODES = ("au", "ag")
LEVEL_PRIORITY = {"important": 0, "warning": 1, "observation": 2}


@dataclass(frozen=True)
class InternalAnomalyScore:
    total: int
    level: AlertLevel
    effective_count: int


@dataclass(frozen=True)
class AnomalyDeliverySelection:
    image_cards: tuple[AnomalyChartCard, ...]
    text_codes: tuple[str, ...]


def score_interpretations(
    results: Mapping[str, InterpretationResult],
) -> dict[str, InternalAnomalyScore]:
    return {
        code: InternalAnomalyScore(
            total=result.strength_score if result.facts.available else 0,
            level=result.level if result.facts.available else "observation",
            effective_count=len(result.effective_dimensions),
        )
        for code, result in results.items()
    }


def select_anomaly_delivery(
    report: AnomalyChartReport,
    results: Mapping[str, InterpretationResult],
) -> AnomalyDeliverySelection:
    scores = score_interpretations(results)
    cards_by_code = {card.product_code: card for card in report.cards}
    eligible = tuple(
        code for code in cards_by_code
        if code in results and results[code].facts.available
    )
    confirmed_codes = _rank_codes(
        (
            code for code in eligible
            if results[code].direction in CONFIRMED_DIRECTIONS
        ),
        scores,
    )
    divergence_codes = _rank_codes(
        (
            code for code in eligible
            if results[code].direction == "信号背离"
        ),
        scores,
    )
    image_codes = list(confirmed_codes[:5] + divergence_codes[:3])
    for code in _rank_codes(eligible, scores):
        if code not in image_codes:
            image_codes.append(code)
        if len(image_codes) == 8:
            break

    selected: list[str] = []
    selected_set: set[str] = set()
    # 文字解读上限与长图对齐：指数/金属各至多 2 个，确认方向至多 4 个、
    # 背离至多 2 个，总封顶 8 个。
    max_text = 8

    def add(code: str) -> None:
        if code not in selected_set and len(selected) < max_text:
            selected.append(code)
            selected_set.add(code)

    for group in (INDEX_CODES, METAL_CODES):
        candidates = _rank_codes(
            (code for code in group if code in eligible), scores
        )
        for code in candidates[:2]:
            add(code)

    added_confirmed = 0
    for code in confirmed_codes:
        if code in selected_set:
            continue
        add(code)
        added_confirmed += 1
        if added_confirmed == 4:
            break
    added_divergence = 0
    for code in divergence_codes:
        if code in selected_set:
            continue
        add(code)
        added_divergence += 1
        if added_divergence == 2:
            break

    image_cards = tuple(
        _apply_result(cards_by_code[code], results[code])
        for code in image_codes
    )
    return AnomalyDeliverySelection(
        image_cards=image_cards,
        text_codes=tuple(selected),
    )


def _apply_result(
    card: AnomalyChartCard, result: InterpretationResult
) -> AnomalyChartCard:
    facts = result.facts
    return replace(
        card,
        severity=result.level,
        oi_pcr=facts.oi_pcr,
        previous_oi_pcr=facts.previous_oi_pcr,
        oi_pcr_change=facts.oi_pcr_change,
        pcr_state=result.pcr_state,
        strength_score=result.strength_score,
        strength_components=result.component_scores,
        effective_dimensions=result.effective_dimensions,
        confirmations=result.confirmations,
        conflicts=result.conflicts,
        direction_label=result.direction,
    )


def _rank_codes(
    codes: Iterable[str], scores: Mapping[str, InternalAnomalyScore]
) -> tuple[str, ...]:
    return tuple(sorted(
        codes,
        key=lambda code: (
            LEVEL_PRIORITY[scores[code].level],
            -scores[code].total,
            -scores[code].effective_count,
            code,
        ),
    ))
