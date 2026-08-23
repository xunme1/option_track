from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class ProductSpec:
    code: str
    name: str
    exchange: str


@dataclass(frozen=True)
class ContractMapping:
    trading_day: str
    product_code: str
    underlying: str
    expire: str
    multiplier: Decimal
    resolved_at_ms: int


@dataclass(frozen=True)
class FuturesChangeQuote:
    product_code: str
    underlying: str
    last_price: Decimal
    change_pct: Decimal
    source_time_ms: int
    volume: int | None = None
    open_interest: int | None = None
    data_source: str = "unknown"


@dataclass(frozen=True)
class MarketSnapshot:
    run_at_ms: int
    data_time_ms: int
    trading_day: str
    product_code: str
    product_name: str
    underlying: str
    last_price: Decimal
    pre_settlement_price: Decimal
    atm_iv: Decimal


@dataclass(frozen=True)
class ContractVolumeState:
    trading_day: str
    underlying: str
    symbol: str
    side: Literal["C", "P"]
    volume: int
    average_price: Decimal | None
    last_price: Decimal
    multiplier: Decimal
    data_time_ms: int


@dataclass(frozen=True)
class ContractOiChange:
    run_at_ms: int
    data_time_ms: int
    trading_day: str
    product_code: str
    product_name: str
    underlying: str
    expire: str
    symbol: str
    side: Literal["C", "P"]
    strike: Decimal
    open_interest: int
    pre_open_interest: int
    delta_open_interest: int
    multiplier: Decimal | None = None
    option_last_price: Decimal | None = None

    @property
    def oi_capital_flow(self) -> Decimal | None:
        if (
            self.option_last_price is None
            or self.option_last_price <= 0
            or self.multiplier is None
            or self.multiplier <= 0
        ):
            return None
        return (
            Decimal(self.delta_open_interest)
            * self.option_last_price
            * self.multiplier
        )

@dataclass(frozen=True)
class FlowSnapshot:
    run_at_ms: int
    data_time_ms: int
    trading_day: str
    product_code: str
    underlying: str
    call_inflow: Decimal
    put_inflow: Decimal
    net_inflow: Decimal
    call_contract_count: int = 0
    put_contract_count: int = 0


@dataclass(frozen=True)
class DailyIvClose:
    trading_day: str
    product_code: str
    data_time_ms: int
    atm_iv: Decimal


@dataclass(frozen=True)
class DailyMarketClose:
    trading_day: str
    product_code: str
    data_time_ms: int
    close_price: Decimal
    atm_iv: Decimal


@dataclass(frozen=True)
class OiConcentration:
    strike: Decimal
    open_interest: int
    share: Decimal


@dataclass(frozen=True)
class OptionAnalyticsSnapshot:
    run_at_ms: int
    data_time_ms: int
    trading_day: str
    product_code: str
    product_name: str
    underlying: str
    expire: str
    rr25: Decimal | None
    call_volume_delta: int
    put_volume_delta: int
    call_turnover_delta: Decimal
    put_turnover_delta: Decimal
    call_open_interest: int
    put_open_interest: int
    call_pre_open_interest: int
    put_pre_open_interest: int
    volume_pcr: Decimal | None
    turnover_pcr: Decimal | None
    oi_pcr: Decimal | None
    oi_concentrations: tuple[OiConcentration, ...]
    flow_baseline_ready: bool
    oi_baseline_ready: bool
    next_expire: str | None = None
    next_atm_iv: Decimal | None = None
    call_oi_baseline_ready: bool = False
    put_oi_baseline_ready: bool = False


@dataclass(frozen=True)
class DailyOptionClose:
    trading_day: str
    product_code: str
    data_time_ms: int
    rr25: Decimal


@dataclass(frozen=True)
class OptionAnomaly:
    run_at_ms: int
    severity: Literal["important", "warning"]
    triggers: tuple[Literal["iv", "skew"], ...]
    product_code: str
    product_name: str
    underlying: str
    side: Literal["call", "put", "neutral"]
    price_change: Decimal | None
    atm_iv: Decimal
    delta_iv: Decimal | None
    hv10: Decimal | None
    iv_hv: Decimal | None
    iv_rank: int | None
    iv_mean: Decimal | None
    rr25: Decimal | None
    delta_rr25: Decimal | None
    skew_rank: int | None
    mean_abs_skew_change: Decimal | None
    option: OptionAnalyticsSnapshot
    implied_move_pct: Decimal | None
    implied_move_amount: Decimal | None
    evidence: tuple[Literal["price", "iv", "oi"], ...]
    pin_risk: bool


@dataclass(frozen=True)
class AnomalyMetric:
    current: Decimal | None
    change: Decimal | None
    rank: int | None
    history_count: int
    history_mean: Decimal | None
    triggered: bool
    available: bool


@dataclass(frozen=True)
class AnomalyChartCard:
    product_code: str
    product_name: str
    underlying: str
    severity: Literal["important", "warning"]
    trigger_categories: tuple[
        Literal["price", "iv", "oi", "skew"], ...
    ]
    data_time_ms: int
    futures_price: Decimal | None
    futures_change_percent: Decimal | None
    price_triggered: bool
    atm_iv: AnomalyMetric
    rr25: AnomalyMetric
    call_oi_delta: int | None
    put_oi_delta: int | None
    call_oi_baseline_ready: bool
    put_oi_baseline_ready: bool
    ranked_contracts: tuple[ContractOiChange, ...]
    evidence: str


@dataclass(frozen=True)
class AnomalyChartReport:
    run_at_ms: int
    collected_count: int
    expected_count: int
    top_increases: tuple[ContractOiChange, ...]
    top_decreases: tuple[ContractOiChange, ...]
    cards: tuple[AnomalyChartCard, ...]
    top_capital_increases: tuple[ContractOiChange, ...] = field(
        default_factory=tuple
    )
    top_capital_decreases: tuple[ContractOiChange, ...] = field(
        default_factory=tuple
    )


@dataclass(frozen=True)
class Trigger:
    category: Literal["price", "iv", "flow", "service"]
    severity: Literal["important", "warning"]
    product_code: str
    product_name: str
    direction: str
    value: Decimal
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HourlyReport:
    run_at_ms: int
    coverage_ratio: Decimal
    missing_products: tuple[str, ...]
    price_entries: tuple[dict[str, Any], ...]
    flow_entries: tuple[dict[str, Any], ...]
    iv_entries: tuple[dict[str, Any], ...]
    iv_change_chart_entries: tuple[dict[str, Any], ...] = ()
    iv_level_chart_entries: tuple[dict[str, Any], ...] = ()
    missing_price_products: tuple[str, ...] = ()
    missing_close_products: tuple[str, ...] = ()
    incomplete_flow_products: tuple[str, ...] = ()
