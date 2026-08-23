from __future__ import annotations

import json
import os
import re
import stat
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, time as clock_time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from option_monitor.aliyun_oss_client import AliyunOssError
from option_monitor.anomaly_chart import (
    AnomalyChartError,
    render_anomaly_chart,
)
from option_monitor.anomaly_interpretation import (
    build_interpretation_results,
    render_anomaly_interpretation,
)
from option_monitor.anomaly_report import build_anomaly_chart_report
from option_monitor.anomaly_selection import select_anomaly_delivery
from option_monitor.collector import (
    MainOptionUnavailable,
    ProductCollection,
    collect_product,
    resolve_mapping,
    resolve_nearest_option_mapping,
)
from option_monitor.dingtalk_alert import build_markdown_payload
from option_monitor.evaluator import (
    evaluate_option_anomaly,
    evaluate_triggers,
)
from option_monitor.hitick_client import HitickError
from option_monitor.iv_chart import render_iv_chart
from option_monitor.models import (
    ContractMapping,
    DailyIvClose,
    DailyMarketClose,
    DailyOptionClose,
    FuturesChangeQuote,
    ProductSpec,
)
from option_monitor.openvlab_snapshot import OpenVlabSnapshotError
from option_monitor.monitor_messages import (
    build_anomaly_chart_markdown,
)
from option_monitor.settings import (
    PRODUCTS,
    SHANGHAI,
    MonitorSettings,
    expected_open_products,
)
from option_monitor.storage import MonitorStore


DAY_MS = 86_400_000
SLOT_MINUTES = 5
RETRY_DELAYS = (0.5, 1.5)
MANUAL_CLOSE_FRESH_WINDOW_MS = 8 * 60 * 60 * 1000
PRICE_SOURCE_CLOCK_SKEW_MS = 5 * 1000
_TIMESTAMP_FIELDS = ("timestamp_ms", "data_time_ms", "time_ms")
_SERIES_FIELDS = ("points", "items", "rows", "data")


@dataclass(frozen=True)
class OutboxMessage:
    kind: Literal["alerts", "hourly", "service"]
    delivery_key: str
    payload_path: Path


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    messages: tuple[OutboxMessage, ...]
    coverage_ratio: Decimal
    missing_products: tuple[str, ...]


