#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'EOF'
Usage: cleanup_local_betting_workspace.sh [options]

Safely prune stale local artifacts and oversized monitor logs for the current
cloudbet-market-maker repo worktree set without touching active git worktrees.

Options:
  --retention-hours N   Delete artifact/log files older than N hours (default: 48)
  --max-log-mb N        Truncate monitor/log files above N MB to their newest tail (default: 1024)
  --docker              Also prune stopped containers, dangling images, and builder cache older than retention
  --prune-stale-dirs    Remove stale repo directories under sibling worktree roots when they are not active git worktrees
  --dry-run             Print actions without mutating
  -h, --help            Show this help
EOF
}

retention_hours=48
max_log_mb=1024
do_docker=false
prune_stale_dirs=false
dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --retention-hours)
      retention_hours="$2"
      shift 2
      ;;
    --max-log-mb)
      max_log_mb="$2"
      shift 2
      ;;
    --docker)
      do_docker=true
      shift
      ;;
    --prune-stale-dirs)
      prune_stale_dirs=true
      shift
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel)"
repo_name="$(basename "$repo_root")"
retention_minutes="$((retention_hours * 60))"

run_cmd() {
  local command="$*"
  if [[ "$dry_run" == "true" ]]; then
    printf '[dry-run] %s\n' "$command"
    return 0
  fi
  bash -c "$command"
}

cap_file_tail() {
  local file="$1"
  local max_mb="$2"
  local tmp_file

  [[ -f "$file" ]] || return 0
  tmp_file="$(mktemp)"
  if [[ "$dry_run" == "true" ]]; then
    printf '[dry-run] truncate tail %s to %sMB\n' "$file" "$max_mb"
    rm -f "$tmp_file"
    return 0
  fi
  if tail -c "${max_mb}M" "$file" > "$tmp_file" 2> /dev/null; then
    cat "$tmp_file" > "$file" 2> /dev/null || true
  fi
  rm -f "$tmp_file" 2> /dev/null || true
}

collect_active_worktrees() {
  git worktree list --porcelain | awk '/^worktree / {print substr($0, 10)}'
}

cleanup_repo_path() {
  local root="$1"
  [[ -d "$root" ]] || return 0

  local artifacts_dir="$root/artifacts"
  if [[ -d "$artifacts_dir" ]]; then
    run_cmd "find \"$artifacts_dir\" -type f -mmin +$retention_minutes -delete 2>/dev/null || true"
    run_cmd "find \"$artifacts_dir\" -type d -empty -delete 2>/dev/null || true"
  fi

  while IFS= read -r log_file; do
    [[ -n "$log_file" ]] || continue
    cap_file_tail "$log_file" "$max_log_mb"
  done < <(
    find "$root" -type f \
      \( -name '*.log' -o -path '*/artifacts/monitors/*' \) \
      -size +"${max_log_mb}"M 2> /dev/null
  )

  run_cmd "find \"$root\" -type f \\( -name '*.log' -o -path '*/artifacts/monitors/*' \\) -mmin +$retention_minutes -delete 2>/dev/null || true"
}

prune_stale_repo_dirs() {
  local root_parent="$1"
  local active_list_file="$2"
  [[ -d "$root_parent" ]] || return 0

  find "$root_parent" -mindepth 1 -maxdepth 2 -type d -name "$repo_name" | while read -r candidate; do
    if grep -Fxq "$candidate" "$active_list_file"; then
      continue
    fi
    if [[ "$dry_run" == "true" ]]; then
      printf '[dry-run] remove stale repo dir %s\n' "$candidate"
    else
      rm -rf "$candidate"
    fi
  done
}

main() {
  local active_worktrees_file
  active_worktrees_file="$(mktemp)"
  collect_active_worktrees > "$active_worktrees_file"

  while IFS= read -r worktree; do
    [[ -n "$worktree" ]] || continue
    cleanup_repo_path "$worktree"
  done < "$active_worktrees_file"

  if [[ "$prune_stale_dirs" == "true" ]]; then
    prune_stale_repo_dirs "$(dirname "$repo_root")" "$active_worktrees_file"
    prune_stale_repo_dirs "$HOME/.codex/worktrees" "$active_worktrees_file"
  fi

  if [[ "$do_docker" == "true" ]] && command -v docker > /dev/null 2>&1; then
    run_cmd "docker container prune -f >/dev/null 2>&1 || true"
    run_cmd "docker image prune -f >/dev/null 2>&1 || true"
    run_cmd "docker builder prune -f --filter \"until=${retention_hours}h\" >/dev/null 2>&1 || true"
  fi

  rm -f "$active_worktrees_file"
}

main
