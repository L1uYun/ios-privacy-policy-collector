#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/data/ios-privacy-policy-collector}"
DATA_ROOT="${DATA_ROOT:-/data/ios-privacy-policy-collector/data}"
DB_PATH="${DB_PATH:-$DATA_ROOT/queue.sqlite}"
PYTHON_BIN="${PYTHON_BIN:-$REPO/.venv/bin/python}"
TARGET_POLICY_DOCUMENTS="${TARGET_POLICY_DOCUMENTS:-10000}"
POLL_SECONDS="${POLL_SECONDS:-60}"
NAME="${NAME:-stage1-10k-auto-stop}"
ONCE="${ONCE:-0}"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

LOG_ROOT="$DATA_ROOT/stage-monitor"
STATUS_JSON="$DATA_ROOT/milestone-status.json"
EVENTS_JSONL="$LOG_ROOT/$NAME.events.jsonl"
mkdir -p "$LOG_ROOT"

write_event() {
  local payload="$1"
  "$PYTHON_BIN" - "$EVENTS_JSONL" "$payload" <<'PY'
import datetime, json, sys
path, payload = sys.argv[1], json.loads(sys.argv[2])
payload["created_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
PY
}

get_policy_document_count() {
  (
    cd "$REPO"
    "$PYTHON_BIN" scripts/queue_store.py --db "$DB_PATH" stats |
      "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["policy_documents"])'
  )
}

while true; do
  count="$(get_policy_document_count)"
  write_event "{\"event\": \"poll\", \"target_policy_documents\": $TARGET_POLICY_DOCUMENTS, \"policy_documents\": $count}"

  if (( count >= TARGET_POLICY_DOCUMENTS )); then
    snapshot_name="$NAME-reached-$TARGET_POLICY_DOCUMENTS-$(date +%Y%m%d-%H%M%S)"
    (
      cd "$REPO"
      "$PYTHON_BIN" scripts/freeze_stage_snapshot.py \
        --db "$DB_PATH" \
        --data-root "$DATA_ROOT" \
        --name "$snapshot_name"
    ) >"$LOG_ROOT/$snapshot_name.snapshot.log" 2>&1

    (cd "$REPO" && bash scripts/stop_policy_workers.sh) >"$LOG_ROOT/$snapshot_name.stop-policy-workers.json" 2>&1 || true
    (cd "$REPO" && "$PYTHON_BIN" scripts/queue_store.py --db "$DB_PATH" requeue-running) >"$LOG_ROOT/$snapshot_name.requeue-running.json" 2>&1

    write_event "{\"event\": \"target_reached\", \"target_policy_documents\": $TARGET_POLICY_DOCUMENTS, \"policy_documents\": $count, \"snapshot_name\": \"$snapshot_name\"}"
    break
  fi

  if [[ "$ONCE" == "1" ]]; then
    break
  fi
  sleep "$POLL_SECONDS"
done
