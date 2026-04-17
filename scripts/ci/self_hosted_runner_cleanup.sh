#!/usr/bin/env bash
set -euo pipefail

runner_layout_config="${RUNNER_LAYOUT_CONFIG:-/etc/cloudbet/self-hosted-runner.conf}"
runner_hygiene_config="${RUNNER_HYGIENE_CONFIG:-/etc/cloudbet/actions-runner-hygiene.conf}"

if [[ -f "$runner_layout_config" ]]; then
  # shellcheck disable=SC1090
  source "$runner_layout_config"
fi

if [[ -f "$runner_hygiene_config" ]]; then
  # shellcheck disable=SC1090
  source "$runner_hygiene_config"
fi

detect_runner_root() {
  local candidate

  for candidate in \
    "${ACTIONS_RUNNER_ROOT:-}" \
    "${RUNNER_ROOT:-}" \
    /opt/actions-runner \
    /home/ubuntu/actions-runner; do
    if [[ -n "$candidate" && -d "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  printf '%s\n' "${ACTIONS_RUNNER_ROOT:-${RUNNER_ROOT:-/home/ubuntu/actions-runner}}"
}

runner_root="$(detect_runner_root)"
runner_diag_root="${RUNNER_DIAG_ROOT:-$runner_root/_diag}"
runner_work_root="${RUNNER_WORK_ROOT:-$runner_root/_work}"
runner_temp_root="${RUNNER_TEMP_ROOT:-$runner_work_root/_temp}"
runner_local_cache_root="${RUNNER_LOCAL_CACHE_ROOT:-$runner_work_root/.ci-cache}"
runner_ci_home="${RUNNER_CI_HOME:-/tmp/cloudbet-market-maker-ci-home}"
runner_ci_home_purge_when_idle="${RUNNER_CI_HOME_PURGE_WHEN_IDLE:-true}"
workspace_root="${SYMPHONY_WORKSPACE_ROOT:-/srv/symphony/workspaces}"
control_repo_root="${SYMPHONY_CONTROL_REPO_ROOT:-/srv/symphony/control-repo}"
diag_retention_days="${RUNNER_DIAG_RETENTION_DAYS:-7}"
diag_max_mb="${RUNNER_DIAG_MAX_MB:-1024}"
precommit_retention_days="${PRECOMMIT_TEMP_RETENTION_DAYS:-2}"
actions_work_retention_days="${ACTIONS_WORK_RETENTION_DAYS:-3}"
runner_temp_retention_days="${RUNNER_TEMP_RETENTION_DAYS:-2}"
runner_local_cache_retention_days="${RUNNER_LOCAL_CACHE_RETENTION_DAYS:-7}"
symphony_workspace_retention_days="${SYMPHONY_WORKSPACE_RETENTION_DAYS:-7}"
control_repo_artifact_retention_days="${SYMPHONY_CONTROL_REPO_ARTIFACT_RETENTION_DAYS:-7}"
root_usage_prune_threshold="${ROOT_USAGE_PRUNE_THRESHOLD_PERCENT:-85}"
active_container_count=0
active_worker_count=0

if command -v docker > /dev/null 2>&1; then
  docker container prune -f --filter status=exited > /dev/null 2>&1 || true
  active_container_count="$(docker ps -q | wc -l | tr -d ' ')"
fi

if command -v pgrep > /dev/null 2>&1; then
  active_worker_count="$(pgrep -fc 'Runner.Worker' || true)"
fi

root_usage_pct="$(
  df -P / | awk 'NR == 2 {gsub(/%/, "", $5); print $5}'
)"

if command -v docker > /dev/null 2>&1 && [[ "$root_usage_pct" -ge "$root_usage_prune_threshold" ]]; then
  docker image prune -af --filter "until=168h" > /dev/null 2>&1 || true
  docker volume prune -f > /dev/null 2>&1 || true
fi

if [[ -d "$runner_diag_root" ]]; then
  find "$runner_diag_root" -type f -mtime "+$diag_retention_days" -delete 2> /dev/null || true

  diag_size_mb="$(
    du -sm "$runner_diag_root" 2> /dev/null | awk '{print $1}'
  )"
  if [[ -n "$diag_size_mb" ]] && [[ "$diag_size_mb" -gt "$diag_max_mb" ]]; then
    while [[ "$diag_size_mb" -gt "$diag_max_mb" ]]; do
      oldest_file="$(
        find "$runner_diag_root" -type f -printf '%T@ %p\n' 2> /dev/null | sort -n | head -n 1 | cut -d' ' -f2-
      )"
      [[ -n "$oldest_file" ]] || break
      rm -f "$oldest_file" 2> /dev/null || true
      diag_size_mb="$(
        du -sm "$runner_diag_root" 2> /dev/null | awk '{print $1}'
      )"
    done
  fi
fi

if [[ -d "$runner_temp_root" && "$active_container_count" -eq 0 && "$active_worker_count" -eq 0 ]]; then
  # Container jobs mount these runner temp paths as /github/home, /github/workflow, and
  # /github/file_commands. If the runner cannot clean them because the container wrote root-owned
  # files, stale Cargo/Rust state can leak into later jobs. Remove them only when the runner is idle.
  find "$runner_temp_root" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2> /dev/null || true
fi

if [[ -d "$runner_work_root" && "$active_container_count" -eq 0 && "$active_worker_count" -eq 0 ]]; then
  find "$runner_work_root" -mindepth 1 -maxdepth 1 -type d \
    ! -name .ci-cache \
    ! -name _actions \
    ! -name _PipelineMapping \
    ! -name _temp \
    ! -name _tool \
    ! -name _update \
    -mtime "+$actions_work_retention_days" \
    -exec rm -rf {} + 2> /dev/null || true
fi

if [[ -d "$runner_local_cache_root" && "$active_container_count" -eq 0 && "$active_worker_count" -eq 0 ]]; then
  find "$runner_local_cache_root" -mindepth 1 -maxdepth 2 -mtime "+$runner_local_cache_retention_days" -exec rm -rf {} + 2> /dev/null || true
fi

if [[ -d "$runner_ci_home" && "$active_container_count" -eq 0 && "$active_worker_count" -eq 0 ]]; then
  if [[ "$runner_ci_home_purge_when_idle" == "true" ]]; then
    rm -rf "$runner_ci_home" 2> /dev/null || true
  else
    find "$runner_ci_home" -mindepth 1 -mtime "+$runner_temp_retention_days" -exec rm -rf {} + 2> /dev/null || true
  fi
fi

if [[ -d "$workspace_root" ]]; then
  find "$workspace_root" -mindepth 2 -maxdepth 2 -type d -name .tmp-precommit -prune | while read -r dir; do
    find "$dir" -mindepth 1 -maxdepth 1 -mtime "+$precommit_retention_days" -exec rm -rf {} + 2> /dev/null || true
  done

  find "$workspace_root" -mindepth 1 -maxdepth 1 -type d -mtime "+$symphony_workspace_retention_days" -exec rm -rf {} + 2> /dev/null || true
fi

if [[ -d "$control_repo_root" ]]; then
  find "$control_repo_root" -mindepth 1 -maxdepth 1 \
    \( -name .cache -o -name .mypy_cache -o -name .pytest_cache -o -name .ruff_cache -o -name .venv -o -name artifacts -o -name dist -o -name target \) \
    -mtime "+$control_repo_artifact_retention_days" \
    -exec rm -rf {} + 2> /dev/null || true
fi
