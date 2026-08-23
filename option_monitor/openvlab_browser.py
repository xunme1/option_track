from __future__ import annotations

import re
import time
from datetime import datetime
import os
from pathlib import Path
from typing import Callable, Literal, Sequence
from urllib.parse import urlsplit

from option_monitor.openvlab_snapshot import (
    OpenVlabSnapshotError,
    RankingEntry,
    compose_openvlab_rankings,
    parse_display_amount,
    validate_top_eight,
)


LOGIN_MARKER = ".login-initialized"
CAPTURE_DEADLINE_SECONDS = 120
RANKING_URL = "https://www.openvlab.cn/flow/ranking"
INITIAL_TABLE_TIMEOUT_MS = 45_000
INTERACTION_TIMEOUT_MS = 15_000


class OpenVlabRankingSnapshotter:
    def __init__(
        self,
        profile_dir: Path,
        session_factory: Callable | None = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.session_factory = session_factory or _playwright_session

    def capture(self, output_path: Path, captured_at: datetime) -> Path:
        _validate_profile(self.profile_dir)
        destination = Path(output_path)
        deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS
        increase_path = destination.with_name(
            f".{destination.stem}-increase-panel.png"
        )
        decrease_path = destination.with_name(
            f".{destination.stem}-decrease-panel.png"
        )
        try:
            with self.session_factory(self.profile_dir, deadline) as session:
                validate_top_eight(
                    session.capture_top_eight("increase", increase_path),
                    "positive",
                )
                validate_top_eight(
                    session.capture_top_eight("decrease", decrease_path),
                    "negative",
                )
            return compose_openvlab_rankings(
                increase_path,
                decrease_path,
                destination,
                captured_at,
            )
        except OpenVlabSnapshotError:
            raise
        except Exception:
            raise OpenVlabSnapshotError(
                "OpenVLab browser capture failed"
            ) from None
        finally:
            increase_path.unlink(missing_ok=True)
            decrease_path.unlink(missing_ok=True)


def _find_unique_header(headers: Sequence[str], target: str) -> int:
    normalized_target = _normalize_header(target)
    matches = [
        index
        for index, value in enumerate(headers)
        if _normalize_header(value) == normalized_target
    ]
    if len(matches) != 1:
        raise OpenVlabSnapshotError("OpenVLab ranking header is invalid")
    return matches[0]


def _ranking_entries(
    symbol_texts: Sequence[str], amount_texts: Sequence[str]
) -> tuple[RankingEntry, ...]:
    if len(symbol_texts) != len(amount_texts):
        raise OpenVlabSnapshotError("OpenVLab ranking rows are invalid")
    entries = []
    for symbol_text, amount_text in zip(symbol_texts, amount_texts):
        lines = tuple(
            line.strip() for line in symbol_text.splitlines() if line.strip()
        )
        if not lines:
            raise OpenVlabSnapshotError("OpenVLab ranking symbol is missing")
        entries.append(
            RankingEntry(lines[-1], parse_display_amount(amount_text))
        )
    return tuple(entries)


def _choose_sorted_entries(
    ranking_kind: Literal["increase", "decrease"],
    click_sort: Callable[[], None],
    read_entries: Callable[[], Sequence[RankingEntry]],
) -> tuple[RankingEntry, ...]:
    expected_sign = (
        "positive" if ranking_kind == "increase" else "negative"
    )
    last_error = None
    for _ in range(2):
        click_sort()
        entries = tuple(read_entries())
        try:
            return validate_top_eight(entries, expected_sign)
        except OpenVlabSnapshotError as error:
            last_error = error
    if last_error is None:
        raise OpenVlabSnapshotError("OpenVLab ranking is invalid")
    raise last_error


class _PlaywrightRankingSession:
    def __init__(self, page_adapter, deadline: float):
        self.page = page_adapter
        self.deadline = deadline
        self.table = None
        self.symbol_index = None
        self.amount_index = None

    def __enter__(self):
        self.page.open_ranking(
            RANKING_URL,
            _remaining_timeout_ms(
                self.deadline, INITIAL_TABLE_TIMEOUT_MS
            ),
        )
        self.table = self.page.find_ranking_table(
            _remaining_timeout_ms(
                self.deadline, INITIAL_TABLE_TIMEOUT_MS
            )
        )
        headers = self.table.header_texts()
        self.symbol_index = _find_unique_header(headers, "期权合约")
        self.amount_index = _find_unique_header(headers, "增仓额")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.page.close()

    def capture_top_eight(
        self,
        ranking_kind: Literal["increase", "decrease"],
        output_path: Path,
    ) -> tuple[RankingEntry, ...]:
        if (
            self.table is None
            or self.symbol_index is None
            or self.amount_index is None
        ):
            raise OpenVlabSnapshotError(
                "OpenVLab ranking session is not open"
            )

        def click_sort() -> None:
            self.table.click_header(
                self.amount_index,
                _remaining_timeout_ms(
                    self.deadline, INTERACTION_TIMEOUT_MS
                ),
            )

        def read_entries() -> tuple[RankingEntry, ...]:
            symbols, amounts = self.table.stable_row_texts(
                self.symbol_index,
                self.amount_index,
                8,
                _remaining_timeout_ms(
                    self.deadline, INTERACTION_TIMEOUT_MS
                ),
            )
            return _ranking_entries(symbols, amounts)

        entries = _choose_sorted_entries(
            ranking_kind, click_sort, read_entries
        )
        self.table.screenshot_first_rows(
            8,
            Path(output_path),
            _remaining_timeout_ms(
                self.deadline, INTERACTION_TIMEOUT_MS
            ),
        )
        return entries


class _PlaywrightPageAdapter:
    def __init__(self, page, close_callback: Callable[[], None]):
        self.page = page
        self.close_callback = close_callback

    def open_ranking(self, url: str, timeout_ms: int) -> None:
        self.page.goto(
            url, wait_until="domcontentloaded", timeout=timeout_ms
        )
        if not _is_ranking_url(self.page.url):
            raise OpenVlabSnapshotError(
                "OpenVLab ranking page is unavailable"
            )

    def find_ranking_table(self, timeout_ms: int):
        tables = self.page.locator("table:visible")
        try:
            tables.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            raise OpenVlabSnapshotError(
                "OpenVLab ranking table is unavailable"
            ) from None

        matches = []
        for table in tables.all():
            adapter = _PlaywrightTableAdapter(table, self.page)
            headers = adapter.header_texts()
            try:
                _find_unique_header(headers, "期权合约")
                _find_unique_header(headers, "增仓额")
                _find_unique_header(headers, "持仓量")
            except OpenVlabSnapshotError:
                continue
            matches.append(adapter)
        if len(matches) != 1:
            raise OpenVlabSnapshotError(
                "OpenVLab ranking table is unavailable"
            )
        return matches[0]

    def close(self) -> None:
        self.close_callback()


class _PlaywrightTableAdapter:
    def __init__(self, table, page):
        self.table = table
        self.page = page

    def header_texts(self) -> tuple[str, ...]:
        return tuple(
            self.table.locator("thead th").all_inner_texts()
        )

    def click_header(self, column_index: int, timeout_ms: int) -> None:
        self.table.locator("thead th").nth(column_index).click(
            timeout=timeout_ms
        )

    def stable_row_texts(
        self,
        symbol_index: int,
        amount_index: int,
        row_count: int,
        timeout_ms: int,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        deadline = time.monotonic() + timeout_ms / 1000
        previous = None
        while True:
            rows = self.table.locator("tbody tr:visible")
            if rows.count() >= row_count:
                symbols = []
                amounts = []
                cell_timeout = _remaining_timeout_ms(deadline, timeout_ms)
                for index in range(row_count):
                    cells = rows.nth(index).locator("td")
                    symbols.append(
                        cells.nth(symbol_index).inner_text(
                            timeout=cell_timeout
                        )
                    )
                    amounts.append(
                        cells.nth(amount_index).inner_text(
                            timeout=cell_timeout
                        )
                    )
                current = (tuple(symbols), tuple(amounts))
                if current == previous:
                    return current
                previous = current
            wait_ms = min(250, _remaining_timeout_ms(deadline, 250))
            self.page.wait_for_timeout(wait_ms)

    def screenshot_first_rows(
        self, row_count: int, output_path: Path, timeout_ms: int
    ) -> None:
        rows = self.table.locator("tbody tr")
        total_rows = rows.count()
        if total_rows < row_count:
            raise OpenVlabSnapshotError(
                "OpenVLab ranking has fewer than eight rows"
            )
        hidden_rows = []
        try:
            for index in range(row_count, total_rows):
                row = rows.nth(index)
                original_style = row.get_attribute("style")
                hidden_rows.append((row, original_style))
                row.evaluate(
                    "element => { element.style.display = 'none'; }"
                )
            self.table.screenshot(
                path=str(output_path), timeout=timeout_ms, type="png"
            )
        finally:
            for row, original_style in hidden_rows:
                if original_style is None:
                    row.evaluate(
                        "element => element.removeAttribute('style')"
                    )
                else:
                    row.evaluate(
                        "(element, style) => "
                        "element.setAttribute('style', style)",
                        original_style,
                    )


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s?？]", "", value)


def _remaining_timeout_ms(deadline: float, cap_ms: int) -> int:
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise OpenVlabSnapshotError(
            "OpenVLab snapshot deadline expired"
        )
    return min(cap_ms, remaining_ms)


def _is_ranking_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.openvlab.cn"
        and parsed.path.rstrip("/") == "/flow/ranking"
        and not parsed.query
        and not parsed.fragment
    )


