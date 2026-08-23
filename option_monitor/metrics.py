from decimal import Decimal
from typing import Iterable, TypeVar

ZERO = Decimal("0")
T = TypeVar("T")


def price_change(current: Decimal, reference: Decimal) -> Decimal:
    if reference <= ZERO:
        raise ValueError("reference price must be positive")
    return (current - reference) / reference


def incremental_turnover(
    current_volume: int,
    current_average_price: Decimal | None,
    current_last_price: Decimal,
    previous_volume: int,
    previous_average_price: Decimal | None,
    multiplier: Decimal,
) -> Decimal:
    if current_volume < previous_volume:
        return ZERO
    if current_average_price is not None and previous_average_price is not None:
        current_amount = Decimal(current_volume) * current_average_price * multiplier
        previous_amount = Decimal(previous_volume) * previous_average_price * multiplier
        return max(ZERO, current_amount - previous_amount)
    delta_volume = max(0, current_volume - previous_volume)
    return Decimal(delta_volume) * current_last_price * multiplier


def cumulative_turnover(
    volume: int,
    average_price: Decimal | None,
    last_price: Decimal,
    multiplier: Decimal,
) -> Decimal:
    price = average_price if average_price is not None else last_price
    return Decimal(volume) * price * multiplier


def flow_severity(net_inflow: Decimal) -> str | None:
    amount = abs(net_inflow)
    if amount >= Decimal("30000000"):
        return "important"
    if amount >= Decimal("10000000"):
        return "warning"
    return None


def iv_high_alert(
    current_iv: Decimal,
    previous_closes: list[Decimal],
    mean_multiplier: Decimal,
) -> bool:
    if len(previous_closes) != 10:
        return False
    mean = sum(previous_closes, ZERO) / Decimal(10)
    rank = 1 + sum(1 for value in previous_closes if value > current_iv)
    return rank <= 2 and current_iv > mean * mean_multiplier


def rank_by_abs(rows: Iterable[tuple[T, Decimal]], limit: int) -> list[tuple[T, Decimal]]:
    return sorted(rows, key=lambda row: abs(row[1]), reverse=True)[:limit]
