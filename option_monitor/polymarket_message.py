from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_symbol_snapshot(path: str | Path, symbol: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for item in data.get("items", []):
        if item.get("symbol") == symbol:
            return item
    raise ValueError(f"Symbol not found in Polymarket snapshot: {symbol}")


def build_gold_polymarket_markdown(
    snapshot: dict[str, Any],
    market_date: str,
    keyword: str = "期权监控",
) -> str:
    up_pct = float(snapshot["up_mid_price"]) * 100
    down_pct = float(snapshot["down_mid_price"]) * 100
    return "\n".join(
        [
            f"### {keyword} | 周五黄金 Polymarket",
            "",
            f"- 日期：{market_date}",
            f"- 标的：{snapshot['symbol']}",
            f"- price_to_beat：{snapshot['price']}",
            f"- Up：{up_pct:.1f}%",
            f"- Down：{down_pct:.1f}%",
            f"- 采集时间（北京时间）：{snapshot['queried_at']}",
            "",
            "说明：Up/Down 为相对开盘基准价的合约中间价，近似反映市场倾向，会受价差、流动性和交易成本影响。",
        ]
    )