class MonitorRunner:
    def __init__(
        self,
        settings: MonitorSettings,
        store: MonitorStore,
        client: Any,
        price_client: Any,
        products: Sequence[ProductSpec] = PRODUCTS,
        sleeper: Callable[[float], None] = time.sleep,
        image_uploader: Any | None = None,
        local_only: bool = False,
        chart_renderer: Callable[[Any, Path], Path] = render_iv_chart,
        anomaly_chart_renderer: Callable[
            [Any, Path], Path
        ] = render_anomaly_chart,
        openvlab_snapshotter: Any | None = None,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.price_client = price_client
        self.products = tuple(products)
        self._products_by_code = {
            product.code: product for product in self.products
        }
        self.sleeper = sleeper
        self.image_uploader = image_uploader
        self.local_only = local_only
        self.chart_renderer = chart_renderer
        self.anomaly_chart_renderer = anomaly_chart_renderer
        self.openvlab_snapshotter = openvlab_snapshotter

    def run(
        self,
        now: datetime,
        *,
        force_anomaly_report: bool = False,
        force_all_products: bool = False,
        require_full_coverage: bool = False,
    ) -> RunManifest:
        if require_full_coverage and not (
            force_anomaly_report and force_all_products
        ):
            raise HitickError(
                "full coverage requires forced all-product report"
            )
        observed_now = _as_beijing(now)
        slot_now = _floor_slot(observed_now)
        run_id = slot_now.strftime("%Y%m%dT%H%M%S%z")
        run_at_ms = int(slot_now.timestamp() * 1000)
        observed_at_ms = int(observed_now.timestamp() * 1000)
        observed_at_monotonic = time.monotonic()
        trading_day = _session_trading_day(observed_now)
        run_directory = self.settings.outbox_root / run_id
        manifest_path = run_directory / "manifest.json"

        _assert_safe_run_directory(
            self.settings.root,
            self.settings.outbox_root,
            run_directory,
            run_id,
            require_exists=False,
        )
        ready_manifest = _load_ready_manifest(
            manifest_path, run_directory, run_id
        )
        if (
            ready_manifest is not None
            and ready_manifest.coverage_ratio == Decimal("1")
            and not force_anomaly_report
            and not force_all_products
        ):
            return ready_manifest

        self.store.record_run(run_id, run_at_ms, None, "running", None, None)
        try:
            run_directory.mkdir(parents=True, exist_ok=True)
            _assert_safe_run_directory(
                self.settings.root,
                self.settings.outbox_root,
                run_directory,
                run_id,
                require_exists=True,
            )
            _clean_run_directory(
                self.settings.root,
                self.settings.outbox_root,
                run_directory,
                run_id,
            )

            expected = (
                tuple(product.code for product in self.products)
                if force_all_products
                else self._expected_products(observed_now)
            )
            main_quotes = self._load_main_quotes(
                expected,
                observed_at_ms,
                observed_at_monotonic,
                max_age_ms=(
                    MANUAL_CLOSE_FRESH_WINDOW_MS
                    if force_all_products
                    else None
                ),
                reject_future=require_full_coverage,
            )
            mappings = self._load_mappings(
                trading_day,
                run_at_ms,
                frozenset(expected),
                main_quotes,
            )
            price_quotes = self._load_contract_quotes(
                expected,
                mappings,
                main_quotes,
                observed_at_ms,
                observed_at_monotonic,
                max_age_ms=(
                    MANUAL_CLOSE_FRESH_WINDOW_MS
                    if force_all_products
                    else None
                ),
                reject_future=require_full_coverage,
            )
            mappings = {
                code: mapping
                for code, mapping in mappings.items()
                if code in price_quotes
                and _same_underlying(
                    price_quotes[code].underlying,
                    mapping.underlying,
                    self._products_by_code.get(code),
                )
            }
            restored = self._load_exact_collections(
                run_at_ms,
                frozenset(expected),
                mappings,
                tolerance_ms=(
                    MANUAL_CLOSE_FRESH_WINDOW_MS
                    if force_all_products
                    else 0
                ),
                allow_prior_session=force_all_products,
            )
            collections = dict(restored)
            for _ in range(max(1, self.settings.max_collection_passes)):
                missing_mappings = {
                    code: mapping
                    for code, mapping in mappings.items()
                    if code not in collections
                }
                if not missing_mappings:
                    break
                collections.update(
                    self._collect_products(
                        missing_mappings,
                        run_at_ms,
                        observed_at_ms,
                        observed_at_monotonic,
                        fresh_window_ms=(
                            MANUAL_CLOSE_FRESH_WINDOW_MS
                            if force_all_products
                            else self.settings.fresh_window_ms
                        ),
                        allow_unchanged=force_all_products,
                    )
                )
            authoritative_mappings = self._authoritative_mappings(
                collections, mappings, observed_at_ms
            )
            histories = self._ensure_histories(
                collections, authoritative_mappings, observed_now
            )

            collected_codes = frozenset(collections)
            missing_products = tuple(
                code for code in expected if code not in collected_codes
            )
            coverage_ratio = (
                Decimal(len(expected) - len(missing_products)) / Decimal(len(expected))
                if expected
                else Decimal("1")
            )

            with self.store.transaction():
                for mapping in authoritative_mappings.values():
                    if mapping.trading_day != trading_day:
                        self.store.save_mapping(mapping)
                for product in self.products:
                    collection = collections.get(product.code)
                    if collection is None or product.code in restored:
                        continue
                    self.store.save_market_snapshot(collection.market)
                    self.store.save_flow_snapshot(collection.flow)
                    if collection.option_snapshot is not None:
                        self.store.save_option_snapshot(
                            collection.option_snapshot
                        )
                    self.store.save_contract_oi_changes(
                        collection.oi_changes
                    )
                    self.store.save_contract_states(collection.contract_states)
                if _is_close_capture_time(observed_now):
                    self._save_daily_closes(collections)
                self.store.prune(
                    observed_at_ms - self.settings.retention_days * DAY_MS
                )

            if require_full_coverage and missing_products:
                raise HitickError("full coverage unavailable")

            messages = self._build_messages(
                run_id=run_id,
                run_at_ms=run_at_ms,
                now=slot_now,
                collections=collections,
                histories=histories,
                price_quotes=price_quotes,
                expected=expected,
                coverage_ratio=coverage_ratio,
                missing_products=missing_products,
                run_directory=run_directory,
                force_anomaly_report=force_anomaly_report,
            )
            manifest = RunManifest(
                run_id=run_id,
                messages=tuple(messages),
                coverage_ratio=coverage_ratio,
                missing_products=missing_products,
            )
            _atomic_write_json(manifest_path, _manifest_document(manifest))
            self.store.record_run(
                run_id,
                run_at_ms,
                int(time.time() * 1000),
                "ready",
                coverage_ratio,
                None,
            )
            return manifest
        except BaseException as error:
            _cleanup_failed_run_files(
                self.settings.root,
                self.settings.outbox_root,
                run_directory,
                run_id,
            )
            try:
                self.store.record_run(
                    run_id,
                    run_at_ms,
                    int(time.time() * 1000),
                    "failed",
                    None,
                    _safe_error_summary(error),
                )
            except Exception:
                pass
            raise

    def _load_mappings(
        self,
        trading_day: str,
        run_at_ms: int,
        expected_codes: frozenset[str],
        price_quotes: Mapping[str, FuturesChangeQuote],
    ) -> dict[str, ContractMapping]:
        mappings: dict[str, ContractMapping] = {}
        for product in self.products:
            if product.code not in expected_codes:
                continue
            quote = price_quotes.get(product.code)
            if quote is None:
                continue
            try:
                mapping = self.store.load_mapping(trading_day, product.code)
                if (
                    mapping is None
                    or not _same_underlying(
                        mapping.underlying, quote.underlying, product
                    )
                    or mapping.expire <= trading_day
                ):
                    try:
                        mapping = resolve_mapping(
                            self.client,
                            product,
                            trading_day,
                            run_at_ms,
                            quote.underlying,
                        )
                    except MainOptionUnavailable:
                        mapping = resolve_nearest_option_mapping(
                            self.client,
                            product,
                            trading_day,
                            run_at_ms,
                            quote.underlying,
                        )
                    self.store.save_mapping(mapping)
            except Exception:
                continue
            mappings[product.code] = mapping
        return mappings

    def _load_exact_collections(
        self,
        run_at_ms: int,
        expected_codes: frozenset[str],
        mappings: Mapping[str, ContractMapping],
        *,
        tolerance_ms: int = 0,
        allow_prior_session: bool = False,
    ) -> dict[str, ProductCollection]:
        collections: dict[str, ProductCollection] = {}
        for product in self.products:
            mapping = mappings.get(product.code)
            if product.code not in expected_codes or mapping is None:
                continue
            market = (
                self.store.market_snapshot_near(
                    product.code, run_at_ms, tolerance_ms
                )
                if tolerance_ms > 0
                else self.store.market_snapshot_at(product.code, run_at_ms)
            )
            snapshot_run_at_ms = (
                market.run_at_ms if market is not None else run_at_ms
            )
            option_snapshot = self.store.option_snapshot_at(
                product.code, snapshot_run_at_ms
            )
            oi_changes = self.store.contract_oi_changes_at(
                product.code, snapshot_run_at_ms
            )
            flow = self.store.flow_snapshot_at(
                product.code, snapshot_run_at_ms
            )
            if (
                market is None
                or flow is None
                or option_snapshot is None
                or market.run_at_ms > run_at_ms
                or (
                    market.trading_day != mapping.trading_day
                    and not allow_prior_session
                )
                or market.underlying != mapping.underlying
                or flow.underlying != mapping.underlying
                or option_snapshot.underlying != mapping.underlying
                or any(
                    row.underlying != mapping.underlying
                    for row in oi_changes
                )
            ):
                continue
            collections[product.code] = ProductCollection(
                market=market,
                flow=flow,
                contract_states=(),
                source_time_ms=(),
                option_snapshot=option_snapshot,
                oi_changes=oi_changes,
            )
        return collections

    def _collect_products(
        self,
        mappings: dict[str, ContractMapping],
        run_at_ms: int,
        observed_at_ms: int,
        observed_at_monotonic: float,
        fresh_window_ms: int,
        allow_unchanged: bool = False,
    ) -> dict[str, ProductCollection]:
        previous_data_times = {
            code: self._latest_data_time(
                code, mapping.underlying, run_at_ms
            )
            for code, mapping in mappings.items()
        }
        results: dict[str, ProductCollection] = {}
        worker_count = max(
            1, min(self.settings.max_workers, len(mappings) or 1)
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    self._collect_one,
                    product,
                    mappings[product.code],
                    run_at_ms,
                    observed_at_ms,
                    observed_at_monotonic,
                ): product.code
                for product in self.products
                if product.code in mappings
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    collection = future.result()
                except Exception:
                    continue
                current_observed_at_ms = _progressed_observed_at_ms(
                    observed_at_ms, observed_at_monotonic
                )
                if not self._is_fresh(
                    collection,
                    current_observed_at_ms,
                    previous_data_times[code],
                    fresh_window_ms,
                    allow_unchanged=allow_unchanged,
                ):
                    continue
                results[code] = collection
        return results

    def _collect_one(
        self,
        product: ProductSpec,
        mapping: ContractMapping,
        run_at_ms: int,
        observed_at_ms: int,
        observed_at_monotonic: float,
    ) -> ProductCollection:
        basic = self._request_with_retry(
            lambda: self.client.basic_by_expire(mapping.underlying, mapping.expire)
        )
        vol = self._request_with_retry(
            lambda: self.client.vol_by_underlying(
                mapping.underlying, mapping.expire, mapping.multiplier
            )
        )
        current_observed_at_ms = _progressed_observed_at_ms(
            observed_at_ms, observed_at_monotonic
        )
        collection = collect_product(
            product,
            mapping,
            basic,
            vol,
            self.store,
            run_at_ms,
            current_observed_at_ms,
        )
        chain_meta = vol.get("chain_meta") if isinstance(vol, dict) else None
        if (
            collection.option_snapshot is not None
            and collection.option_snapshot.rr25 is None
            and isinstance(chain_meta, dict)
            and chain_meta.get("truncated") is True
        ):
            full_vol = self._request_with_retry(
                lambda: self.client.vol_by_underlying(
                    mapping.underlying,
                    mapping.expire,
                    mapping.multiplier,
                    full_chain=True,
                )
            )
            current_observed_at_ms = _progressed_observed_at_ms(
                observed_at_ms, observed_at_monotonic
            )
            collection = collect_product(
                product,
                mapping,
                basic,
                full_vol,
                self.store,
                run_at_ms,
                current_observed_at_ms,
            )
        return collection

    def _request_with_retry(self, request: Callable[[], Any]) -> Any:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return request()
            except Exception:
                if attempt == len(RETRY_DELAYS):
                    raise
                self.sleeper(RETRY_DELAYS[attempt])
        raise AssertionError("unreachable retry state")

    def _latest_data_time(
        self,
        product_code: str,
        underlying: str,
        run_at_ms: int,
    ) -> int | None:
        previous = self.store.market_snapshot_near(
            product_code, run_at_ms, max(run_at_ms, 1)
        )
        if previous is None or previous.underlying != underlying:
            return None
        return previous.data_time_ms

    def _is_fresh(
        self,
        collection: ProductCollection,
        observed_at_ms: int,
        previous_data_time_ms: int | None,
        fresh_window_ms: int | None = None,
        *,
        allow_unchanged: bool = False,
    ) -> bool:
        timestamps = (
            collection.source_time_ms
            or (collection.market.data_time_ms,)
        )
        if any(
            observed_at_ms - timestamp < 0
            or observed_at_ms - timestamp > (
                self.settings.fresh_window_ms
                if fresh_window_ms is None
                else fresh_window_ms
            )
            for timestamp in timestamps
        ):
            return False
        return (
            allow_unchanged
            or previous_data_time_ms is None
            or collection.market.data_time_ms > previous_data_time_ms
        )

    def _authoritative_mappings(
        self,
        collections: dict[str, ProductCollection],
        mappings: dict[str, ContractMapping],
        resolved_at_ms: int,
    ) -> dict[str, ContractMapping]:
        authoritative = dict(mappings)
        for code, collection in collections.items():
            mapping = mappings.get(code)
            if (
                mapping is not None
                and mapping.trading_day != collection.market.trading_day
            ):
                authoritative[code] = replace(
                    mapping,
                    trading_day=collection.market.trading_day,
                    resolved_at_ms=resolved_at_ms,
                )
        return authoritative

    def _ensure_histories(
        self,
        collections: dict[str, ProductCollection],
        mappings: dict[str, ContractMapping],
        now: datetime,
    ) -> dict[str, list[DailyIvClose]]:
        histories: dict[str, list[DailyIvClose]] = {}
        for code, collection in collections.items():
            trading_day = collection.market.trading_day
            existing_iv_history = _prior_iv_closes(
                self.store, code, trading_day
            )
            mapping = mappings.get(code)
            if mapping is None:
                histories[code] = existing_iv_history
                continue
            try:
                market_closes = ensure_daily_market_history(
                    self.store,
                    self.client,
                    mapping,
                    now,
                    trading_day=trading_day,
                )
                histories[code] = (
                    existing_iv_history
                    if len(existing_iv_history) >= 10
                    else _iv_history_for_alert(
                        self.store, code, trading_day, market_closes
                    )
                )
            except Exception:
                histories[code] = (
                    existing_iv_history
                    if len(existing_iv_history) >= 10
                    else _iv_history_for_alert(
                        self.store,
                        code,
                        trading_day,
                        _prior_market_closes(
                            self.store, code, trading_day
                        ),
                    )
                )
        return histories

    def _expected_products(self, now: datetime) -> tuple[str, ...]:
        configured_codes = frozenset(product.code for product in self.products)
        return tuple(
            code for code in expected_open_products(now) if code in configured_codes
        )

    def _load_main_quotes(
        self,
        expected: tuple[str, ...],
        observed_at_ms: int,
        observed_at_monotonic: float,
        max_age_ms: int | None = None,
        reject_future: bool = False,
    ) -> dict[str, FuturesChangeQuote]:
        expected_codes = frozenset(expected)
        products = tuple(
            product
            for product in self.products
            if product.code in expected_codes
        )
        if not products:
            return {}
        received: dict[str, Any] = {}
        remaining = products
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                batch = self._request_with_retry(
                    lambda requested=remaining: (
                        self.price_client.fetch_main_quotes(requested)
                    )
                )
            except Exception:
                batch = {}
            if isinstance(batch, Mapping):
                received.update(batch)
            remaining = tuple(
                product for product in remaining
                if product.code not in received
            )
            if not remaining:
                break
            if attempt < len(RETRY_DELAYS):
                self.sleeper(RETRY_DELAYS[attempt])

        current_observed_at_ms = _progressed_observed_at_ms(
            observed_at_ms, observed_at_monotonic
        )
        product_by_code = {product.code: product for product in products}
        quotes: dict[str, FuturesChangeQuote] = {}
        for code in expected:
            quote = received.get(code)
            product = product_by_code.get(code)
            if (
                product is None
                or not isinstance(quote, FuturesChangeQuote)
                or quote.product_code != code
                or not quote.underlying
            ):
                continue
            age_ms = current_observed_at_ms - quote.source_time_ms
            quote_max_age_ms = (
                max_age_ms
                if max_age_ms is not None
                else (
                    self.settings.cffex_price_quote_max_age_ms
                    if product.exchange == "CFFEX"
                    else self.settings.price_quote_max_age_ms
                )
            )
            minimum_age_ms = (
                0 if reject_future else -PRICE_SOURCE_CLOCK_SKEW_MS
            )
            if minimum_age_ms <= age_ms <= quote_max_age_ms:
                quotes[code] = quote
        return quotes

    def _load_contract_quotes(
        self,
        expected: tuple[str, ...],
        mappings: Mapping[str, ContractMapping],
        main_quotes: Mapping[str, FuturesChangeQuote],
        observed_at_ms: int,
        observed_at_monotonic: float,
        max_age_ms: int | None = None,
        reject_future: bool = False,
    ) -> dict[str, FuturesChangeQuote]:
        quotes = {
            code: replace(quote, underlying=mappings[code].underlying)
            for code, quote in main_quotes.items()
            if code in mappings
            and _same_underlying(
                mappings[code].underlying,
                quote.underlying,
                self._products_by_code.get(code),
            )
        }
        fallback_mappings = {
            code: mapping
            for code, mapping in mappings.items()
            if code not in quotes
        }
        if not fallback_mappings:
            return quotes

        expected_codes = frozenset(fallback_mappings)
        products = tuple(
            product for product in self.products
            if product.code in expected_codes
        )
        try:
            received = self._request_with_retry(
                lambda: self.price_client.fetch_quotes(
                    products, fallback_mappings
                )
            )
        except Exception:
            return quotes
        if not isinstance(received, Mapping):
            return quotes

        current_observed_at_ms = _progressed_observed_at_ms(
            observed_at_ms, observed_at_monotonic
        )
        product_by_code = {product.code: product for product in products}
        for code in expected:
            mapping = fallback_mappings.get(code)
            quote = received.get(code)
            product = product_by_code.get(code)
            if (
                mapping is None
                or product is None
                or not isinstance(quote, FuturesChangeQuote)
                or quote.product_code != code
                or not _same_underlying(
                    quote.underlying, mapping.underlying, product
                )
            ):
                continue
            age_ms = current_observed_at_ms - quote.source_time_ms
            quote_max_age_ms = (
                max_age_ms
                if max_age_ms is not None
                else (
                    self.settings.cffex_price_quote_max_age_ms
                    if product.exchange == "CFFEX"
                    else self.settings.price_quote_max_age_ms
                )
            )
            minimum_age_ms = (
                0 if reject_future else -PRICE_SOURCE_CLOCK_SKEW_MS
            )
            if minimum_age_ms <= age_ms <= quote_max_age_ms:
                quotes[code] = replace(
                    quote, underlying=mapping.underlying
                )
        return quotes

    def _save_daily_closes(
        self, collections: dict[str, ProductCollection]
    ) -> None:
        for code, collection in collections.items():
            candidate = DailyIvClose(
                trading_day=collection.market.trading_day,
                product_code=code,
                data_time_ms=collection.market.data_time_ms,
                atm_iv=collection.market.atm_iv,
            )
            existing = next(
                (
                    close
                    for close in self.store.daily_iv_closes(code, 10)
                    if close.trading_day == candidate.trading_day
                ),
                None,
            )
            if existing is None or candidate.data_time_ms > existing.data_time_ms:
                self.store.save_daily_iv_close(candidate)
            market_candidate = DailyMarketClose(
                trading_day=collection.market.trading_day,
                product_code=code,
                data_time_ms=collection.market.data_time_ms,
                close_price=collection.market.last_price,
                atm_iv=collection.market.atm_iv,
            )
            existing_market = next(
                (
                    close
                    for close in self.store.daily_market_closes(code, 10)
                    if close.trading_day == market_candidate.trading_day
                ),
                None,
            )
            if (
                existing_market is None
                or market_candidate.data_time_ms
                > existing_market.data_time_ms
            ):
                self.store.save_daily_market_close(market_candidate)
            option_snapshot = collection.option_snapshot
            if option_snapshot is None or option_snapshot.rr25 is None:
                continue
            option_candidate = DailyOptionClose(
                trading_day=collection.market.trading_day,
                product_code=code,
                data_time_ms=option_snapshot.data_time_ms,
                rr25=option_snapshot.rr25,
            )
            existing_option = next(
                (
                    close
                    for close in self.store.daily_option_closes(code, 32)
                    if close.trading_day == option_candidate.trading_day
                ),
                None,
            )
            if (
                existing_option is None
                or option_candidate.data_time_ms
                > existing_option.data_time_ms
            ):
                self.store.save_daily_option_close(option_candidate)

    def _build_messages(
        self,
        *,
        run_id: str,
        run_at_ms: int,
        now: datetime,
        collections: dict[str, ProductCollection],
        histories: dict[str, list[DailyIvClose]],
        price_quotes: Mapping[str, FuturesChangeQuote],
        expected: tuple[str, ...],
        coverage_ratio: Decimal,
        missing_products: tuple[str, ...],
        run_directory: Path,
        force_anomaly_report: bool,
    ) -> list[OutboxMessage]:
        triggers = []
        anomalies = []
        option_histories: dict[str, tuple[DailyOptionClose, ...]] = {}
        for product in self.products:
            collection = collections.get(product.code)
            if collection is None:
                continue
            product_triggers = evaluate_triggers(
                collection.market,
                collection.flow,
                price_quotes.get(product.code),
                [close.atm_iv for close in histories.get(product.code, [])],
                self.settings.price_alert_threshold,
                self.settings.iv_mean_multiplier,
            )
            triggers.extend(
                trigger
                for trigger in product_triggers
                if trigger.category != "flow"
            )
            option_snapshot = collection.option_snapshot
            if option_snapshot is None:
                continue
            prior_markets = [
                close
                for close in self.store.daily_market_closes(
                    product.code, 64
                )
                if close.trading_day < collection.market.trading_day
            ][-11:]
            prior_options = [
                close
                for close in self.store.daily_option_closes(
                    product.code, 64
                )
                if close.trading_day < collection.market.trading_day
            ][-11:]
            option_histories[product.code] = tuple(prior_options)
            anomaly = evaluate_option_anomaly(
                collection.market,
                option_snapshot,
                price_quotes.get(product.code),
                prior_markets,
                [
                    close.atm_iv
                    for close in histories.get(product.code, [])
                ],
                prior_options,
                self.settings.iv_mean_multiplier,
                self.settings.skew_mean_multiplier,
            )
            if anomaly is not None:
                anomalies.append(anomaly)
        anomaly_codes = frozenset(
            anomaly.product_code for anomaly in anomalies
        )
        triggers = [
            trigger
            for trigger in triggers
            if not (
                trigger.category == "iv"
                and trigger.product_code in anomaly_codes
            )
        ]
        trigger_priority = {
            ("price", "warning"): 0,
            ("iv", "warning"): 1,
        }
        triggers.sort(
            key=lambda trigger: trigger_priority.get(
                (trigger.category, trigger.severity), 4
            )
        )

        payloads: list[tuple[Literal["alerts"], dict[str, Any]]] = []
        if force_anomaly_report or now.minute in (0, 15, 30):
            iv_history_values = {
                code: tuple(
                    close.atm_iv for close in histories.get(code, ())
                )
                for code in collections
            }
            anomaly_report = build_anomaly_chart_report(
                run_at_ms=run_at_ms,
                collections=collections,
                price_quotes=price_quotes,
                triggers=triggers,
                anomalies=anomalies,
                iv_histories=iv_history_values,
                option_histories=option_histories,
                expected_count=len(expected),
            )
            if anomaly_report is not None:
                delivery_report = anomaly_report
                try:
                    results = build_interpretation_results(
                        report=anomaly_report,
                        collections=collections,
                        price_quotes=price_quotes,
                        iv_histories=iv_history_values,
                        option_histories=option_histories,
                        product_names={
                            product.code: product.name
                            for product in self.products
                        },
                    )
                    selection = select_anomaly_delivery(
                        anomaly_report, results
                    )
                    delivery_report = replace(
                        anomaly_report,
                        cards=selection.image_cards,
                    )
                    interpretation = render_anomaly_interpretation(
                        results, selection.text_codes
                    )
                except Exception:
                    interpretation = (
                        "## 异常解读\n\n"
                        "解读生成失败，本次长图仍可正常查看。"
                    )
                if self.local_only:
                    try:
                        anomaly_path = (
                            run_directory
                            / f"anomaly-chart-{run_id}.png"
                        ).resolve()
                        self.anomaly_chart_renderer(
                            delivery_report, anomaly_path
                        )
                    except AnomalyChartError:
                        pass
                    return payloads
                anomaly_image_url = None
                anomaly_chart_failed = self.image_uploader is None
                if self.image_uploader is not None:
                    try:
                        anomaly_path = (
                            run_directory
                            / f"anomaly-chart-{run_id}.png"
                        ).resolve()
                        self.anomaly_chart_renderer(
                            delivery_report, anomaly_path
                        )
                        anomaly_image_url = (
                            self.image_uploader.upload_png(anomaly_path)
                        )
                    except (AnomalyChartError, AliyunOssError):
                        anomaly_chart_failed = True
                openvlab_image_url = None
                openvlab_failed = None
                if _is_openvlab_snapshot_time(now):
                    openvlab_failed = True
                    if (
                        self.image_uploader is not None
                        and self.openvlab_snapshotter is not None
                    ):
                        try:
                            openvlab_path = (
                                run_directory
                                / f"openvlab-ranking-{run_id}.png"
                            ).resolve()
                            self.openvlab_snapshotter.capture(
                                openvlab_path, now
                            )
                            openvlab_image_url = (
                                self.image_uploader.upload_png(openvlab_path)
                            )
                            openvlab_failed = False
                        except (OpenVlabSnapshotError, AliyunOssError):
                            openvlab_failed = True
                payloads.append((
                    "alerts",
                    build_markdown_payload(
                        "期权监控 异常长图",
                        build_anomaly_chart_markdown(
                            delivery_report,
                            image_url=anomaly_image_url,
                            chart_failed=anomaly_chart_failed,
                            openvlab_image_url=openvlab_image_url,
                            openvlab_failed=openvlab_failed,
                            interpretation_markdown=interpretation,
                            image_public_host=(
                                f"{self.settings.aliyun_oss_bucket}."
                                f"oss-{self.settings.aliyun_oss_region}."
                                "aliyuncs.com"
                            ),
                            image_prefix=self.settings.aliyun_oss_prefix,
                        ),
                    ),
                ))

        messages: list[OutboxMessage] = []
        for kind, payload in payloads:
            payload_path = (run_directory / f"{kind}.json").resolve()
            _atomic_write_json(payload_path, payload)
            messages.append(OutboxMessage(
                kind=kind,
                delivery_key=f"monitor:{run_id}:{kind}",
                payload_path=payload_path,
            ))
        return messages

def _load_ready_manifest(
    manifest_path: Path,
    run_directory: Path,
    expected_run_id: str,
) -> RunManifest | None:
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        if manifest_path.stat().st_size > 1_000_000:
            return None
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("status") != "ready"
            or document.get("run_id") != expected_run_id
        ):
            return None
        coverage_ratio = Decimal(str(document.get("coverage_ratio")))
        if (
            not coverage_ratio.is_finite()
            or coverage_ratio < 0
            or coverage_ratio > 1
        ):
            return None
        raw_missing = document.get("missing_products")
        raw_messages = document.get("messages")
        if (
            not isinstance(raw_missing, list)
            or not all(isinstance(code, str) for code in raw_missing)
            or not isinstance(raw_messages, list)
        ):
            return None

        root = _absolute_lexical(run_directory)
        messages: list[OutboxMessage] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                return None
            kind = raw_message.get("kind")
            delivery_key = raw_message.get("delivery_key")
            raw_payload_path = raw_message.get("payload_path")
            if (
                kind not in ("alerts", "hourly", "service")
                or delivery_key != f"monitor:{expected_run_id}:{kind}"
                or not isinstance(raw_payload_path, str)
            ):
                return None
            payload_path = Path(raw_payload_path)
            if not payload_path.is_absolute():
                return None
            resolved_payload = _absolute_lexical(payload_path)
            if (
                not resolved_payload.is_relative_to(root)
                or not resolved_payload.is_file()
                or resolved_payload.name != f"{kind}.json"
                or _is_reparse(resolved_payload)
            ):
                return None
            messages.append(OutboxMessage(
                kind=kind,
                delivery_key=delivery_key,
                payload_path=resolved_payload,
            ))
        return RunManifest(
            run_id=expected_run_id,
            messages=tuple(messages),
            coverage_ratio=coverage_ratio,
            missing_products=tuple(raw_missing),
        )
    except (InvalidOperation, OSError, TypeError, ValueError):
        return None


