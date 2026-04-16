#!/usr/bin/env bash
set -euo pipefail

canonicalize_path() {
  python3 - "$1" << 'PY'
import os
import sys

print(os.path.realpath(sys.argv[1]))
PY
}

workspace_path="${1:-}"
cache_path="${2:-}"
target_uid="${3:-}"
target_gid="${4:-}"
runner_layout_config="${RUNNER_LAYOUT_CONFIG:-/etc/cloudbet/self-hosted-runner.conf}"

if [[ -z "$workspace_path" || -z "$cache_path" || -z "$target_uid" || -z "$target_gid" ]]; then
  echo "Usage: repair-github-runner-workspace.sh <workspace> <cache> <uid> <gid>" >&2
  exit 1
fi

if [[ -f "$runner_layout_config" ]]; then
  # shellcheck disable=SC1090
  source "$runner_layout_config"
fi

if [[ -z "${RUNNER_ROOT:-}" ]]; then
  echo "RUNNER_ROOT is not configured; expected ${runner_layout_config}" >&2
  exit 1
fi

canonical_runner_root="$(canonicalize_path "$RUNNER_ROOT")"
canonical_workspace="$(canonicalize_path "$workspace_path")"
canonical_cache="$(canonicalize_path "$cache_path")"
allowed_prefix="${canonical_runner_root}/_work/"

case "$canonical_workspace" in
  "$allowed_prefix"*) ;;
  *)
    echo "Workspace path must live under ${canonical_runner_root}/_work: $canonical_workspace" >&2
    exit 1
    ;;
esac

case "$canonical_cache" in
  "$allowed_prefix"*) ;;
  *)
    echo "Cache path must live under ${canonical_runner_root}/_work: $canonical_cache" >&2
    exit 1
    ;;
esac

install -d -m 0755 "$canonical_workspace" "$canonical_cache"
chown -R "${target_uid}:${target_gid}" "$canonical_workspace" "$canonical_cache"
