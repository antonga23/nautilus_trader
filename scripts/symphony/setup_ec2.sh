#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script is intended to run on the EC2 host." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y \
  nginx \
  build-essential \
  ca-certificates \
  curl \
  git \
  jq \
  openssl \
  unzip \
  python3 \
  python3-pip \
  openssh-client

if ! command -v aws > /dev/null 2>&1; then
  tmp_dir=$(mktemp -d)
  trap 'rm -rf "$tmp_dir"' EXIT
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$tmp_dir/awscliv2.zip"
  (cd "$tmp_dir" && unzip -q awscliv2.zip && sudo ./aws/install)
fi

if ! command -v gh > /dev/null 2>&1; then
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg |
    sudo tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" |
    sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
  sudo apt-get update
  sudo apt-get install -y gh
fi

if ! command -v node > /dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

if ! command -v uv > /dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if ! command -v mise > /dev/null 2>&1; then
  curl https://mise.run | sh
fi

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.local/share/mise/shims:$PATH"

if ! command -v codex > /dev/null 2>&1; then
  sudo npm install -g @openai/codex
fi

install_root=/opt/symphony
if [[ ! -d "$install_root/.git" ]]; then
  sudo git clone --depth 1 https://github.com/openai/symphony.git "$install_root"
else
  sudo git -C "$install_root" fetch origin
  sudo git -C "$install_root" reset --hard origin/main
fi
sudo chown -R "$USER":"$USER" "$install_root"

cd "$install_root/elixir"
mise trust
mise install
mise exec -- mix setup
mise exec -- mix build

sudo install -d -m 2775 -o "$USER" -g symphony /srv/symphony 2> /dev/null || {
  sudo install -d -m 755 /srv/symphony
}
if getent group symphony > /dev/null 2>&1; then
  sudo chgrp symphony /srv/symphony
  sudo chmod 2775 /srv/symphony
fi
sudo install -d -m 2775 /srv/symphony/control-repo
sudo install -d -m 2775 /srv/symphony/workspaces
sudo install -d -m 755 /var/log/symphony
if getent group symphony > /dev/null 2>&1; then
  sudo chgrp symphony /srv/symphony/control-repo /srv/symphony/workspaces
  sudo chmod 2775 /srv/symphony/control-repo /srv/symphony/workspaces
fi

if [[ -n "${GITHUB_REPO:-}" ]]; then
  if [[ ! -d /srv/symphony/control-repo/.git ]]; then
    git clone "https://github.com/${GITHUB_REPO}.git" /srv/symphony/control-repo
  else
    git -C /srv/symphony/control-repo fetch origin
    git -C /srv/symphony/control-repo checkout sports-arbitrage || true
    git -C /srv/symphony/control-repo pull --ff-only origin sports-arbitrage || true
  fi
fi

echo "Base runtime is installed. Sync repo files, provision worker users, install worker auth, and then start the detached launcher/control plane." >&2
