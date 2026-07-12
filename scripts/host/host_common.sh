# shellcheck shell=bash
# Shared helpers for the portable trading-node host toolkit.
#
# Sourced by node-host-health.sh and disk-hygiene.sh (and copied alongside them by
# the installers). Pure-logic helpers take their inputs as arguments/stdin so the
# node-host-health.sh --self-test can exercise them with injected values instead of
# reading the real host. No side effects at source time.

# -- logging --------------------------------------------------------------------

hc_log() {
  # hc_log <level> <message...> — one structured line to stderr (journald captures it).
  local level="$1"
  shift
  printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$level" "$*" >&2
}

# -- numeric / parsing helpers (pure) -------------------------------------------

hc_is_uint() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

hc_parse_df_pct() {
  # Read a `df -P <path>` block on stdin and echo the integer use-percent (0-100),
  # or 0 when it cannot be parsed. Kept separate from the df call so it is testable.
  local value
  value="$(awk 'NR == 2 {gsub(/%/, "", $5); print $5}')"
  if hc_is_uint "$value"; then
    printf '%s\n' "$value"
  else
    printf '0\n'
  fi
}

hc_mem_preflight_ok() {
  # hc_mem_preflight_ok <available_mb> <need_mb> <floor_mb>
  # Return 0 when starting a node needing <need_mb> would still leave MemAvailable at
  # or above <floor_mb>; return 1 otherwise. This is the OOM anti-recurrence guard.
  local available="$1" need="$2" floor="$3"
  hc_is_uint "$available" && hc_is_uint "$need" && hc_is_uint "$floor" || return 2
  [[ $((available - need)) -ge $floor ]]
}

hc_recommend_max_nodes() {
  # hc_recommend_max_nodes <total_mb> <per_node_mb> <reserve_mb>
  # floor((total - reserve) / per_node), never negative. Heuristic capacity planner.
  local total="$1" per_node="$2" reserve="$3"
  hc_is_uint "$total" && hc_is_uint "$per_node" && hc_is_uint "$reserve" || return 2
  [[ "$per_node" -gt 0 ]] || return 2
  local usable=$((total - reserve))
  if [[ "$usable" -le 0 ]]; then
    printf '0\n'
    return 0
  fi
  printf '%s\n' $((usable / per_node))
}

hc_restart_allowed() {
  # hc_restart_allowed <count_in_window> <max_per_window>
  # Return 0 while under the restart-storm cap, 1 once it is reached.
  local count="$1" max="$2"
  hc_is_uint "$count" && hc_is_uint "$max" || return 2
  [[ "$count" -lt "$max" ]]
}

