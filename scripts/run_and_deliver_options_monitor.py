from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from option_monitor.dingtalk_alert import send_markdown
from option_monitor.settings import load_monitor_settings
from scripts.run_options_monitor import main as run_monitor


RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}[+-]\d{4}$")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the monitor and deliver ready DingTalk messages."
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--now")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-anomaly-report", action="store_true")
    parser.add_argument("--force-all-products", action="store_true")
    parser.add_argument("--require-full-coverage", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        with _run_lock(root):
            command = ["--root", str(root)]
            if args.now:
                command.extend(("--now", args.now))
            if args.dry_run:
                command.append("--dry-run")
            if args.force_anomaly_report:
                command.append("--force-anomaly-report")
            if args.force_all_products:
                command.append("--force-all-products")
            if args.require_full_coverage:
                command.append("--require-full-coverage")
            if run_monitor(command) != 0:
                return 1
            if args.dry_run:
                return 0
            delivered = deliver_ready_messages(root)
    except BlockingIOError:
        print("SKIPPED_OVERLAP=1")
        return 0
    except Exception:
        print("monitor delivery failed", file=sys.stderr)
        return 1

    print(f"DINGTALK_DELIVERED={delivered}")
    return 0


def deliver_ready_messages(root: Path) -> int:
    settings = load_monitor_settings(root)
    outbox_root = settings.outbox_root.resolve()
    state_path = settings.delivery_state_path.resolve()
    if not outbox_root.is_dir() or outbox_root.is_symlink():
        raise ValueError("outbox is unavailable")

    state = _read_state(state_path)
    delivered = 0
    failed = False
    for run_directory, messages in _ready_alert_messages(outbox_root):
        all_sent = True
        for delivery_key, payload_path in messages:
            existing = state["deliveries"].get(delivery_key, {})
            if existing.get("status") == "sent":
                continue
            payload = _read_payload(payload_path)
            try:
                response = send_markdown(
                    settings.dingtalk_webhook,
                    settings.dingtalk_secret,
                    payload["markdown"]["title"],
                    payload["markdown"]["text"],
                )
                if int(response.get("errcode", -1)) != 0:
                    raise ValueError("DingTalk rejected the message")
            except Exception:
                state["deliveries"][delivery_key] = {"status": "failed"}
                _write_state(state_path, state)
                failed = True
                all_sent = False
                continue
            state["deliveries"][delivery_key] = {"status": "sent"}
            _write_state(state_path, state)
            delivered += 1
        if all_sent:
            _remove_sent_run(outbox_root, run_directory)
    if failed:
        raise RuntimeError("DingTalk delivery failed")
    return delivered


def _ready_alert_messages(
    outbox_root: Path,
) -> Iterator[tuple[Path, tuple[tuple[str, Path], ...]]]:
    for run_directory in sorted(outbox_root.iterdir()):
        if (
            not run_directory.is_dir()
            or run_directory.is_symlink()
            or RUN_ID_PATTERN.fullmatch(run_directory.name) is None
        ):
            continue
        manifest_path = run_directory / "manifest.json"
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if (
            not isinstance(document, dict)
            or document.get("status") != "ready"
            or document.get("run_id") != run_directory.name
            or not isinstance(document.get("messages"), list)
        ):
            continue
        messages: list[tuple[str, Path]] = []
        valid = True
        for item in document["messages"]:
            if not isinstance(item, dict) or item.get("kind") != "alerts":
                continue
            key = item.get("delivery_key")
            raw_path = item.get("payload_path")
            if (
                not isinstance(key, str)
                or key != f"monitor:{run_directory.name}:alerts"
                or not isinstance(raw_path, str)
            ):
                valid = False
                break
            payload_path = Path(raw_path).resolve()
            if (
                payload_path.parent != run_directory.resolve()
                or payload_path.name != "alerts.json"
                or not payload_path.is_file()
                or payload_path.is_symlink()
            ):
                valid = False
                break
            messages.append((key, payload_path))
        if valid and messages:
            yield run_directory, tuple(messages)


def _read_payload(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        markdown = payload["markdown"]
        if (
            payload.get("msgtype") != "markdown"
            or not isinstance(markdown, dict)
            or not isinstance(markdown.get("title"), str)
            or not isinstance(markdown.get("text"), str)
            or not markdown["title"].strip()
            or not markdown["text"].strip()
        ):
            raise ValueError("payload is invalid")
        return payload
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("payload is invalid") from None


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {"deliveries": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(
            state.get("deliveries"), dict
        ):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError):
        raise ValueError("delivery state is invalid") from None


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_sent_run(outbox_root: Path, run_directory: Path) -> None:
    if (
        run_directory.parent.resolve() != outbox_root
        or RUN_ID_PATTERN.fullmatch(run_directory.name) is None
        or run_directory.is_symlink()
    ):
        raise ValueError("outbox run directory is invalid")
    shutil.rmtree(run_directory)


@contextmanager
def _run_lock(root: Path):
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "run.lock"
    with lock_path.open("a+", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())
