#!/usr/bin/env bash
set -euo pipefail

canonicalize_path() {
  python3 - "$1" <<'PY'
import os
import sys

print(os.path.realpath(sys.argv[1]))
PY
}

workspace_path="${1:-}"
cache_path="${2:-}"
target_uid="${3:-}"
target_gid="${4:-}"

if [[ -z "$workspace_path" || -z "$cache_path" || -z "$target_uid" || -z "$target_gid" ]]; then
  echo "Usage: repair-github-runner-workspace.sh <workspace> <cache> <uid> <gid>" >&2
  exit 1
fi

canonical_workspace="$(canonicalize_path "$workspace_path")"
canonical_cache="$(canonicalize_path "$cache_path")"
derived_runner_root="${canonical_workspace%%/_work/*}"
allowed_prefix="${derived_runner_root}/_work/"

if [[ -z "$derived_runner_root" || "$derived_runner_root" == "$canonical_workspace" ]]; then
  echo "Workspace path must contain /_work/: $canonical_workspace" >&2
  exit 1
fi

case "$derived_runner_root" in
  /*/actions-runner|/*/*/actions-runner|/*/*/*/actions-runner|/*/*/*/*/actions-runner|/*/*/*/*/*/actions-runner) ;;
  *)
    echo "Workspace path must resolve under a runner root ending with /actions-runner: $derived_runner_root" >&2
    exit 1
    ;;
esac

case "$canonical_workspace" in
  "$allowed_prefix"*) ;;
  *)
    echo "Workspace path must live under ${derived_runner_root}/_work: $canonical_workspace" >&2
    exit 1
    ;;
esac

case "$canonical_cache" in
  "$allowed_prefix"*) ;;
  *)
    echo "Cache path must live under ${derived_runner_root}/_work: $canonical_cache" >&2
    exit 1
    ;;
esac

install -d -m 0755 "$canonical_workspace" "$canonical_cache"
chown -R "${target_uid}:${target_gid}" "$canonical_workspace" "$canonical_cache"
