from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from option_monitor.anomaly_interpretation import InterpretationResult


def append_strength_records(
    results: Mapping[str, InterpretationResult],
    path: Path,
    *,
    run_at_ms: int,
) -> int:
    """把本轮各品种的评分明细追加写入 JSONL，供后续阈值校准回查。

    返回写入条数。写盘失败由调用方捕获，本函数不吞异常。
    """
    records = [
        _record(result, run_at_ms)
        for result in results.values()
        if result.facts.available
    ]
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


def _record(result: InterpretationResult, run_at_ms: int) -> dict[str, Any]:
    facts = result.facts
    return {
        "run_at_ms": run_at_ms,
        "product_code": facts.product_code,
        "score": result.strength_score,
        "level": result.level,
        "direction": result.direction,
        "components": dict(result.component_scores),
        "effective_dimensions": list(result.effective_dimensions),
        "confirmations": list(result.confirmations),
        "conflicts": list(result.conflicts),
        "pcr_state": result.pcr_state,
        "price_change": _decimal(facts.price_change),
        "atm_iv": _decimal(facts.atm_iv),
        "delta_iv": _decimal(facts.delta_iv),
        "rr25": _decimal(facts.rr25),
        "delta_rr25": _decimal(facts.delta_rr25),
        "call_oi_delta": facts.call_oi_delta,
        "put_oi_delta": facts.put_oi_delta,
        "oi_pcr": _decimal(facts.oi_pcr),
        "oi_pcr_change": _decimal(facts.oi_pcr_change),
    }


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
