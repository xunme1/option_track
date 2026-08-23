from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from decimal import Decimal
from decimal import InvalidOperation
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urlencode

from option_monitor.models import (
    ContractMapping,
    FuturesChangeQuote,
    ProductSpec,
)


FUTURES_BASE_URL = "https://futsseapi.eastmoney.com/list/market"
FUTURES_FIELDS = "dm,sc,name,p,o,zdf,vol,ccl"
FUTURES_TOKEN = "58b2fa8f54638b60b87d69b31969089c"
MAX_RESPONSE_BYTES = 1_000_000
MARKET_IDS = {
    "SHFE": "113",
    "DCE": "114",
    "CZCE": "115",
    "INE": "142",
    "CFFEX": "220",
    "GFEX": "225",
}
INDEX_FUTURES_PREFIXES = {
    "IO": "IF",
    "MO": "IM",
    "HO": "IH",
}


class EastmoneyError(RuntimeError):
    """A safe-to-log Eastmoney quote failure."""


class EastmoneyTransport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> bytes:
        raise NotImplementedError


class UrllibEastmoneyTransport:
    def get(self, url: str, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise EastmoneyError("Eastmoney quote request failed")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except EastmoneyError:
            raise
        except (
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ):
            raise EastmoneyError("Eastmoney quote request failed") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise EastmoneyError("invalid Eastmoney quote response")
        return body


class EastmoneyFuturesClient:
    def __init__(
        self,
        transport: EastmoneyTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ):
        self._transport = transport or UrllibEastmoneyTransport()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def fetch_main_quotes(
        self,
        products: Sequence[ProductSpec],
    ) -> dict[str, FuturesChangeQuote]:
        return self._fetch_grouped_quotes(products, _select_main_quote)

    def fetch_quotes(
        self,
        products: Sequence[ProductSpec],
        mappings: Mapping[str, ContractMapping],
    ) -> dict[str, FuturesChangeQuote]:
        mapped_products = tuple(
            product for product in products
            if product.code in mappings
        )
        return self._fetch_grouped_quotes(
            mapped_products,
            lambda product, rows, fetched_at_ms: _select_exact_quote(
                product,
                mappings[product.code],
                rows,
                fetched_at_ms,
            ),
        )

    def _fetch_grouped_quotes(
        self,
        products: Sequence[ProductSpec],
        selector: Callable[
            [ProductSpec, Sequence[object], int],
            FuturesChangeQuote | None,
        ],
    ) -> dict[str, FuturesChangeQuote]:
        products_by_exchange: dict[str, list[ProductSpec]] = {}
        for product in products:
            products_by_exchange.setdefault(product.exchange, []).append(product)

        quotes: dict[str, FuturesChangeQuote] = {}
        last_error: EastmoneyError | None = None
        successful_exchange_count = 0
        for exchange, exchange_products in products_by_exchange.items():
            try:
                exchange_quotes = self._fetch_exchange_quotes(
                    exchange, exchange_products, selector
                )
            except EastmoneyError as error:
                last_error = error
                continue
            successful_exchange_count += 1
            quotes.update(exchange_quotes)

        if successful_exchange_count == 0 and last_error is not None:
            raise last_error
        return quotes

    def _fetch_exchange_quotes(
        self,
        exchange: str,
        products: Sequence[ProductSpec],
        selector: Callable[
            [ProductSpec, Sequence[object], int],
            FuturesChangeQuote | None,
        ],
    ) -> dict[str, FuturesChangeQuote]:
        try:
            market_id = MARKET_IDS[exchange]
        except KeyError:
            raise EastmoneyError("invalid Eastmoney quote request") from None
        query = urlencode({
            "orderBy": "zdf",
            "sort": "desc",
            "pageSize": "500",
            "pageIndex": "0",
            "token": FUTURES_TOKEN,
            "field": FUTURES_FIELDS,
        })
        body = self._transport.get(
            f"{FUTURES_BASE_URL}/{market_id}?{query}",
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        if len(body) > MAX_RESPONSE_BYTES:
            raise EastmoneyError("invalid Eastmoney quote response")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            raise EastmoneyError("invalid Eastmoney quote response") from None
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("list"), list)
        ):
            raise EastmoneyError("invalid Eastmoney quote response")

        fetched_at_ms = self._clock_ms()
        return {
            product.code: quote
            for product in products
            if (
                quote := selector(product, document["list"], fetched_at_ms)
            ) is not None
        }

def _select_main_quote(
    product: ProductSpec,
    rows: Sequence[object],
    fetched_at_ms: int,
) -> FuturesChangeQuote | None:
    prefix = INDEX_FUTURES_PREFIXES.get(product.code, product.code)
    continuous_code = f"{prefix}m".upper()
    continuous_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("dm"), str)
        and row["dm"].upper() == continuous_code
    ]
    if len(continuous_rows) != 1:
        return None
    identity = _main_identity(continuous_rows[0])
    if identity is None:
        return None

    contract_pattern = re.compile(
        rf"{re.escape(prefix)}(?:\d{{3}}|\d{{4}})", re.IGNORECASE
    )
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("dm"), str)
        and contract_pattern.fullmatch(row["dm"]) is not None
        and _main_identity(row) == identity
    ]
    if len(matches) != 1:
        return None
    exact = matches[0]
    last_price, opening_price, volume, open_interest = identity
    return FuturesChangeQuote(
        product_code=product.code,
        underlying=exact["dm"],
        last_price=last_price,
        change_pct=(last_price - opening_price) / opening_price,
        source_time_ms=fetched_at_ms,
        volume=volume,
        open_interest=open_interest,
        data_source="eastmoney",
    )


def _select_exact_quote(
    product: ProductSpec,
    mapping: ContractMapping,
    rows: Sequence[object],
    fetched_at_ms: int,
) -> FuturesChangeQuote | None:
    if mapping.product_code != product.code:
        return None
    prefix = INDEX_FUTURES_PREFIXES.get(product.code, product.code)
    contract_pattern = re.compile(
        rf"{re.escape(prefix)}(?:\d{{3}}|\d{{4}})", re.IGNORECASE
    )
    if contract_pattern.fullmatch(mapping.underlying) is None:
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("dm"), str)
        and row["dm"].upper() == mapping.underlying.upper()
    ]
    if len(matches) != 1:
        return None
    identity = _main_identity(matches[0])
    if identity is None:
        return None
    last_price, opening_price, volume, open_interest = identity
    return FuturesChangeQuote(
        product_code=product.code,
        underlying=matches[0]["dm"],
        last_price=last_price,
        change_pct=(last_price - opening_price) / opening_price,
        source_time_ms=fetched_at_ms,
        volume=volume,
        open_interest=open_interest,
        data_source="eastmoney",
    )


def _main_identity(
    row: Mapping[str, object],
) -> tuple[Decimal, Decimal, int, int] | None:
    last_price = _finite_decimal(row.get("p"))
    opening_price = _finite_decimal(row.get("o"))
    volume = _nonnegative_integer(row.get("vol"))
    open_interest = _nonnegative_integer(row.get("ccl"))
    if (
        last_price is None
        or opening_price is None
        or opening_price <= 0
        or volume is None
        or open_interest is None
    ):
        return None
    return last_price, opening_price, volume, open_interest


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _nonnegative_integer(value: object) -> int | None:
    parsed = _finite_decimal(value)
    if parsed is None or parsed < 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)
