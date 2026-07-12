#!/usr/bin/env bash
#
# Idempotent installer for the disk-hygiene routine (script + systemd oneshot + daily
# timer), mirroring scripts/ci/install_runner_hygiene.sh. Copies the script and its
# shared lib into the host-tools install root, installs the units only if absent,
# seeds the config once, then enables the timer. Re-runnable safely.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_root="${HOST_TOOLS_INSTALL_ROOT:-/opt/cloudbet/host-tools}"
config_dir="/etc/cloudbet"
config_dst="$config_dir/node-host-disk-hygiene.conf"
service_dst="/etc/systemd/system/disk-hygiene.service"
timer_dst="/etc/systemd/system/disk-hygiene.timer"

sudo() { if [[ "$(id -u)" -eq 0 ]]; then "$@"; else command sudo "$@"; fi; }

sudo install -d -m 0755 "$install_root" "$config_dir"
sudo install -m 0755 "$script_dir/disk-hygiene.sh" "$install_root/disk-hygiene.sh"
sudo install -m 0644 "$script_dir/host_common.sh" "$install_root/host_common.sh"

if sudo test -f "$service_dst"; then
  echo "Existing $service_dst kept."
else
  sudo install -m 0644 "$script_dir/disk-hygiene.service" "$service_dst"
fi
if sudo test -f "$timer_dst"; then
  echo "Existing $timer_dst kept."
else
  sudo install -m 0644 "$script_dir/disk-hygiene.timer" "$timer_dst"
fi

if sudo test -f "$config_dst"; then
  echo "Existing $config_dst kept (preserving overrides)."
else
  echo "Seeding $config_dst"
  cat << EOF | sudo tee "$config_dst" > /dev/null
# disk-hygiene configuration (see scripts/host/disk-hygiene.sh header).
NODEOPS_NODES_ROOT=${NODEOPS_NODES_ROOT:-/opt/cloudbet/strategy-nodes}
DISK_HYGIENE_SESSION_KEEP=${DISK_HYGIENE_SESSION_KEEP:-10}
DISK_HYGIENE_SESSION_MAX_AGE_DAYS=${DISK_HYGIENE_SESSION_MAX_AGE_DAYS:-30}
DISK_HYGIENE_ARCHIVE_RETENTION_DAYS=${DISK_HYGIENE_ARCHIVE_RETENTION_DAYS:-14}
DISK_HYGIENE_JOURNAL_MAX_SIZE=${DISK_HYGIENE_JOURNAL_MAX_SIZE:-500M}
DISK_HYGIENE_JOURNAL_MAX_AGE=${DISK_HYGIENE_JOURNAL_MAX_AGE:-14d}
DISK_HYGIENE_DOCKER_BUILD_CACHE_UNTIL=${DISK_HYGIENE_DOCKER_BUILD_CACHE_UNTIL:-168h}
DISK_HYGIENE_PRUNE_DOCKER=${DISK_HYGIENE_PRUNE_DOCKER:-1}
EOF
fi

sudo systemctl daemon-reload
sudo systemctl enable --now disk-hygiene.timer
echo "disk-hygiene installed. Timer:"
sudo systemctl status disk-hygiene.timer --no-pager || true
