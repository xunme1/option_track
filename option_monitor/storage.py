from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from option_monitor.models import (
    ContractMapping,
    ContractOiChange,
    ContractVolumeState,
    DailyIvClose,
    DailyMarketClose,
    DailyOptionClose,
    FlowSnapshot,
    MarketSnapshot,
    OiConcentration,
    OptionAnalyticsSnapshot,
)


class MonitorStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self._transaction_connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS contract_mappings (
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expire TEXT NOT NULL,
                    multiplier TEXT NOT NULL,
                    resolved_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (trading_day, product_code)
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    run_at_ms INTEGER NOT NULL,
                    data_time_ms INTEGER NOT NULL,
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    last_price TEXT NOT NULL,
                    pre_settlement_price TEXT NOT NULL,
                    atm_iv TEXT NOT NULL,
                    PRIMARY KEY (run_at_ms, product_code)
                );

                CREATE TABLE IF NOT EXISTS contract_volume_state (
                    symbol TEXT PRIMARY KEY,
                    trading_day TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('C', 'P')),
                    volume INTEGER NOT NULL,
                    average_price TEXT,
                    last_price TEXT NOT NULL,
                    multiplier TEXT NOT NULL,
                    data_time_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contract_oi_changes (
                    run_at_ms INTEGER NOT NULL,
                    data_time_ms INTEGER NOT NULL,
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expire TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('C', 'P')),
                    strike TEXT NOT NULL,
                    open_interest INTEGER NOT NULL CHECK (open_interest >= 0),
                    pre_open_interest INTEGER NOT NULL CHECK (pre_open_interest >= 0),
                    delta_open_interest INTEGER NOT NULL,
                    multiplier TEXT,
                    option_last_price TEXT,
                    PRIMARY KEY (run_at_ms, symbol)
                );

                CREATE INDEX IF NOT EXISTS idx_contract_oi_product_run
                ON contract_oi_changes (product_code, run_at_ms);

                CREATE TABLE IF NOT EXISTS flow_snapshots (
                    run_at_ms INTEGER NOT NULL,
                    data_time_ms INTEGER NOT NULL,
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    call_inflow TEXT NOT NULL,
                    put_inflow TEXT NOT NULL,
                    net_inflow TEXT NOT NULL,
                    PRIMARY KEY (run_at_ms, product_code)
                );

                CREATE TABLE IF NOT EXISTS daily_iv_closes (
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    data_time_ms INTEGER NOT NULL,
                    atm_iv TEXT NOT NULL,
                    PRIMARY KEY (trading_day, product_code)
                );

                CREATE TABLE IF NOT EXISTS daily_market_closes (
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    data_time_ms INTEGER NOT NULL,
                    close_price TEXT NOT NULL,
                    atm_iv TEXT NOT NULL,
                    PRIMARY KEY (trading_day, product_code)
                );

                CREATE TABLE IF NOT EXISTS option_analytics_snapshots (
                    run_at_ms INTEGER NOT NULL,
                    data_time_ms INTEGER NOT NULL,
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expire TEXT NOT NULL,
                    rr25 TEXT,
                    call_volume_delta INTEGER NOT NULL,
                    put_volume_delta INTEGER NOT NULL,
                    call_turnover_delta TEXT NOT NULL,
                    put_turnover_delta TEXT NOT NULL,
                    call_open_interest INTEGER NOT NULL,
                    put_open_interest INTEGER NOT NULL,
                    call_pre_open_interest INTEGER NOT NULL,
                    put_pre_open_interest INTEGER NOT NULL,
                    volume_pcr TEXT,
                    turnover_pcr TEXT,
                    oi_pcr TEXT,
                    oi_concentrations_json TEXT NOT NULL,
                    flow_baseline_ready INTEGER NOT NULL,
                    oi_baseline_ready INTEGER NOT NULL,
                    next_expire TEXT,
                    next_atm_iv TEXT,
                    call_oi_baseline_ready INTEGER NOT NULL DEFAULT 0,
                    put_oi_baseline_ready INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (run_at_ms, product_code)
                );

                CREATE TABLE IF NOT EXISTS daily_option_closes (
                    trading_day TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    data_time_ms INTEGER NOT NULL,
                    rr25 TEXT,
                    call_open_interest INTEGER,
                    put_open_interest INTEGER,
                    PRIMARY KEY (trading_day, product_code)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at_ms INTEGER NOT NULL,
                    finished_at_ms INTEGER,
                    status TEXT NOT NULL,
                    coverage_ratio TEXT,
                    error_summary TEXT
                );
                """
            )
            contract_oi_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(contract_oi_changes)"
                )
            }
            daily_option_close_columns = {
                row[1]: row
                for row in connection.execute(
                    "PRAGMA table_info(daily_option_closes)"
                )
            }
            rr25_column = daily_option_close_columns.get("rr25")
            if rr25_column is not None and rr25_column[3]:
                call_column = (
                    "call_open_interest"
                    if "call_open_interest" in daily_option_close_columns
                    else "NULL"
                )
                put_column = (
                    "put_open_interest"
                    if "put_open_interest" in daily_option_close_columns
                    else "NULL"
                )
                connection.execute(
                    """
                    ALTER TABLE daily_option_closes
                    RENAME TO daily_option_closes_not_null_legacy
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
                    f"""
                    INSERT INTO daily_option_closes (
                        trading_day, product_code, data_time_ms, rr25,
                        call_open_interest, put_open_interest
                    )
                    SELECT trading_day, product_code, data_time_ms, rr25,
                           {call_column}, {put_column}
                    FROM daily_option_closes_not_null_legacy
                    """
                )
                connection.execute(
                    "DROP TABLE daily_option_closes_not_null_legacy"
                )
                daily_option_close_columns = {
                    row[1]: row
                    for row in connection.execute(
                        "PRAGMA table_info(daily_option_closes)"
                    )
                }
            if "call_open_interest" not in daily_option_close_columns:
                connection.execute(
                    """
                    ALTER TABLE daily_option_closes
                    ADD COLUMN call_open_interest INTEGER
                    """
                )
            if "put_open_interest" not in daily_option_close_columns:
                connection.execute(
                    """
                    ALTER TABLE daily_option_closes
                    ADD COLUMN put_open_interest INTEGER
                    """
                )
            if "multiplier" not in contract_oi_columns:
                connection.execute(
                    "ALTER TABLE contract_oi_changes ADD COLUMN multiplier TEXT"
                )
            if "option_last_price" not in contract_oi_columns:
                connection.execute(
                    """
                    ALTER TABLE contract_oi_changes
                    ADD COLUMN option_last_price TEXT
                    """
                )
            flow_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(flow_snapshots)")
            }
            if "call_contract_count" not in flow_columns:
                connection.execute(
                    """
                    ALTER TABLE flow_snapshots
                    ADD COLUMN call_contract_count INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "put_contract_count" not in flow_columns:
                connection.execute(
                    """
                    ALTER TABLE flow_snapshots
                    ADD COLUMN put_contract_count INTEGER NOT NULL DEFAULT 0
                    """
                )
            option_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(option_analytics_snapshots)"
                )
            }
            if "call_oi_baseline_ready" not in option_columns:
                connection.execute(
                    """
                    ALTER TABLE option_analytics_snapshots
                    ADD COLUMN call_oi_baseline_ready INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "put_oi_baseline_ready" not in option_columns:
                connection.execute(
                    """
                    ALTER TABLE option_analytics_snapshots
                    ADD COLUMN put_oi_baseline_ready INTEGER NOT NULL DEFAULT 0
                    """
                )
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._transaction_connection is not None:
            yield self._transaction_connection
            return

        connection = self._connect()
        self._transaction_connection = connection
        try:
            connection.execute("BEGIN")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            self._transaction_connection = None
            connection.close()

    def save_mapping(self, mapping: ContractMapping) -> None:
        self._write(
            """
            INSERT INTO contract_mappings (
                trading_day, product_code, underlying, expire, multiplier, resolved_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_day, product_code) DO UPDATE SET
                underlying = excluded.underlying,
                expire = excluded.expire,
                multiplier = excluded.multiplier,
                resolved_at_ms = excluded.resolved_at_ms
            """,
            (
                mapping.trading_day, mapping.product_code, mapping.underlying,
                mapping.expire, str(mapping.multiplier), mapping.resolved_at_ms,
            ),
        )

    def load_mapping(self, trading_day: str, product_code: str) -> ContractMapping | None:
        row = self._read_one(
            """
            SELECT trading_day, product_code, underlying, expire, multiplier, resolved_at_ms
            FROM contract_mappings WHERE trading_day = ? AND product_code = ?
            """,
            (trading_day, product_code),
        )
        return None if row is None else ContractMapping(
            row["trading_day"], row["product_code"], row["underlying"], row["expire"],
            Decimal(row["multiplier"]), row["resolved_at_ms"]
        )

    def load_contract_state(self, symbol: str) -> ContractVolumeState | None:
        row = self._read_one(
            """
            SELECT trading_day, underlying, symbol, side, volume, average_price, last_price,
                   multiplier, data_time_ms
            FROM contract_volume_state WHERE symbol = ?
            """,
            (symbol,),
        )
        return None if row is None else self._contract_state_from_row(row)

    def save_contract_states(self, states: tuple[ContractVolumeState, ...]) -> None:
        self._write_many(
            """
            INSERT INTO contract_volume_state (
                symbol, trading_day, underlying, side, volume, average_price, last_price,
                multiplier, data_time_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                trading_day = excluded.trading_day,
                underlying = excluded.underlying,
                side = excluded.side,
                volume = excluded.volume,
                average_price = excluded.average_price,
                last_price = excluded.last_price,
                multiplier = excluded.multiplier,
                data_time_ms = excluded.data_time_ms
            """,
            [
                (
                    state.symbol, state.trading_day, state.underlying, state.side, state.volume,
                    None if state.average_price is None else str(state.average_price),
                    str(state.last_price), str(state.multiplier), state.data_time_ms,
                )
                for state in states
            ],
        )

    def save_contract_oi_changes(
        self, rows: tuple[ContractOiChange, ...]
    ) -> None:
        self._write_many(
            """
            INSERT INTO contract_oi_changes (
                run_at_ms, data_time_ms, trading_day, product_code,
                product_name, underlying, expire, symbol, side, strike,
                open_interest, pre_open_interest, delta_open_interest, multiplier,
                option_last_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_at_ms, symbol) DO UPDATE SET
                data_time_ms = excluded.data_time_ms,
                trading_day = excluded.trading_day,
                product_code = excluded.product_code,
                product_name = excluded.product_name,
                underlying = excluded.underlying,
                expire = excluded.expire,
                side = excluded.side,
                strike = excluded.strike,
                open_interest = excluded.open_interest,
                pre_open_interest = excluded.pre_open_interest,
                delta_open_interest = excluded.delta_open_interest,
                multiplier = excluded.multiplier,
                option_last_price = excluded.option_last_price
            """,
            [
                (
                    row.run_at_ms,
                    row.data_time_ms,
                    row.trading_day,
                    row.product_code,
                    row.product_name,
                    row.underlying,
                    row.expire,
                    row.symbol,
                    row.side,
                    str(row.strike),
                    row.open_interest,
                    row.pre_open_interest,
                    row.delta_open_interest,
                    None if row.multiplier is None else str(row.multiplier),
                    (
                        None
                        if row.option_last_price is None
                        else str(row.option_last_price)
                    ),
                )
                for row in rows
            ],
        )

    def contract_oi_changes_at(
        self, product_code: str, run_at_ms: int
    ) -> tuple[ContractOiChange, ...]:
        rows = self._read_all(
            """
            SELECT run_at_ms, data_time_ms, trading_day, product_code,
                   product_name, underlying, expire, symbol, side, strike,
                   open_interest, pre_open_interest, delta_open_interest,
                   multiplier, option_last_price
            FROM contract_oi_changes
            WHERE product_code = ? AND run_at_ms = ?
            ORDER BY symbol ASC
            """,
            (product_code, run_at_ms),
        )
        return tuple(self._contract_oi_change_from_row(row) for row in rows)

    def save_market_snapshot(self, snapshot: MarketSnapshot) -> None:
        self._write(
            """
            INSERT INTO market_snapshots (
                run_at_ms, data_time_ms, trading_day, product_code, product_name, underlying,
                last_price, pre_settlement_price, atm_iv
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_at_ms, product_code) DO UPDATE SET
                data_time_ms = excluded.data_time_ms,
                trading_day = excluded.trading_day,
                product_name = excluded.product_name,
                underlying = excluded.underlying,
                last_price = excluded.last_price,
                pre_settlement_price = excluded.pre_settlement_price,
                atm_iv = excluded.atm_iv
            """,
            (
                snapshot.run_at_ms, snapshot.data_time_ms, snapshot.trading_day,
                snapshot.product_code, snapshot.product_name, snapshot.underlying,
                str(snapshot.last_price), str(snapshot.pre_settlement_price), str(snapshot.atm_iv),
            ),
        )

    def market_snapshot_near(
        self, product_code: str, target_ms: int, tolerance_ms: int
    ) -> MarketSnapshot | None:
        row = self._read_one(
            """
            SELECT run_at_ms, data_time_ms, trading_day, product_code, product_name, underlying,
                   last_price, pre_settlement_price, atm_iv
            FROM market_snapshots
            WHERE product_code = ? AND run_at_ms BETWEEN ? AND ?
            ORDER BY run_at_ms DESC
            LIMIT 1
            """,
            (product_code, target_ms - tolerance_ms, target_ms + tolerance_ms),
        )
        return None if row is None else self._market_snapshot_from_row(row)

    def market_snapshot_at(
        self, product_code: str, run_at_ms: int
    ) -> MarketSnapshot | None:
        row = self._read_one(
            """
            SELECT run_at_ms, data_time_ms, trading_day, product_code, product_name,
                   underlying, last_price, pre_settlement_price, atm_iv
            FROM market_snapshots
            WHERE product_code = ? AND run_at_ms = ?
            """,
            (product_code, run_at_ms),
        )
        return None if row is None else self._market_snapshot_from_row(row)

    def save_flow_snapshot(self, snapshot: FlowSnapshot) -> None:
        self._write(
            """
            INSERT INTO flow_snapshots (
                run_at_ms, data_time_ms, trading_day, product_code, underlying, call_inflow,
                put_inflow, net_inflow, call_contract_count, put_contract_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_at_ms, product_code) DO UPDATE SET
                data_time_ms = excluded.data_time_ms,
                trading_day = excluded.trading_day,
                underlying = excluded.underlying,
                call_inflow = excluded.call_inflow,
                put_inflow = excluded.put_inflow,
                net_inflow = excluded.net_inflow,
                call_contract_count = excluded.call_contract_count,
                put_contract_count = excluded.put_contract_count
            """,
            (
                snapshot.run_at_ms, snapshot.data_time_ms, snapshot.trading_day,
                snapshot.product_code, snapshot.underlying, str(snapshot.call_inflow),
                str(snapshot.put_inflow), str(snapshot.net_inflow),
                snapshot.call_contract_count, snapshot.put_contract_count,
            ),
        )

    def sum_hour_flow(self, product_code: str, current_ms: int) -> Decimal:
        rows = self._read_all(
            """
            SELECT net_inflow FROM flow_snapshots
            WHERE product_code = ? AND run_at_ms > ? AND run_at_ms <= ?
            """,
            (product_code, current_ms - 3_600_000, current_ms),
        )
        return sum((Decimal(row["net_inflow"]) for row in rows), Decimal("0"))

    def flow_snapshot_at(
        self, product_code: str, run_at_ms: int
    ) -> FlowSnapshot | None:
        row = self._read_one(
            """
            SELECT run_at_ms, data_time_ms, trading_day, product_code, underlying,
                   call_inflow, put_inflow, net_inflow, call_contract_count,
                   put_contract_count
            FROM flow_snapshots
            WHERE product_code = ? AND run_at_ms = ?
            """,
            (product_code, run_at_ms),
        )
        return None if row is None else self._flow_snapshot_from_row(row)

    def save_daily_iv_close(self, close: DailyIvClose) -> None:
        self._write(
            """
            INSERT INTO daily_iv_closes (trading_day, product_code, data_time_ms, atm_iv)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trading_day, product_code) DO UPDATE SET
                data_time_ms = excluded.data_time_ms,
                atm_iv = excluded.atm_iv
            """,
            (close.trading_day, close.product_code, close.data_time_ms, str(close.atm_iv)),
        )

    def save_option_snapshot(self, snapshot: OptionAnalyticsSnapshot) -> None:
        concentrations = json.dumps(
            [
                {
                    "strike": str(item.strike),
                    "open_interest": item.open_interest,
                    "share": str(item.share),
                }
                for item in snapshot.oi_concentrations
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        self._write(
            """
            INSERT INTO option_analytics_snapshots (
                run_at_ms, data_time_ms, trading_day, product_code, product_name,
                underlying, expire, rr25, call_volume_delta, put_volume_delta,
                call_turnover_delta, put_turnover_delta, call_open_interest,
                put_open_interest, call_pre_open_interest, put_pre_open_interest,
                volume_pcr, turnover_pcr, oi_pcr, oi_concentrations_json,
                flow_baseline_ready, oi_baseline_ready, next_expire, next_atm_iv,
                call_oi_baseline_ready, put_oi_baseline_ready
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_at_ms, product_code) DO UPDATE SET
                data_time_ms = excluded.data_time_ms,
                trading_day = excluded.trading_day,
                product_name = excluded.product_name,
                underlying = excluded.underlying,
                expire = excluded.expire,
                rr25 = excluded.rr25,
                call_volume_delta = excluded.call_volume_delta,
                put_volume_delta = excluded.put_volume_delta,
                call_turnover_delta = excluded.call_turnover_delta,
                put_turnover_delta = excluded.put_turnover_delta,
                call_open_interest = excluded.call_open_interest,
                put_open_interest = excluded.put_open_interest,
                call_pre_open_interest = excluded.call_pre_open_interest,
                put_pre_open_interest = excluded.put_pre_open_interest,
                volume_pcr = excluded.volume_pcr,
                turnover_pcr = excluded.turnover_pcr,
                oi_pcr = excluded.oi_pcr,
                oi_concentrations_json = excluded.oi_concentrations_json,
                flow_baseline_ready = excluded.flow_baseline_ready,
                oi_baseline_ready = excluded.oi_baseline_ready,
                next_expire = excluded.next_expire,
                next_atm_iv = excluded.next_atm_iv,
                call_oi_baseline_ready = excluded.call_oi_baseline_ready,
                put_oi_baseline_ready = excluded.put_oi_baseline_ready
            """,
            (
                snapshot.run_at_ms, snapshot.data_time_ms, snapshot.trading_day,
                snapshot.product_code, snapshot.product_name, snapshot.underlying,
                snapshot.expire, _optional_text(snapshot.rr25),
                snapshot.call_volume_delta, snapshot.put_volume_delta,
                str(snapshot.call_turnover_delta), str(snapshot.put_turnover_delta),
                snapshot.call_open_interest, snapshot.put_open_interest,
                snapshot.call_pre_open_interest, snapshot.put_pre_open_interest,
                _optional_text(snapshot.volume_pcr),
                _optional_text(snapshot.turnover_pcr),
                _optional_text(snapshot.oi_pcr), concentrations,
                int(snapshot.flow_baseline_ready), int(snapshot.oi_baseline_ready),
                snapshot.next_expire, _optional_text(snapshot.next_atm_iv),
                int(snapshot.call_oi_baseline_ready),
                int(snapshot.put_oi_baseline_ready),
            ),
        )

    def option_snapshot_at(
        self, product_code: str, run_at_ms: int
    ) -> OptionAnalyticsSnapshot | None:
        row = self._read_one(
            """
            SELECT * FROM option_analytics_snapshots
            WHERE product_code = ? AND run_at_ms = ?
            """,
            (product_code, run_at_ms),
        )
        return None if row is None else self._option_snapshot_from_row(row)

    def save_daily_option_close(self, close: DailyOptionClose) -> None:
        self._write(
            """
            INSERT INTO daily_option_closes (
                trading_day, product_code, data_time_ms, rr25,
                call_open_interest, put_open_interest
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_day, product_code) DO UPDATE SET
                data_time_ms = excluded.data_time_ms,
                rr25 = COALESCE(
                    excluded.rr25,
                    daily_option_closes.rr25
                ),
                call_open_interest = COALESCE(
                    excluded.call_open_interest,
                    daily_option_closes.call_open_interest
                ),
                put_open_interest = COALESCE(
                    excluded.put_open_interest,
                    daily_option_closes.put_open_interest
                )
            """,
            (
                close.trading_day, close.product_code,
                close.data_time_ms, _optional_text(close.rr25),
                close.call_open_interest, close.put_open_interest,
            ),
        )

    def daily_option_closes(
        self, product_code: str, limit: int
    ) -> list[DailyOptionClose]:
        rows = self._read_all(
            """
            SELECT trading_day, product_code, data_time_ms, rr25,
                   call_open_interest, put_open_interest
            FROM (
                SELECT trading_day, product_code, data_time_ms, rr25,
                       call_open_interest, put_open_interest
                FROM daily_option_closes
                WHERE product_code = ?
                ORDER BY trading_day DESC
                LIMIT ?
            )
            ORDER BY trading_day ASC
            """,
            (product_code, limit),
        )
        return [
            DailyOptionClose(
                row["trading_day"], row["product_code"],
                row["data_time_ms"], (
                    Decimal(row["rr25"])
                    if row["rr25"] is not None else None
                ),
                row["call_open_interest"], row["put_open_interest"],
            )
            for row in rows
        ]

    def save_daily_market_close(self, close: DailyMarketClose) -> None:
        self._write(
            """
            INSERT INTO daily_market_closes (
                trading_day, product_code, data_time_ms, close_price, atm_iv
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(trading_day, product_code) DO UPDATE SET
                data_time_ms = excluded.data_time_ms,
                close_price = excluded.close_price,
                atm_iv = excluded.atm_iv
            """,
            (
                close.trading_day, close.product_code, close.data_time_ms,
                str(close.close_price), str(close.atm_iv),
            ),
        )

    def daily_market_closes(self, product_code: str, limit: int) -> list[DailyMarketClose]:
        rows = self._read_all(
            """
            SELECT trading_day, product_code, data_time_ms, close_price, atm_iv
            FROM (
                SELECT trading_day, product_code, data_time_ms, close_price, atm_iv
                FROM daily_market_closes
                WHERE product_code = ?
                ORDER BY trading_day DESC
                LIMIT ?
            )
            ORDER BY trading_day ASC
            """,
            (product_code, limit),
        )
        return [self._daily_market_close_from_row(row) for row in rows]

    def previous_market_close(
        self, product_code: str, trading_day: str
    ) -> DailyMarketClose | None:
        row = self._read_one(
            """
            SELECT trading_day, product_code, data_time_ms, close_price, atm_iv
            FROM daily_market_closes
            WHERE product_code = ? AND trading_day < ?
            ORDER BY trading_day DESC
            LIMIT 1
            """,
            (product_code, trading_day),
        )
        return None if row is None else self._daily_market_close_from_row(row)

    def daily_iv_closes(self, product_code: str, limit: int) -> list[DailyIvClose]:
        rows = self._read_all(
            """
            SELECT trading_day, product_code, data_time_ms, atm_iv
            FROM (
                SELECT trading_day, product_code, data_time_ms, atm_iv
                FROM daily_iv_closes
                WHERE product_code = ?
                ORDER BY trading_day DESC
                LIMIT ?
            )
            ORDER BY trading_day ASC
            """,
            (product_code, limit),
        )
        return [
            DailyIvClose(
                row["trading_day"], row["product_code"], row["data_time_ms"],
                Decimal(row["atm_iv"]),
            )
            for row in rows
        ]

    def record_run(
        self,
        run_id: str,
        started_at_ms: int,
        finished_at_ms: int | None,
        status: str,
        coverage_ratio: Decimal | None,
        error_summary: str | None,
    ) -> None:
        self._write(
            """
            INSERT INTO runs (
                run_id, started_at_ms, finished_at_ms, status, coverage_ratio, error_summary
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                started_at_ms = excluded.started_at_ms,
                finished_at_ms = excluded.finished_at_ms,
                status = excluded.status,
                coverage_ratio = excluded.coverage_ratio,
                error_summary = excluded.error_summary
            """,
            (
                run_id, started_at_ms, finished_at_ms, status,
                None if coverage_ratio is None else str(coverage_ratio), error_summary,
            ),
        )

    def prune(self, cutoff_ms: int) -> None:
        for statement, column in (
            ("market_snapshots", "run_at_ms"),
            ("flow_snapshots", "run_at_ms"),
            ("option_analytics_snapshots", "run_at_ms"),
            ("contract_oi_changes", "run_at_ms"),
            ("daily_iv_closes", "data_time_ms"),
            ("daily_market_closes", "data_time_ms"),
            ("daily_option_closes", "data_time_ms"),
            ("runs", "started_at_ms"),
        ):
            self._write(f"DELETE FROM {statement} WHERE {column} < ?", (cutoff_ms,))
        self._write(
            "DELETE FROM contract_mappings WHERE resolved_at_ms < ?",
            (cutoff_ms,),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _write(self, statement: str, parameters: tuple[object, ...]) -> None:
        if self._transaction_connection is not None:
            self._transaction_connection.execute(statement, parameters)
            return
        connection = self._connect()
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    def _write_many(self, statement: str, parameters: list[tuple[object, ...]]) -> None:
        if self._transaction_connection is not None:
            self._transaction_connection.executemany(statement, parameters)
            return
        connection = self._connect()
        try:
            connection.executemany(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    def _read_one(self, statement: str, parameters: tuple[object, ...]) -> sqlite3.Row | None:
        if self._transaction_connection is not None:
            return self._transaction_connection.execute(statement, parameters).fetchone()
        connection = self._connect()
        try:
            return connection.execute(statement, parameters).fetchone()
        finally:
            connection.close()

    def _read_all(self, statement: str, parameters: tuple[object, ...]) -> list[sqlite3.Row]:
        if self._transaction_connection is not None:
            return self._transaction_connection.execute(statement, parameters).fetchall()
        connection = self._connect()
        try:
            return connection.execute(statement, parameters).fetchall()
        finally:
            connection.close()

    @staticmethod
    def _contract_state_from_row(row: sqlite3.Row) -> ContractVolumeState:
        return ContractVolumeState(
            row["trading_day"], row["underlying"], row["symbol"], row["side"],
            row["volume"],
            None if row["average_price"] is None else Decimal(row["average_price"]),
            Decimal(row["last_price"]), Decimal(row["multiplier"]), row["data_time_ms"],
        )

    @staticmethod
    def _contract_oi_change_from_row(
        row: sqlite3.Row,
    ) -> ContractOiChange:
        side = row["side"]
        open_interest = row["open_interest"]
        pre_open_interest = row["pre_open_interest"]
        delta_open_interest = row["delta_open_interest"]
        if side not in ("C", "P"):
            raise ValueError("invalid contract OI data")
        if (
            not isinstance(open_interest, int)
            or isinstance(open_interest, bool)
            or open_interest < 0
            or not isinstance(pre_open_interest, int)
            or isinstance(pre_open_interest, bool)
            or pre_open_interest < 0
            or not isinstance(delta_open_interest, int)
            or isinstance(delta_open_interest, bool)
            or delta_open_interest != open_interest - pre_open_interest
        ):
            raise ValueError("invalid contract OI data")
        try:
            strike = Decimal(row["strike"])
        except Exception:
            raise ValueError("invalid contract OI data") from None
        if not strike.is_finite() or strike <= 0:
            raise ValueError("invalid contract OI data")
        raw_multiplier = row["multiplier"]
        if raw_multiplier is None:
            multiplier = None
        else:
            try:
                multiplier = Decimal(raw_multiplier)
            except Exception:
                raise ValueError("invalid contract OI data") from None
            if not multiplier.is_finite() or multiplier <= 0:
                raise ValueError("invalid contract OI data")
        raw_option_last_price = row["option_last_price"]
        if raw_option_last_price is None:
            option_last_price = None
        else:
            try:
                option_last_price = Decimal(raw_option_last_price)
            except Exception:
                raise ValueError("invalid contract OI data") from None
            if not option_last_price.is_finite() or option_last_price <= 0:
                raise ValueError("invalid contract OI data")
        for field in (
            "trading_day",
            "product_code",
            "product_name",
            "underlying",
            "expire",
            "symbol",
        ):
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError("invalid contract OI data")
        return ContractOiChange(
            run_at_ms=row["run_at_ms"],
            data_time_ms=row["data_time_ms"],
            trading_day=row["trading_day"],
            product_code=row["product_code"],
            product_name=row["product_name"],
            underlying=row["underlying"],
            expire=row["expire"],
            symbol=row["symbol"],
            side=side,
            strike=strike,
            open_interest=open_interest,
            pre_open_interest=pre_open_interest,
            delta_open_interest=delta_open_interest,
            multiplier=multiplier,
            option_last_price=option_last_price,
        )

    @staticmethod
    def _market_snapshot_from_row(row: sqlite3.Row) -> MarketSnapshot:
        return MarketSnapshot(
            row["run_at_ms"], row["data_time_ms"], row["trading_day"],
            row["product_code"], row["product_name"], row["underlying"],
            Decimal(row["last_price"]), Decimal(row["pre_settlement_price"]),
            Decimal(row["atm_iv"]),
        )

    @staticmethod
    def _flow_snapshot_from_row(row: sqlite3.Row) -> FlowSnapshot:
        return FlowSnapshot(
            row["run_at_ms"], row["data_time_ms"], row["trading_day"],
            row["product_code"], row["underlying"], Decimal(row["call_inflow"]),
            Decimal(row["put_inflow"]), Decimal(row["net_inflow"]),
            row["call_contract_count"], row["put_contract_count"],
        )

    @staticmethod
    def _daily_market_close_from_row(row: sqlite3.Row) -> DailyMarketClose:
        return DailyMarketClose(
            row["trading_day"], row["product_code"], row["data_time_ms"],
            Decimal(row["close_price"]), Decimal(row["atm_iv"]),
        )

    @staticmethod
    def _option_snapshot_from_row(row: sqlite3.Row) -> OptionAnalyticsSnapshot:
        raw_concentrations = json.loads(row["oi_concentrations_json"])
        if not isinstance(raw_concentrations, list):
            raise ValueError("invalid option concentration data")
        concentrations: list[OiConcentration] = []
        for item in raw_concentrations:
            if (
                not isinstance(item, dict)
                or set(item) != {"strike", "open_interest", "share"}
                or not isinstance(item["open_interest"], int)
                or isinstance(item["open_interest"], bool)
                or item["open_interest"] < 0
            ):
                raise ValueError("invalid option concentration data")
            concentrations.append(OiConcentration(
                Decimal(item["strike"]), item["open_interest"],
                Decimal(item["share"]),
            ))
        return OptionAnalyticsSnapshot(
            run_at_ms=row["run_at_ms"],
            data_time_ms=row["data_time_ms"],
            trading_day=row["trading_day"],
            product_code=row["product_code"],
            product_name=row["product_name"],
            underlying=row["underlying"],
            expire=row["expire"],
            rr25=_optional_decimal(row["rr25"]),
            call_volume_delta=row["call_volume_delta"],
            put_volume_delta=row["put_volume_delta"],
            call_turnover_delta=Decimal(row["call_turnover_delta"]),
            put_turnover_delta=Decimal(row["put_turnover_delta"]),
            call_open_interest=row["call_open_interest"],
            put_open_interest=row["put_open_interest"],
            call_pre_open_interest=row["call_pre_open_interest"],
            put_pre_open_interest=row["put_pre_open_interest"],
            volume_pcr=_optional_decimal(row["volume_pcr"]),
            turnover_pcr=_optional_decimal(row["turnover_pcr"]),
            oi_pcr=_optional_decimal(row["oi_pcr"]),
            oi_concentrations=tuple(concentrations),
            flow_baseline_ready=bool(row["flow_baseline_ready"]),
            oi_baseline_ready=bool(row["oi_baseline_ready"]),
            next_expire=row["next_expire"],
            next_atm_iv=_optional_decimal(row["next_atm_iv"]),
            call_oi_baseline_ready=_strict_bool(
                row["call_oi_baseline_ready"], "option baseline"
            ),
            put_oi_baseline_ready=_strict_bool(
                row["put_oi_baseline_ready"], "option baseline"
            ),
        )


def _optional_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _strict_bool(value: int, field: str) -> bool:
    if value not in (0, 1):
        raise ValueError(f"invalid {field} data")
    return bool(value)
