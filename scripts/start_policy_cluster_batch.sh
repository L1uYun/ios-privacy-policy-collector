#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/data/ios-privacy-policy-collector}"
DATA_ROOT="${DATA_ROOT:-/data/ios-privacy-policy-collector/data}"
DB_PATH="${DB_PATH:-$DATA_ROOT/queue.sqlite}"
PYTHON_BIN="${PYTHON_BIN:-$REPO/.venv/bin/python}"
NAME="${NAME:-stage1-10k}"
WORKERS="${WORKERS:-8}"
LIMIT_PER_WORKER="${LIMIT_PER_WORKER:-250}"
COUNTRIES="${COUNTRIES:-}"
SOURCES="${SOURCES:-apple-search:*}"
PROXY="${PROXY:-}"
MIN_POLICY_CHARS="${MIN_POLICY_CHARS:-500}"
TIMEOUT="${TIMEOUT:-45}"
JS_TIMEOUT="${JS_TIMEOUT:-30}"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

LOG_ROOT="$DATA_ROOT/policy-batch-bg"
BATCH_NAME="cluster-${NAME}-$(date +%Y%m%d-%H%M%S)"
BATCH_DIR="$LOG_ROOT/$BATCH_NAME"
OUT_LOG="$LOG_ROOT/$BATCH_NAME.out.log"
ERR_LOG="$LOG_ROOT/$BATCH_NAME.err.log"
SUMMARY_JSON="$LOG_ROOT/$BATCH_NAME.summary.json"

mkdir -p "$BATCH_DIR"

cmd=(
  "$PYTHON_BIN" scripts/run_batch.py
  --db "$DB_PATH"
  --output-dir "$DATA_ROOT/policy-clusters"
  --log-dir "$BATCH_DIR"
  --workers "$WORKERS"
  --limit-per-worker "$LIMIT_PER_WORKER"
  --worker-prefix "$BATCH_NAME"
  --active-only
  --sources "$SOURCES"
  --claim-order newest
  --min-policy-chars "$MIN_POLICY_CHARS"
  --timeout "$TIMEOUT"
  --fallback-timeout 8
  --max-attempts 2
  --cluster-max-depth 1
  --cluster-max-docs 8
  --cluster-min-chars 200
  --js-timeout "$JS_TIMEOUT"
  --js-wait-ms 1000
  --poll-seconds 30
  --summary-json "$SUMMARY_JSON"
)

if [[ -n "$COUNTRIES" ]]; then
  cmd+=(--countries "$COUNTRIES")
fi
if [[ -n "$PROXY" ]]; then
  cmd+=(--proxy "$PROXY")
fi

(
  cd "$REPO"
  nohup "${cmd[@]}" >"$OUT_LOG" 2>"$ERR_LOG" &
  pid=$!
  "$PYTHON_BIN" - <<PY
import json
print(json.dumps({
    "process_id": $pid,
    "batch_name": "$BATCH_NAME",
    "workers": int("$WORKERS"),
    "limit_per_worker": int("$LIMIT_PER_WORKER"),
    "countries": "$COUNTRIES",
    "sources": "$SOURCES",
    "proxy": "$PROXY",
    "log_dir": "$BATCH_DIR",
    "out_log": "$OUT_LOG",
    "err_log": "$ERR_LOG",
    "summary_json": "$SUMMARY_JSON",
}, sort_keys=True))
PY
)
