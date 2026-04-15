#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/ubuntu
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"

source /srv/symphony/symphony.env

cd /opt/symphony/elixir

exec mise exec -- ./bin/symphony \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails \
  --logs-root /var/log/symphony \
  --port 4000 \
  /srv/symphony/control-repo/WORKFLOW.md
