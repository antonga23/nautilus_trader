#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << USAGE
Usage: $0 --container <name> [--container <name> ...] [--root <path>] [--archive-root <path>] [--stop]

Archive strategy-node runtime artifacts and optional Docker state before stopping
stale validation containers. This script is intended to run on the EC2 deploy
host, not on CI runners.
USAGE
}

root_dir="/opt/cloudbet/strategy-nodes"
archive_root="/opt/cloudbet/strategy-nodes/archives"
stop_container="false"
declare -a containers=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      containers+=("$2")
      shift 2
      ;;
    --root)
      root_dir="$2"
      shift 2
      ;;
    --archive-root)
      archive_root="$2"
      shift 2
      ;;
    --stop)
      stop_container="true"
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

if [[ "${#containers[@]}" -eq 0 ]]; then
  usage >&2
  exit 1
fi

command -v docker > /dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$archive_root/$timestamp"

copy_tree() {
  local source="$1"
  local dest="$2"

  if command -v rsync > /dev/null 2>&1; then
    rsync -a --delete "$source"/ "$dest"/
  else
    mkdir -p "$dest"
    cp -a "$source"/. "$dest"/
  fi
}

for container in "${containers[@]}"; do
  case "$container" in
    '' | *[!A-Za-z0-9_.-]*)
      echo "Invalid container name: $container" >&2
      exit 1
      ;;
  esac

  node_dir="$root_dir/$container"
  archive_dir="$archive_root/$timestamp/$container"
  mkdir -p "$archive_dir"

  {
    echo "container=$container"
    echo "archived_at=$timestamp"
    echo "node_dir=$node_dir"
    echo "archive_dir=$archive_dir"
    echo "stop_requested=$stop_container"
  } > "$archive_dir/summary.env"

  if [[ -d "$node_dir" ]]; then
    copy_tree "$node_dir" "$archive_dir/node-dir"
  else
    echo "node_dir_missing=true" >> "$archive_dir/summary.env"
  fi

  if docker container inspect "$container" > /dev/null 2>&1; then
    docker container inspect "$container" > "$archive_dir/docker-inspect.json"
    docker logs --timestamps "$container" > "$archive_dir/docker.log" 2>&1 || true
    docker stats --no-stream "$container" > "$archive_dir/docker-stats.txt" 2>&1 || true
    if [[ "$stop_container" == "true" ]]; then
      docker stop "$container" > "$archive_dir/docker-stop.txt"
    fi
  else
    echo "docker_container_missing=true" >> "$archive_dir/summary.env"
  fi

  echo "archived=$archive_dir"
done
