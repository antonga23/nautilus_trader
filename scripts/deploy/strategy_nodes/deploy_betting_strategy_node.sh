#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << USAGE
Usage: $0 --manifest <path> --image <image> --name <container-name> [--env-file <path>] [--root <path>] [--registry-user <user>] [--registry-token-file <path>]
USAGE
}

manifest_path=""
image_ref=""
container_name=""
env_file=""
root_dir="/opt/cloudbet/strategy-nodes"
registry_user=""
registry_token_file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)
      manifest_path="$2"
      shift 2
      ;;
    --image)
      image_ref="$2"
      shift 2
      ;;
    --name)
      container_name="$2"
      shift 2
      ;;
    --env-file)
      env_file="$2"
      shift 2
      ;;
    --root)
      root_dir="$2"
      shift 2
      ;;
    --registry-user)
      registry_user="$2"
      shift 2
      ;;
    --registry-token-file)
      registry_token_file="$2"
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

if [[ -z "$manifest_path" || -z "$image_ref" || -z "$container_name" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -f "$manifest_path" ]]; then
  echo "Manifest not found: $manifest_path" >&2
  exit 1
fi

if [[ -n "$env_file" && ! -f "$env_file" ]]; then
  echo "Env file not found: $env_file" >&2
  exit 1
fi

if [[ -n "$registry_token_file" && ! -f "$registry_token_file" ]]; then
  echo "Registry token file not found: $registry_token_file" >&2
  exit 1
fi

if [[ -n "$registry_token_file" && -z "$registry_user" ]]; then
  echo "--registry-user is required when --registry-token-file is provided" >&2
  exit 1
fi

command -v docker > /dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}
command -v python3 > /dev/null 2>&1 || {
  echo "python3 is required" >&2
  exit 1
}

node_dir="$root_dir/$container_name"
runtime_manifest="$node_dir/manifest.runtime.json"
release_meta="$node_dir/release.json"
previous_image_file="$node_dir/previous-image.txt"
current_image_file="$node_dir/current-image.txt"
current_session_file="$node_dir/current-session.json"
sessions_dir="$node_dir/sessions"
session_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
session_dir="$sessions_dir/$session_id"
event_log="$session_dir/events.jsonl"
node_log="$session_dir/node.log"
wrapper_path="$node_dir/run_with_logs.sh"

ensure_dir() {
  local target="$1"
  if mkdir -p "$target" 2> /dev/null; then
    return 0
  fi
  if command -v sudo > /dev/null 2>&1; then
    sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 775 "$target"
    return 0
  fi
  echo "Cannot create directory: $target" >&2
  exit 1
}

ensure_dir "$root_dir"
ensure_dir "$node_dir"
ensure_dir "$sessions_dir"
ensure_dir "$session_dir"
ensure_dir "$node_dir/semantic-rule-cache"
ensure_dir "$node_dir/semantic-rule-cache-seed"
ensure_dir "$node_dir/commands"

write_event() {
  local event_type="$1"
  local message="${2:-}"
  python3 - "$event_log" "$event_type" "$message" << 'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone

path = pathlib.Path(sys.argv[1])
payload = {
    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "event": sys.argv[2],
    "message": sys.argv[3],
}
with path.open("a", encoding="utf8") as f:
    f.write(json.dumps(payload, sort_keys=True) + "\n")
PY
}

write_event "deploy_started" "Preparing strategy-node deployment"

python3 - "$manifest_path" "$runtime_manifest" << 'PY'
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
dest = pathlib.Path(sys.argv[2])
data = json.loads(source.read_text())
data["rendered_config_path"] = "/var/lib/nautilus-node/trading-node-config.json"
data["status_path"] = "/var/lib/nautilus-node/status.json"
data["heartbeat_path"] = "/var/lib/nautilus-node/heartbeat.json"
if data.get("semantic_rule_cache_dir"):
    data["semantic_rule_cache_dir"] = "/var/lib/nautilus-node/semantic-rule-cache"
if data.get("semantic_rule_cache_seed_dir"):
    data["semantic_rule_cache_seed_dir"] = "/var/lib/nautilus-node/semantic-rule-cache-seed"
dest.write_text(json.dumps(data, indent=2) + "\n")
PY

cat > "$wrapper_path" << 'SH'
#!/bin/sh
set -eu

log_file="${NODE_LOG_FILE:-/var/lib/nautilus-node/node.log}"
event_log="${NODE_EVENT_LOG:-/var/lib/nautilus-node/events.jsonl}"
session_id="${NODE_SESSION_ID:-unknown}"

mkdir -p "$(dirname "$log_file")" "$(dirname "$event_log")"

