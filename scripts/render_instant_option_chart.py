from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
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
        description="Collect one product live and render a four-panel option PNG."
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
        image = render_instant_product_chart(
            InstantProductChartData(
                product=product,
                mapping=mapping,
                collection=collection,
                futures_quote=exact_quote,
                rendered_at_ms=int(time.time() * 1000),
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


if __name__ == "__main__":
    raise SystemExit(main())
