from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol, Sequence

from option_monitor.models import (
    ContractMapping,
    FuturesChangeQuote,
    ProductSpec,
)


SHANGHAI = timezone(timedelta(hours=8))
INDEX_FUTURES_PREFIXES = {
    "IO": "IF",
    "MO": "IM",
    "HO": "IH",
}


class RqdataError(RuntimeError):
    """A safe-to-log RQData quote failure."""


@dataclass(frozen=True)
class RqdataBar:
    underlying: str
    last_price: Decimal
    opening_price: Decimal
    volume: int
    open_interest: int
    source_time_ms: int


class RqdataProvider(Protocol):
    def dominant_contract(self, symbol: str, as_of: date) -> str | None:
        raise NotImplementedError

    def quote_bars(
        self, contracts: Sequence[str], as_of: date
    ) -> dict[str, RqdataBar]:
        raise NotImplementedError


class RqdatacProvider:
    def __init__(
        self,
        api_key: str | None = None,
        module_loader: Callable[[], Any] | None = None,
    ):
        self._api_key = api_key.strip() if isinstance(api_key, str) else None
        self._module_loader = module_loader or (
            lambda: importlib.import_module("rqdatac")
        )
        self._module: Any | None = None

    def dominant_contract(self, symbol: str, as_of: date) -> str | None:
        module = self._client()
        try:
            result = module.futures.get_dominant(
                symbol.upper(), start_date=as_of, end_date=as_of
            )
            value = _last_series_value(result)
        except Exception:
            raise RqdataError("RQData dominant contract request failed") from None
        return value.upper() if value else None

    def quote_bars(
        self, contracts: Sequence[str], as_of: date
    ) -> dict[str, RqdataBar]:
        requested = tuple(dict.fromkeys(
            contract.upper() for contract in contracts if contract
        ))
        if not requested:
            return {}
        module = self._client()
        try:
            minute = module.get_price(
                list(requested),
                start_date=as_of,
                end_date=as_of,
                frequency="1m",
                fields=["close", "volume", "open_interest"],
                adjust_type="none",
            )
            daily = module.get_price(
                list(requested),
                start_date=as_of - timedelta(days=10),
                end_date=as_of,
                frequency="1d",
                fields=["open"],
                adjust_type="none",
            )
        except Exception:
            raise RqdataError("RQData futures quote request failed") from None

        bars: dict[str, RqdataBar] = {}
        for contract in requested:
            try:
                minute_rows = minute.xs(contract, level="order_book_id")
                daily_rows = daily.xs(contract, level="order_book_id")
                if len(minute_rows) == 0 or len(daily_rows) == 0:
                    continue
                last = minute_rows.iloc[-1]
                latest_daily = daily_rows.iloc[-1]
                opening_price = _positive_decimal(latest_daily.get("open"))
                last_price = _positive_decimal(last.get("close"))
                volume = _summed_nonnegative_integer(minute_rows["volume"])
                open_interest = _nonnegative_integer(last.get("open_interest"))
                source_time_ms = _timestamp_ms(minute_rows.index[-1])
                if (
                    last_price is None
                    or opening_price is None
                    or volume is None
                    or open_interest is None
                    or source_time_ms is None
                ):
                    continue
                bars[contract] = RqdataBar(
                    contract,
                    last_price,
                    opening_price,
                    volume,
                    open_interest,
                    source_time_ms,
                )
            except Exception:
                continue
        return bars

    def _client(self) -> Any:
        if self._module is None:
            try:
                module = self._module_loader()
                if self._api_key:
                    module.init(username="license", password=self._api_key)
                else:
                    module.init()
            except Exception:
                raise RqdataError("RQData initialization failed") from None
            self._module = module
        return self._module


