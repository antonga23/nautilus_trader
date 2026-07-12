#!/usr/bin/env bash
#
# node-host-health.sh — periodic health monitor with auto-remediation for a trading
# node host. Runs as a systemd oneshot on a ~2min timer (see node-host-health.timer)
# and guards against the failure that motivated this toolkit: a root-disk ENOSPC plus
# OOM kills that wedged Docker with no auto-remediation.
#
# Subcommands:
#   check                 Full sweep: disk, memory/OOM, containers (default).
#   preflight --need-mb N Exit non-zero if starting a node needing N MB would breach
#                         the memory floor. A deploy calls this to refuse over-
#                         subscribing the box (the specific OOM anti-recurrence guard).
#   recommend             Print the recommended max node count for this host's RAM.
#   self-test             Run the pure-logic assertions and exit (no host access).
#
# Configuration is env-driven (systemd EnvironmentFile /etc/cloudbet/node-host-health.conf):
#   NODE_HOST_DISK_PCT            soft disk threshold, triggers remediation (default 85)
#   NODE_HOST_DISK_HARD_PCT       hard disk threshold, alerts if still over (default 92)
#   NODE_HOST_MEM_FLOOR_MB        MemAvailable floor / deploy reserve (default 1500)
#   NODE_HOST_PER_NODE_MB         steady-state RAM budget per node (default 3072)
#   NODEOPS_NODES_ROOT            strategy-node root (default /opt/cloudbet/strategy-nodes)
#   NODE_HOST_SESSION_KEEP        session dirs kept uncompressed per node (default 5)
#   NODE_HOST_HEARTBEAT_STALE_SECS  heartbeat age alert threshold (default 180)
#   NODE_HOST_STATUS_STALE_SECS     status.json updatedAt age threshold (default 300)
#   NODE_HOST_NODE_PREFIX        monitored container/dir name prefix
#                                (default betting-arbitrage-node)
#   NODE_HOST_AUTO_RESTART       docker-restart wedged containers (default 1)
#   NODE_HOST_MAX_RESTARTS_PER_HOUR  restart-storm cap per container (default 3)
#   NODE_HOST_STATE_DIR          state/dedupe files (default /var/lib/cloudbet/node-host-health)
#   NODEOPS_ALERT_WEBHOOK        alert webhook (reused from nodeops); --webhook overrides
set -Eeuo pipefail
trap 'hc_log error "node-host-health failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/host/host_common.sh
source "$SCRIPT_DIR/host_common.sh"

DISK_PCT="${NODE_HOST_DISK_PCT:-85}"
DISK_HARD_PCT="${NODE_HOST_DISK_HARD_PCT:-92}"
MEM_FLOOR_MB="${NODE_HOST_MEM_FLOOR_MB:-1500}"
PER_NODE_MB="${NODE_HOST_PER_NODE_MB:-3072}"
NODES_ROOT="${NODEOPS_NODES_ROOT:-/opt/cloudbet/strategy-nodes}"
SESSION_KEEP="${NODE_HOST_SESSION_KEEP:-5}"
HEARTBEAT_STALE_SECS="${NODE_HOST_HEARTBEAT_STALE_SECS:-${NODEOPS_HEARTBEAT_STALE_SECS:-180}}"
STATUS_STALE_SECS="${NODE_HOST_STATUS_STALE_SECS:-300}"
NODE_PREFIX="${NODE_HOST_NODE_PREFIX:-betting-arbitrage-node}"
AUTO_RESTART="${NODE_HOST_AUTO_RESTART:-1}"
MAX_RESTARTS_PER_HOUR="${NODE_HOST_MAX_RESTARTS_PER_HOUR:-3}"
STATE_DIR="${NODE_HOST_STATE_DIR:-/var/lib/cloudbet/node-host-health}"
WEBHOOK="${NODE_HOST_WEBHOOK:-${NODEOPS_ALERT_WEBHOOK:-}}"

usage() {
  sed -n '2,/^set -Eeuo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; s/^#//; $d'
}

# Reuse the webhook the nodeops unit is already configured with, when the operator did
# not pass one explicitly. Best-effort: systemctl may be absent (tests) or nodeops
# unconfigured, in which case WEBHOOK stays empty and alerting is simply inert.
resolve_webhook_from_nodeops() {
  [[ -z "$WEBHOOK" ]] || return 0
  command -v systemctl > /dev/null 2>&1 || return 0
  local env_line
  env_line="$(systemctl show nodeops --property=Environment 2> /dev/null || true)"
  [[ "$env_line" == *NODEOPS_ALERT_WEBHOOK=* ]] || return 0
  local extracted
  extracted="$(printf '%s\n' "$env_line" | tr ' ' '\n' | sed -n 's/^NODEOPS_ALERT_WEBHOOK=//p' | head -n 1)"
  [[ -n "$extracted" ]] && WEBHOOK="$extracted"
}

