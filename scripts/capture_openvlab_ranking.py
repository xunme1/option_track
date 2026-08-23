from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from option_monitor.openvlab_browser import OpenVlabRankingSnapshotter
from option_monitor.settings import SHANGHAI


def main(
    argv: Sequence[str] | None = None,
    *,
    snapshotter_factory: Callable[[Path], object] = (
        OpenVlabRankingSnapshotter
    ),
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--at")
    args = parser.parse_args(argv)

    try:
        root = args.root.resolve()
        state = (root / "state").resolve()
        state.mkdir(parents=True, exist_ok=True)
        output = args.output.resolve()
        output.relative_to(state)
        if output.suffix.lower() != ".png" or state.is_symlink():
            raise ValueError("invalid output")
        output.parent.mkdir(parents=True, exist_ok=True)
        captured_at = _parse_at(args.at)
        snapshotter = snapshotter_factory(
            state / "openvlab-browser-profile"
        )
        snapshotter.capture(output, captured_at)
    except Exception:
        print("OpenVLab snapshot failed", file=sys.stderr)
        return 1
    print(f"OPENVLAB_SNAPSHOT={output}")
    return 0


def _parse_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


if __name__ == "__main__":
    raise SystemExit(main())
