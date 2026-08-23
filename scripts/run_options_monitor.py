from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from option_monitor.aliyun_oss_client import create_aliyun_oss_uploader
from option_monitor.eastmoney_client import EastmoneyFuturesClient
from option_monitor.hitick_client import HitickClient
from option_monitor.openvlab_browser import OpenVlabRankingSnapshotter
from option_monitor.rqdata_client import (
    PrimaryFallbackFuturesClient,
    RqdataFuturesClient,
)
from option_monitor.runner import MonitorRunner
from option_monitor.settings import SHANGHAI, load_monitor_settings
from option_monitor.storage import MonitorStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--now")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-anomaly-report", action="store_true")
    parser.add_argument("--force-all-products", action="store_true")
    parser.add_argument("--require-full-coverage", action="store_true")
    args = parser.parse_args(argv)
    if args.require_full_coverage and not (
        args.force_anomaly_report and args.force_all_products
    ):
        parser.error(
            "--require-full-coverage requires --force-anomaly-report "
            "and --force-all-products"
        )
    if args.force_all_products and not args.force_anomaly_report:
        parser.error(
            "--force-all-products requires --force-anomaly-report"
        )

    try:
        root = args.root.resolve()
        settings = load_monitor_settings(root)
        store = MonitorStore(settings.database_path)
        store.initialize()
        image_uploader = (
            None if args.dry_run else (
            create_aliyun_oss_uploader(
                access_key_id=settings.aliyun_oss_access_key_id,
                access_key_secret=settings.aliyun_oss_access_key_secret,
                region=settings.aliyun_oss_region,
                bucket=settings.aliyun_oss_bucket,
                endpoint=settings.aliyun_oss_endpoint,
                prefix=settings.aliyun_oss_prefix,
            )
            if settings.aliyun_oss_configured
            else None
            )
        )
        runner = MonitorRunner(
            settings=settings,
            store=store,
            client=HitickClient(settings.orange_api_token),
            price_client=PrimaryFallbackFuturesClient(
                RqdataFuturesClient(api_key=settings.rqdata_api_key),
                EastmoneyFuturesClient(),
            ),
            image_uploader=image_uploader,
            local_only=args.dry_run,
            openvlab_snapshotter=(
                None if args.dry_run else OpenVlabRankingSnapshotter(
                    settings.openvlab_profile_dir
                )
            ),
        )
        now = _parse_now(args.now)
        if args.force_anomaly_report:
            manifest = runner.run(
                now,
                force_anomaly_report=True,
                force_all_products=args.force_all_products,
                require_full_coverage=args.require_full_coverage,
            )
        else:
            manifest = runner.run(now)
        manifest_path = (
            settings.outbox_root / manifest.run_id / "manifest.json"
        ).resolve()
    except Exception:
        print("monitor run failed", file=sys.stderr)
        return 1

    print(f"MANIFEST_PATH={manifest_path}")
    return 0


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


if __name__ == "__main__":
    raise SystemExit(main())
