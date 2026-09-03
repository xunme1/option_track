from __future__ import annotations

import importlib.util
import sqlite3
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_render_script():
    spec = importlib.util.spec_from_file_location(
        "render_instant_option_chart",
        REPO_ROOT / "scripts" / "render_instant_option_chart.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(root: Path, rows):
    state = root / "state"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "option_monitor.sqlite3") as connection:
        connection.execute(
            """
            CREATE TABLE daily_iv_closes (
                trading_day TEXT NOT NULL,
                product_code TEXT NOT NULL,
                data_time_ms INTEGER NOT NULL,
                atm_iv TEXT NOT NULL,
                underlying TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'main',
                PRIMARY KEY (trading_day, product_code, role)
            )
            """
        )
        connection.executemany(
            "INSERT INTO daily_iv_closes VALUES (?, ?, ?, ?, ?, ?)", rows
        )


def test_previous_iv_baseline_reads_latest_prior_close(tmp_path):
    _make_db(
        tmp_path,
        [
            ("20260828", "sc", 1, "0.42", "SC2610", "main"),
            ("20260831", "sc", 2, "0.45", "SC2610", "main"),
            ("20260901", "sc", 3, "0.49", "SC2610", "main"),
            ("20260901", "au", 4, "0.22", "AU2610", "main"),
        ],
    )
    module = _load_render_script()
    baseline = module._previous_iv_baseline(
        tmp_path, "sc", "SC2610", "20260902"
    )
    assert baseline == ("20260901", Decimal("0.49"))


def test_previous_iv_baseline_excludes_current_trading_day(tmp_path):
    _make_db(
        tmp_path,
        [
            ("20260831", "sc", 1, "0.45", "SC2610", "main"),
            ("20260901", "sc", 2, "0.49", "SC2610", "main"),
        ],
    )
    module = _load_render_script()
    baseline = module._previous_iv_baseline(
        tmp_path, "sc", "SC2610", "20260901"
    )
    assert baseline == ("20260831", Decimal("0.45"))


def test_previous_iv_baseline_matches_only_same_underlying(tmp_path):
    # 主力（SC2611）与近月（SC2610）各有收盘，互不相串
    _make_db(
        tmp_path,
        [
            ("20260901", "sc", 1, "0.40", "SC2611", "main"),
            ("20260901", "sc", 2, "0.49", "SC2610", "near"),
        ],
    )
    module = _load_render_script()
    assert module._previous_iv_baseline(
        tmp_path, "sc", "SC2610", "20260902"
    ) == ("20260901", Decimal("0.49"))
    assert module._previous_iv_baseline(
        tmp_path, "sc", "SC2611", "20260902"
    ) == ("20260901", Decimal("0.40"))


def test_previous_iv_baseline_matches_underlying_case_insensitively(tmp_path):
    _make_db(
        tmp_path,
        [("20260901", "sc", 1, "0.49", "SC2610", "main")],
    )
    module = _load_render_script()
    assert module._previous_iv_baseline(
        tmp_path, "sc", "sc2610", "20260902"
    ) == ("20260901", Decimal("0.49"))


def test_previous_iv_baseline_missing(tmp_path):
    module = _load_render_script()
    assert module._previous_iv_baseline(
        tmp_path, "sc", "SC2610", "20260902"
    ) is None
    # 只有别的合约的记录时，不允许借来当基线
    _make_db(
        tmp_path,
        [("20260901", "sc", 1, "0.22", "SC2611", "main")],
    )
    assert module._previous_iv_baseline(
        tmp_path, "sc", "SC2610", "20260902"
    ) is None
