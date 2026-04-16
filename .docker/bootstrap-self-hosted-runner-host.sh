#!/usr/bin/env bash
set -euo pipefail

canonicalize_path() {
  python3 - "$1" << 'PY'
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
    / | /bin | /boot | /dev | /etc | /home | /lib | /lib64 | /media | /mnt | /opt | /proc | /root | /run | /sbin | /srv | /sys | /tmp | /usr | /var)
      echo "Refusing unsafe ${label}: $path" >&2
      exit 1
      ;;
  esac

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

validate_runner_root_path() {
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

validate_account_name() {
  local label="$1"
  local value="$2"

  if [[ ! "$value" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
    echo "${label} must be a valid system account name, got: ${value:-<empty>}" >&2
    exit 1
  fi

  printf '%s\n' "$value"
}

validate_workspace_repair_target_path() {
  local path

  path="$(validate_managed_path RUNNER_WORKSPACE_REPAIR_TARGET "$1")"
  case "$path" in
    /usr/local/bin/repair-github-runner-workspace | /usr/local/bin/repair-github-runner-workspace.sh | /usr/bin/repair-github-runner-workspace | /usr/bin/repair-github-runner-workspace.sh) ;;
    *)
      echo "RUNNER_WORKSPACE_REPAIR_TARGET must install to a repair-helper path under /usr/local/bin or /usr/bin, got: $path" >&2
      exit 1
      ;;
  esac

  printf '%s\n' "$path"
}

validate_runner_layout_config_path() {
  local path

  path="$(validate_managed_path RUNNER_LAYOUT_CONFIG "$1")"
  case "$path" in
    /etc/cloudbet/*.conf)
      ;;
    *)
      echo "RUNNER_LAYOUT_CONFIG must live under /etc/cloudbet and end with .conf, got: $path" >&2
      exit 1
      ;;
  esac

  printf '%s\n' "$path"
}

bootstrap_mode="${BOOTSTRAP_MODE:-host}"
runner_user="${RUNNER_USER:-actions-runner}"
runner_group="${RUNNER_GROUP:-$runner_user}"
runner_home="${RUNNER_HOME:-/home/$runner_user}"
runner_root="${RUNNER_ROOT:-/opt/actions-runner}"
packages_file="${PACKAGES_FILE:-/usr/local/share/cloudbet/self-hosted-runner-packages.txt}"
sudoers_file="/etc/sudoers.d/90-${runner_user}"
install_runner="${INSTALL_GITHUB_ACTIONS_RUNNER:-false}"
runner_installer="${RUNNER_INSTALLER_PATH:-/usr/local/bin/install-github-actions-runner}"
workspace_repair_target="${RUNNER_WORKSPACE_REPAIR_TARGET:-/usr/local/bin/repair-github-runner-workspace}"
runner_layout_config="${RUNNER_LAYOUT_CONFIG:-/etc/cloudbet/self-hosted-runner.conf}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
default_workspace_repair_source="${script_dir}/repair-github-runner-workspace.sh"

if [[ ! -f "$default_workspace_repair_source" && -f "${script_dir}/repair-github-runner-workspace" ]]; then
  default_workspace_repair_source="${script_dir}/repair-github-runner-workspace"
fi

workspace_repair_source="${RUNNER_WORKSPACE_REPAIR_SOURCE:-$default_workspace_repair_source}"

if [[ ! -f "$packages_file" ]]; then
  echo "Package manifest not found: $packages_file" >&2
  exit 1
fi

if [[ "$bootstrap_mode" != "image" && "${EUID}" -ne 0 ]]; then
  if command -v sudo > /dev/null 2>&1; then
    exec sudo --preserve-env=BOOTSTRAP_MODE,RUNNER_USER,RUNNER_GROUP,RUNNER_HOME,RUNNER_ROOT,PACKAGES_FILE,INSTALL_GITHUB_ACTIONS_RUNNER,RUNNER_INSTALLER_PATH,RUNNER_WORKSPACE_REPAIR_SOURCE,RUNNER_WORKSPACE_REPAIR_TARGET,RUNNER_LAYOUT_CONFIG "$0" "$@"
  fi

  echo "bootstrap-self-hosted-runner-host.sh must run as root" >&2
  exit 1
fi

runner_user="$(validate_account_name RUNNER_USER "$runner_user")"
runner_group="$(validate_account_name RUNNER_GROUP "$runner_group")"
runner_home="$(validate_runner_home_path "$runner_home")"
runner_root="$(validate_runner_root_path "$runner_root")"
workspace_repair_target="$(validate_workspace_repair_target_path "$workspace_repair_target")"
runner_layout_config="$(validate_runner_layout_config_path "$runner_layout_config")"

mapfile -t packages < <(grep -Ev '^\s*(#|$)' "$packages_file")

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends "${packages[@]}"
apt-get clean
rm -rf /var/lib/apt/lists/*

if ! getent group "$runner_group" > /dev/null 2>&1; then
  groupadd --system "$runner_group"
fi

if ! id -u "$runner_user" > /dev/null 2>&1; then
  useradd \
    --system \
    --create-home \
    --home-dir "$runner_home" \
    --shell /bin/bash \
    --gid "$runner_group" \
    "$runner_user"
fi

if ! getent group docker > /dev/null 2>&1; then
  groupadd docker
fi

usermod -aG docker "$runner_user"

install -d -m 0755 -o "$runner_user" -g "$runner_group" \
  "$runner_home" \
  "$runner_home/.cache" \
  "$runner_root" \
  "$runner_root/_diag" \
  "$runner_root/_work"

if [[ ! -f "$workspace_repair_source" ]]; then
  echo "Runner workspace repair helper not found: $workspace_repair_source" >&2
  exit 1
fi

install -m 0755 "$workspace_repair_source" "$workspace_repair_target"
install -d -m 0755 "$(dirname "$runner_layout_config")"
cat > "$runner_layout_config" << EOF
RUNNER_ROOT=$runner_root
EOF
chmod 0644 "$runner_layout_config"

cat > "$sudoers_file" << EOF
$runner_user ALL=(root) NOPASSWD:${workspace_repair_target} *
EOF
chmod 440 "$sudoers_file"

if [[ "$bootstrap_mode" = "host" ]] && command -v systemctl > /dev/null 2>&1; then
  systemctl enable docker > /dev/null 2>&1 || true
  systemctl start docker > /dev/null 2>&1 || true
fi

if [[ "$install_runner" = "true" ]]; then
  if [[ ! -x "$runner_installer" ]]; then
    echo "Runner installer not found: $runner_installer" >&2
    exit 1
  fi

  "$runner_installer"
fi

echo "Bootstrap mode: $bootstrap_mode"
echo "Runner user: $runner_user"
echo "Runner root: $runner_root"
echo "Installed packages: ${packages[*]}"