def _is_openvlab_snapshot_time(now: datetime) -> bool:
    return now.weekday() < 5 and (now.hour, now.minute) in {
        (10, 15),
        (14, 30),
    }


def _clean_run_directory(
    project_root: Path,
    outbox_root: Path,
    run_directory: Path,
    run_id: str,
) -> None:
    _assert_safe_run_directory(
        project_root,
        outbox_root,
        run_directory,
        run_id,
        require_exists=True,
    )
    chart_names = {
        f"iv-chart-{run_id}.png",
        f"anomaly-chart-{run_id}.png",
        f"openvlab-ranking-{run_id}.png",
    }
    allowed = {
        "manifest.json",
        "alerts.json",
        "hourly.json",
        "service.json",
    }
    allowed.update(chart_names)
    allowed.update(f"{name}.tmp" for name in chart_names)
    allowed.update(f".{name}.tmp" for name in chart_names)
    temporary_pattern = re.compile(
        rf"^\.(?:manifest|alerts|hourly|service)\.json\.{os.getpid()}\.tmp$"
    )
    entries = list(run_directory.iterdir())
    for entry in entries:
        if (
            _is_reparse(entry)
            or not entry.is_file()
            or (
                entry.name not in allowed
                and temporary_pattern.fullmatch(entry.name) is None
            )
        ):
            raise HitickError("unsafe option monitor run directory entry")
    for entry in entries:
        entry.unlink()


