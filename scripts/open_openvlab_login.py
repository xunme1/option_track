from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from option_monitor.openvlab_browser import LOGIN_MARKER, RANKING_URL


def main(
    argv: Sequence[str] | None = None,
    *,
    login_runner: Callable[[Path], bool] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    runner = login_runner or run_interactive_login

    try:
        root = args.root.resolve()
        profile = root / "state" / "openvlab-browser-profile"
        _prepare_profile(root, profile)
        if not runner(profile):
            raise RuntimeError("ranking table was not confirmed")
        (profile / LOGIN_MARKER).write_text("ready\n", encoding="ascii")
    except Exception:
        print("OpenVLab login failed", file=sys.stderr)
        return 1
    print("OpenVLab login initialized")
    return 0


def run_interactive_login(profile_dir: Path) -> bool:
    from playwright.sync_api import sync_playwright

    confirmed = False
    with sync_playwright() as playwright:
        launch_options = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "viewport": {"width": 1600, "height": 1000},
            "locale": "zh-CN",
        }
        browser_channel = os.environ.get("OPENVLAB_BROWSER_CHANNEL")
        if browser_channel:
            launch_options["channel"] = browser_channel
        context = playwright.chromium.launch_persistent_context(
            **launch_options,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                RANKING_URL,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            print(
                "请在浏览器中登录 OpenVLab；看到期权排名表后关闭窗口。"
            )
            while not page.is_closed():
                if _has_ranking_table(page):
                    confirmed = True
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break
        finally:
            try:
                context.close()
            except Exception:
                pass
    return confirmed


def _has_ranking_table(page) -> bool:
    try:
        if page.get_by_text("请先登录", exact=True).count() > 0:
            return False
        for table in page.locator("table:visible").all():
            headers = "".join(
                table.locator("thead th").all_inner_texts()
            )
            if all(label in headers for label in ("期权合约", "增仓额", "持仓量")):
                return table.locator("tbody tr:visible").count() >= 8
    except Exception:
        return False
    return False


def _prepare_profile(root: Path, profile: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("invalid project root")
    state = profile.parent
    state.mkdir(parents=True, exist_ok=True)
    if state.is_symlink() or profile.is_symlink():
        raise RuntimeError("invalid browser profile")
    profile.mkdir(exist_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
