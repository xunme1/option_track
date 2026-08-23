from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from option_monitor.hitick_client import HitickError
from option_monitor.metrics import cumulative_turnover, incremental_turnover
from option_monitor.models import (
    ContractMapping,
    ContractOiChange,
    ContractVolumeState,
    FlowSnapshot,
    MarketSnapshot,
    OiConcentration,
    OptionAnalyticsSnapshot,
    ProductSpec,
)
from option_monitor.option_analytics import risk_reversal_25, safe_ratio


ZERO = Decimal("0")
UNDERLYING_PREFIXES = {
    "IO": "IF",
    "MO": "IM",
    "HO": "IH",
}


class MainOptionUnavailable(HitickError):
    """The requested main futures contract has no usable option chain."""


@dataclass(frozen=True)
class ProductCollection:
    market: MarketSnapshot
    flow: FlowSnapshot
    contract_states: tuple[ContractVolumeState, ...]
    source_time_ms: tuple[int, ...] = ()
    option_snapshot: OptionAnalyticsSnapshot | None = None
    oi_changes: tuple[ContractOiChange, ...] = ()


def resolve_mapping(
    client: Any,
    product: ProductSpec,
    trading_day: str,
    resolved_at_ms: int,
    required_underlying: str,
) -> ContractMapping:
    if (
        not isinstance(required_underlying, str)
        or not required_underlying
        or required_underlying.strip() != required_underlying
    ):
        raise HitickError("invalid required underlying")
    prefix = UNDERLYING_PREFIXES.get(product.code, product.code)
    resolved = client.resolve_subject(required_underlying)
    query_underlying = _orange_contract_alias(product, required_underlying)
    if (
        isinstance(resolved, dict)
        and resolved.get("found") is False
        and query_underlying.casefold()
        != required_underlying.casefold()
    ):
        resolved = client.resolve_subject(query_underlying)
    if not isinstance(resolved, dict):
        raise HitickError("invalid subject resolution")
    found = resolved.get("found")
    if found is False:
        raise MainOptionUnavailable(
            "subject resolution did not find a main contract option"
        )
    if found is not True:
        raise HitickError("invalid subject resolution status")
    if resolved.get("ambiguous") is not False:
        raise HitickError("subject resolution is ambiguous")

    raw_candidates = []
    selected = resolved.get("selected")
    if isinstance(selected, dict):
        raw_candidates.append(selected)
    candidates = resolved.get("candidates")
    if isinstance(candidates, list):
        raw_candidates.extend(
            candidate for candidate in candidates
            if isinstance(candidate, dict)
        )

    exact: dict[tuple[str, Decimal], str] = {}
    selected_identity: tuple[str, Decimal] | None = None
    expired = False
    for candidate in raw_candidates:
        candidate_underlying = candidate.get("underlying")
        if not isinstance(candidate_underlying, str):
            continue
        if product.exchange == "CZCE":
            try:
                same_contract = _contract_month(
                    candidate_underlying, prefix, trading_day
                ) == _contract_month(
                    required_underlying, prefix, trading_day
                )
            except HitickError:
                same_contract = False
        else:
            same_contract = (
                candidate_underlying.casefold()
                == required_underlying.casefold()
            )
        if not same_contract:
            continue
        expire = _required_text(candidate, "expire", "main contract")
        multiplier = _decimal(
            candidate.get("multiplier"), "main multiplier"
        )
        if expire <= trading_day:
            expired = True
            continue
        if multiplier <= ZERO:
            raise HitickError("main multiplier must be positive")
        exact[(expire, multiplier)] = candidate_underlying
        if candidate is selected:
            selected_identity = (expire, multiplier)
    if not exact:
        message = (
            "main contract option is expired"
            if expired
            else "subject resolution does not prove main contract"
        )
        raise MainOptionUnavailable(message)
    if len(exact) != 1:
        if selected_identity is None or selected_identity not in exact:
            raise MainOptionUnavailable(
                "main contract has multiple option expiries"
            )
        exact = {selected_identity: exact[selected_identity]}

    expire, multiplier = next(iter(exact))
    underlying = exact[(expire, multiplier)]
    return ContractMapping(
        trading_day=trading_day,
        product_code=product.code,
        underlying=underlying,
        expire=expire,
        multiplier=multiplier,
        resolved_at_ms=resolved_at_ms,
    )


