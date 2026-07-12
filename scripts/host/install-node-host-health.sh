#!/usr/bin/env bash
#
# Idempotent installer for the node-host-health monitor (script + systemd oneshot +
# ~2min timer), mirroring scripts/ci/install_runner_hygiene.sh. Copies the monitor and
# its shared lib into an install root, installs the units only if absent (preserving
# operator NODE_HOST_* overrides on re-run), seeds the config file once, then enables
# and starts the timer. Re-runnable safely.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_root="${HOST_TOOLS_INSTALL_ROOT:-/opt/cloudbet/host-tools}"
config_dir="/etc/cloudbet"
config_dst="$config_dir/node-host-health.conf"
service_dst="/etc/systemd/system/node-host-health.service"
timer_dst="/etc/systemd/system/node-host-health.timer"

sudo() { if [[ "$(id -u)" -eq 0 ]]; then "$@"; else command sudo "$@"; fi; }

sudo install -d -m 0755 "$install_root" "$config_dir"
sudo install -m 0755 "$script_dir/node-host-health.sh" "$install_root/node-host-health.sh"
sudo install -m 0644 "$script_dir/host_common.sh" "$install_root/host_common.sh"

if sudo test -f "$service_dst"; then
  echo "Existing $service_dst kept."
else
  sudo install -m 0644 "$script_dir/node-host-health.service" "$service_dst"
fi
if sudo test -f "$timer_dst"; then
  echo "Existing $timer_dst kept."
else
  sudo install -m 0644 "$script_dir/node-host-health.timer" "$timer_dst"
fi

if sudo test -f "$config_dst"; then
  echo "Existing $config_dst kept (preserving overrides)."
else
  echo "Seeding $config_dst"
  cat << EOF | sudo tee "$config_dst" > /dev/null
# node-host-health configuration (see scripts/host/node-host-health.sh header).
NODE_HOST_DISK_PCT=${NODE_HOST_DISK_PCT:-85}
NODE_HOST_DISK_HARD_PCT=${NODE_HOST_DISK_HARD_PCT:-92}
NODE_HOST_MEM_FLOOR_MB=${NODE_HOST_MEM_FLOOR_MB:-1500}
NODE_HOST_PER_NODE_MB=${NODE_HOST_PER_NODE_MB:-3072}
NODEOPS_NODES_ROOT=${NODEOPS_NODES_ROOT:-/opt/cloudbet/strategy-nodes}
NODE_HOST_SESSION_KEEP=${NODE_HOST_SESSION_KEEP:-5}
NODE_HOST_HEARTBEAT_STALE_SECS=${NODE_HOST_HEARTBEAT_STALE_SECS:-180}
NODE_HOST_STATUS_STALE_SECS=${NODE_HOST_STATUS_STALE_SECS:-300}
NODE_HOST_NODE_PREFIX=${NODE_HOST_NODE_PREFIX:-betting-arbitrage-node}
NODE_HOST_AUTO_RESTART=${NODE_HOST_AUTO_RESTART:-1}
NODE_HOST_MAX_RESTARTS_PER_HOUR=${NODE_HOST_MAX_RESTARTS_PER_HOUR:-3}
# Alert webhook: leave blank to inherit the nodeops NODEOPS_ALERT_WEBHOOK at runtime.
NODEOPS_ALERT_WEBHOOK=${NODEOPS_ALERT_WEBHOOK:-}
EOF
fi

sudo systemctl daemon-reload
sudo systemctl enable --now node-host-health.timer
sudo systemctl start node-host-health.service || true
echo "node-host-health installed. Timer:"
sudo systemctl status node-host-health.timer --no-pager || true
