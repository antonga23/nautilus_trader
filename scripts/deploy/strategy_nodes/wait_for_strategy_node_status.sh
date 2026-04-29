#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << USAGE
Usage: $0 --status-file <path> [--timeout-seconds <n>] [--success-status <csv>] [--require-semantic-cache-ready]
USAGE
}

status_file=""
timeout_seconds=300
success_statuses="running,completed,validated,built"
require_semantic_cache_ready="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status-file)
      status_file="$2"
      shift 2
      ;;
    --timeout-seconds)
      timeout_seconds="$2"
      shift 2
      ;;
    --success-status)
      success_statuses="$2"
      shift 2
      ;;
    --require-semantic-cache-ready)
      require_semantic_cache_ready="true"
      shift 1
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$status_file" ]]; then
  usage >&2
  exit 1
fi

command -v python3 > /dev/null 2>&1 || {
  echo "python3 is required" >&2
  exit 1
}

python3 - "$status_file" "$timeout_seconds" "$success_statuses" "$require_semantic_cache_ready" << 'PY'
import json
import pathlib
import sys
import time

status_path = pathlib.Path(sys.argv[1])
timeout_seconds = int(sys.argv[2])
success_statuses = {item.strip() for item in sys.argv[3].split(',') if item.strip()}
require_semantic_cache_ready = sys.argv[4].strip().lower() == 'true'

deadline = time.time() + timeout_seconds
last_observation = None

while time.time() < deadline:
    if status_path.exists():
        try:
            payload = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            time.sleep(2)
            continue
        status = payload.get('status')
        semantic_cache = payload.get('semanticCache') or {}
        semantic_cache_ready = bool(semantic_cache.get('ready'))
        observation = (status, semantic_cache_ready)
        if observation != last_observation:
            print(json.dumps(payload))
            last_observation = observation
        if status in success_statuses and (
            not require_semantic_cache_ready or semantic_cache_ready
        ):
            sys.exit(0)
        if status == 'failed':
            sys.exit(1)
    time.sleep(2)

print(json.dumps({
    'status': 'timeout',
    'statusFile': str(status_path),
    'timeoutSeconds': timeout_seconds,
}))
sys.exit(2)
PY
