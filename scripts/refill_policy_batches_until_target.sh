#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/data/ios-privacy-policy-collector}"
DATA_ROOT="${DATA_ROOT:-/data/ios-privacy-policy-collector/data}"
DB_PATH="${DB_PATH:-$DATA_ROOT/queue.sqlite}"
PYTHON_BIN="${PYTHON_BIN:-$REPO/.venv/bin/python}"
NAME="${NAME:-stage1-refill}"
TARGET_POLICY_DOCUMENTS="${TARGET_POLICY_DOCUMENTS:-10000}"
MIN_RUNNING_FETCHES="${MIN_RUNNING_FETCHES:-16}"
WORKERS="${WORKERS:-8}"
LIMIT_PER_WORKER="${LIMIT_PER_WORKER:-300}"
POLL_SECONDS="${POLL_SECONDS:-120}"
MAX_BATCHES="${MAX_BATCHES:-40}"
SOURCES="${SOURCES:-apple-search:game,apple-search:travel,apple-search:music,apple-search:video,apple-search:shopping,apple-search:sports,apple-search:food,apple-search:weather,apple-search:fitness,apple-search:education,apple-search:kids,apple-search:news,apple-search:social,apple-search:productivity}"
PROXY="${PROXY:-}"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

EVENT_DIR="$DATA_ROOT/stage-monitor"
EVENT_LOG="$EVENT_DIR/$NAME.events.jsonl"
mkdir -p "$EVENT_DIR"

write_event() {
  local event="$1"
  local payload="${2:-{}}"
  "$PYTHON_BIN" - "$EVENT_LOG" "$event" "$payload" <<'PY'
import datetime, json, sys
path, event, raw_payload = sys.argv[1], sys.argv[2], sys.argv[3]

def parse_last_json_object(text):
    decoder = json.JSONDecoder()
    index = 0
    last = None
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            next_line = text.find("\n", index)
            if next_line == -1:
                break
            index = next_line + 1
            continue
        if isinstance(value, dict):
            last = value
    if last is None:
        raise SystemExit(f"no JSON event payload found: {text[:500]!r}")
    return last

payload = parse_last_json_object(raw_payload)
payload["event"] = event
payload["created_at"] = datetime.datetime.utcnow().isoformat(timespec="microseconds") + "Z"
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
PY
}

queue_stats() {
  (cd "$REPO" && "$PYTHON_BIN" scripts/queue_store.py --db "$DB_PATH" stats)
}

stats_fields() {
  local stats_output
  stats_output="$(queue_stats)"
  "$PYTHON_BIN" - "$stats_output" <<'PY'
import json
import sys

text = sys.argv[1]
decoder = json.JSONDecoder()
index = 0
last = None
while index < len(text):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        break
    try:
        value, index = decoder.raw_decode(text, index)
    except json.JSONDecodeError:
        next_line = text.find("\n", index)
        if next_line == -1:
            break
        index = next_line + 1
        continue
    if isinstance(value, dict):
        last = value

if last is None:
    raise SystemExit(f"no JSON stats object found in queue_store output: {text[:500]!r}")

print(
    int(last.get("policy_documents", 0)),
    int(last.get("running_fetches", 0)),
    int(last.get("pending_fetches", 0)),
    int(last.get("permanent_error_fetches", 0)),
)
PY
}

started_batches=0
write_event start "$("$PYTHON_BIN" - <<PY
import json
print(json.dumps({
    "target_policy_documents": int("$TARGET_POLICY_DOCUMENTS"),
    "min_running_fetches": int("$MIN_RUNNING_FETCHES"),
    "workers": int("$WORKERS"),
    "limit_per_worker": int("$LIMIT_PER_WORKER"),
    "sources": "$SOURCES",
    "proxy": "$PROXY",
}))
PY
)"

while true; do
  read -r policy_documents running_fetches pending_fetches permanent_error_fetches < <(stats_fields)

  write_event poll "$("$PYTHON_BIN" - <<PY
import json
print(json.dumps({
    "policy_documents": int("$policy_documents"),
    "running_fetches": int("$running_fetches"),
    "pending_fetches": int("$pending_fetches"),
    "permanent_error_fetches": int("$permanent_error_fetches"),
    "started_batches": int("$started_batches"),
}))
PY
)"

  if (( policy_documents >= TARGET_POLICY_DOCUMENTS )); then
    write_event target_reached "{\"policy_documents\": $policy_documents, \"started_batches\": $started_batches}"
    exit 0
  fi

  if (( started_batches >= MAX_BATCHES )); then
    write_event max_batches_reached "{\"max_batches\": $MAX_BATCHES, \"policy_documents\": $policy_documents, \"running_fetches\": $running_fetches}"
    exit 1
  fi

  if (( running_fetches < MIN_RUNNING_FETCHES )); then
    batch_name="$NAME-$((started_batches + 1))"
    write_event starting_batch "{\"batch_name\": \"$batch_name\", \"policy_documents\": $policy_documents, \"running_fetches\": $running_fetches}"
    (
      export REPO DATA_ROOT DB_PATH PYTHON_BIN WORKERS LIMIT_PER_WORKER SOURCES PROXY
      NAME="$batch_name" bash "$REPO/scripts/start_policy_cluster_batch.sh"
    ) >>"$EVENT_DIR/$NAME.start.log" 2>>"$EVENT_DIR/$NAME.start.err.log" || true
    write_event batch_started "{\"batch_name\": \"$batch_name\", \"workers\": $WORKERS, \"limit_per_worker\": $LIMIT_PER_WORKER, \"sources\": \"$SOURCES\"}"
    started_batches=$((started_batches + 1))
  fi

  sleep "$POLL_SECONDS"
done