def _cleanup_failed_run_files(
    project_root: Path,
    outbox_root: Path,
    run_directory: Path,
    run_id: str,
) -> None:
    try:
        _assert_safe_run_directory(
            project_root,
            outbox_root,
            run_directory,
            run_id,
            require_exists=True,
        )
    except (HitickError, OSError):
        return
    chart_names = {
        f"iv-chart-{run_id}.png",
        f"anomaly-chart-{run_id}.png",
        f"openvlab-ranking-{run_id}.png",
    }
    names = {
        "manifest.json",
    }
    names.update(chart_names)
    names.update(f"{name}.tmp" for name in chart_names)
    names.update(f".{name}.tmp" for name in chart_names)
    names.update(
        f".{name}.json.{os.getpid()}.tmp"
        for name in ("manifest", "alerts", "hourly", "service")
    )
    for name in names:
        path = run_directory / name
        if path.is_file() and not _is_reparse(path):
            path.unlink(missing_ok=True)


def _assert_safe_run_directory(
    project_root: Path,
    outbox_root: Path,
    run_directory: Path,
    run_id: str,
    *,
    require_exists: bool,
) -> None:
    root = _absolute_lexical(project_root)
    outbox = _absolute_lexical(outbox_root)
    run = _absolute_lexical(run_directory)
    expected_outbox = root / "state" / "outbox"
    if (
        re.fullmatch(r"\d{8}T\d{6}[+-]\d{4}", run_id) is None
        or not _same_path(outbox, expected_outbox)
        or not _same_path(run.parent, outbox)
        or run.name != run_id
        or _same_path(run, outbox)
    ):
        raise HitickError("unsafe option monitor run directory")
    _assert_no_reparse_chain(root, run)
    if require_exists and (
        not run.is_dir() or _is_reparse(run)
    ):
        raise HitickError("unsafe option monitor run directory")


