#!/usr/bin/env bash
# 静默采集：运行监控生成图表与钉钉文案（state/option_monitor.sqlite3、outbox），但不投递。
# 10:15/14:30 主任务与 15:00 收盘任务共用；群推送由龙虾连接器的定时任务完成。
# 与投递/手动任务共用 state/run.lock，避免并发互相踩状态。
# 采集成功后删除本次 outbox 运行目录：同槽位（5 分钟）的后续运行会基于 sqlite
# 状态重新生成 outbox，避免旧 alerts 被其他投递方拾取重复推送。
set -euo pipefail
cd /opt/option-monitor
exec 9>>/opt/option-monitor/state/run.lock
flock -w 900 9
OUT=$(/opt/option-monitor/.venv/bin/python /opt/option-monitor/scripts/run_options_monitor.py \
  --root /opt/option-monitor --force-anomaly-report --force-all-products)
printf '%s\n' "$OUT"
MANIFEST=$(printf '%s\n' "$OUT" | sed -n 's/^MANIFEST_PATH=//p' | tail -1)
if [ -n "$MANIFEST" ]; then
  rm -rf "$(dirname "$MANIFEST")"
fi
