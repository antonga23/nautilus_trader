#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << USAGE
Usage: $0 --name <container-name> [--env-file <path>] [--root <path>]
USAGE
}

container_name=""
env_file=""
root_dir="/opt/cloudbet/strategy-nodes"

while [[ $# -gt 0 ]]; do
  case "$1" in
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

if [[ -z "$container_name" ]]; then
  usage >&2
  exit 1
fi

node_dir="$root_dir/$container_name"
previous_image_file="$node_dir/previous-image.txt"
runtime_manifest="$node_dir/manifest.runtime.json"

if [[ ! -f "$previous_image_file" ]]; then
  echo "No previous image recorded for $container_name" >&2
  exit 1
fi

previous_image="$(cat "$previous_image_file")"
if [[ -z "$previous_image" ]]; then
  echo "Previous image file is empty for $container_name" >&2
  exit 1
fi

args=(
  --manifest "$runtime_manifest"
  --image "$previous_image"
  --name "$container_name"
  --root "$root_dir"
)

if [[ -n "$env_file" ]]; then
  args+=(--env-file "$env_file")
fi

exec "$(dirname "$0")/deploy_betting_strategy_node.sh" "${args[@]}"
