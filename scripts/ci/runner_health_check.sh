#!/usr/bin/env bash
set -euo pipefail

runner_role="${RUNNER_ROLE:-generic}"
runner_root="${ACTIONS_RUNNER_ROOT:-/home/ubuntu/actions-runner}"
runner_diag_root="${RUNNER_DIAG_ROOT:-$runner_root/_diag}"
runner_work_root="${RUNNER_WORK_ROOT:-$runner_root/_work}"
runner_local_cache_root="${RUNNER_LOCAL_CACHE_ROOT:-$runner_work_root/.ci-cache}"
workspace_root="${SYMPHONY_WORKSPACE_ROOT:-/srv/symphony/workspaces}"
control_repo_root="${SYMPHONY_CONTROL_REPO_ROOT:-/srv/symphony/control-repo}"
runner_service_names="${RUNNER_SERVICE_NAMES:-${GITHUB_RUNNER_SERVICE_NAME:-actions.runner.antonga23-cloudbet-market-maker.ip-172-31-21-124-cloudbet-market-maker.service}}"
optional_service_names="${OPTIONAL_SERVICE_NAMES:-actions-runner-hygiene.timer}"

print_dir_size() {
  local path="$1"

  if [[ -e "$path" ]]; then
    du -sh "$path" 2> /dev/null || true
  else
    printf 'missing\t%s\n' "$path"
  fi
}

print_dir_children() {
  local path="$1"
  local depth="${2:-1}"

  if [[ -d "$path" ]]; then
    du -xhd"$depth" "$path" 2> /dev/null | sort -h || true
  else
    printf 'missing\t%s\n' "$path"
  fi
}

print_command_version() {
  local binary="$1"
  local label="$2"

  if command -v "$binary" > /dev/null 2>&1; then
    printf '%s\t%s\n' "$label" "$("$binary" --version 2>&1 | head -n1)"
  else
    printf '%s\tmissing\n' "$label"
  fi
}

print_service_state() {
  local service="$1"

  if [[ -z "$service" ]]; then
    return
  fi

  if command -v systemctl > /dev/null 2>&1; then
    printf '%s\t%s\n' "$service" "$(systemctl is-active "$service" 2> /dev/null || true)"
  else
    printf '%s\tunavailable (systemctl missing)\n' "$service"
  fi
}

print_service_group() {
  local csv="$1"
  local service

  IFS=',' read -r -a service_names <<< "$csv"
  for service in "${service_names[@]}"; do
    service="${service#"${service%%[![:space:]]*}"}"
    service="${service%"${service##*[![:space:]]}"}"
    print_service_state "$service"
  done
}

echo "== Summary =="
printf 'host\t%s\n' "$(hostname 2> /dev/null || echo unknown)"
printf 'role\t%s\n' "$runner_role"
printf 'runner_root\t%s\n' "$runner_root"
echo

echo "== Tooling =="
print_command_version bash bash
print_command_version python3 python3
print_command_version git git
print_command_version uv uv
print_command_version gcc gcc
print_command_version clang clang
if command -v docker > /dev/null 2>&1; then
  printf 'docker\t%s\n' "$(docker --version 2> /dev/null || echo unavailable)"
else
  printf 'docker\tmissing\n'
fi
echo

echo "== Filesystem =="
df -hP /
echo

echo "== Runner paths =="
print_dir_size "$runner_root"
print_dir_size "$runner_diag_root"
print_dir_size "$runner_local_cache_root"
echo

echo "== Runner work usage =="
print_dir_children "$runner_work_root" 1
echo

echo "== Services =="
print_service_group "$runner_service_names"
print_service_group "$optional_service_names"
echo

echo "== Docker =="
if command -v docker > /dev/null 2>&1; then
  docker ps -a --format 'table {{.ID}}\t{{.Status}}\t{{.Names}}' 2> /dev/null || true
else
  echo "docker not installed"
fi
echo

if [[ -d "$workspace_root" ]]; then
  echo "== Symphony workspaces =="
  print_dir_children "$workspace_root" 1
  echo
fi

if [[ -d "$control_repo_root" ]]; then
  echo "== Control repo derived artifacts =="
  for artifact in .cache .mypy_cache .pytest_cache .ruff_cache .venv artifacts dist target; do
    print_dir_size "$control_repo_root/$artifact"
  done
fi
