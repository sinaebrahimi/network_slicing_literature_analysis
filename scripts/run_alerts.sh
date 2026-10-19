#!/usr/bin/env bash
# Unattended alert run for cron / launchd (macOS, Linux).
#   bash scripts/run_alerts.sh                  # all projects, download by score
#   bash scripts/run_alerts.sh -p network_slicing --mode report
# Scheduled runs cannot ask questions, so the default mode is 'auto'. Output is appended to
# logs/alerts-cron.log. Example crontab line (every weekday at 07:40):
#   40 7 * * 1-5  /bin/bash /path/to/repo/scripts/run_alerts.sh
set -uo pipefail
cd "$(dirname "$0")/.."
[ -f .venv/bin/activate ] && . .venv/bin/activate
mkdir -p logs
ARGS=("$@")
[ ${#ARGS[@]} -eq 0 ] && ARGS=(--all-projects --mode auto)
{
  echo "=== $(date -Iseconds) litsuite alerts ${ARGS[*]}"
  litsuite alerts "${ARGS[@]}"
} >> logs/alerts-cron.log 2>&1
