#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Usage: $0 --manifest <path> --image <image> --name <container-name> [--env-file <path>] [--root <path>]
USAGE
}

manifest_path=""
image_ref=""
container_name=""
env_file=""
root_dir="/opt/cloudbet/strategy-nodes"

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
    -h|--help)
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

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

node_dir="$root_dir/$container_name"
runtime_manifest="$node_dir/manifest.runtime.json"
release_meta="$node_dir/release.json"
previous_image_file="$node_dir/previous-image.txt"
current_image_file="$node_dir/current-image.txt"

mkdir -p "$node_dir"

python3 - "$manifest_path" "$runtime_manifest" <<'PY'
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
dest = pathlib.Path(sys.argv[2])
data = json.loads(source.read_text())
data["rendered_config_path"] = "/var/lib/nautilus-node/trading-node-config.json"
data["status_path"] = "/var/lib/nautilus-node/status.json"
data["heartbeat_path"] = "/var/lib/nautilus-node/heartbeat.json"
dest.write_text(json.dumps(data, indent=2) + "\n")
PY

if docker image inspect "$image_ref" >/dev/null 2>&1; then
  :
else
  docker pull "$image_ref"
fi

if docker container inspect "$container_name" >/dev/null 2>&1; then
  current_running_image="$(docker inspect --format '{{.Config.Image}}' "$container_name")"
  printf '%s\n' "$current_running_image" > "$previous_image_file"
  docker rm -f "$container_name" >/dev/null
elif [[ -f "$current_image_file" ]]; then
  cp "$current_image_file" "$previous_image_file"
fi

printf '%s\n' "$image_ref" > "$current_image_file"

run_args=(
  run -d
  --restart unless-stopped
  --name "$container_name"
  -v "$node_dir:/var/lib/nautilus-node"
  -v "$runtime_manifest:/srv/node/manifest.json:ro"
)

if [[ -n "$env_file" ]]; then
  run_args+=(--env-file "$env_file")
fi

run_args+=(
  "$image_ref"
  run
  --manifest /srv/node/manifest.json
)

docker "${run_args[@]}" >/dev/null

python3 - "$release_meta" "$container_name" "$image_ref" "$runtime_manifest" "$env_file" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone

meta = {
    "container": sys.argv[2],
    "image": sys.argv[3],
    "manifest": sys.argv[4],
    "envFile": sys.argv[5] or None,
    "deployedAt": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(meta, indent=2) + "\n")
PY

echo "container=$container_name"
echo "runtime_manifest=$runtime_manifest"
echo "status_file=$node_dir/status.json"
echo "heartbeat_file=$node_dir/heartbeat.json"
