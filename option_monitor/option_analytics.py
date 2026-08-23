from __future__ import annotations

import math
import statistics
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal, Sequence


ZERO = Decimal("0")
CALL_TARGET = Decimal("0.25")
PUT_TARGET = Decimal("-0.25")


def risk_reversal_25(rows: object) -> Decimal | None:
    call_iv = _interpolated_wing_iv(rows, "call", CALL_TARGET)
    put_iv = _interpolated_wing_iv(rows, "put", PUT_TARGET)
    if call_iv is None or put_iv is None:
        return None
    return call_iv - put_iv


def safe_ratio(numerator: object, denominator: object) -> Decimal | None:
    top = _decimal(numerator)
    bottom = _decimal(denominator)
    if top is None or bottom is None or top < ZERO or bottom <= ZERO:
        return None
    return top / bottom


def turnover_bias(
    turnover_pcr: Decimal | None,
) -> Literal["call", "put", "neutral", "unavailable"]:
    if turnover_pcr is None or not turnover_pcr.is_finite() or turnover_pcr < ZERO:
        return "unavailable"
    if turnover_pcr <= Decimal("0.8"):
        return "call"
    if turnover_pcr >= Decimal("1.25"):
        return "put"
    return "neutral"


def historical_volatility_10(closes: Sequence[Decimal]) -> Decimal | None:
    if len(closes) < 11:
        return None
    selected = tuple(closes[-11:])
    if any(
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value <= ZERO
        for value in selected
    ):
        return None
    returns = [
        math.log(float(current / previous))
        for previous, current in zip(selected, selected[1:])
    ]
    try:
        annualized = statistics.stdev(returns) * math.sqrt(252)
    except (OverflowError, statistics.StatisticsError, ValueError):
        return None
    if not math.isfinite(annualized) or annualized < 0:
        return None
    return Decimal(str(annualized))


def skew_change_alert(
    current_rr: Decimal,
    prior_rr_closes: Sequence[Decimal],
    mean_multiplier: Decimal,
) -> bool:
    if (
        not isinstance(current_rr, Decimal)
        or not current_rr.is_finite()
        or not isinstance(mean_multiplier, Decimal)
        or not mean_multiplier.is_finite()
        or mean_multiplier <= ZERO
        or len(prior_rr_closes) < 11
    ):
        return False
    selected = tuple(prior_rr_closes[-11:])
    if any(not value.is_finite() for value in selected):
        return False
    history_changes = [
        abs(current - previous)
        for previous, current in zip(selected, selected[1:])
    ]
    current_change = abs(current_rr - selected[-1])
    mean_change = sum(history_changes, ZERO) / Decimal(len(history_changes))
    rank = 1 + sum(1 for value in history_changes if value > current_change)
    return rank <= 2 and current_change > mean_change * mean_multiplier


def implied_move(
    atm_iv: Decimal,
    days_to_expiry: int,
    spot: Decimal,
) -> tuple[Decimal, Decimal] | None:
    if (
        not isinstance(atm_iv, Decimal)
        or not atm_iv.is_finite()
        or atm_iv < ZERO
        or not isinstance(days_to_expiry, int)
        or isinstance(days_to_expiry, bool)
        or days_to_expiry <= 0
        or not isinstance(spot, Decimal)
        or not spot.is_finite()
        or spot <= ZERO
    ):
        return None
    with localcontext() as context:
        context.prec = 28
        time_fraction = Decimal(days_to_expiry) / Decimal(365)
        move_pct = atm_iv * time_fraction.sqrt()
    return move_pct, spot * move_pct


def _interpolated_wing_iv(
    rows: object,
    side: Literal["call", "put"],
    target: Decimal,
) -> Decimal | None:
    points = _wing_points(rows, side)
    if points is None or not points:
        return None
    for delta, volatility in points:
        if delta == target:
            return volatility
    for (left_delta, left_iv), (right_delta, right_iv) in zip(points, points[1:]):
        if left_delta < target < right_delta:
            weight = (target - left_delta) / (right_delta - left_delta)
            return left_iv + (right_iv - left_iv) * weight
    return None


def _wing_points(
    rows: object, side: Literal["call", "put"]
) -> list[tuple[Decimal, Decimal]] | None:
    if not isinstance(rows, list):
        return None
    by_delta: dict[Decimal, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        quote = row.get(side)
        if not isinstance(quote, dict):
            continue
        delta = _decimal(quote.get("delta"))
        volatility = _quote_volatility(quote)
        if delta is None or volatility is None:
            continue
        existing = by_delta.get(delta)
        if existing is not None and existing != volatility:
            return None
        by_delta[delta] = volatility
    return sorted(by_delta.items())


def _quote_volatility(quote: dict[str, Any]) -> Decimal | None:
    theoretical = quote.get("theo_vol")
    if theoretical is not None:
        value = _decimal(theoretical)
        return value if value is not None and value > ZERO else None
    value = _decimal(quote.get("implied_vol"))
    return value if value is not None and value > ZERO else None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None
