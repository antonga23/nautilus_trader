#!/usr/bin/env bash
set -euo pipefail

cd /opt/symphony/elixir
export HOME=/home/ubuntu
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"

set -a
source /srv/symphony/symphony.env
set +a

nohup "$HOME/.local/bin/mise" exec -- ./bin/symphony \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails \
  --logs-root /var/log/symphony \
  --port 4000 \
  /srv/symphony/control-repo/WORKFLOW.md \
  > /var/log/symphony/launcher.stdout.log \
  2> /var/log/symphony/launcher.stderr.log &

launcher_pid="$!"
actual_pid=""

for _ in $(seq 1 20); do
  actual_pid="$(pgrep -n -f './bin/symphony --i-understand-that-this-will-be-running-without-the-usual-guardrails' || true)"
  if [ -n "$actual_pid" ]; then
    break
  fi
  sleep 1
done

echo "${actual_pid:-$launcher_pid}" > /srv/symphony/symphony.pid
