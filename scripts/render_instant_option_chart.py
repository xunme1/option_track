from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from option_monitor.collector import (
    MainOptionUnavailable,
    collect_product,
    resolve_mapping,
    resolve_nearest_option_mapping,
)
from option_monitor.config import load_env_file
from option_monitor.eastmoney_client import EastmoneyFuturesClient
from option_monitor.hitick_client import HitickClient
from option_monitor.instant_product_chart import (
    InstantProductChartData,
    render_instant_product_chart,
)
from option_monitor.rqdata_client import (
    PrimaryFallbackFuturesClient,
    RqdataFuturesClient,
)
from option_monitor.settings import PRODUCTS, SHANGHAI


class _EmptyStateStore:
    """Avoid treating an earlier monitoring run as input to an instant chart."""

    def load_contract_state(self, symbol: str):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect one product live and render a six-panel option PNG."
    )
    parser.add_argument(
        "--product",
        required=True,
        help="Supported Chinese product name or code, for example 甲醇 or MA.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Caller-selected destination PNG path; parent directories are created.",
    )
    parser.add_argument(
        "--contract",
        help=(
            "Optional specific futures contract, for example MA609 or IF2609. "
            "Without it, the current main contract is resolved."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="Repository root containing .env (default: script repository).",
    )
    parser.add_argument(
        "--trading-day",
        help="Optional YYYYMMDD used to resolve the option contract; defaults to Beijing today.",
    )
    args = parser.parse_args(argv)

    try:
        root = args.root.expanduser().resolve()
        product = _find_product(args.product)
        now = datetime.now(SHANGHAI)
        trading_day = _trading_day(args.trading_day, now)
        output = args.output.expanduser().resolve()
        if output.suffix.casefold() != ".png":
            parser.error("--output must end in .png")
        requested_contract = _contract(args.contract)
        load_env_file(root / ".env")
        token = os.environ.get("ORANGE_API_TOKEN", "").strip()
        if not token:
            raise ValueError("ORANGE_API_TOKEN is unavailable")

        hitick = HitickClient(token)
        price_client = PrimaryFallbackFuturesClient(
            RqdataFuturesClient(api_key=os.environ.get("RQDATA_API_KEY") or None),
            EastmoneyFuturesClient(),
        )
        resolved_at_ms = int(time.time() * 1000)
        if requested_contract is not None:
            # A directed request must never silently fall back to the main or
            # nearest contract: the chart has to describe precisely what was
            # asked for.
            mapping = resolve_mapping(
                hitick,
                product,
                trading_day,
                resolved_at_ms,
                requested_contract,
            )
        else:
            main_quote = price_client.fetch_main_quotes((product,)).get(product.code)
            if main_quote is None:
                raise RuntimeError("futures price quote is unavailable")
            try:
                mapping = resolve_mapping(
                    hitick,
                    product,
                    trading_day,
                    resolved_at_ms,
                    main_quote.underlying,
                )
            except MainOptionUnavailable:
                mapping = resolve_nearest_option_mapping(
                    hitick,
                    product,
                    trading_day,
                    resolved_at_ms,
                    main_quote.underlying,
                )
        basic = hitick.basic_by_expire(mapping.underlying, mapping.expire)
        vol = hitick.vol_by_underlying(
            mapping.underlying, mapping.expire, mapping.multiplier
        )
        run_at_ms = int(time.time() * 1000)
        collection = collect_product(
            product,
            mapping,
            basic,
            vol,
            _EmptyStateStore(),
            run_at_ms,
            run_at_ms,
        )
        if (
            collection.option_snapshot is not None
            and collection.option_snapshot.rr25 is None
            and isinstance(vol.get("chain_meta"), dict)
            and vol["chain_meta"].get("truncated") is True
        ):
            vol = hitick.vol_by_underlying(
                mapping.underlying,
                mapping.expire,
                mapping.multiplier,
                full_chain=True,
            )
            run_at_ms = int(time.time() * 1000)
            collection = collect_product(
                product,
                mapping,
                basic,
                vol,
                _EmptyStateStore(),
                run_at_ms,
                run_at_ms,
            )

        exact_quote = price_client.fetch_quotes(
            (product,), {product.code: mapping}
        ).get(product.code)
        baseline = _previous_rr25_baseline(
            root, product.code, mapping.underlying,
            collection.market.trading_day,
        )
        iv_baseline = _previous_iv_baseline(
            root, product.code, mapping.underlying,
            collection.market.trading_day,
        )
        current_rr25 = (
            collection.option_snapshot.rr25
            if collection.option_snapshot is not None else None
        )
        image = render_instant_product_chart(
            InstantProductChartData(
                product=product,
                mapping=mapping,
                collection=collection,
                futures_quote=exact_quote,
                rendered_at_ms=int(time.time() * 1000),
                rr25_change=(
                    current_rr25 - baseline[1]
                    if current_rr25 is not None and baseline is not None else None
                ),
                rr25_baseline_trading_day=(
                    baseline[0] if baseline is not None else None
                ),
                iv_change=(
                    collection.market.atm_iv - iv_baseline[1]
                    if iv_baseline is not None else None
                ),
                iv_baseline_trading_day=(
                    iv_baseline[0] if iv_baseline is not None else None
                ),
            ),
            output,
        )
    except SystemExit:
        raise
    except Exception:
        print("instant option chart failed", file=sys.stderr)
        return 1

    print(f"IMAGE_PATH={image}")
    print(f"PRODUCT_CODE={product.code}")
    print(f"UNDERLYING={mapping.underlying}")
    return 0


def _find_product(value: str):
    query = value.strip()
    if not query:
        raise ValueError("product is empty")
    exact = tuple(
        product
        for product in PRODUCTS
        if query.casefold() == product.code.casefold() or query == product.name
    )
    if len(exact) != 1:
        raise ValueError("unsupported product")
    return exact[0]


def _trading_day(value: str | None, now: datetime) -> str:
    candidate = now.strftime("%Y%m%d") if value is None else value.strip()
    if len(candidate) != 8 or not candidate.isdigit():
        raise ValueError("trading day must be YYYYMMDD")
    return candidate


def _contract(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().upper()
    if not candidate:
        raise ValueError("contract is empty")
    return candidate


def _previous_rr25_baseline(
    root: Path, product_code: str, underlying: str, trading_day: str
) -> tuple[str, Decimal] | None:
    """Read the previous daily RR25 close of the same contract, read-only.

    只匹配同一合约（underlying 一致）的历史收盘；查不到时返回 None，
    图上显示"——"，避免拿别的合约（比如主力）的基线冒充。
    """
    database = root / "state" / "option_monitor.sqlite3"
    if not database.is_file():
        return None
    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                """
                SELECT trading_day, rr25
                FROM daily_option_closes
                WHERE product_code = ? AND underlying = ? COLLATE NOCASE
                  AND trading_day < ?
                ORDER BY trading_day DESC
                LIMIT 1
                """,
                (product_code, underlying, trading_day),
            ).fetchone()
        if row is None or row[1] is None:
            return None
        return str(row[0]), Decimal(str(row[1]))
    except (OSError, sqlite3.Error, InvalidOperation):
        return None


def _previous_iv_baseline(
    root: Path, product_code: str, underlying: str, trading_day: str
) -> tuple[str, Decimal] | None:
    """Read the previous daily IV close of the same contract, read-only."""
    database = root / "state" / "option_monitor.sqlite3"
    if not database.is_file():
        return None
    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                """
                SELECT trading_day, atm_iv
                FROM daily_iv_closes
                WHERE product_code = ? AND underlying = ? COLLATE NOCASE
                  AND trading_day < ?
                ORDER BY trading_day DESC
                LIMIT 1
                """,
                (product_code, underlying, trading_day),
            ).fetchone()
        if row is None:
            return None
        return str(row[0]), Decimal(str(row[1]))
    except (OSError, sqlite3.Error, InvalidOperation):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
