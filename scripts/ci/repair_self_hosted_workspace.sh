#!/usr/bin/env bash
set -euo pipefail

workspace_path="${GITHUB_WORKSPACE:-}"
runner_ci_home="${RUNNER_CI_HOME:-}"
target_uid="${RUNNER_WORKSPACE_OWNER_UID:-$(id -u)}"
target_gid="${RUNNER_WORKSPACE_OWNER_GID:-$(id -g)}"

if [[ -z "$workspace_path" ]]; then
  echo "GITHUB_WORKSPACE must be set" >&2
  exit 1
fi

workspace_parent="$(dirname "$workspace_path")"
cache_path="${RUNNER_LOCAL_CACHE_ROOT:-$workspace_parent/.ci-cache}"

run_maybe_sudo() {
  if [[ "${EUID}" -ne 0 ]] && command -v sudo > /dev/null 2>&1; then
    sudo "$@"
    return
  fi

  "$@"
}

if [[ -n "${ACTIONS_RUNNER_ROOT:-}" && -z "${RUNNER_ROOT:-}" ]]; then
  export RUNNER_ROOT="$ACTIONS_RUNNER_ROOT"
fi

if [[ -x /usr/local/bin/repair-github-runner-workspace ]]; then
  run_maybe_sudo \
    /usr/local/bin/repair-github-runner-workspace \
    "$workspace_path" \
    "$cache_path" \
    "$target_uid" \
    "$target_gid"
else
  run_maybe_sudo mkdir -p "$workspace_path" "$cache_path"
  run_maybe_sudo chown -R "${target_uid}:${target_gid}" "$workspace_path" "$cache_path"
fi

if [[ -n "$runner_ci_home" ]]; then
  run_maybe_sudo mkdir -p "$runner_ci_home/.cache"
  run_maybe_sudo chown -R "${target_uid}:${target_gid}" "$runner_ci_home"
fi

if [[ ! -e "$workspace_path/.git" ]]; then
  run_maybe_sudo find "$workspace_path" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi
