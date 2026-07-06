#!/usr/bin/env bash
# Idempotent installer for the daily devig live-calibration pipeline on the deploy host.
#
# Installs a systemd service+timer that once a day:
#   1. refreshes the semantic corpus into a dedicated calibration cache for every
#      provider whose credentials are present (Cloudbet also pulls settled bets,
#      which are the settlement evidence the calibration joins against),
#   2. runs semantic_rule_mining.py validate over that cache,
#   3. scores devig methods with devig_live_calibration.py into latest.json.
#
# Provider credentials are read from the repo-local env files that
# semantic_rule_mining.py already loads (.env.cloud-workspace.local / .env.local /
# .env) or from the optional EnvironmentFile at /etc/cloudbet/devig-calibration.conf.
# No secrets are written by this installer.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
install_root="${DEVIG_CALIBRATION_ROOT:-/opt/cloudbet/devig-calibration}"
cache_dir="$install_root/cache"
out_path="$install_root/latest.json"
run_dst="$install_root/run_devig_calibration.sh"
env_file="${DEVIG_CALIBRATION_ENV_FILE:-/etc/cloudbet/devig-calibration.conf}"
runner_user="${DEVIG_CALIBRATION_USER:-ubuntu}"
service_dst="/etc/systemd/system/devig-calibration.service"
timer_dst="/etc/systemd/system/devig-calibration.timer"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

sudo install -d -m 0755 "$install_root" "$(dirname "$env_file")"
sudo install -d -m 0755 -o "$runner_user" "$cache_dir"

cat > "$tmp_dir/run_devig_calibration.sh" << 'RUN'
#!/usr/bin/env bash
set -euo pipefail

repo_root="${DEVIG_CALIBRATION_REPO_ROOT:?}"
cache_dir="${DEVIG_CALIBRATION_CACHE_DIR:?}"
out_path="${DEVIG_CALIBRATION_OUT:?}"
bet_max_pages="${DEVIG_CALIBRATION_BET_MAX_PAGES:-20}"
min_books="${DEVIG_CALIBRATION_MIN_BOOKS:-50}"

python="$repo_root/.venv/bin/python"
if [ ! -x "$python" ]; then
  python="$(command -v python3)"
fi
mining="$repo_root/scripts/betting/semantic_rule_mining.py"

has_key() {
  local name="$1" file
  if [ -n "${!name:-}" ]; then
    return 0
  fi
  for file in "$repo_root/.env.cloud-workspace.local" "$repo_root/.env.local" "$repo_root/.env"; do
    if [ -f "$file" ] && grep -qE "^${name}=." "$file"; then
      return 0
    fi
  done
  return 1
}

if has_key CLOUDBET_API_KEY; then
  "$python" "$mining" refresh-corpus \
    --provider cloudbet \
    --include-bets \
    --settled-bets \
    --bet-page-size 50 \
    --bet-max-pages "$bet_max_pages" \
    --cache-dir "$cache_dir"
  "$python" "$mining" validate \
    --provider cloudbet \
    --cache-dir "$cache_dir"
else
  echo "Skipping cloudbet refresh: CLOUDBET_API_KEY not configured" >&2
fi

if has_key SXBET_API_KEY; then
  "$python" "$mining" refresh-corpus --provider sxbet --cache-dir "$cache_dir"
else
  echo "Skipping sxbet refresh: SXBET_API_KEY not configured" >&2
fi

if has_key POLYMARKET_API_KEY && has_key POLYMARKET_API_SECRET && has_key POLYMARKET_PASSPHRASE \
  && has_key POLYMARKET_PK && has_key POLYMARKET_FUNDER; then
  "$python" "$mining" refresh-corpus --provider polymarket --cache-dir "$cache_dir"
else
  echo "Skipping polymarket refresh: POLYMARKET_* credentials not configured" >&2
fi

"$python" "$repo_root/scripts/betting/devig_live_calibration.py" \
  --cache-dir "$cache_dir" \
  --min-books "$min_books" \
  --out "$out_path"
RUN

cat > "$tmp_dir/devig-calibration.service" << UNIT
[Unit]
Description=Devig live-data calibration (corpus refresh + method scoring)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$runner_user
WorkingDirectory=$repo_root
Environment=DEVIG_CALIBRATION_REPO_ROOT=$repo_root
Environment=DEVIG_CALIBRATION_CACHE_DIR=$cache_dir
Environment=DEVIG_CALIBRATION_OUT=$out_path
EnvironmentFile=-$env_file
ExecStart=$run_dst
UNIT

cat > "$tmp_dir/devig-calibration.timer" << 'UNIT'
[Unit]
Description=Daily devig live-data calibration

[Timer]
OnBootSec=30m
OnUnitActiveSec=24h
RandomizedDelaySec=15m
Persistent=true
Unit=devig-calibration.service

[Install]
WantedBy=timers.target
UNIT

sudo install -m 0755 "$tmp_dir/run_devig_calibration.sh" "$run_dst"
sudo install -m 0644 "$tmp_dir/devig-calibration.service" "$service_dst"
sudo install -m 0644 "$tmp_dir/devig-calibration.timer" "$timer_dst"

if ! sudo test -f "$env_file"; then
  cat << 'EOF' | sudo tee "$env_file" > /dev/null
# Optional overrides for the devig-calibration service. Provider credentials are
# normally read from the repo-local .env files; set them here only if the repo
# checkout has none. Never commit real values anywhere.
# CLOUDBET_API_KEY=
# SXBET_API_KEY=
# POLYMARKET_API_KEY=
# POLYMARKET_API_SECRET=
# POLYMARKET_PASSPHRASE=
# POLYMARKET_PK=
# POLYMARKET_FUNDER=
# DEVIG_CALIBRATION_BET_MAX_PAGES=20
# DEVIG_CALIBRATION_MIN_BOOKS=50
EOF
  sudo chmod 0600 "$env_file"
fi

sudo systemctl daemon-reload
sudo systemctl enable --now devig-calibration.timer
sudo systemctl status devig-calibration.timer --no-pager

echo "Installed. First run happens via the timer; to kick one off now:" >&2
echo "  sudo systemctl start devig-calibration.service" >&2
echo "Latest report: $out_path" >&2
