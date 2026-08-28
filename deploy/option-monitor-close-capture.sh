#!/usr/bin/env bash
# 收盘静默采集：写入 RR25 日度收盘基线（state/option_monitor.sqlite3），不投递钉钉。
# 与投递任务共用 state/run.lock，避免与 14:30 运行或龙虾手动任务互相踩状态。
# 采集成功后删除本次 outbox 运行目录，防止投递任务稍后拾取其中的 alerts 重复推送。
set -euo pipefail
cd /opt/option-monitor
exec 9>>/opt/option-monitor/state/run.lock
flock -w 900 9
OUT=$(/opt/option-monitor/.venv/bin/python /opt/option-monitor/scripts/run_options_monitor.py \
  --root /opt/option-monitor --force-all-products)
printf '%s\n' "$OUT"
MANIFEST=$(printf '%s\n' "$OUT" | sed -n 's/^MANIFEST_PATH=//p' | tail -1)
if [ -n "$MANIFEST" ]; then
  rm -rf "$(dirname "$MANIFEST")"
fi
