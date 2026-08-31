from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from option_monitor.models import DailyOptionClose
from option_monitor.runner import MonitorRunner
from option_monitor.storage import MonitorStore
from tests.test_extract_facts_history import make_collection


def test_daily_option_close_roundtrip_with_open_interest(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    close = DailyOptionClose(
        trading_day="2026-08-31",
        product_code="IO",
        data_time_ms=1725000000000,
        rr25=Decimal("0.0123"),
        call_open_interest=120000,
        put_open_interest=135000,
    )
    store.save_daily_option_close(close)
    (loaded,) = store.daily_option_closes("IO", 10)
    assert loaded.rr25 == Decimal("0.0123")
    assert loaded.call_open_interest == 120000
    assert loaded.put_open_interest == 135000


def test_daily_option_close_legacy_rows_keep_null_oi(tmp_path: Path):
    db_path = tmp_path / "monitor.sqlite3"
    store = MonitorStore(db_path)
    store.initialize()
    # 模拟旧版本写入的、没有持仓列数据的行
    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-08-28",
            product_code="IO",
            data_time_ms=1724700000000,
            rr25=Decimal("0.01"),
        )
    )
    (loaded,) = store.daily_option_closes("IO", 10)
    assert loaded.call_open_interest is None
    assert loaded.put_open_interest is None


def test_daily_option_close_roundtrip_with_oi_only(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-08-31",
            product_code="IO",
            data_time_ms=1725000000000,
            rr25=None,
            call_open_interest=120000,
            put_open_interest=135000,
        )
    )
    (loaded,) = store.daily_option_closes("IO", 10)
    assert loaded.rr25 is None
    assert loaded.call_open_interest == 120000
    assert loaded.put_open_interest == 135000


def test_daily_option_close_upsert_preserves_oi_when_missing(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-08-31",
            product_code="IO",
            data_time_ms=1725000000000,
            rr25=Decimal("0.01"),
            call_open_interest=120000,
            put_open_interest=135000,
        )
    )
    # 同日更晚的快照没有持仓数据时，不应把已写入的持仓覆盖成 NULL
    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-08-31",
            product_code="IO",
            data_time_ms=1725003600000,
            rr25=Decimal("0.02"),
        )
    )
    (loaded,) = store.daily_option_closes("IO", 10)
    assert loaded.rr25 == Decimal("0.02")
    assert loaded.call_open_interest == 120000
    assert loaded.put_open_interest == 135000


def test_daily_option_close_oi_only_upsert_preserves_rr25(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-08-31",
            product_code="IO",
            data_time_ms=1725000000000,
            rr25=Decimal("0.01"),
        )
    )
    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-08-31",
            product_code="IO",
            data_time_ms=1725003600000,
            rr25=None,
            call_open_interest=120000,
            put_open_interest=135000,
        )
    )
    (loaded,) = store.daily_option_closes("IO", 10)
    assert loaded.rr25 == Decimal("0.01")
    assert loaded.call_open_interest == 120000
    assert loaded.put_open_interest == 135000


def test_daily_option_close_migration_adds_columns(tmp_path: Path):
    db_path = tmp_path / "monitor.sqlite3"
    # 先手工建一个旧版表结构（无持仓列），再交给 MonitorStore 迁移
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE daily_option_closes (
                trading_day TEXT NOT NULL,
                product_code TEXT NOT NULL,
                data_time_ms INTEGER NOT NULL,
                rr25 TEXT NOT NULL,
                PRIMARY KEY (trading_day, product_code)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO daily_option_closes (
                trading_day, product_code, data_time_ms, rr25
            ) VALUES ('2026-08-28', 'IO', 1724700000000, '0.01')
            """
        )
    store = MonitorStore(db_path)
    store.initialize()
    with sqlite3.connect(db_path) as connection:
        rr25_column = next(
            row
            for row in connection.execute(
                "PRAGMA table_info(daily_option_closes)"
            )
            if row[1] == "rr25"
        )
    assert rr25_column[3] == 0
    (loaded,) = store.daily_option_closes("IO", 10)
    assert loaded.rr25 == Decimal("0.01")
    assert loaded.call_open_interest is None
    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-08-31",
            product_code="IO",
            data_time_ms=1725000000000,
            rr25=Decimal("0.02"),
            call_open_interest=100,
            put_open_interest=110,
        )
    )
    rows = store.daily_option_closes("IO", 10)
    assert len(rows) == 2

    store.save_daily_option_close(
        DailyOptionClose(
            trading_day="2026-09-01",
            product_code="IO",
            data_time_ms=1725086400000,
            rr25=None,
            call_open_interest=120,
            put_open_interest=130,
        )
    )
    latest = store.daily_option_closes("IO", 10)[-1]
    assert latest.rr25 is None
    assert latest.call_open_interest == 120


def test_runner_saves_oi_close_when_rr25_is_missing(tmp_path: Path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    collection = make_collection()
    assert collection.option_snapshot is not None
    collection = replace(
        collection,
        option_snapshot=replace(collection.option_snapshot, rr25=None),
    )
    runner = object.__new__(MonitorRunner)
    runner.store = store
    runner.products = (SimpleNamespace(code="IO"),)

    runner._save_daily_closes({"IO": collection})

    (loaded,) = store.daily_option_closes("IO", 10)
    assert loaded.rr25 is None
    assert loaded.call_open_interest == 121000
    assert loaded.put_open_interest == 133000
