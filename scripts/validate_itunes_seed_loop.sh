#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/data/ios-privacy-policy-collector}"
DATA_ROOT="${DATA_ROOT:-/data/ios-privacy-policy-collector/data}"
DB_PATH="${DB_PATH:-$DATA_ROOT/queue.sqlite}"
PYTHON_BIN="${PYTHON_BIN:-$REPO/.venv/bin/python}"
NAME="${NAME:-itunes-seed-validation}"
LIMIT="${LIMIT:-20000}"
BATCH_SIZE="${BATCH_SIZE:-100}"
SLEEP_SECONDS="${SLEEP_SECONDS:-0.05}"
POLL_SECONDS="${POLL_SECONDS:-300}"
PROGRESS_EVERY="${PROGRESS_EVERY:-5000}"
COUNTRIES="${COUNTRIES:-}"
SOURCES="${SOURCES:-}"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

LOG_ROOT="$DATA_ROOT/seed-validation-bg"
mkdir -p "$LOG_ROOT"

run_one() {
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local out_json="$LOG_ROOT/$NAME-$stamp.json"
  local out_log="$LOG_ROOT/$NAME-$stamp.out.log"
  local err_log="$LOG_ROOT/$NAME-$stamp.err.log"
  local args=(
    "$PYTHON_BIN" scripts/validate_itunes_seeds.py
    --db "$DB_PATH"
    --limit "$LIMIT"
    --batch-size "$BATCH_SIZE"
    --sleep "$SLEEP_SECONDS"
    --progress-every "$PROGRESS_EVERY"
    --output-json "$out_json"
  )
  if [[ -n "$COUNTRIES" ]]; then
    # validate_itunes_seeds.py accepts one country per run; keep this loop simple.
    local country
    IFS=',' read -r country _rest <<<"$COUNTRIES"
    args+=(--country "$country")
  fi
  if [[ -n "$SOURCES" ]]; then
    local source
    IFS=',' read -r source _rest <<<"$SOURCES"
    args+=(--source "$source")
  fi
  (
    cd "$REPO"
    "${args[@]}"
  ) >"$out_log" 2>"$err_log"
}

while true; do
  run_one || true
  sleep "$POLL_SECONDS"
done
