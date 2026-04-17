#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cleanup_src="$repo_root/scripts/ci/self_hosted_runner_cleanup.sh"
service_src="$repo_root/scripts/ci/actions-runner-hygiene.service"
timer_src="$repo_root/scripts/ci/actions-runner-hygiene.timer"
cleanup_dst="/usr/local/bin/cloudbet-self-hosted-runner-cleanup"
service_dst="/etc/systemd/system/actions-runner-hygiene.service"
timer_dst="/etc/systemd/system/actions-runner-hygiene.timer"

sudo install -d -m 0755 "$(dirname "$cleanup_dst")"
sudo install -m 0755 "$cleanup_src" "$cleanup_dst"
sudo install -m 0644 "$service_src" "$service_dst"
sudo install -m 0644 "$timer_src" "$timer_dst"
sudo systemctl daemon-reload
sudo systemctl enable --now actions-runner-hygiene.timer
sudo systemctl start actions-runner-hygiene.service
sudo systemctl status actions-runner-hygiene.timer --no-pager