alert() {
  hc_emit_alert "$WEBHOOK" "$1" "$2" "$3" "$4"
}

# -- disk -----------------------------------------------------------------------

rotate_sessions_all_nodes() {
  # Gzip node.log / events.jsonl in session dirs beyond the newest SESSION_KEEP per
  # node to reclaim disk under pressure. Newest sessions stay uncompressed for tailing.
  local node_dir sessions_dir stale name target
  [[ -d "$NODES_ROOT" ]] || return 0
  for node_dir in "$NODES_ROOT"/*/; do
    [[ -d "$node_dir" ]] || continue
    name="$(basename "$node_dir")"
    [[ "$name" == "archives" ]] && continue
    sessions_dir="$node_dir/sessions"
    [[ -d "$sessions_dir" ]] || continue
    while IFS= read -r stale; do
      [[ -n "$stale" ]] || continue
      for target in "$sessions_dir/$stale/node.log" "$sessions_dir/$stale/events.jsonl"; do
        if [[ -f "$target" ]]; then
          gzip -f "$target" 2> /dev/null || true
        fi
      done
    done < <(hc_list_subdir_names "$sessions_dir" | hc_sessions_to_rotate "$SESSION_KEEP")
  done
}

check_disk() {
  local pct
  pct="$(hc_path_usage_pct /)"
  hc_log info "disk / usage ${pct}% (soft ${DISK_PCT}% hard ${DISK_HARD_PCT}%)"
  [[ "$pct" -ge "$DISK_PCT" ]] || return 0

  hc_log warn "disk over soft threshold; remediating"
  if hc_docker_available; then
    docker image prune -f > /dev/null 2>&1 || true
    docker container prune -f > /dev/null 2>&1 || true
  fi
  rotate_sessions_all_nodes
  pct="$(hc_path_usage_pct /)"
  hc_log info "disk / usage after remediation ${pct}%"

  if [[ "$pct" -ge "$DISK_HARD_PCT" ]]; then
    alert "__host__" "disk_critical" "high" \
      "root disk ${pct}% still over hard threshold ${DISK_HARD_PCT}% after remediation"
  elif [[ "$pct" -ge "$DISK_PCT" ]]; then
    alert "__host__" "disk_pressure" "warning" \
      "root disk ${pct}% still over soft threshold ${DISK_PCT}% after remediation"
  fi
}

# -- memory / OOM ---------------------------------------------------------------

check_memory() {
  local avail
  avail="$(hc_mem_available_mb)"
  if [[ -n "$avail" ]]; then
    hc_log info "MemAvailable ${avail}MB (floor ${MEM_FLOOR_MB}MB)"
    if [[ "$avail" -lt "$MEM_FLOOR_MB" ]]; then
      alert "__host__" "memory_low" "warning" \
        "MemAvailable ${avail}MB below floor ${MEM_FLOOR_MB}MB"
    fi
  fi
  scan_oom
}

scan_oom() {
  # Report a recent kernel OOM kill once. The matched line is hashed into a state file
  # so the same event is not re-alerted on every 2-min run.
  local line=""
  if command -v journalctl > /dev/null 2>&1; then
    line="$(journalctl -k --since "-15min" --no-pager 2> /dev/null |
      grep -iE 'out of memory|oom-kill|killed process' | tail -n 1 || true)"
  fi
  if [[ -z "$line" ]] && command -v dmesg > /dev/null 2>&1; then
    line="$(dmesg 2> /dev/null | grep -iE 'out of memory|oom-kill|killed process' | tail -n 1 || true)"
  fi
  [[ -n "$line" ]] || return 0

  local victim
  victim="$(printf '%s\n' "$line" | grep -oiE 'killed process [0-9]+ \(([^)]+)\)' | tail -n 1 || true)"
  [[ -n "$victim" ]] || victim="$line"

  local sig marker
  sig="$(printf '%s' "$line" | cksum | awk '{print $1}')"
  marker="$STATE_DIR/last-oom-sig"
  mkdir -p "$STATE_DIR" 2> /dev/null || true
  if [[ -f "$marker" ]] && [[ "$(cat "$marker" 2> /dev/null || true)" == "$sig" ]]; then
    return 0
  fi
  printf '%s\n' "$sig" > "$marker" 2> /dev/null || true
  alert "__host__" "oom_kill" "high" "recent kernel OOM kill: ${victim}"
}

# -- containers -----------------------------------------------------------------

restart_count_last_hour() {
  local logfile="$1" now cutoff count=0 ts
  now="$(date -u +%s)"
  cutoff=$((now - 3600))
  [[ -f "$logfile" ]] || {
    printf '0\n'
    return 0
  }
  while IFS= read -r ts; do
    hc_is_uint "$ts" || continue
    [[ "$ts" -ge "$cutoff" ]] && count=$((count + 1))
  done < "$logfile"
  printf '%s\n' "$count"
}

maybe_restart() {
  local node="$1" reason="$2"
  [[ "$AUTO_RESTART" == "1" ]] || return 0
  hc_docker_available || return 0
  local logfile="$STATE_DIR/restarts/$node.log" count
  mkdir -p "$STATE_DIR/restarts" 2> /dev/null || true
  count="$(restart_count_last_hour "$logfile")"
  if ! hc_restart_allowed "$count" "$MAX_RESTARTS_PER_HOUR"; then
    alert "$node" "restart_storm" "high" \
      "wedged ($reason) but restart-storm cap ${MAX_RESTARTS_PER_HOUR}/h reached; not restarting"
    return 0
  fi
  hc_log warn "restarting $node ($reason); restarts in last hour=$count"
  if docker restart "$node" > /dev/null 2>&1; then
    date -u +%s >> "$logfile" 2> /dev/null || true
    alert "$node" "container_restarted" "warning" "auto-restarted after: $reason"
  else
    alert "$node" "restart_failed" "high" "docker restart failed for $reason"
  fi
}

json_field_age_secs() {
  # Echo the age in seconds of a top-level string timestamp field in a JSON file,
  # or nothing when the file/field is missing or unparseable.
  local file="$1" field="$2" value now epoch
  [[ -f "$file" ]] || return 0
  value="$(sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$file" | head -n 1)"
  [[ -n "$value" ]] || return 0
  epoch="$(hc_iso_to_epoch "$value")"
  [[ -n "$epoch" ]] || return 0
  now="$(date -u +%s)"
  printf '%s\n' $((now - epoch))
}

check_containers() {
  [[ -d "$NODES_ROOT" ]] || {
    hc_log info "nodes root $NODES_ROOT absent; no containers to check"
    return 0
  }
  local node_dir node state hb_age st_age wedged
  for node_dir in "$NODES_ROOT"/*/; do
    [[ -d "$node_dir" ]] || continue
    node="$(basename "$node_dir")"
    [[ "$node" == "archives" ]] && continue
    [[ "$node" == "$NODE_PREFIX"* ]] || continue

    wedged=""
    state="missing"
    if hc_docker_available; then
      state="$(docker inspect --format '{{.State.Status}}' "$node" 2> /dev/null || true)"
      state="$(printf '%s' "$state" | tr -d '[:space:]')"
      [[ -n "$state" ]] || state="missing"
    fi
    if [[ "$state" != "running" ]]; then
      alert "$node" "container_not_running" "warning" "container state=${state}"
      wedged="state=${state}"
    fi

    hb_age="$(json_field_age_secs "$node_dir/heartbeat.json" at)"
    if [[ -n "$hb_age" ]] && [[ "$hb_age" -gt "$HEARTBEAT_STALE_SECS" ]]; then
      alert "$node" "heartbeat_stale" "warning" \
        "heartbeat ${hb_age}s old (> ${HEARTBEAT_STALE_SECS}s)"
      [[ "$state" == "running" ]] && wedged="heartbeat_stale=${hb_age}s"
    fi

    st_age="$(json_field_age_secs "$node_dir/status.json" updatedAt)"
    if [[ -n "$st_age" ]] && [[ "$st_age" -gt "$STATUS_STALE_SECS" ]]; then
      alert "$node" "status_stale" "warning" \
        "status.json updatedAt ${st_age}s old (> ${STATUS_STALE_SECS}s)"
      [[ "$state" == "running" ]] && wedged="status_stale=${st_age}s"
    fi

    [[ -n "$wedged" ]] && maybe_restart "$node" "$wedged"
  done
}

