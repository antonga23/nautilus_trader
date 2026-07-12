#!/usr/bin/env bash
#
# disk-hygiene.sh — committed, scheduled disk-hygiene routine for a trading-node host.
# This replaces the ad-hoc host cron that previously kept the strategy-node disk in
# check. It is the routine counterpart to node-host-health.sh: the health monitor
# reacts to urgent pressure every ~2min, this runs the steady daily cleanup.
#
# Steps (all safe/idempotent, each guarded by tool availability):
#   * journald vacuum (cap size and age)
#   * docker prune: dangling images, stopped containers, build cache older than a bound
#   * strategy-node session-log rotation: keep the newest N session dirs per node
#     uncompressed, gzip node.log/events.jsonl in older ones, delete session dirs past
#     a max age
#   * archive-dir retention under <nodes_root>/archives
#
# Config (systemd EnvironmentFile /etc/cloudbet/node-host-disk-hygiene.conf):
#   NODEOPS_NODES_ROOT              strategy-node root (default /opt/cloudbet/strategy-nodes)
#   DISK_HYGIENE_SESSION_KEEP      sessions kept uncompressed per node (default 10)
#   DISK_HYGIENE_SESSION_MAX_AGE_DAYS  delete rotated session dirs older than (default 30)
#   DISK_HYGIENE_ARCHIVE_RETENTION_DAYS  archive timestamp-dir retention (default 14)
#   DISK_HYGIENE_JOURNAL_MAX_SIZE  journald vacuum size cap (default 500M)
#   DISK_HYGIENE_JOURNAL_MAX_AGE   journald vacuum age cap (default 14d)
#   DISK_HYGIENE_DOCKER_BUILD_CACHE_UNTIL  build-cache prune age (default 168h)
#   DISK_HYGIENE_PRUNE_DOCKER      run docker prune when 1 (default 1)
set -Eeuo pipefail
trap 'hc_log error "disk-hygiene failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/host/host_common.sh
source "$SCRIPT_DIR/host_common.sh"

NODES_ROOT="${NODEOPS_NODES_ROOT:-/opt/cloudbet/strategy-nodes}"
SESSION_KEEP="${DISK_HYGIENE_SESSION_KEEP:-10}"
SESSION_MAX_AGE_DAYS="${DISK_HYGIENE_SESSION_MAX_AGE_DAYS:-30}"
ARCHIVE_RETENTION_DAYS="${DISK_HYGIENE_ARCHIVE_RETENTION_DAYS:-14}"
JOURNAL_MAX_SIZE="${DISK_HYGIENE_JOURNAL_MAX_SIZE:-500M}"
JOURNAL_MAX_AGE="${DISK_HYGIENE_JOURNAL_MAX_AGE:-14d}"
DOCKER_BUILD_CACHE_UNTIL="${DISK_HYGIENE_DOCKER_BUILD_CACHE_UNTIL:-168h}"
PRUNE_DOCKER="${DISK_HYGIENE_PRUNE_DOCKER:-1}"

vacuum_journal() {
  command -v journalctl > /dev/null 2>&1 || return 0
  journalctl --vacuum-size="$JOURNAL_MAX_SIZE" --vacuum-time="$JOURNAL_MAX_AGE" > /dev/null 2>&1 || true
  hc_log info "journald vacuumed (size<=$JOURNAL_MAX_SIZE age<=$JOURNAL_MAX_AGE)"
}

prune_docker() {
  [[ "$PRUNE_DOCKER" == "1" ]] || return 0
  hc_docker_available || return 0
  docker image prune -f > /dev/null 2>&1 || true
  docker container prune -f --filter status=exited > /dev/null 2>&1 || true
  docker builder prune -f --filter "until=$DOCKER_BUILD_CACHE_UNTIL" > /dev/null 2>&1 || true
  hc_log info "docker pruned (dangling images, stopped containers, build cache > $DOCKER_BUILD_CACHE_UNTIL)"
}

rotate_sessions() {
  [[ -d "$NODES_ROOT" ]] || return 0
  local node_dir name sessions_dir stale sdir target
  for node_dir in "$NODES_ROOT"/*/; do
    [[ -d "$node_dir" ]] || continue
    name="$(basename "$node_dir")"
    if [[ "$name" == "archives" ]]; then continue; fi
    sessions_dir="$node_dir/sessions"
    [[ -d "$sessions_dir" ]] || continue
    while IFS= read -r stale; do
      [[ -n "$stale" ]] || continue
      sdir="$sessions_dir/$stale"
      [[ -d "$sdir" ]] || continue
      if find "$sdir" -maxdepth 0 -type d -mtime "+$SESSION_MAX_AGE_DAYS" 2> /dev/null | grep -q .; then
        rm -rf "$sdir" 2> /dev/null || true
        continue
      fi
      for target in "$sdir/node.log" "$sdir/events.jsonl"; do
        if [[ -f "$target" ]]; then gzip -f "$target" 2> /dev/null || true; fi
      done
    done < <(hc_list_subdir_names "$sessions_dir" | hc_sessions_to_rotate "$SESSION_KEEP")
  done
  hc_log info "session logs rotated (keep newest $SESSION_KEEP, delete > ${SESSION_MAX_AGE_DAYS}d)"
}

prune_archives() {
  local archive_root="$NODES_ROOT/archives"
  [[ -d "$archive_root" ]] || return 0
  find "$archive_root" -mindepth 1 -maxdepth 1 -type d -mtime "+$ARCHIVE_RETENTION_DAYS" \
    -exec rm -rf {} + 2> /dev/null || true
  hc_log info "archives pruned (retention ${ARCHIVE_RETENTION_DAYS}d)"
}

main() {
  hc_log info "disk-hygiene start (root / at $(hc_path_usage_pct /)%)"
  vacuum_journal
  prune_docker
  rotate_sessions
  prune_archives
  hc_log info "disk-hygiene done (root / at $(hc_path_usage_pct /)%)"
}

main "$@"
