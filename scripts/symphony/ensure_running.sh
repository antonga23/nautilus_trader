#!/usr/bin/env bash
set -euo pipefail

pid_file="/srv/symphony/symphony.pid"
match_pattern='./bin/symphony --i-understand-that-this-will-be-running-without-the-usual-guardrails'

if pgrep -f "$match_pattern" >/dev/null 2>&1; then
  if curl -fsS http://127.0.0.1:4000/api/v1/state >/dev/null 2>&1; then
    pgrep -n -f "$match_pattern" >"$pid_file"
    exit 0
  fi
fi

if [ -f "$pid_file" ]; then
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    if curl -fsS http://127.0.0.1:4000/api/v1/state >/dev/null 2>&1; then
      exit 0
    fi
  fi
  rm -f "$pid_file"
fi

pkill -f "$match_pattern" >/dev/null 2>&1 || true

exec /srv/symphony/control-repo/scripts/symphony/start_detached.sh