# -- subcommands ----------------------------------------------------------------

cmd_check() {
  resolve_webhook_from_nodeops
  hc_log info "node-host-health check start (webhook $([[ -n "$WEBHOOK" ]] && echo configured || echo unset))"
  check_disk
  check_memory
  check_containers
  hc_log info "node-host-health check done"
}

cmd_preflight() {
  local need_mb=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --need-mb)
        need_mb="$2"
        shift 2
        ;;
      *)
        hc_log error "preflight: unknown arg $1"
        return 2
        ;;
    esac
  done
  hc_is_uint "$need_mb" || {
    hc_log error "preflight requires --need-mb <positive integer>"
    return 2
  }
  local avail
  avail="$(hc_mem_available_mb)"
  if [[ -z "$avail" ]]; then
    hc_log error "preflight: MemAvailable unreadable (not Linux?); refusing to assert"
    return 2
  fi
  if hc_mem_preflight_ok "$avail" "$need_mb" "$MEM_FLOOR_MB"; then
    hc_log info "preflight OK: MemAvailable ${avail}MB, need ${need_mb}MB, floor ${MEM_FLOOR_MB}MB"
    return 0
  fi
  hc_log error "preflight REFUSE: MemAvailable ${avail}MB - need ${need_mb}MB would breach floor ${MEM_FLOOR_MB}MB"
  return 1
}

