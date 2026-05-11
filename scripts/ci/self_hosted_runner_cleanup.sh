#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "self_hosted_runner_cleanup: failed at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

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
runner_ci_home="${RUNNER_CI_HOME:-$runner_work_root/.ci-home}"
runner_ci_home_purge_when_idle="${RUNNER_CI_HOME_PURGE_WHEN_IDLE:-true}"
workspace_root="${SYMPHONY_WORKSPACE_ROOT:-/srv/symphony/workspaces}"
control_repo_root="${SYMPHONY_CONTROL_REPO_ROOT:-/srv/symphony/control-repo}"
diag_retention_days="${RUNNER_DIAG_RETENTION_DAYS:-2}"
diag_max_mb="${RUNNER_DIAG_MAX_MB:-1024}"
precommit_retention_days="${PRECOMMIT_TEMP_RETENTION_DAYS:-2}"
actions_work_retention_days="${ACTIONS_WORK_RETENTION_DAYS:-2}"
runner_temp_retention_days="${RUNNER_TEMP_RETENTION_DAYS:-2}"
runner_local_cache_retention_days="${RUNNER_LOCAL_CACHE_RETENTION_DAYS:-7}"
symphony_workspace_retention_days="${SYMPHONY_WORKSPACE_RETENTION_DAYS:-2}"
control_repo_artifact_retention_days="${SYMPHONY_CONTROL_REPO_ARTIFACT_RETENTION_DAYS:-2}"
artifact_retention_days="${RUNNER_ARTIFACT_RETENTION_DAYS:-2}"
monitor_log_retention_days="${RUNNER_MONITOR_LOG_RETENTION_DAYS:-2}"
monitor_log_max_mb="${RUNNER_MONITOR_LOG_MAX_MB:-1024}"
docker_build_cache_until="${DOCKER_BUILD_CACHE_PRUNE_UNTIL:-48h}"
prune_target_artifacts="${RUNNER_PRUNE_TARGET_ARTIFACTS:-false}"
root_usage_prune_threshold="${ROOT_USAGE_PRUNE_THRESHOLD_PERCENT:-85}"
root_usage_critical_prune_threshold="${ROOT_USAGE_CRITICAL_PRUNE_THRESHOLD_PERCENT:-95}"
root_tmp_retention_days="${ROOT_TMP_RETENTION_DAYS:-2}"
active_container_count=0
active_worker_count=0

is_non_negative_integer() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

docker_available() {
  command -v docker > /dev/null 2>&1 && docker info > /dev/null 2>&1
}

path_usage_pct() {
  local path="$1"
  local value

  value="$({ df -P "$path" 2> /dev/null || true; } | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')"
  if is_non_negative_integer "$value"; then
    printf '%s\n' "$value"
  else
    printf '0\n'
  fi
}

path_size_mb() {
  local path="$1"
  local value

  value="$({ du -sm "$path" 2> /dev/null || true; } | awk 'NR == 1 {print $1}')"
  if is_non_negative_integer "$value"; then
    printf '%s\n' "$value"
  else
    printf '0\n'
  fi
}

count_running_docker_containers() {
  { docker ps -q 2> /dev/null || true; } | wc -l | tr -d ' '
}

cap_large_file_to_tail() {
  local file="$1"
  local max_mb="$2"
  local tmp_file

  [[ -f "$file" ]] || return 0
  tmp_file="$(mktemp)"
  if tail -c "${max_mb}M" "$file" > "$tmp_file" 2> /dev/null; then
    cat "$tmp_file" > "$file" 2> /dev/null || true
  fi
  rm -f "$tmp_file" 2> /dev/null || true
}

prune_old_artifacts_and_cap_logs() {
  local root="$1"

  [[ -d "$root" ]] || return 0
  find "$root" -type f -mtime "+$artifact_retention_days" \
    \( -path '*/artifacts/*' -o -path '*/dist/*' \) \
    -delete 2> /dev/null || true
  if [[ "$prune_target_artifacts" == "true" ]]; then
    find "$root" -type f -mtime "+$artifact_retention_days" \
      -path '*/target/*' \
      -delete 2> /dev/null || true
  fi
  find "$root" -type f -mtime "+$monitor_log_retention_days" \
    \( -name '*.log' -o -path '*/artifacts/monitors/*' \) \
    -delete 2> /dev/null || true
  find "$root" -type f -size +"${monitor_log_max_mb}"M \
    \( -name '*.log' -o -path '*/artifacts/monitors/*' \) \
    -print 2> /dev/null | while read -r log_file; do
    cap_large_file_to_tail "$log_file" "$monitor_log_max_mb"
  done || true
}

