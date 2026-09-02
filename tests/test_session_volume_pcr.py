from __future__ import annotations

from decimal import Decimal

from option_monitor.collector import collect_product
from option_monitor.models import ContractMapping, ProductSpec

MAPPING = ContractMapping(
    trading_day="20260902",
    product_code="sc",
    underlying="sc2610",
    expire="20260911",
    multiplier=Decimal("1000"),
    resolved_at_ms=1,
)
PRODUCT = ProductSpec(code="sc", name="原油", exchange="INE")


class EmptyStore:
    def load_contract_state(self, symbol: str):
        return None


def option_row(symbol, side, volume, open_interest=1000, pre_open_interest=900):
    return {
        "instrument_kind": "OPTION",
        "option_type": side,
        "symbol": symbol,
        "underlying_symbol": "sc2610",
        "trading_day": "20260902",
        "timestamp_ms": 1000,
        "volume": volume,
        "average_price": Decimal("5.0"),
        "last_price": Decimal("5.2"),
        "multiplier": Decimal("1000"),
        "open_interest": open_interest,
        "pre_open_interest": pre_open_interest,
    }


def payload(call_rows, put_rows):
    basic = {
        "underlying": "sc2610",
        "expire": "20260911",
        "rows": [
            {
                "instrument_kind": "FUTURE",
                "symbol": "sc2610",
                "trading_day": "20260902",
                "timestamp_ms": 1000,
            }
        ]
        + call_rows
        + put_rows,
    }
    vol = {
        "underlying": "sc2610",
        "expire": "20260911",
        "data_time_ms": 1000,
        "atm_iv": Decimal("0.45"),
        "underlying_quote": {
            "symbol": "sc2610",
            "timestamp_ms": 1000,
            "last_price": Decimal("660.0"),
            "pre_settlement_price": Decimal("637.2"),
        },
        "rows": call_rows + put_rows,
    }
    return basic, vol


def collect(call_rows, put_rows):
    basic, vol = payload(call_rows, put_rows)
    return collect_product(
        PRODUCT, MAPPING, basic, vol, EmptyStore(), 1000, 1000
    )


def test_session_volume_pcr_from_cumulative_volumes():
    calls = [
        option_row("sc2610C700", "C", 300),
        option_row("sc2610C710", "C", 100),
    ]
    puts = [
        option_row("sc2610P600", "P", 500),
        option_row("sc2610P590", "P", 300),
    ]
    collection = collect(calls, puts)
    snapshot = collection.option_snapshot
    # 累计 Volume PCR = 800 / 400 = 2.0，无需任何历史基线
    assert snapshot.session_volume_pcr == Decimal("2")


def test_session_volume_pcr_none_when_one_side_missing():
    collection = collect([option_row("sc2610C700", "C", 300)], [])
    assert collection.option_snapshot.session_volume_pcr is None


def test_session_volume_pcr_none_when_call_volume_zero():
    calls = [option_row("sc2610C700", "C", 0)]
    puts = [option_row("sc2610P600", "P", 300)]
    collection = collect(calls, puts)
    assert collection.option_snapshot.session_volume_pcr is None


def test_session_volume_pcr_independent_from_delta_volume_pcr():
    """无基线时增量口径 volume_pcr 为 None，累计口径照常出值。"""
    calls = [option_row("sc2610C700", "C", 200)]
    puts = [option_row("sc2610P600", "P", 100)]
    snapshot = collect(calls, puts).option_snapshot
    assert snapshot.volume_pcr is None
    assert snapshot.session_volume_pcr == Decimal("0.5")


def test_session_volume_pcr_survives_storage_roundtrip(tmp_path):
    """报告流程会复用库存快照，session_volume_pcr 必须持久化。"""
    from option_monitor.storage import MonitorStore

    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    snapshot = collect(
        [option_row("sc2610C700", "C", 200)],
        [option_row("sc2610P600", "P", 100)],
    ).option_snapshot
    store.save_option_snapshot(snapshot)
    loaded = store.option_snapshot_at("sc", snapshot.run_at_ms)
    assert loaded is not None
    assert loaded.session_volume_pcr == Decimal("0.5")


def test_session_volume_pcr_legacy_row_loads_as_none(tmp_path):
    """迁移前的旧行没有该列数据，读出应为 None 而不是报错。"""
    from option_monitor.storage import MonitorStore

    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.initialize()
    snapshot = collect(
        [option_row("sc2610C700", "C", 200)],
        [option_row("sc2610P600", "P", 100)],
    ).option_snapshot
    object.__setattr__(snapshot, "session_volume_pcr", None)
    store.save_option_snapshot(snapshot)
    loaded = store.option_snapshot_at("sc", snapshot.run_at_ms)
    assert loaded is not None
    assert loaded.session_volume_pcr is None
