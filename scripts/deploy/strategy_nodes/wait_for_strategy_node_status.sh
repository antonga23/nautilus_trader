#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << USAGE
Usage: $0 --status-file <path> [--timeout-seconds <n>] [--success-status <csv>] [--require-semantic-cache-ready] [--require-runtime-probe] [--require-rust-semantic-topology] [--min-connected-nodes <n>] [--min-match-instruments <n>] [--min-positive-margin-candidates <n>]
USAGE
}

status_file=""
timeout_seconds=300
success_statuses="running,completed,validated,built"
require_semantic_cache_ready="false"
require_runtime_probe="false"
require_rust_semantic_topology="false"
min_connected_nodes=0
min_match_instruments=0
min_positive_margin_candidates=0

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
    --require-runtime-probe)
      require_runtime_probe="true"
      shift 1
      ;;
    --require-rust-semantic-topology)
      require_rust_semantic_topology="true"
      shift 1
      ;;
    --min-connected-nodes)
      min_connected_nodes="$2"
      shift 2
      ;;
    --min-match-instruments)
      min_match_instruments="$2"
      shift 2
      ;;
    --min-positive-margin-candidates)
      min_positive_margin_candidates="$2"
      shift 2
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

python3 - \
  "$status_file" \
  "$timeout_seconds" \
  "$success_statuses" \
  "$require_semantic_cache_ready" \
  "$require_runtime_probe" \
  "$require_rust_semantic_topology" \
  "$min_connected_nodes" \
  "$min_match_instruments" \
  "$min_positive_margin_candidates" << 'PY'
import json
import pathlib
import sys
import time

status_path = pathlib.Path(sys.argv[1])
timeout_seconds = int(sys.argv[2])
success_statuses = {item.strip() for item in sys.argv[3].split(',') if item.strip()}
require_semantic_cache_ready = sys.argv[4].strip().lower() == 'true'
require_runtime_probe = sys.argv[5].strip().lower() == 'true'
require_rust_semantic_topology = sys.argv[6].strip().lower() == 'true'
min_connected_nodes = int(sys.argv[7])
min_match_instruments = int(sys.argv[8])
min_positive_margin_candidates = int(sys.argv[9])

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
        runtime_probe = payload.get('runtimeProbe') or {}
        positive_margin_candidates = runtime_probe.get('positiveMarginCandidates') or {}
        runtime_probe_ready = bool(runtime_probe) and (
            int(runtime_probe.get('connectedNodes') or 0) >= min_connected_nodes
            and int(runtime_probe.get('semanticMatchInstruments') or 0) >= min_match_instruments
            and int(positive_margin_candidates.get('total') or 0) >= min_positive_margin_candidates
        )
        rust_semantic_topology_ready = (
            runtime_probe.get('graphEngine') == 'rust'
            and runtime_probe.get('topologySource') == 'rust_semantic'
            and int(runtime_probe.get('semanticTemplateCount') or 0) > 0
        )
        observation = (
            status,
            semantic_cache_ready,
            runtime_probe_ready,
            rust_semantic_topology_ready,
        )
        if observation != last_observation:
            print(json.dumps(payload))
            last_observation = observation
        if status in success_statuses and (
            not require_semantic_cache_ready or semantic_cache_ready
        ) and (
            not require_runtime_probe or runtime_probe_ready
        ) and (
            not require_rust_semantic_topology or rust_semantic_topology_ready
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