prune_root_tmp_and_logs() {
  local path

  for path in /tmp /var/tmp; do
    [[ -d "$path" ]] || continue
    find "$path" -xdev -mindepth 1 -mtime "+$root_tmp_retention_days" -exec rm -rf {} + 2> /dev/null || true
  done

  if command -v journalctl > /dev/null 2>&1; then
    journalctl --vacuum-time="${monitor_log_retention_days}d" --vacuum-size="${monitor_log_max_mb}M" > /dev/null 2>&1 || true
  fi

  if command -v apt-get > /dev/null 2>&1; then
    apt-get clean > /dev/null 2>&1 || true
  fi

  if [[ -d /var/log ]]; then
    find /var/log -xdev -type f -mtime "+$monitor_log_retention_days" \
      \( -name '*.gz' -o -name '*.1' -o -name '*.old' -o -name '*.log.*' \) \
      -delete 2> /dev/null || true
    find /var/log -xdev -type f -size +"${monitor_log_max_mb}"M \
      \( -name '*.log' -o -name 'syslog' -o -name 'kern.log' \) \
      -print 2> /dev/null | while read -r log_file; do
      cap_large_file_to_tail "$log_file" "$monitor_log_max_mb"
    done || true
  fi
}

if docker_available; then
  docker container prune -f --filter status=exited > /dev/null 2>&1 || true
  docker image prune -f > /dev/null 2>&1 || true
  docker builder prune -f --filter "until=$docker_build_cache_until" > /dev/null 2>&1 || true
  active_container_count="$(count_running_docker_containers)"
fi

if command -v pgrep > /dev/null 2>&1; then
  active_worker_count="$(pgrep -fc 'Runner.Worker' 2> /dev/null || printf '0')"
  if ! is_non_negative_integer "$active_worker_count"; then
    active_worker_count=0
  fi
fi

root_usage_pct="$(path_usage_pct /)"

if [[ "$root_usage_pct" -ge "$root_usage_prune_threshold" ]]; then
  prune_root_tmp_and_logs
  root_usage_pct="$(path_usage_pct /)"
fi

if docker_available && [[ "$root_usage_pct" -ge "$root_usage_prune_threshold" ]]; then
  docker image prune -af --filter "until=168h" > /dev/null 2>&1 || true
  docker volume prune -f > /dev/null 2>&1 || true
  root_usage_pct="$(path_usage_pct /)"
fi

if docker_available && [[ "$root_usage_pct" -ge "$root_usage_critical_prune_threshold" ]]; then
  # At critical root pressure, recent Docker layers are less valuable than preserving
  # the runner. This remains conditional, so normal builds keep warm base layers.
  docker container prune -f > /dev/null 2>&1 || true
  docker image prune -af > /dev/null 2>&1 || true
  docker builder prune -af > /dev/null 2>&1 || true
  docker volume prune -f > /dev/null 2>&1 || true
  root_usage_pct="$(path_usage_pct /)"
fi

if [[ -d "$runner_diag_root" ]]; then
  find "$runner_diag_root" -type f -mtime "+$diag_retention_days" -delete 2> /dev/null || true

  diag_size_mb="$(path_size_mb "$runner_diag_root")"
  if [[ -n "$diag_size_mb" ]] && [[ "$diag_size_mb" -gt "$diag_max_mb" ]]; then
    while [[ "$diag_size_mb" -gt "$diag_max_mb" ]]; do
      oldest_file="$(
        { find "$runner_diag_root" -type f -printf '%T@ %p\n' 2> /dev/null || true; } | sort -n | head -n 1 | cut -d' ' -f2-
      )"
      [[ -n "$oldest_file" ]] || break
      rm -f "$oldest_file" 2> /dev/null || true
      diag_size_mb="$(path_size_mb "$runner_diag_root")"
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
  prune_old_artifacts_and_cap_logs "$workspace_root"

  find "$workspace_root" -mindepth 2 -maxdepth 2 -type d -name .tmp-precommit -prune | while read -r dir; do
    find "$dir" -mindepth 1 -maxdepth 1 -mtime "+$precommit_retention_days" -exec rm -rf {} + 2> /dev/null || true
  done || true

  find "$workspace_root" -mindepth 1 -maxdepth 1 -type d -mtime "+$symphony_workspace_retention_days" -exec rm -rf {} + 2> /dev/null || true
fi

if [[ -d "$control_repo_root" ]]; then
  prune_old_artifacts_and_cap_logs "$control_repo_root"

  find "$control_repo_root" -mindepth 1 -maxdepth 1 \
    \( -name .cache -o -name .mypy_cache -o -name .pytest_cache -o -name .ruff_cache -o -name .venv -o -name artifacts -o -name dist \) \
    -mtime "+$control_repo_artifact_retention_days" \
    -exec rm -rf {} + 2> /dev/null || true
  if [[ "$prune_target_artifacts" == "true" ]]; then
    find "$control_repo_root" -mindepth 1 -maxdepth 1 \
      -name target \
      -mtime "+$control_repo_artifact_retention_days" \
      -exec rm -rf {} + 2> /dev/null || true
  fi
fi

printf 'self_hosted_runner_cleanup: complete runner_root=%s root_usage_pct=%s active_containers=%s active_workers=%s\n' \
  "$runner_root" \
  "$(path_usage_pct /)" \
  "$active_container_count" \
  "$active_worker_count"
