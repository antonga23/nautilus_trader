#!/usr/bin/env bash
set -euo pipefail

canonicalize_path() {
  python3 - "$1" <<'PY'
import os
import sys

print(os.path.realpath(sys.argv[1]))
PY
}

validate_managed_path() {
  local label="$1"
  local raw_path="$2"
  local path

  path="$(canonicalize_path "${raw_path%/}")"
  if [[ -z "$path" || "$path" != /* ]]; then
    echo "${label} must be an absolute path, got: ${raw_path:-<empty>}" >&2
    exit 1
  fi

  case "$path" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
      echo "Refusing unsafe ${label}: $path" >&2
      exit 1
      ;;
  esac

  printf '%s\n' "$path"
}

runner_user="${RUNNER_USER:-actions-runner}"
runner_group="${RUNNER_GROUP:-$runner_user}"
runner_home="${RUNNER_HOME:-/home/$runner_user}"
runner_root="$(canonicalize_path "${RUNNER_ROOT:-/opt/actions-runner}")"
runner_version="${GITHUB_RUNNER_VERSION:-2.333.1}"
runner_platform="${GITHUB_RUNNER_PLATFORM:-linux}"
runner_arch="${GITHUB_RUNNER_ARCH:-x64}"
runner_url="${GITHUB_RUNNER_URL:-}"
runner_token="${GITHUB_RUNNER_TOKEN:-}"
runner_name="${GITHUB_RUNNER_NAME:-$(hostname)}"
runner_labels="${GITHUB_RUNNER_LABELS:-self-hosted,Linux,X64}"
runner_group_name="${GITHUB_RUNNER_GROUP_NAME:-Default}"
runner_workdir="$(canonicalize_path "${GITHUB_RUNNER_WORKDIR:-$runner_root/_work}")"
disable_update="${GITHUB_RUNNER_DISABLE_UPDATE:-true}"
start_service="${START_RUNNER_SERVICE:-true}"
enable_service="${ENABLE_RUNNER_SERVICE:-auto}"
create_service="${CREATE_RUNNER_SERVICE:-true}"
force_reinstall="${FORCE_REINSTALL_RUNNER:-false}"
force_reconfigure="${FORCE_RECONFIGURE_RUNNER:-false}"
service_name="${GITHUB_RUNNER_SERVICE_NAME:-actions.runner.${runner_name}.service}"
template_path="${RUNNER_SERVICE_TEMPLATE:-/usr/local/share/cloudbet/github-actions-runner.service.tmpl}"
checksums_file="${RUNNER_CHECKSUMS_FILE:-/usr/local/share/cloudbet/github-actions-runner-sha256sums.txt}"
tmp_dir="$(mktemp -d)"
archive="actions-runner-${runner_platform}-${runner_arch}-${runner_version}.tar.gz"
download_url="https://github.com/actions/runner/releases/download/v${runner_version}/${archive}"
configured_runner="false"

validate_runner_root_for_destructive_reset() {
  local path
  local base

  path="$(validate_managed_path RUNNER_ROOT "$1")"
  base="$(basename "$path")"
  if [[ "$base" != *runner* && "$base" != "$runner_user" ]]; then
    echo "RUNNER_ROOT must point to a dedicated runner directory, got: $path" >&2
    exit 1
  fi

  printf '%s\n' "$path"
}

validate_runner_home_path() {
  local path
  local base

  path="$(validate_managed_path RUNNER_HOME "$1")"
  base="$(basename "$path")"
  if [[ "$base" != "$runner_user" ]]; then
    echo "RUNNER_HOME must end with the runner user name ($runner_user), got: $path" >&2
    exit 1
  fi

  printf '%s\n' "$path"
}

validate_runner_workdir_path() {
  local path

  path="$(validate_managed_path GITHUB_RUNNER_WORKDIR "$1")"
  case "$path" in
    "$runner_root"/*|"$runner_home"/*)
      ;;
    *)
      echo "GITHUB_RUNNER_WORKDIR must live under RUNNER_ROOT or RUNNER_HOME, got: $path" >&2
      exit 1
      ;;
  esac

  printf '%s\n' "$path"
}

cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo --preserve-env=RUNNER_USER,RUNNER_GROUP,RUNNER_HOME,RUNNER_ROOT,GITHUB_RUNNER_VERSION,GITHUB_RUNNER_PLATFORM,GITHUB_RUNNER_ARCH,GITHUB_RUNNER_URL,GITHUB_RUNNER_TOKEN,GITHUB_RUNNER_NAME,GITHUB_RUNNER_LABELS,GITHUB_RUNNER_GROUP_NAME,GITHUB_RUNNER_WORKDIR,GITHUB_RUNNER_DISABLE_UPDATE,START_RUNNER_SERVICE,ENABLE_RUNNER_SERVICE,CREATE_RUNNER_SERVICE,FORCE_REINSTALL_RUNNER,FORCE_RECONFIGURE_RUNNER,GITHUB_RUNNER_SERVICE_NAME,RUNNER_SERVICE_TEMPLATE,RUNNER_CHECKSUMS_FILE "$0" "$@"
  fi

  echo "install-github-actions-runner.sh must run as root" >&2
  exit 1
fi

runner_home="$(validate_runner_home_path "$runner_home")"
runner_root="$(validate_runner_root_for_destructive_reset "$runner_root")"
runner_workdir="$(validate_runner_workdir_path "$runner_workdir")"

install -d -m 0755 -o "$runner_user" -g "$runner_group" "$runner_home" "$runner_root" "$runner_workdir"
chown -R "$runner_user:$runner_group" "$runner_root"

if [[ "$force_reinstall" = "true" || ! -x "$runner_root/config.sh" || ! -x "$runner_root/runsvc.sh" ]]; then
  rm -rf "$runner_root"
  install -d -m 0755 -o "$runner_user" -g "$runner_group" "$runner_root" "$runner_workdir"

  if [[ ! -f "$checksums_file" ]]; then
    echo "Runner checksum manifest not found: $checksums_file" >&2
    exit 1
  fi

  expected_sha="$(awk -v archive="$archive" '$2 == archive {print $1}' "$checksums_file")"
  if [[ -z "$expected_sha" ]]; then
    echo "No pinned SHA-256 found for runner archive: $archive" >&2
    exit 1
  fi

  curl -fsSL "$download_url" -o "$tmp_dir/$archive"
  actual_sha="$(sha256sum "$tmp_dir/$archive" | awk '{print $1}')"
  if [[ "$actual_sha" != "$expected_sha" ]]; then
    echo "Runner archive checksum mismatch for $archive" >&2
    echo "Expected: $expected_sha" >&2
    echo "Actual:   $actual_sha" >&2
    exit 1
  fi

  tar -xzf "$tmp_dir/$archive" -C "$runner_root"
  chown -R "$runner_user:$runner_group" "$runner_root"
fi

if [[ -n "$runner_url" && -n "$runner_token" ]]; then
  if [[ "$force_reconfigure" = "true" || ! -f "$runner_root/.runner" ]]; then
    if [[ -f "$runner_root/.runner" ]]; then
      sudo -u "$runner_user" HOME="$runner_home" "$runner_root/config.sh" remove --unattended --token "$runner_token" || true
    fi

    config_args=(
      --url "$runner_url"
      --token "$runner_token"
      --name "$runner_name"
      --runnergroup "$runner_group_name"
      --work "$runner_workdir"
      --labels "$runner_labels"
      --unattended
      --replace
    )

    if [[ "$disable_update" = "true" ]]; then
      config_args+=(--disableupdate)
    fi

    sudo -u "$runner_user" HOME="$runner_home" "$runner_root/config.sh" "${config_args[@]}"
  fi
fi

if [[ -f "$runner_root/.runner" ]]; then
  configured_runner="true"
fi

if [[ "$create_service" = "true" && -f "$template_path" && -d /etc/systemd/system ]]; then
  unit_path="/etc/systemd/system/${service_name}"

  sed \
    -e "s#__SERVICE_NAME__#${service_name}#g" \
    -e "s#__RUNNER_ROOT__#${runner_root}#g" \
    -e "s#__RUNNER_USER__#${runner_user}#g" \
    -e "s#__RUNNER_HOME__#${runner_home}#g" \
    "$template_path" > "$unit_path"

  chmod 0644 "$unit_path"
  systemctl daemon-reload
  if [[ "$enable_service" = "true" && "$configured_runner" != "true" ]]; then
    echo "Cannot enable ${service_name} before the runner is configured" >&2
    exit 1
  fi

  if [[ "$configured_runner" = "true" && "$enable_service" != "false" ]]; then
    systemctl enable "$service_name" >/dev/null 2>&1 || true

    if [[ "$start_service" = "true" ]]; then
      systemctl restart "$service_name"
    fi
  fi
fi

echo "Runner root: $runner_root"
echo "Runner version: $runner_version"
echo "Runner name: $runner_name"
echo "Service name: $service_name"
echo "Runner configured: $configured_runner"
