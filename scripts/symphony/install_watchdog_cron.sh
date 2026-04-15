#!/usr/bin/env bash
set -euo pipefail

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT

{
  crontab -l 2>/dev/null || true
} | sed '/# BEGIN SYMPHONY WATCHDOG/,/# END SYMPHONY WATCHDOG/d' >"$tmp_file"

cat >>"$tmp_file" <<'EOF'
# BEGIN SYMPHONY WATCHDOG
@reboot /srv/symphony/control-repo/scripts/symphony/ensure_running.sh >/var/log/symphony/cron.log 2>&1
* * * * * /srv/symphony/control-repo/scripts/symphony/ensure_running.sh >/var/log/symphony/cron.log 2>&1
# END SYMPHONY WATCHDOG
EOF

crontab "$tmp_file"