def resolve_nearest_option_mapping(
    client: Any,
    product: ProductSpec,
    trading_day: str,
    resolved_at_ms: int,
    required_underlying: str,
) -> ContractMapping:
    resolved = client.resolve_subject(product.code)
    if not isinstance(resolved, dict):
        raise HitickError("invalid subject resolution")
    if resolved.get("found") is not True:
        raise HitickError("subject resolution did not find a contract")
    if resolved.get("ambiguous") is not False:
        raise HitickError("subject resolution is ambiguous")

    raw_candidates = []
    selected = resolved.get("selected")
    if isinstance(selected, dict):
        raw_candidates.append(selected)
    candidates = resolved.get("candidates")
    if isinstance(candidates, list):
        raw_candidates.extend(
            candidate for candidate in candidates
            if isinstance(candidate, dict)
        )

    prefix = UNDERLYING_PREFIXES.get(product.code, product.code)
    contract_pattern = re.compile(
        rf"{re.escape(prefix)}(?:\d{{3}}|\d{{4}})", re.IGNORECASE
    )
    required_month = _contract_month(
        required_underlying, prefix, trading_day
    )
    valid: dict[
        tuple[tuple[int, int], str, str, Decimal], str
    ] = {}
    for candidate in raw_candidates:
        underlying = _required_text(candidate, "underlying", "option contract")
        expire = _required_text(candidate, "expire", "option contract")
        multiplier = _decimal(
            candidate.get("multiplier"), "option contract multiplier"
        )
        if contract_pattern.fullmatch(underlying) is None:
            continue
        candidate_month = _contract_month(underlying, prefix, trading_day)
        if (
            candidate_month <= required_month
            or expire <= trading_day
            or multiplier <= ZERO
        ):
            continue
        valid[(
            candidate_month,
            underlying.casefold(),
            expire,
            multiplier,
        )] = underlying
    if not valid:
        raise HitickError(
            "subject resolution has no later unexpired option contract"
        )

    nearest_month = min(key[0] for key in valid)
    nearest_underlyings = {
        key[1] for key in valid if key[0] == nearest_month
    }
    if len(nearest_underlyings) != 1:
        raise HitickError("nearest option contract is ambiguous")
    nearest_key = next(iter(nearest_underlyings))
    identities = {
        (expire, multiplier)
        for month, underlying_key, expire, multiplier in valid
        if month == nearest_month and underlying_key == nearest_key
    }
    if len(identities) != 1:
        raise HitickError("nearest option contract is ambiguous")
    expire, multiplier = next(iter(identities))
    nearest_underlying = next(
        underlying
        for (
            month,
            underlying_key,
            candidate_expire,
            candidate_multiplier,
        ), underlying in valid.items()
        if month == nearest_month
        and underlying_key == nearest_key
        and candidate_expire == expire
        and candidate_multiplier == multiplier
    )
    return ContractMapping(
        trading_day=trading_day,
        product_code=product.code,
        underlying=nearest_underlying,
        expire=expire,
        multiplier=multiplier,
        resolved_at_ms=resolved_at_ms,
    )


def _contract_month(
    underlying: str,
    prefix: str,
    trading_day: str,
) -> tuple[int, int]:
    match = re.fullmatch(
        rf"{re.escape(prefix)}(\d{{3}}|\d{{4}})",
        underlying,
        re.IGNORECASE,
    )
    if match is None or re.fullmatch(r"\d{8}", trading_day) is None:
        raise HitickError("invalid futures contract month")
    digits = match.group(1)
    month = int(digits[-2:])
    if not 1 <= month <= 12:
        raise HitickError("invalid futures contract month")
    if len(digits) == 4:
        return 2000 + int(digits[:2]), month

    trading_year = int(trading_day[:4])
    year_digit = int(digits[0])
    decade_year = trading_year - trading_year % 10 + year_digit
    year = min(
        (decade_year - 10, decade_year, decade_year + 10),
        key=lambda candidate: (abs(candidate - trading_year), candidate),
    )
    return year, month