def _assert_no_reparse_chain(root: Path, target: Path) -> None:
    root = _absolute_lexical(root)
    target = _absolute_lexical(target)
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise HitickError("unsafe option monitor path") from None
    if not root.is_dir() or _is_reparse(root):
        raise HitickError("unsafe option monitor path")
    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current) and _is_reparse(current):
            raise HitickError("unsafe option monitor path")


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def ensure_daily_market_history(
    store: MonitorStore,
    client: Any,
    mapping: ContractMapping,
    now: datetime,
    *,
    trading_day: str | None = None,
    force: bool = False,
) -> list[DailyMarketClose]:
    trading_day = trading_day or _as_beijing(now).strftime("%Y%m%d")
    existing = _prior_market_closes(
        store, mapping.product_code, trading_day
    )
    if len(existing) >= 11 and not force:
        return existing

    end_ms = int(_as_beijing(now).timestamp() * 1000)
    response = client.vol_time_series(
        mapping.underlying, end_ms - 20 * DAY_MS, end_ms
    )
    points = _series_points(response)
    closes_by_day: dict[str, DailyMarketClose] = {}
    valid_point_count = 0
    for point in points:
        try:
            timestamp_ms, close_price, atm_iv = _market_series_point(point)
        except HitickError:
            continue
        valid_point_count += 1
        try:
            local = datetime.fromtimestamp(timestamp_ms / 1000, SHANGHAI)
        except (OSError, OverflowError, ValueError):
            raise HitickError("unsupported volatility time-series shape") from None
        local_time = local.timetz().replace(tzinfo=None)
        if not clock_time(14, 45) <= local_time <= clock_time(15, 15):
            continue
        point_trading_day = local.strftime("%Y%m%d")
        if point_trading_day >= trading_day:
            continue
        candidate = DailyMarketClose(
            point_trading_day,
            mapping.product_code,
            timestamp_ms,
            close_price,
            atm_iv,
        )
        previous = closes_by_day.get(point_trading_day)
        if previous is None or candidate.data_time_ms > previous.data_time_ms:
            closes_by_day[point_trading_day] = candidate

    if not valid_point_count:
        raise HitickError("unsupported volatility time-series shape")

    newest = sorted(
        closes_by_day.values(),
        key=lambda close: (close.trading_day, close.data_time_ms),
    )[-11:]
    existing_iv_days = {
        close.trading_day
        for close in store.daily_iv_closes(mapping.product_code, 64)
    }
    for close in newest:
        store.save_daily_market_close(close)
        if close.trading_day not in existing_iv_days:
            store.save_daily_iv_close(DailyIvClose(
                close.trading_day,
                close.product_code,
                close.data_time_ms,
                close.atm_iv,
            ))
    return _prior_market_closes(
        store, mapping.product_code, trading_day
    )


