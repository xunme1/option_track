from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from option_monitor.collector import (
    MainOptionUnavailable,
    resolve_near_month_mapping,
)
from option_monitor.hitick_client import HitickError
from option_monitor.models import (
    DailyIvClose,
    DailyOptionClose,
    ProductSpec,
)
from option_monitor.runner import MonitorRunner
from option_monitor.storage import MonitorStore
from tests.test_extract_facts_history import make_collection


def _iv_close(day: str, iv: str, underlying: str, role: str) -> DailyIvClose:
    return DailyIvClose(
        trading_day=day,
        product_code="sc",
        data_time_ms=1,
        atm_iv=Decimal(iv),
        underlying=underlying,
        role=role,
    )


def _option_close(
    day: str, rr25: str, underlying: str, role: str
) -> DailyOptionClose:
    return DailyOptionClose(
        trading_day=day,
        product_code="sc",
        data_time_ms=1,
        rr25=Decimal(rr25),
        underlying=underlying,
        role=role,
    )


def test_daily_close_roles_are_separated(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    store.save_daily_iv_close(_iv_close("20260901", "0.40", "SC2611", "main"))
    store.save_daily_iv_close(_iv_close("20260901", "0.49", "SC2610", "near"))
    store.save_daily_option_close(
        _option_close("20260901", "0.010", "SC2611", "main")
    )
    store.save_daily_option_close(
        _option_close("20260901", "0.025", "SC2610", "near")
    )

    (main_iv,) = store.daily_iv_closes("sc", 10)
    (near_iv,) = store.daily_iv_closes("sc", 10, role="near")
    assert main_iv.atm_iv == Decimal("0.40")
    assert main_iv.underlying == "SC2611"
    assert near_iv.atm_iv == Decimal("0.49")
    assert near_iv.underlying == "SC2610"

    (main_option,) = store.daily_option_closes("sc", 10)
    (near_option,) = store.daily_option_closes("sc", 10, role="near")
    assert main_option.rr25 == Decimal("0.010")
    assert near_option.rr25 == Decimal("0.025")


def test_previous_close_for_underlying_matches_same_contract(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    store.save_daily_iv_close(_iv_close("20260831", "0.41", "SC2611", "main"))
    store.save_daily_iv_close(_iv_close("20260901", "0.40", "SC2611", "main"))
    store.save_daily_iv_close(_iv_close("20260901", "0.49", "SC2610", "near"))
    store.save_daily_option_close(
        _option_close("20260901", "0.025", "SC2610", "near")
    )

    iv = store.previous_daily_iv_close_for_underlying(
        "sc", "SC2611", "20260902"
    )
    assert iv is not None and iv.atm_iv == Decimal("0.40")
    # 大小写不敏感
    iv = store.previous_daily_iv_close_for_underlying(
        "sc", "sc2610", "20260902"
    )
    assert iv is not None and iv.atm_iv == Decimal("0.49")
    option = store.previous_daily_option_close_for_underlying(
        "sc", "SC2610", "20260902"
    )
    assert option is not None and option.rr25 == Decimal("0.025")
    # 查不到该合约的记录时必须为空，不能借用别的合约
    assert (
        store.previous_daily_iv_close_for_underlying(
            "sc", "SC2612", "20260902"
        )
        is None
    )
    assert (
        store.previous_daily_option_close_for_underlying(
            "sc", "SC2611", "20260902"
        )
        is None
    )


def test_role_migration_backfills_underlying_from_snapshots(tmp_path: Path):
    db_path = tmp_path / "monitor.sqlite3"
    with sqlite3.connect(db_path) as connection:
        # 旧版表结构：无 underlying/role 列，主键 (trading_day, product_code)
        connection.execute(
            """
            CREATE TABLE daily_iv_closes (
                trading_day TEXT NOT NULL,
                product_code TEXT NOT NULL,
                data_time_ms INTEGER NOT NULL,
                atm_iv TEXT NOT NULL,
                PRIMARY KEY (trading_day, product_code)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO daily_iv_closes VALUES
                ('20260831', 'sc', 1, '0.41'),
                ('20260901', 'sc', 2, '0.40')
            """
        )
        connection.execute(
            """
            CREATE TABLE daily_option_closes (
                trading_day TEXT NOT NULL,
                product_code TEXT NOT NULL,
                data_time_ms INTEGER NOT NULL,
                rr25 TEXT,
                call_open_interest INTEGER,
                put_open_interest INTEGER,
                PRIMARY KEY (trading_day, product_code)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO daily_option_closes VALUES
                ('20260901', 'sc', 2, '0.012', 100, 110)
            """
        )
        # 当天监控快照：主力是 SC2611
        connection.execute(
            """
            CREATE TABLE option_analytics_snapshots (
                run_at_ms INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                trading_day TEXT NOT NULL,
                underlying TEXT NOT NULL,
                PRIMARY KEY (run_at_ms, product_code)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO option_analytics_snapshots VALUES
                (10, 'sc', '20260901', 'SC2611')
            """
        )
    store = MonitorStore(db_path)
    store.initialize()

    iv_rows = store.daily_iv_closes("sc", 10)
    assert [row.underlying for row in iv_rows] == ["", "SC2611"]
    assert all(row.role == "main" for row in iv_rows)
    (option_row,) = store.daily_option_closes("sc", 10)
    assert option_row.underlying == "SC2611"
    assert option_row.rr25 == Decimal("0.012")
    # 迁移后的表结构可以直接按新口径写入
    store.save_daily_iv_close(_iv_close("20260901", "0.49", "SC2610", "near"))
    (near_iv,) = store.daily_iv_closes("sc", 10, role="near")
    assert near_iv.underlying == "SC2610"


def test_runner_saves_near_month_closes_with_role(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    main_collection = make_collection()
    near_collection = replace(
        main_collection,
        market=replace(
            main_collection.market,
            underlying="IF2607",
            atm_iv=Decimal("0.30"),
        ),
        option_snapshot=replace(
            main_collection.option_snapshot,
            underlying="IF2607",
            rr25=Decimal("0.03"),
        ),
    )
    runner = object.__new__(MonitorRunner)
    runner.store = store

    runner._save_daily_closes(
        {"IO": main_collection}, {"IO": near_collection}
    )

    (main_iv,) = store.daily_iv_closes("IO", 10)
    (near_iv,) = store.daily_iv_closes("IO", 10, role="near")
    assert main_iv.underlying == "IF"
    assert near_iv.underlying == "IF2607"
    assert near_iv.atm_iv == Decimal("0.30")

    (main_option,) = store.daily_option_closes("IO", 10)
    (near_option,) = store.daily_option_closes("IO", 10, role="near")
    assert main_option.underlying == "IF"
    assert near_option.underlying == "IF2607"
    assert near_option.rr25 == Decimal("0.03")

    # 期货收盘表只记录主力序列
    market_days = [
        close.trading_day for close in store.daily_market_closes("IO", 10)
    ]
    assert market_days == ["2026-08-31"]

    # 近月行可作为该合约的基线被查到
    baseline = store.previous_daily_option_close_for_underlying(
        "IO", "IF2607", "2026-09-01"
    )
    assert baseline is not None and baseline.rr25 == Decimal("0.03")


class _FakeSubjectClient:
    def __init__(self, resolved):
        self._resolved = resolved

    def resolve_subject(self, subject):
        return self._resolved


def _subject_response(*candidates):
    return {
        "found": True,
        "ambiguous": False,
        "selected": candidates[0],
        "candidates": list(candidates),
    }


def _candidate(underlying: str, expire: str, multiplier: str = "1000"):
    return {
        "underlying": underlying,
        "expire": expire,
        "multiplier": multiplier,
    }


PRODUCT = ProductSpec("sc", "原油", "INE")
TRADING_DAY = "20260902"


def test_near_month_mapping_picks_earliest_unexpired_month():
    client = _FakeSubjectClient(
        _subject_response(
            _candidate("SC2611", "20261015"),
            _candidate("SC2610", "20260911"),
            _candidate("SC2612", "20261113"),
        )
    )
    mapping = resolve_near_month_mapping(
        client, PRODUCT, TRADING_DAY, 1, "SC2611"
    )
    # 近月 = 月份最近的未到期合约，与它在主力之前还是之后无关
    assert mapping.underlying == "SC2610"
    assert mapping.expire == "20260911"


def test_near_month_mapping_picks_nearest_different_month():
    client = _FakeSubjectClient(
        _subject_response(
            _candidate("SC2611", "20261015"),
            _candidate("SC2609", "20260814"),
            _candidate("SC2610", "20260911"),
        )
    )
    mapping = resolve_near_month_mapping(
        client, PRODUCT, TRADING_DAY, 1, "SC2611"
    )
    assert mapping.underlying == "SC2610"


def test_near_month_mapping_skipped_when_main_is_the_near_month():
    client = _FakeSubjectClient(
        _subject_response(
            _candidate("SC2610", "20260911"),
            _candidate("SC2611", "20261015"),
            _candidate("SC2612", "20261113"),
        )
    )
    # 主力本身就是最近月：近月即主力，没有额外的近月可记录
    with pytest.raises(MainOptionUnavailable):
        resolve_near_month_mapping(
            client, PRODUCT, TRADING_DAY, 1, "SC2610"
        )


def test_near_month_mapping_skips_expired_contracts():
    client = _FakeSubjectClient(
        _subject_response(
            _candidate("SC2611", "20261015"),
            _candidate("SC2610", "20260901"),  # 已到期
            _candidate("SC2612", "20261113"),
        )
    )
    # 2610 已到期，最近未到期月份就是主力 2611 → 没有额外的近月可记
    with pytest.raises(MainOptionUnavailable):
        resolve_near_month_mapping(
            client, PRODUCT, TRADING_DAY, 1, "SC2611"
        )


def test_near_month_mapping_ignores_expired_when_earlier_month_alive():
    client = _FakeSubjectClient(
        _subject_response(
            _candidate("SC2611", "20261015"),
            _candidate("SC2609", "20260814"),  # 已到期
            _candidate("SC2610", "20260911"),
        )
    )
    mapping = resolve_near_month_mapping(
        client, PRODUCT, TRADING_DAY, 1, "SC2611"
    )
    assert mapping.underlying == "SC2610"


def test_near_month_mapping_without_other_month_raises():
    client = _FakeSubjectClient(
        _subject_response(_candidate("SC2611", "20261015"))
    )
    with pytest.raises(HitickError):
        resolve_near_month_mapping(
            client, PRODUCT, TRADING_DAY, 1, "SC2611"
        )
