from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from option_monitor.anomaly_interpretation import interpret_facts
from option_monitor.strength_log import append_strength_records
from tests.test_strength_scoring import make_facts


def build_results():
    available = interpret_facts(make_facts(
        price_change=Decimal("0.03"),
        oi_pcr=Decimal("0.9"),
        previous_oi_pcr=Decimal("1.0"),
        oi_pcr_change=Decimal("-0.10"),
    ))
    unavailable = interpret_facts(make_facts(
        product_code="MO",
        available=False,
        price=None,
        price_change=None,
        atm_iv=None,
        rr25=None,
    ))
    return {"IO": available, "MO": unavailable}


def test_append_strength_records_writes_available_only(tmp_path: Path):
    path = tmp_path / "sub" / "strength_scores.jsonl"
    count = append_strength_records(
        build_results(), path, run_at_ms=1725000000000
    )
    assert count == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["product_code"] == "IO"
    assert record["run_at_ms"] == 1725000000000
    assert isinstance(record["score"], int)
    assert record["level"] in ("observation", "warning", "important")
    assert set(record["components"]) == {
        "价格", "ATM IV", "RR25", "持仓", "OI PCR"
    }
    assert record["price_change"] == "0.03"
    assert record["oi_pcr_change"] == "-0.10"
    assert record["call_oi_delta"] is None


def test_append_strength_records_appends(tmp_path: Path):
    path = tmp_path / "strength_scores.jsonl"
    results = build_results()
    append_strength_records(results, path, run_at_ms=1)
    append_strength_records(results, path, run_at_ms=2)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["run_at_ms"] == 2


def test_append_strength_records_no_available(tmp_path: Path):
    path = tmp_path / "strength_scores.jsonl"
    results = build_results()
    count = append_strength_records(
        {"MO": results["MO"]}, path, run_at_ms=1
    )
    assert count == 0
    assert not path.exists()
