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

runner_role="${RUNNER_ROLE:-generic}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
runner_package_manifest="${RUNNER_PACKAGE_MANIFEST:-$repo_root/.docker/self-hosted-runner-packages.txt}"
runner_service_name_prefix="${RUNNER_SERVICE_NAME_PREFIX:-actions.runner.antonga23-cloudbet-market-maker}"
runner_service_glob="${RUNNER_SERVICE_GLOB:-${runner_service_name_prefix}*.service}"

trim_whitespace() {
  local value="$1"

  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

discover_runner_service_names() {
  local exact_service
  local -a matches=()

  if [[ -n "${RUNNER_SERVICE_NAMES:-}" ]]; then
    printf '%s\n' "$RUNNER_SERVICE_NAMES"
    return
  fi

  if [[ -n "${GITHUB_RUNNER_SERVICE_NAME:-}" ]]; then
    printf '%s\n' "$GITHUB_RUNNER_SERVICE_NAME"
    return
  fi

  if ! command -v systemctl > /dev/null 2>&1; then
    printf '%s\n' ""
    return
  fi

  if [[ -n "${RUNNER_NAME:-}" ]]; then
    exact_service="${runner_service_name_prefix}.${RUNNER_NAME}.service"
    mapfile -t matches < <(
      systemctl list-unit-files --type=service --no-legend "$exact_service" 2> /dev/null |
        awk '{print $1}' |
        sed '/^$/d'
    )
    if [[ "${#matches[@]}" -gt 0 ]]; then
      printf '%s\n' "$exact_service"
      return
    fi

    mapfile -t matches < <(
      systemctl list-units --all --type=service --no-legend "$exact_service" 2> /dev/null |
        awk '{print $1}' |
        sed '/^$/d'
    )
    if [[ "${#matches[@]}" -gt 0 ]]; then
      printf '%s\n' "$exact_service"
      return
    fi
  fi

  mapfile -t matches < <(
    systemctl list-units --all --type=service --state=active --no-legend "$runner_service_glob" 2> /dev/null |
      awk '{print $1}' |
      sed '/^$/d' |
      sort -u
  )
  if [[ "${#matches[@]}" -gt 0 ]]; then
    (
      IFS=,
      printf '%s\n' "${matches[*]}"
    )
    return
  fi

  mapfile -t matches < <(
    systemctl list-unit-files --type=service --no-legend "$runner_service_glob" 2> /dev/null |
      awk '{print $1}' |
      sed '/^$/d' |
      sort -u
  )
  if [[ "${#matches[@]}" -gt 0 ]]; then
    (
      IFS=,
      printf '%s\n' "${matches[*]}"
    )
    return
  fi

  printf '%s\n' ""
}

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
runner_local_cache_root="${RUNNER_LOCAL_CACHE_ROOT:-$runner_work_root/.ci-cache}"
runner_temp_root="${RUNNER_TEMP_ROOT:-$runner_work_root/_temp}"
runner_ci_home="${RUNNER_CI_HOME:-/tmp/cloudbet-market-maker-ci-home}"
workspace_root="${SYMPHONY_WORKSPACE_ROOT:-/srv/symphony/workspaces}"
control_repo_root="${SYMPHONY_CONTROL_REPO_ROOT:-/srv/symphony/control-repo}"
runner_service_names="$(discover_runner_service_names)"
optional_service_names="${OPTIONAL_SERVICE_NAMES:-actions-runner-hygiene.timer}"
health_failures=0

record_failure() {
  local message="$1"

  printf 'ERROR\t%s\n' "$message" >&2
  health_failures=$((health_failures + 1))
}

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

service_state() {
  local service="$1"

  if [[ -z "$service" ]]; then
    printf '%s\n' "unconfigured"
    return
  fi

  if command -v systemctl > /dev/null 2>&1; then
    systemctl is-active "$service" 2> /dev/null || true
  else
    printf '%s\n' "unavailable (systemctl missing)"
  fi
}

print_service_group() {
  local csv="$1"
  local required="${2:-false}"
  local service
  local state
  local found=0

  IFS=',' read -r -a service_names <<< "$csv"
  for service in "${service_names[@]}"; do
    service="$(trim_whitespace "$service")"
    if [[ -z "$service" ]]; then
      continue
    fi

    found=1
    state="$(service_state "$service")"
    printf '%s\t%s\n' "$service" "$state"
    if [[ "$required" == "true" && "$state" != "active" ]]; then
      record_failure "required service is not active: $service ($state)"
    fi
  done

  if [[ "$required" == "true" && "$found" -eq 0 ]]; then
    printf 'runner-services\tmissing (%s)\n' "$runner_service_glob"
    record_failure "no runner service matched ${RUNNER_NAME:-<unknown runner name>} or ${runner_service_glob}"
  fi
}

print_package_state() {
  local package="$1"

  if command -v dpkg > /dev/null 2>&1 && dpkg -s "$package" > /dev/null 2>&1; then
    printf '%s\tinstalled\n' "$package"
    return 0
  fi

  printf '%s\tmissing\n' "$package"
  return 1
}

check_ci_bootstrap_packages() {
  local package
  local -a missing_packages=()

  printf 'manifest\t%s\n' "$runner_package_manifest"
  if [[ ! -f "$runner_package_manifest" ]]; then
    record_failure "runner package manifest not found: $runner_package_manifest"
    return
  fi

  if ! command -v dpkg > /dev/null 2>&1; then
    record_failure "dpkg is unavailable; cannot verify CI bootstrap packages"
    return
  fi

  while IFS= read -r package; do
    if ! print_package_state "$package"; then
      missing_packages+=("$package")
    fi
  done < <(grep -Ev '^\s*(#|$)' "$runner_package_manifest")

  if [[ "${#missing_packages[@]}" -gt 0 ]]; then
    record_failure "CI bootstrap packages missing: ${missing_packages[*]}"
  fi
}

echo "== Summary =="
printf 'host\t%s\n' "$(hostname 2> /dev/null || echo unknown)"
printf 'role\t%s\n' "$runner_role"
printf 'runner_root\t%s\n' "$runner_root"
printf 'runner_service_glob\t%s\n' "$runner_service_glob"
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
print_dir_size "$runner_temp_root"
print_dir_size "$runner_local_cache_root"
print_dir_size "$runner_ci_home"
echo

echo "== Runner work usage =="
print_dir_children "$runner_work_root" 1
echo

if [[ "$runner_role" == "ci" ]]; then
  echo "== CI bootstrap packages =="
  check_ci_bootstrap_packages
  echo
fi

echo "== Services =="
print_service_group "$runner_service_names" true
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

echo
if [[ "$health_failures" -gt 0 ]]; then
  printf 'health\tfailed (%d issue(s))\n' "$health_failures" >&2
  exit 1
fi

printf 'health\tok\n'
