#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cleanup_src="$repo_root/scripts/ci/self_hosted_runner_cleanup.sh"
service_src="$repo_root/scripts/ci/actions-runner-hygiene.service"
timer_src="$repo_root/scripts/ci/actions-runner-hygiene.timer"
install_root="${RUNNER_HYGIENE_INSTALL_ROOT:-/opt/cloudbet-runner-hygiene}"
cleanup_dst="$install_root/self_hosted_runner_cleanup.sh"
cleanup_link="/usr/local/bin/cloudbet-self-hosted-runner-cleanup"
service_dst="/etc/systemd/system/actions-runner-hygiene.service"
timer_dst="/etc/systemd/system/actions-runner-hygiene.timer"
config_dir="/etc/cloudbet"
layout_config_dst="$config_dir/self-hosted-runner.conf"
hygiene_config_dst="$config_dir/actions-runner-hygiene.conf"

detect_runner_root() {
  local candidate

  for candidate in \
    "${ACTIONS_RUNNER_ROOT:-}" \
    /opt/actions-runner \
    /home/ubuntu/actions-runner; do
    if [[ -n "$candidate" && -d "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  printf '%s\n' "${ACTIONS_RUNNER_ROOT:-/home/ubuntu/actions-runner}"
}

runner_root="$(detect_runner_root)"
runner_work_root="${RUNNER_WORK_ROOT:-$runner_root/_work}"
runner_local_cache_root="${RUNNER_LOCAL_CACHE_ROOT:-$runner_work_root/.ci-cache}"
runner_ci_home="${RUNNER_CI_HOME:-$runner_work_root/.ci-home}"

sudo install -d -m 0755 "$install_root" "$(dirname "$cleanup_link")" "$config_dir"
sudo install -m 0755 "$cleanup_src" "$cleanup_dst"
sudo ln -sf "$cleanup_dst" "$cleanup_link"
sudo install -m 0644 "$service_src" "$service_dst"
sudo install -m 0644 "$timer_src" "$timer_dst"
cat << EOF | sudo tee "$layout_config_dst" > /dev/null
RUNNER_ROOT=$runner_root
RUNNER_DIAG_ROOT=$runner_root/_diag
RUNNER_WORK_ROOT=$runner_work_root
RUNNER_LOCAL_CACHE_ROOT=$runner_local_cache_root
EOF

if ! sudo test -f "$hygiene_config_dst"; then
  cat << EOF | sudo tee "$hygiene_config_dst" > /dev/null
RUNNER_CI_HOME=$runner_ci_home
RUNNER_CI_HOME_PURGE_WHEN_IDLE=true
EOF
fi

sudo systemctl daemon-reload
sudo systemctl enable --now actions-runner-hygiene.timer
sudo systemctl start actions-runner-hygiene.service
sudo systemctl status actions-runner-hygiene.timer --no-pager
