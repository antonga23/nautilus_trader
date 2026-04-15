#!/usr/bin/env bash
set -euo pipefail

repo_root="/srv/symphony/control-repo"
pid_file="/srv/symphony/symphony.pid"

cd "$repo_root"
./scripts/symphony/render_env_from_secret.sh
./scripts/symphony/restore_worker_auths_from_secret.sh || true
./scripts/symphony/restore_antigravity_auths_from_secret.sh || true
install -d -m 755 /var/log/symphony

if [ -f "$pid_file" ]; then
  pid="$(cat "$pid_file" 2> /dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2> /dev/null; then
    exit 0
  fi
  rm -f "$pid_file"
fi

/usr/bin/script -qfec "$repo_root/scripts/symphony/launch_nohup_inner.sh" \
  /var/log/symphony/launcher.tty.log