cmd_recommend() {
  local total rec
  total="$(hc_mem_total_mb)"
  if [[ -z "$total" ]]; then
    hc_log error "recommend: MemTotal unreadable (not Linux?)"
    return 2
  fi
  rec="$(hc_recommend_max_nodes "$total" "$PER_NODE_MB" "$MEM_FLOOR_MB")"
  hc_log info "MemTotal ${total}MB, per-node ${PER_NODE_MB}MB, reserve ${MEM_FLOOR_MB}MB"
  printf 'recommended_max_nodes=%s\n' "$rec"
}

cmd_self_test() {
  local failures=0
  assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
      printf 'ok   %s\n' "$label"
    else
      printf 'FAIL %s (expected=%s actual=%s)\n' "$label" "$expected" "$actual"
      failures=$((failures + 1))
    fi
  }
  assert_rc() {
    local label="$1" expected="$2"
    shift 2
    local rc=0
    "$@" > /dev/null 2>&1 || rc=$?
    assert_eq "$label" "$expected" "$rc"
  }

  # disk-pct parse
  assert_eq "disk_pct/basic" 84 "$(printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/root 100 84 16 84%% /\n' | hc_parse_df_pct)"
  assert_eq "disk_pct/garbage" 0 "$(printf 'header\nno percent here\n' | hc_parse_df_pct)"

  # mem-preflight math
  assert_rc "mem_preflight/refuse_exact" 1 hc_mem_preflight_ok 3000 3000 1500
  assert_rc "mem_preflight/ok" 0 hc_mem_preflight_ok 6000 3000 1500
  assert_rc "mem_preflight/refuse_over" 1 hc_mem_preflight_ok 2000 3000 1500
  assert_rc "mem_preflight/boundary_ok" 0 hc_mem_preflight_ok 4500 3000 1500

  # recommend max nodes
  assert_eq "recommend/16g" 4 "$(hc_recommend_max_nodes 16000 3072 1500)"
  assert_eq "recommend/4g" 0 "$(hc_recommend_max_nodes 4000 3072 1500)"
  assert_eq "recommend/tiny" 0 "$(hc_recommend_max_nodes 1000 3072 1500)"

  # restart-storm guard
  assert_rc "restart/under_cap" 0 hc_restart_allowed 2 3
  assert_rc "restart/at_cap" 1 hc_restart_allowed 3 3
  assert_rc "restart/over_cap" 1 hc_restart_allowed 5 3

  # session-log rotation selection (keep newest 2 of 4)
  assert_eq "sessions/rotate_older" "$(printf '20260101T000000Z-1\n20260102T000000Z-2\n')" \
    "$(printf '20260104T000000Z-4\n20260101T000000Z-1\n20260103T000000Z-3\n20260102T000000Z-2\n' | hc_sessions_to_rotate 2)"
  assert_eq "sessions/keep_all_when_few" "" \
    "$(printf 'a\nb\n' | hc_sessions_to_rotate 5)"

  printf '\nself-test: %s failure(s)\n' "$failures"
  [[ "$failures" -eq 0 ]]
}

main() {
  local cmd="${1:-check}"
  case "$cmd" in
    check)
      shift || true
      cmd_check "$@"
      ;;
    preflight)
      shift
      cmd_preflight "$@"
      ;;
    recommend)
      shift || true
      cmd_recommend
      ;;
    self-test | --self-test)
      cmd_self_test
      ;;
    -h | --help | help)
      usage
      ;;
    *)
      hc_log error "unknown subcommand: $cmd"
      usage
      exit 2
      ;;
  esac
}

main "$@"