class RqdataFuturesClient:
    def __init__(
        self,
        provider: RqdataProvider | None = None,
        date_provider: Callable[[], date] | None = None,
        api_key: str | None = None,
    ):
        self._provider = provider or RqdatacProvider(api_key=api_key)
        self._date_provider = date_provider or (
            lambda: datetime.now(SHANGHAI).date()
        )

    def fetch_main_quotes(
        self, products: Sequence[ProductSpec]
    ) -> dict[str, FuturesChangeQuote]:
        as_of = self._date_provider()
        underlyings: dict[str, str] = {}
        for product in products:
            symbol = INDEX_FUTURES_PREFIXES.get(
                product.code, product.code
            ).upper()
            try:
                underlying = self._provider.dominant_contract(symbol, as_of)
            except Exception:
                continue
            if underlying:
                underlyings[product.code] = underlying
        return self._quotes_for_underlyings(products, underlyings, as_of)

    def fetch_quotes(
        self,
        products: Sequence[ProductSpec],
        mappings: Mapping[str, ContractMapping],
    ) -> dict[str, FuturesChangeQuote]:
        as_of = self._date_provider()
        quote_underlyings = {
            product.code: mappings[product.code].underlying
            for product in products
            if product.code in mappings
        }
        provider_underlyings = {
            product.code: _rqdata_contract(
                product, quote_underlyings[product.code], as_of
            )
            for product in products
            if product.code in quote_underlyings
        }
        return self._quotes_for_underlyings(
            products,
            provider_underlyings,
            as_of,
            quote_underlyings=quote_underlyings,
        )

    def _quotes_for_underlyings(
        self,
        products: Sequence[ProductSpec],
        underlyings: Mapping[str, str],
        as_of: date,
        quote_underlyings: Mapping[str, str] | None = None,
    ) -> dict[str, FuturesChangeQuote]:
        try:
            bars = self._provider.quote_bars(
                tuple(underlyings.values()), as_of
            )
        except Exception:
            return {}
        bars_by_key = {
            underlying.casefold(): bar
            for underlying, bar in bars.items()
        }
        quotes: dict[str, FuturesChangeQuote] = {}
        for product in products:
            underlying = underlyings.get(product.code)
            if not underlying:
                continue
            bar = bars_by_key.get(underlying.casefold())
            if (
                bar is None
                or bar.opening_price <= 0
                or bar.last_price <= 0
                or bar.volume < 0
                or bar.open_interest < 0
                or bar.source_time_ms <= 0
            ):
                continue
            quotes[product.code] = FuturesChangeQuote(
                product_code=product.code,
                underlying=(
                    quote_underlyings.get(product.code, underlying)
                    if quote_underlyings is not None
                    else underlying
                ),
                last_price=bar.last_price,
                change_pct=(
                    bar.last_price / bar.opening_price - Decimal("1")
                ),
                source_time_ms=bar.source_time_ms,
                volume=bar.volume,
                open_interest=bar.open_interest,
                data_source="rqdata",
            )
        return quotes


class PrimaryFallbackFuturesClient:
    def __init__(self, primary: Any, fallback: Any):
        self.primary = primary
        self.fallback = fallback

    def fetch_main_quotes(
        self, products: Sequence[ProductSpec]
    ) -> dict[str, FuturesChangeQuote]:
        return self._merge(
            products,
            lambda requested: self.primary.fetch_main_quotes(requested),
            lambda requested: self.fallback.fetch_main_quotes(requested),
        )

    def fetch_quotes(
        self,
        products: Sequence[ProductSpec],
        mappings: Mapping[str, ContractMapping],
    ) -> dict[str, FuturesChangeQuote]:
        return self._merge(
            products,
            lambda requested: self.primary.fetch_quotes(
                requested,
                {
                    item.code: mappings[item.code]
                    for item in requested
                    if item.code in mappings
                },
            ),
            lambda requested: self.fallback.fetch_quotes(
                requested,
                {
                    item.code: mappings[item.code]
                    for item in requested
                    if item.code in mappings
                },
            ),
        )

    @staticmethod
    def _merge(
        products: Sequence[ProductSpec],
        primary_fetch: Callable[[Sequence[ProductSpec]], Any],
        fallback_fetch: Callable[[Sequence[ProductSpec]], Any],
    ) -> dict[str, FuturesChangeQuote]:
        try:
            primary = primary_fetch(products)
        except Exception:
            primary = {}
        quotes = dict(primary) if isinstance(primary, Mapping) else {}
        missing = tuple(
            product for product in products if product.code not in quotes
        )
        if not missing:
            return quotes
        try:
            fallback = fallback_fetch(missing)
        except Exception:
            fallback = {}
        if isinstance(fallback, Mapping):
            for product in missing:
                quote = fallback.get(product.code)
                if isinstance(quote, FuturesChangeQuote):
                    quotes[product.code] = quote
        return quotes


def _last_series_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    try:
        values = value.dropna()
        if len(values) == 0:
            return None
        result = values.iloc[-1]
    except Exception:
        return None
    return result.strip() if isinstance(result, str) and result.strip() else None


def _rqdata_contract(
    product: ProductSpec, underlying: str, as_of: date
) -> str:
    if product.exchange != "CZCE":
        return underlying.upper()
    prefix = product.code.upper()
    match = re.fullmatch(
        rf"{re.escape(prefix)}(\d{{3}})", underlying, re.IGNORECASE
    )
    if match is None:
        return underlying.upper()
    digits = match.group(1)
    year_digit = int(digits[0])
    decade_year = as_of.year - as_of.year % 10 + year_digit
    year = min(
        (decade_year - 10, decade_year, decade_year + 10),
        key=lambda candidate: (abs(candidate - as_of.year), candidate),
    )
    return f"{prefix}{year % 100:02d}{digits[1:]}"


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_integer(value: Any) -> int | None:
    parsed = _decimal(value)
    if parsed is None or parsed < 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _summed_nonnegative_integer(values: Any) -> int | None:
    try:
        total = values.sum()
    except Exception:
        return None
    return _nonnegative_integer(total)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _timestamp_ms(value: Any) -> int | None:
    try:
        converted = value.to_pydatetime()
    except Exception:
        converted = value
    if not isinstance(converted, datetime):
        return None
    if converted.tzinfo is None or converted.utcoffset() is None:
        converted = converted.replace(tzinfo=SHANGHAI)
    else:
        converted = converted.astimezone(SHANGHAI)
    return int(converted.timestamp() * 1000)