write_event() {
  event_type="$1"
  message="${2:-}"
  python3 - "$event_log" "$event_type" "$message" "$session_id" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone

path = pathlib.Path(sys.argv[1])
payload = {
    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "event": sys.argv[2],
    "message": sys.argv[3],
    "sessionId": sys.argv[4],
}
with path.open("a", encoding="utf8") as f:
    f.write(json.dumps(payload, sort_keys=True) + "\n")
PY
}

write_event "process_started" "Launching betting arbitrage trading node"
set +e
python3 - "$log_file" <<'PY'
import subprocess
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
cmd = [
    sys.executable,
    "-m",
    "nautilus_trader.live.strategy_nodes.betting_arbitrage",
    "run",
    "--manifest",
    "/srv/node/manifest.json",
]
with log_path.open("a", encoding="utf8") as log_file:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        log_file.write(line)
        log_file.flush()
    sys.exit(process.wait())
PY
status="$?"
set -e
write_event "process_exited" "Trading node exited with status ${status}"
exit "$status"
SH
chmod 755 "$wrapper_path"

if [[ -n "$registry_token_file" ]]; then
  docker login ghcr.io -u "$registry_user" --password-stdin < "$registry_token_file" > /dev/null
fi

if docker image inspect "$image_ref" > /dev/null 2>&1; then
  :
else
  docker pull "$image_ref"
fi

if docker container inspect "$container_name" > /dev/null 2>&1; then
  current_running_image="$(docker inspect --format '{{.Config.Image}}' "$container_name")"
  if [[ -f "$current_session_file" ]]; then
    previous_session_dir="$(
      python3 - "$current_session_file" "$sessions_dir" << 'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
sessions_dir = pathlib.Path(sys.argv[2])
try:
    payload = json.loads(path.read_text())
    print(payload.get("hostSessionDir") or sessions_dir / payload.get("sessionId", "unknown"))
except Exception:
    print(sessions_dir / "unknown")
PY
    )"
    ensure_dir "$previous_session_dir"
    docker logs --timestamps "$container_name" > "$previous_session_dir/docker-before-redeploy.log" 2>&1 || true
  fi
  write_event "container_removed" "Removing existing container before redeploy"
  printf '%s\n' "$current_running_image" > "$previous_image_file"
  docker rm -f "$container_name" > /dev/null
elif [[ -f "$current_image_file" ]]; then
  cp "$current_image_file" "$previous_image_file"
fi

printf '%s\n' "$image_ref" > "$current_image_file"
rm -f "$node_dir/status.json" "$node_dir/heartbeat.json"
# Pending approvals are in-memory only, so approval ids from the previous session
# can never exist in the new one; drop any queued command files with them.
rm -f "$node_dir"/commands/*.json

run_args=(
  run -d
  --restart unless-stopped
  --log-driver json-file
  --log-opt max-size=20m
  --log-opt max-file=5
  --name "$container_name"
  --entrypoint /var/lib/nautilus-node/run_with_logs.sh
  -e "NODE_SESSION_ID=$session_id"
  -e "NODE_LOG_FILE=/var/lib/nautilus-node/sessions/$session_id/node.log"
  -e "NODE_EVENT_LOG=/var/lib/nautilus-node/sessions/$session_id/events.jsonl"
  -v "$node_dir:/var/lib/nautilus-node"
  -v "$runtime_manifest:/srv/node/manifest.json:ro"
)

if [[ -n "$env_file" ]]; then
  run_args+=(--env-file "$env_file")
fi

run_args+=(
  "$image_ref"
)

docker "${run_args[@]}" > /dev/null
write_event "container_started" "Started container $container_name"

python3 - \
  "$release_meta" \
  "$current_session_file" \
  "$container_name" \
  "$image_ref" \
  "$runtime_manifest" \
  "$env_file" \
  "$session_id" \
  "$session_dir" \
  "$node_log" \
  "$event_log" << 'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone

deployed_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
meta = {
    "container": sys.argv[3],
    "image": sys.argv[4],
    "manifest": sys.argv[5],
    "envFile": sys.argv[6] or None,
    "sessionId": sys.argv[7],
    "sessionDir": sys.argv[8],
    "logPath": sys.argv[9],
    "eventLogPath": sys.argv[10],
    "deployedAt": deployed_at,
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(meta, indent=2) + "\n")
pathlib.Path(sys.argv[2]).write_text(json.dumps({
    "container": sys.argv[3],
    "sessionId": sys.argv[7],
    "hostSessionDir": sys.argv[8],
    "logPath": sys.argv[9],
    "eventLogPath": sys.argv[10],
    "startedAt": deployed_at,
}, indent=2) + "\n")
PY

echo "container=$container_name"
echo "runtime_manifest=$runtime_manifest"
echo "status_file=$node_dir/status.json"
echo "heartbeat_file=$node_dir/heartbeat.json"
echo "session_id=$session_id"
echo "node_log=$node_log"
echo "event_log=$event_log"
