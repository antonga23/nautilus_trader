#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
config_path="$repo_root/scripts/symphony/workers.json"
shared_group="symphony"
worker_state_root="/srv/symphony/worker-state"

if [ ! -f "$config_path" ]; then
  echo "Missing worker config: $config_path" >&2
  exit 1
fi

if ! getent group "$shared_group" >/dev/null 2>&1; then
  sudo groupadd --system "$shared_group"
fi

sudo usermod -a -G "$shared_group" ubuntu
sudo install -d -o ubuntu -g "$shared_group" -m 2775 /srv/symphony

jq -c '.workers[]' "$config_path" | while read -r worker_json; do
  user="$(jq -r '.user' <<<"$worker_json")"

  if ! id "$user" >/dev/null 2>&1; then
    sudo useradd --create-home --shell /bin/bash --groups "$shared_group" "$user"
  else
    sudo usermod -a -G "$shared_group" "$user"
  fi

  sudo install -d -o "$user" -g "$user" -m 700 "/home/$user/.codex"
  sudo install -d -o "$user" -g "$user" -m 755 \
    "/home/$user/.cache" \
    "/home/$user/.cache/pre-commit" \
    "/home/$user/.cache/go-build" \
    "/home/$user/.cache/uv" \
    "/home/$user/.local/state"
  sudo tee "/home/$user/.codex/config.toml" >/dev/null <<'CONFIG'
cli_auth_credentials_store = "file"
forced_login_method = "chatgpt"
CONFIG
  sudo chown "$user:$user" "/home/$user/.codex/config.toml"
  sudo chmod 600 "/home/$user/.codex/config.toml"
done

sudo install -d -o ubuntu -g "$shared_group" -m 2775 /srv/symphony/control-repo
sudo install -d -o ubuntu -g "$shared_group" -m 2775 /srv/symphony/workspaces
sudo install -d -o ubuntu -g "$shared_group" -m 2775 "$worker_state_root"
sudo install -d -o ubuntu -g "$shared_group" -m 2775 "$worker_state_root/locks"
sudo install -d -o ubuntu -g "$shared_group" -m 2775 "$worker_state_root/workers"
sudo install -d -o ubuntu -g "$shared_group" -m 2775 /var/log/symphony/workers

sudo chgrp -R "$shared_group" /srv/symphony/control-repo
sudo chmod -R g+rX /srv/symphony/control-repo
sudo find /srv/symphony/control-repo -type d -exec chmod g+s {} +

if [ -f /home/ubuntu/.codex/auth.json ]; then
  sudo mv /home/ubuntu/.codex/auth.json "/home/ubuntu/.codex/auth.json.disabled-$(date +%Y%m%d%H%M%S)"
fi
