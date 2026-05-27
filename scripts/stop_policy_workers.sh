#!/usr/bin/env bash
set -euo pipefail

PATTERN="${PATTERN:-ios-privacy-policy-collector}"
WHAT_IF="${WHAT_IF:-0}"

mapfile -t pids < <(
  pgrep -af "$PATTERN" |
    grep -E 'scripts/run_batch.py|scripts/queue_worker.py|cluster-stage1-|cluster-batch|run-policy-cluster' |
    awk '{print $1}' |
    grep -v "^$$$" || true
)

stopped=0
printf '{"what_if": %s, "processes": [' "$([[ "$WHAT_IF" == "1" ]] && echo true || echo false)"
first=1
for pid in "${pids[@]}"; do
  [[ -z "$pid" ]] && continue
  cmd="$(ps -p "$pid" -o command= || true)"
  [[ -z "$cmd" ]] && continue
  if [[ "$first" -eq 0 ]]; then printf ','; fi
  first=0
  if [[ "$WHAT_IF" == "1" ]]; then
    printf '%s' "$(python3 -c 'import json,sys; print(json.dumps({"process_id": int(sys.argv[1]), "command_line": sys.argv[2], "stopped": False, "what_if": True}, sort_keys=True))' "$pid" "$cmd")"
  else
    if kill "$pid" 2>/dev/null; then
      stopped=$((stopped + 1))
      printf '%s' "$(python3 -c 'import json,sys; print(json.dumps({"process_id": int(sys.argv[1]), "command_line": sys.argv[2], "stopped": True}, sort_keys=True))' "$pid" "$cmd")"
    else
      printf '%s' "$(python3 -c 'import json,sys; print(json.dumps({"process_id": int(sys.argv[1]), "command_line": sys.argv[2], "stopped": False}, sort_keys=True))' "$pid" "$cmd")"
    fi
  fi
done
printf '], "matched_count": %d, "stopped_count": %d}\n' "${#pids[@]}" "$stopped"