def _validate_profile(profile_dir: Path) -> None:
    marker = profile_dir / LOGIN_MARKER
    if (
        not profile_dir.is_dir()
        or profile_dir.is_symlink()
        or not marker.is_file()
        or marker.is_symlink()
    ):
        raise OpenVlabSnapshotError("OpenVLab login is not initialized")
    try:
        marker_text = marker.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise OpenVlabSnapshotError(
            "OpenVLab login is not initialized"
        ) from None
    if marker_text != "ready\n":
        raise OpenVlabSnapshotError("OpenVLab login is not initialized")


def _playwright_session(profile_dir: Path, deadline: float):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise OpenVlabSnapshotError(
            "OpenVLab browser is unavailable"
        ) from None

    playwright = None
    context = None
    try:
        playwright = sync_playwright().start()
        launch_options = {
            "user_data_dir": str(profile_dir),
            "headless": True,
            "viewport": {"width": 2048, "height": 1200},
            "locale": "zh-CN",
        }
        browser_channel = os.environ.get("OPENVLAB_BROWSER_CHANNEL")
        if browser_channel:
            launch_options["channel"] = browser_channel
        context = playwright.chromium.launch_persistent_context(
            **launch_options,
        )
        page = context.pages[0] if context.pages else context.new_page()

        def close() -> None:
            try:
                context.close()
            finally:
                playwright.stop()

        return _PlaywrightRankingSession(
            _PlaywrightPageAdapter(page, close), deadline
        )
    except Exception:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        raise OpenVlabSnapshotError(
            "OpenVLab browser is unavailable"
        ) from None