def _orange_contract_alias(
    product: ProductSpec, underlying: str
) -> str:
    if product.exchange != "CZCE":
        return underlying
    prefix = UNDERLYING_PREFIXES.get(product.code, product.code)
    match = re.fullmatch(
        rf"{re.escape(prefix)}(\d{{4}})", underlying, re.IGNORECASE
    )
    if match is None:
        return underlying
    digits = match.group(1)
    return f"{prefix}{digits[1:]}"


def collect_product(
    product: ProductSpec,
    mapping: ContractMapping,
    basic: dict[str, Any],
    vol: dict[str, Any],
    store: Any,
    run_at_ms: int,
    observed_at_ms: int | None = None,
) -> ProductCollection:
    _validate_mapping_identity(basic, mapping)
    _validate_mapping_identity(vol, mapping)
    vol_time_ms, quote_time_ms, quote = _fresh_quote(vol, mapping)

    rows = basic.get("rows") if isinstance(basic, dict) else None
    if not isinstance(rows, list):
        raise HitickError("invalid basic data rows")
    future = next(
        (
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("instrument_kind") in ("FUTURE", "underlying")
            and row.get("symbol") == mapping.underlying
        ),
        None,
    )
    if future is None:
        raise HitickError("matching future row is unavailable")
    future_time_ms = _positive_integer(
        future.get("timestamp_ms"), "future timestamp"
    )
    source_trading_day = _required_text(
        future, "trading_day", "future row"
    )
    session_trading_day = mapping.trading_day
    data_time_ms = min(vol_time_ms, quote_time_ms)
    market = MarketSnapshot(
        run_at_ms=run_at_ms,
        data_time_ms=data_time_ms,
        trading_day=session_trading_day,
        product_code=product.code,
        product_name=product.name,
        underlying=mapping.underlying,
        last_price=_positive_decimal(quote.get("last_price"), "underlying last price"),
        pre_settlement_price=_positive_decimal(
            quote.get("pre_settlement_price"), "underlying pre-settlement price"
        ),
        atm_iv=_nonnegative_decimal(vol.get("atm_iv"), "ATM IV"),
    )

    call_inflow = ZERO
    put_inflow = ZERO
    call_contract_count = 0
    put_contract_count = 0
    call_volume_delta = 0
    put_volume_delta = 0
    call_turnover_delta = ZERO
    put_turnover_delta = ZERO
    comparable_call_count = 0
    comparable_put_count = 0
    call_open_interest = 0
    put_open_interest = 0
    call_pre_open_interest = 0
    put_pre_open_interest = 0
    call_oi_count = 0
    put_oi_count = 0
    call_oi_baseline_complete = True
    put_oi_baseline_complete = True
    oi_by_strike: dict[Decimal, int] = {}
    states: list[ContractVolumeState] = []
    oi_changes: list[ContractOiChange] = []
    latest_allowed_time_ms = (
        run_at_ms if observed_at_ms is None else observed_at_ms
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("instrument_kind") not in ("OPTION", "option"):
            continue
        side = row.get("option_type")
        if side not in ("C", "P"):
            continue

        try:
            state = _contract_state(
                row,
                mapping,
                side,
                source_trading_day,
                session_trading_day,
            )
            if state.data_time_ms > latest_allowed_time_ms:
                raise HitickError("option timestamp is in the future")
        except HitickError:
            continue
        amount = cumulative_turnover(
            state.volume,
            state.average_price,
            state.last_price,
            state.multiplier,
        )
        previous = store.load_contract_state(state.symbol)
        comparable = (
            previous is not None
            and previous.trading_day == state.trading_day
            and previous.underlying == state.underlying
            and previous.side == state.side
            and previous.multiplier == state.multiplier
            and state.volume >= previous.volume
        )
        if comparable:
            delta_volume = state.volume - previous.volume
            delta_turnover = incremental_turnover(
                state.volume,
                state.average_price,
                state.last_price,
                previous.volume,
                previous.average_price,
                state.multiplier,
            )
            if side == "C":
                call_volume_delta += delta_volume
                call_turnover_delta += delta_turnover
                comparable_call_count += 1
            else:
                put_volume_delta += delta_volume
                put_turnover_delta += delta_turnover
                comparable_put_count += 1

        open_interest = _optional_nonnegative_integer(row.get("open_interest"))
        pre_open_interest = _optional_nonnegative_integer(
            row.get("pre_open_interest")
        )
        side_baseline_complete = (
            open_interest is not None and pre_open_interest is not None
        )
        if side == "C" and not side_baseline_complete:
            call_oi_baseline_complete = False
        if side == "P" and not side_baseline_complete:
            put_oi_baseline_complete = False
        if open_interest is not None:
            strike = _optional_nonnegative_decimal(row.get("strike_price"))
            if strike is not None and strike > ZERO:
                oi_by_strike[strike] = (
                    oi_by_strike.get(strike, 0) + open_interest
                )
                if pre_open_interest is not None:
                    oi_changes.append(ContractOiChange(
                        run_at_ms=run_at_ms,
                        data_time_ms=state.data_time_ms,
                        trading_day=session_trading_day,
                        product_code=product.code,
                        product_name=product.name,
                        underlying=mapping.underlying,
                        expire=mapping.expire,
                        symbol=state.symbol,
                        side=side,
                        strike=strike,
                        open_interest=open_interest,
                        pre_open_interest=pre_open_interest,
                        delta_open_interest=(
                            open_interest - pre_open_interest
                        ),
                        multiplier=state.multiplier,
                        option_last_price=(
                            state.last_price
                            if state.last_price > ZERO
                            else None
                        ),
                    ))
            if side == "C":
                call_open_interest += open_interest
                call_oi_count += 1
            else:
                put_open_interest += open_interest
                put_oi_count += 1
            if pre_open_interest is not None and side == "C":
                call_pre_open_interest += pre_open_interest
            elif pre_open_interest is not None:
                put_pre_open_interest += pre_open_interest
        states.append(state)
        if side == "C":
            call_inflow += amount
            call_contract_count += 1
        else:
            put_inflow += amount
            put_contract_count += 1

    if call_contract_count + put_contract_count == 0:
        raise HitickError("option chain has no valid call or put rows")

    flow = FlowSnapshot(
        run_at_ms=run_at_ms,
        data_time_ms=data_time_ms,
        trading_day=session_trading_day,
        product_code=product.code,
        underlying=mapping.underlying,
        call_inflow=call_inflow,
        put_inflow=put_inflow,
        net_inflow=call_inflow - put_inflow,
        call_contract_count=call_contract_count,
        put_contract_count=put_contract_count,
    )
    flow_baseline_ready = (
        comparable_call_count > 0 and comparable_put_count > 0
    )
    total_oi = call_open_interest + put_open_interest
    concentrations = tuple(
        OiConcentration(
            strike=strike,
            open_interest=open_interest,
            share=Decimal(open_interest) / Decimal(total_oi),
        )
        for strike, open_interest in sorted(
            oi_by_strike.items(), key=lambda item: (-item[1], item[0])
        )[:3]
    ) if total_oi > 0 else ()
    option_snapshot = OptionAnalyticsSnapshot(
        run_at_ms=run_at_ms,
        data_time_ms=data_time_ms,
        trading_day=session_trading_day,
        product_code=product.code,
        product_name=product.name,
        underlying=mapping.underlying,
        expire=mapping.expire,
        rr25=risk_reversal_25(vol.get("rows")),
        call_volume_delta=call_volume_delta,
        put_volume_delta=put_volume_delta,
        call_turnover_delta=call_turnover_delta,
        put_turnover_delta=put_turnover_delta,
        call_open_interest=call_open_interest,
        put_open_interest=put_open_interest,
        call_pre_open_interest=call_pre_open_interest,
        put_pre_open_interest=put_pre_open_interest,
        volume_pcr=(
            safe_ratio(put_volume_delta, call_volume_delta)
            if flow_baseline_ready else None
        ),
        turnover_pcr=(
            safe_ratio(put_turnover_delta, call_turnover_delta)
            if flow_baseline_ready else None
        ),
        oi_pcr=safe_ratio(put_open_interest, call_open_interest),
        oi_concentrations=concentrations,
        flow_baseline_ready=flow_baseline_ready,
        oi_baseline_ready=(
            call_oi_baseline_complete
            and put_oi_baseline_complete
            and call_oi_count > 0
            and put_oi_count > 0
        ),
        call_oi_baseline_ready=(
            call_oi_baseline_complete and call_oi_count > 0
        ),
        put_oi_baseline_ready=(
            put_oi_baseline_complete and put_oi_count > 0
        ),
    )
    return ProductCollection(
        market=market,
        flow=flow,
        contract_states=tuple(states),
        source_time_ms=(vol_time_ms, quote_time_ms),
        option_snapshot=option_snapshot,
        oi_changes=tuple(sorted(oi_changes, key=lambda item: item.symbol)),
    )


def _fresh_quote(
    vol: dict[str, Any], mapping: ContractMapping
) -> tuple[int, int, dict[str, Any]]:
    if not isinstance(vol, dict):
        raise HitickError("invalid volatility data")
    data_time_ms = _positive_integer(vol.get("data_time_ms"), "data time")
    quote = vol.get("underlying_quote")
    if not isinstance(quote, dict):
        raise HitickError("invalid underlying quote")
    quote_time_ms = _positive_integer(
        quote.get("timestamp_ms"), "underlying quote timestamp"
    )
    if quote.get("symbol") != mapping.underlying:
        raise HitickError("underlying quote does not match mapping")
    return data_time_ms, quote_time_ms, quote


def _contract_state(
    row: dict[str, Any],
    mapping: ContractMapping,
    side: str,
    source_trading_day: str,
    session_trading_day: str,
) -> ContractVolumeState:
    symbol = _required_text(row, "symbol", "option row")
    underlying_symbol = row.get("underlying_symbol")
    if (
        underlying_symbol is not None
        and underlying_symbol != mapping.underlying
    ) or (
        underlying_symbol is None
        and not symbol.startswith(f"{mapping.underlying}{side}")
    ):
        raise HitickError("option symbol does not match mapping")
    trading_day = _required_text(row, "trading_day", "option row")
    if trading_day != source_trading_day:
        raise HitickError("option trading day does not match future")
    data_time_ms = _positive_integer(
        row.get("timestamp_ms"), "option timestamp"
    )
    volume = row.get("volume")
    if not isinstance(volume, int) or isinstance(volume, bool) or volume < 0:
        raise HitickError("invalid option volume")

    average_price = _optional_nonnegative_decimal(row.get("average_price"))
    multiplier = _positive_decimal(row.get("multiplier"), "option multiplier")
    if multiplier != mapping.multiplier:
        raise HitickError("option multiplier does not match mapping")
    return ContractVolumeState(
        trading_day=session_trading_day,
        underlying=mapping.underlying,
        symbol=symbol,
        side=side,
        volume=volume,
        average_price=average_price,
        last_price=_nonnegative_decimal(row.get("last_price"), "option last price"),
        multiplier=multiplier,
        data_time_ms=data_time_ms,
    )


def _validate_mapping_identity(
    response: dict[str, Any], mapping: ContractMapping
) -> None:
    if not isinstance(response, dict):
        raise HitickError("invalid market data")
    if (
        "underlying" in response
        and response["underlying"] != mapping.underlying
    ):
        raise HitickError("market data underlying does not match mapping")
    for field in ("expire", "expire_date"):
        if field in response and response[field] != mapping.expire:
            raise HitickError("market data expiry does not match mapping")
    if "multiplier" in response:
        multiplier = _positive_decimal(
            response["multiplier"], "market data multiplier"
        )
        if multiplier != mapping.multiplier:
            raise HitickError("market data multiplier does not match mapping")


def _required_text(values: dict[str, Any], field: str, context: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HitickError(f"invalid {context} {field}")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise HitickError(f"invalid {field}")
    return value


def _positive_decimal(value: Any, field: str) -> Decimal:
    decimal = _decimal(value, field)
    if decimal <= ZERO:
        raise HitickError(f"invalid {field}")
    return decimal


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    decimal = _decimal(value, field)
    if decimal < ZERO:
        raise HitickError(f"invalid {field}")
    return decimal


def _optional_nonnegative_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        decimal = _decimal(value, "option average price")
    except HitickError:
        return None
    return decimal if decimal >= ZERO else None


def _optional_nonnegative_integer(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise HitickError(f"invalid {field}")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HitickError(f"invalid {field}") from None
    if not decimal.is_finite():
        raise HitickError(f"invalid {field}")
    return decimal