def _prior_market_closes(
    store: MonitorStore,
    product_code: str,
    trading_day: str,
    limit: int = 11,
) -> list[DailyMarketClose]:
    closes = store.daily_market_closes(product_code, limit + 32)
    return [
        close for close in closes if close.trading_day < trading_day
    ][-limit:]


def _iv_history_for_alert(
    store: MonitorStore,
    product_code: str,
    trading_day: str,
    market_closes: list[DailyMarketClose],
) -> list[DailyIvClose]:
    existing_iv_closes = _prior_iv_closes(
        store, product_code, trading_day
    )
    if len(existing_iv_closes) >= 10:
        return existing_iv_closes
    return [
        DailyIvClose(
            close.trading_day,
            close.product_code,
            close.data_time_ms,
            close.atm_iv,
        )
        for close in market_closes[-10:]
    ]


def _prior_iv_closes(
    store: MonitorStore,
    product_code: str,
    trading_day: str,
    limit: int = 10,
) -> list[DailyIvClose]:
    closes = store.daily_iv_closes(product_code, limit + 32)
    return [
        close for close in closes if close.trading_day < trading_day
    ][-limit:]


def _series_points(response: Any) -> list[Any]:
    if not isinstance(response, dict):
        raise HitickError("unsupported volatility time-series shape")
    for field in _SERIES_FIELDS:
        value = response.get(field)
        if isinstance(value, list):
            return value
    raise HitickError("unsupported volatility time-series shape")


