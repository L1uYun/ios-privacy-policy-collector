#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/data/ios-privacy-policy-collector}"
DATA_ROOT="${DATA_ROOT:-/data/ios-privacy-policy-collector/data}"
DB_PATH="${DB_PATH:-$DATA_ROOT/queue.sqlite}"
PYTHON_BIN="${PYTHON_BIN:-$REPO/.venv/bin/python}"
BACKUP_DIR="${BACKUP_DIR:-$DATA_ROOT/db-backups}"
EVENT_LOG="${EVENT_LOG:-$DATA_ROOT/stage-monitor/queue-backup.events.jsonl}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-1800}"
KEEP_BACKUPS="${KEEP_BACKUPS:-12}"
ONCE="${ONCE:-0}"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

while true; do
  (
    cd "$REPO"
    "$PYTHON_BIN" scripts/backup_queue_db.py \
      --db "$DB_PATH" \
      --backup-dir "$BACKUP_DIR" \
      --event-log "$EVENT_LOG" \
      --keep "$KEEP_BACKUPS"
  )
  if [[ "$ONCE" == "1" ]]; then
    break
  fi
  sleep "$INTERVAL_SECONDS"
done
