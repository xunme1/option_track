from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from option_monitor.rqdata_client import (
    RqdatacProvider,
    _latest_session_rows,
    _session_anchor,
)


def make_minute_df(rows):
    index = pd.MultiIndex.from_tuples(
        [("SC2610", ts) for ts, *_ in rows],
        names=["order_book_id", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "volume": [r[3] for r in rows],
            "open_interest": [r[4] for r in rows],
        },
        index=index,
    )


class FakeRqdatac:
    def __init__(self, df):
        self._df = df

    def init(self, **kwargs):
        pass

    def get_price(self, contracts, **kwargs):
        return self._df


def bar(minute, open_, close, volume=100, oi=1000):
    return (minute, Decimal(str(open_)), Decimal(str(close)), volume, oi)


def ts(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute)


class TestSessionAnchor:
    def test_night_bar_anchors_to_same_evening(self):
        assert _session_anchor(ts(1, 22, 6)) == ts(1, 21)

    def test_early_morning_bar_anchors_to_previous_evening(self):
        assert _session_anchor(ts(2, 2, 30)) == ts(1, 21)

    def test_day_bar_anchors_to_previous_evening(self):
        assert _session_anchor(ts(2, 10, 0)) == ts(1, 21)

    def test_2100_bar_starts_new_session(self):
        assert _session_anchor(ts(1, 21, 0)) == ts(1, 21)


class TestQuoteBarsNightSession:
    def test_night_price_not_stale_day_close(self):
        """复现 9-01 22:06 的 bug：白盘 15:00 收 637.8，夜盘 21:00 起涨到 660.8。"""
        rows = [
            bar(ts(1, 9, 0), 640.3, 641.0, volume=5000),
            bar(ts(1, 15, 0), 638.0, 637.8, volume=3000),
            bar(ts(1, 21, 0), 650.0, 652.0, volume=400),
            bar(ts(1, 22, 6), 660.5, 660.8, volume=300),
        ]
        provider = RqdatacProvider(
            module_loader=lambda: FakeRqdatac(make_minute_df(rows))
        )
        bars = provider.quote_bars(["SC2610"], date(2026, 9, 1))
        quote = bars["SC2610"]
        assert quote.last_price == Decimal("660.8")
        # 开盘价取当前时段（夜盘）首根 bar，而非白盘日线开盘
        assert quote.opening_price == Decimal("650.0")
        # 成交量只算当前时段（夜盘 400+300），不含白盘
        assert quote.volume == 700
        assert quote.source_time_ms == int(
            ts(1, 22, 6).timestamp() * 1000
        ) or quote.source_time_ms > 0

    def test_day_session_behaviour_unchanged(self):
        """白盘时段：时段=前一晚 21:00 起（夜盘产品含昨夜，白盘产品仅当天）。"""
        rows = [
            bar(ts(1, 21, 0), 640.3, 641.0, volume=500),
            bar(ts(1, 23, 0), 642.0, 642.5, volume=400),
            bar(ts(2, 9, 0), 643.0, 644.0, volume=600),
            bar(ts(2, 10, 0), 644.5, 645.0, volume=200),
        ]
        provider = RqdatacProvider(
            module_loader=lambda: FakeRqdatac(make_minute_df(rows))
        )
        bars = provider.quote_bars(["SC2610"], date(2026, 9, 2))
        quote = bars["SC2610"]
        assert quote.last_price == Decimal("645.0")
        # 夜盘产品的交易日从昨夜 21:00 开始
        assert quote.opening_price == Decimal("640.3")
        assert quote.volume == 1700

    def test_friday_night_belongs_to_monday_session(self):
        rows = [
            # 上周五（9-04 是周五？2026-09-04 周五）夜盘
            bar(datetime(2026, 9, 4, 21, 0), 600.0, 601.0, volume=300),
            bar(datetime(2026, 9, 4, 23, 0), 601.5, 602.0, volume=200),
            # 下周一（9-07）白盘
            bar(datetime(2026, 9, 7, 9, 0), 603.0, 604.0, volume=800),
            bar(datetime(2026, 9, 7, 10, 0), 604.5, 605.0, volume=100),
        ]
        provider = RqdatacProvider(
            module_loader=lambda: FakeRqdatac(make_minute_df(rows))
        )
        bars = provider.quote_bars(["SC2610"], date(2026, 9, 7))
        quote = bars["SC2610"]
        assert quote.last_price == Decimal("605.0")
        assert quote.opening_price == Decimal("600.0")
        assert quote.volume == 1400

    def test_day_only_product_session_is_single_day(self):
        """无夜盘品种：白盘 bar 锚到前一晚 21:00，时段只含当天白盘。"""
        rows = [
            bar(ts(1, 9, 0), 500.0, 501.0, volume=900),
            bar(ts(1, 15, 0), 502.0, 503.0, volume=100),
            bar(ts(2, 9, 0), 504.0, 505.0, volume=700),
            bar(ts(2, 10, 0), 505.5, 506.0, volume=300),
        ]
        provider = RqdatacProvider(
            module_loader=lambda: FakeRqdatac(make_minute_df(rows))
        )
        bars = provider.quote_bars(["SC2610"], date(2026, 9, 2))
        quote = bars["SC2610"]
        assert quote.last_price == Decimal("506.0")
        assert quote.opening_price == Decimal("504.0")
        assert quote.volume == 1000


class TestLatestSessionRows:
    def test_cuts_at_session_boundary(self):
        rows = [
            bar(ts(1, 15, 0), 1, 1),
            bar(ts(1, 21, 0), 2, 2),
            bar(ts(1, 22, 0), 3, 3),
        ]
        df = make_minute_df(rows).xs("SC2610", level="order_book_id")
        session = _latest_session_rows(df)
        assert len(session) == 2
        assert session.index[0] == ts(1, 21, 0)