hc_list_subdir_names() {
  # Echo the basename of each immediate subdirectory of <parent>, one per line.
  # Glob-based so it is portable (no GNU `find -printf`) and testable off-Linux.
  local parent="$1" d
  for d in "$parent"/*/; do
    [[ -d "$d" ]] || continue
    basename "$d"
  done
}

hc_sessions_to_rotate() {
  # hc_sessions_to_rotate <keep_n> — read newline-separated session names on stdin,
  # echo the ones to rotate: everything except the newest <keep_n>. Session dir names
  # are timestamp-prefixed (YYYYmmddTHHMMSSZ-pid) so a reverse lexical sort is newest
  # first. Deterministic and side-effect free so the selection is unit-testable.
  local keep_n="$1"
  hc_is_uint "$keep_n" || return 2
  # Reverse-sort (newest first), drop the newest keep_n, then re-sort ascending so the
  # rotate set is emitted oldest-first.
  sort -r | awk -v keep="$keep_n" 'NR > keep' | sort
}

# -- host readings (impure; guarded) --------------------------------------------

hc_path_usage_pct() {
  local path="$1"
  { df -P "$path" 2> /dev/null || true; } | hc_parse_df_pct
}

hc_mem_available_mb() {
  # MemAvailable from /proc/meminfo in MB, or empty when unavailable (e.g. non-Linux).
  local kb
  kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2> /dev/null || true)"
  if hc_is_uint "$kb"; then
    printf '%s\n' $((kb / 1024))
  fi
}

hc_mem_total_mb() {
  local kb
  kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2> /dev/null || true)"
  if hc_is_uint "$kb"; then
    printf '%s\n' $((kb / 1024))
  fi
}

hc_docker_available() {
  command -v docker > /dev/null 2>&1 && docker info > /dev/null 2>&1
}

hc_iso_to_epoch() {
  # Convert an ISO-8601 UTC timestamp (optionally with a fractional part and a Z) to
  # epoch seconds. Uses GNU date when present, else python3; echoes nothing on failure.
  local iso="$1" epoch=""
  epoch="$(date -u -d "$iso" +%s 2> /dev/null || true)"
  if ! hc_is_uint "$epoch"; then
    epoch="$(
      python3 - "$iso" 2> /dev/null << 'PY' || true
import sys, re
from datetime import datetime, timezone
text = sys.argv[1].strip()
if text.endswith("Z"):
    text = text[:-1] + "+00:00"
text = re.sub(r"\.(\d{6})\d+", r".\1", text)
try:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    print(int(dt.timestamp()))
except Exception:
    pass
PY
    )"
  fi
  hc_is_uint "$epoch" && printf '%s\n' "$epoch"
}

# -- alerting (mirrors the nodeops webhook payload shape) -----------------------

hc_json_escape() {
  # Escape a string for embedding in a JSON double-quoted value.
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\n'/\\n}"
  printf '%s' "$s"
}

hc_build_alert() {
  # hc_build_alert <node> <condition> <severity> <detail>
  # Emit the same {ts_utc,node,condition,severity,detail} shape the nodeops sampler
  # POSTs, plus source/host so a shared webhook can tell host alerts from node ones.
  local node="$1" condition="$2" severity="$3" detail="$4"
  printf '{"ts_utc":"%s","source":"node-host-health","host":"%s","node":"%s","condition":"%s","severity":"%s","detail":"%s"}' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$(hc_json_escape "$(hostname 2> /dev/null || echo unknown)")" \
    "$(hc_json_escape "$node")" \
    "$(hc_json_escape "$condition")" \
    "$(hc_json_escape "$severity")" \
    "$(hc_json_escape "$detail")"
}

hc_post_webhook() {
  # hc_post_webhook <url> <json> — best-effort POST, 5s timeout, never fails the run.
  local url="$1" payload="$2"
  [[ -n "$url" ]] || return 0
  if command -v curl > /dev/null 2>&1; then
    curl -fsS -m 5 -X POST -H 'Content-Type: application/json' \
      -d "$payload" "$url" > /dev/null 2>&1 || true
  elif command -v wget > /dev/null 2>&1; then
    wget -q -T 5 -O /dev/null --header='Content-Type: application/json' \
      --post-data="$payload" "$url" > /dev/null 2>&1 || true
  elif command -v python3 > /dev/null 2>&1; then
    HC_WEBHOOK_URL="$url" HC_WEBHOOK_BODY="$payload" python3 - << 'PY' > /dev/null 2>&1 || true
import os, urllib.request
req = urllib.request.Request(
    os.environ["HC_WEBHOOK_URL"],
    data=os.environ["HC_WEBHOOK_BODY"].encode("utf8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req, timeout=5).read()
except Exception:
    pass
PY
  fi
}

hc_emit_alert() {
  # hc_emit_alert <webhook_url> <node> <condition> <severity> <detail>
  local url="$1" node="$2" condition="$3" severity="$4" detail="$5"
  local payload
  payload="$(hc_build_alert "$node" "$condition" "$severity" "$detail")"
  hc_log "alert" "node=$node condition=$condition severity=$severity detail=$detail"
  hc_post_webhook "$url" "$payload"
}