def _market_series_point(
    point: Any,
) -> tuple[int, Decimal, Decimal]:
    if (
        not isinstance(point, dict)
        or "atm_iv" not in point
        or "underlying_last_price" not in point
    ):
        raise HitickError("unsupported volatility time-series shape")
    timestamp_value = next(
        (point[field] for field in _TIMESTAMP_FIELDS if field in point), None
    )
    try:
        if isinstance(timestamp_value, bool):
            raise ValueError
        timestamp = Decimal(str(timestamp_value))
        if not timestamp.is_finite() or timestamp <= 0 or timestamp != timestamp.to_integral():
            raise ValueError
        timestamp_ms = int(timestamp)
        if isinstance(point["atm_iv"], bool):
            raise ValueError
        atm_iv = Decimal(str(point["atm_iv"]))
        if not atm_iv.is_finite() or atm_iv < 0:
            raise ValueError
        if isinstance(point["underlying_last_price"], bool):
            raise ValueError
        close_price = Decimal(str(point["underlying_last_price"]))
        if not close_price.is_finite() or close_price <= 0:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise HitickError("unsupported volatility time-series shape") from None
    return timestamp_ms, close_price, atm_iv


def _floor_slot(now: datetime) -> datetime:
    local = _as_beijing(now)
    return local.replace(
        minute=(local.minute // SLOT_MINUTES) * SLOT_MINUTES,
        second=0,
        microsecond=0,
    )


def _progressed_observed_at_ms(
    observed_at_ms: int, observed_at_monotonic: float
) -> int:
    elapsed_ms = max(
        0, int((time.monotonic() - observed_at_monotonic) * 1000)
    )
    return observed_at_ms + elapsed_ms


def _same_underlying(
    left: str,
    right: str,
    product: ProductSpec | None = None,
) -> bool:
    """Compare underlying symbols, including the CZCE year-code alias.

    RQData represents Zhengzhou contracts with a four-digit year (for
    example, ``TA2701``), whereas Orange represents the same contract with
    its three-digit exchange form (``TA701``).  A saved Orange mapping must
    remain usable across collection runs instead of forcing another Orange
    subject-resolution request.
    """
    if left.casefold() == right.casefold():
        return True
    if product is None or product.exchange != "CZCE":
        return False

    pattern = re.compile(
        rf"{re.escape(product.code)}(\d{{3,4}})", re.IGNORECASE
    )
    left_match = pattern.fullmatch(left)
    right_match = pattern.fullmatch(right)
    if left_match is None or right_match is None:
        return False

    left_digits = left_match.group(1)
    right_digits = right_match.group(1)
    return (
        {len(left_digits), len(right_digits)} == {3, 4}
        and (
            left_digits == right_digits[-3:]
            or right_digits == left_digits[-3:]
        )
    )


def _session_trading_day(now: datetime) -> str:
    local = _as_beijing(now)
    session_date = local.date()
    if local.hour >= 21:
        session_date += timedelta(days=1)
    while session_date.weekday() >= 5:
        session_date += timedelta(days=1)
    return session_date.strftime("%Y%m%d")


def _as_beijing(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=SHANGHAI)
    return now.astimezone(SHANGHAI)


def _is_close_capture_time(now: datetime) -> bool:
    return (now.hour, now.minute) in ((14, 50), (15, 0), (15, 10))


def _manifest_document(manifest: RunManifest) -> dict[str, Any]:
    return {
        "run_id": manifest.run_id,
        "status": "ready",
        "coverage_ratio": str(manifest.coverage_ratio),
        "missing_products": list(manifest.missing_products),
        "messages": [
            {
                "kind": message.kind,
                "delivery_key": message.delivery_key,
                "payload_path": str(message.payload_path.resolve()),
            }
            for message in manifest.messages
        ],
    }


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_error_summary(error: BaseException) -> str:
    if isinstance(error, HitickError):
        return str(error)
    return f"monitor run failed ({type(error).__name__})"
