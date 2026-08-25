from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}[+-]\d{4}$")
MAX_PAYLOAD_BYTES = 256 * 1024


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a generated monitor DingTalk Markdown body only."
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--payload",
        type=Path,
        help="Absolute or project-relative alerts.json path.",
    )
    source.add_argument(
        "--run-id",
        help="Read alerts.json from state/outbox/<run-id>/.",
    )
    source.add_argument(
        "--latest-ready",
        action="store_true",
        help="Read the newest ready alert manifest in state/outbox.",
    )
    parser.add_argument(
        "--include-title",
        action="store_true",
        help="Print the Markdown title on the first line before the body.",
    )
    args = parser.parse_args(argv)

    try:
        root = args.root.resolve()
        payload_path = _resolve_payload_path(root, args)
        title, text = _read_markdown_payload(payload_path)
    except (OSError, ValueError):
        print("monitor message is unavailable", file=sys.stderr)
        return 1

    if args.include_title:
        print(title)
        print()
    print(text)
    return 0


def _resolve_payload_path(root: Path, args) -> Path:
    outbox = (root / "state" / "outbox").resolve()
    if not outbox.is_dir() or outbox.is_symlink():
        raise ValueError("outbox is unavailable")
    if args.payload is not None:
        candidate = args.payload
        if not candidate.is_absolute():
            candidate = root / candidate
        path = candidate.resolve()
        if path.name != "alerts.json" or not path.is_relative_to(outbox):
            raise ValueError("payload path is invalid")
        return path
    if args.run_id is not None:
        if RUN_ID_PATTERN.fullmatch(args.run_id) is None:
            raise ValueError("run id is invalid")
        return _payload_from_manifest(outbox / args.run_id)
    return _latest_payload(outbox)


def _latest_payload(outbox: Path) -> Path:
    candidates = sorted(
        (
            item
            for item in outbox.iterdir()
            if item.is_dir()
            and not item.is_symlink()
            and RUN_ID_PATTERN.fullmatch(item.name) is not None
        ),
        reverse=True,
    )
    for run_directory in candidates:
        try:
            return _payload_from_manifest(run_directory)
        except (OSError, ValueError):
            continue
    raise ValueError("no ready alert payload")


def _payload_from_manifest(run_directory: Path) -> Path:
    manifest_path = run_directory / "manifest.json"
    document = _read_json(manifest_path)
    if (
        not isinstance(document, dict)
        or document.get("status") != "ready"
        or document.get("run_id") != run_directory.name
        or not isinstance(document.get("messages"), list)
    ):
        raise ValueError("manifest is invalid")
    messages = [
        item for item in document["messages"]
        if isinstance(item, dict) and item.get("kind") == "alerts"
    ]
    if len(messages) != 1:
        raise ValueError("alert payload is unavailable")
    payload_value = messages[0].get("payload_path")
    if not isinstance(payload_value, str):
        raise ValueError("alert payload is invalid")
    payload_path = Path(payload_value).resolve()
    if (
        payload_path.parent != run_directory.resolve()
        or payload_path.name != "alerts.json"
        or payload_path.is_symlink()
    ):
        raise ValueError("alert payload is invalid")
    return payload_path


def _read_markdown_payload(path: Path) -> tuple[str, str]:
    document = _read_json(path)
    try:
        markdown = document["markdown"]
        title = markdown["title"]
        text = markdown["text"]
    except (KeyError, TypeError):
        raise ValueError("payload is invalid") from None
    if (
        document.get("msgtype") != "markdown"
        or not isinstance(title, str)
        or not isinstance(text, str)
        or not title.strip()
        or not text.strip()
    ):
        raise ValueError("payload is invalid")
    return title, text


def _read_json(path: Path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_PAYLOAD_BYTES:
        raise ValueError("payload is invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ValueError("payload is invalid") from None


if __name__ == "__main__":
    raise SystemExit(main())
