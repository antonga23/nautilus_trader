#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
worktree_root="${CODEX_WORKTREE_ROOT:-$HOME/.codex/worktrees}"
personal_repo_root="${PERSONAL_REPO_ROOT:-$HOME/personal/cloudbet-market-maker}"
base_branch="${BASE_BRANCH:-origin/develop}"
monitor_retention_hours="${MONITOR_RETENTION_HOURS:-24}"
monitor_total_max_mb="${MONITOR_TOTAL_MAX_MB:-1024}"
monitor_file_max_mb="${MONITOR_FILE_MAX_MB:-256}"
monitor_active_grace_minutes="${MONITOR_ACTIVE_GRACE_MINUTES:-30}"
cache_retention_days="${CACHE_RETENTION_DAYS:-3}"
worktree_retention_hours="${WORKTREE_RETENTION_HOURS:-48}"
apply=false
stat_flavor=""

usage() {
  cat << 'EOF'
Usage: scripts/dev/local_storage_hygiene.sh [--apply]

Rotates old monitor logs, caps monitor directories, removes detached or merged
Codex worktrees that are clean except for local artifacts, and deletes stale
regenerable caches such as target/, .mypy_cache/, .pytest_cache/, and
.ruff_cache/ under ~/.codex/worktrees.

Environment overrides:
  CODEX_WORKTREE_ROOT
  PERSONAL_REPO_ROOT
  BASE_BRANCH
  MONITOR_RETENTION_HOURS
  MONITOR_TOTAL_MAX_MB
  MONITOR_FILE_MAX_MB
  MONITOR_ACTIVE_GRACE_MINUTES
  CACHE_RETENTION_DAYS
  WORKTREE_RETENTION_HOURS
EOF
}

log() {
  printf '%s\n' "$*"
}

now_epoch() {
  date +%s
}

detect_stat_flavor() {
  if stat -f '%m' / > /dev/null 2>&1; then
    printf '%s\n' "bsd"
  elif stat -c '%Y' / > /dev/null 2>&1; then
    printf '%s\n' "gnu"
  else
    printf 'Unable to determine stat flavor\n' >&2
    exit 1
  fi
}

file_mtime() {
  local path="$1"
  [[ -e "$path" ]] || {
    printf '0\n'
    return
  }

  case "$stat_flavor" in
    bsd)
      stat -f '%m' "$path"
      ;;
    gnu)
      stat -c '%Y' "$path"
      ;;
  esac
}

file_size_bytes() {
  local path="$1"
  [[ -e "$path" ]] || {
    printf '0\n'
    return
  }

  case "$stat_flavor" in
    bsd)
      stat -f '%z' "$path"
      ;;
    gnu)
      stat -c '%s' "$path"
      ;;
  esac
}

remove_path_if_stale() {
  local path="$1"
  local reason="$2"
  [[ -e "$path" ]] || return
  remove_path "$path" "$reason"
}

mark_branch_cleanup_candidate() {
  local branch="$1"
  if branch_is_cleanup_candidate "$branch" && branch_is_merged "$branch"; then
    return 0
  fi
  return 1
}

codex_worktree_owner() {
  local path="$1"
  local relative first remainder second candidate

  [[ "$path" == "$worktree_root/"* ]] || return 1
  relative="${path#"$worktree_root"/}"
  first="${relative%%/*}"
  candidate="$worktree_root/$first"
  if git -C "$candidate" rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    printf '%s\n' "$candidate"
    return 0
  fi

  remainder="${relative#"$first"/}"
  second="${remainder%%/*}"
  candidate="$worktree_root/$first/$second"
  if git -C "$candidate" rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    printf '%s\n' "$candidate"
    return 0
  fi

  return 1
}

dir_size_mb() {
  local path="$1"
  du -sm "$path" 2> /dev/null | awk '{print $1}'
}

remove_path() {
  local path="$1"
  local reason="$2"
  log "remove $reason $path"
  if [[ "$apply" == true ]]; then
    /bin/rm -rf "$path"
  fi
}

is_monitor_file() {
  case "$1" in
    *.log | *.jsonl | *.out | *.txt) return 0 ;;
    *) return 1 ;;
  esac
}

list_monitor_files() {
  local dir="$1"
  find "$dir" -type f -print0 2> /dev/null
}

cleanup_monitor_dir() {
  local dir="$1"
  local now age_seconds age_minutes age_hours size_bytes size_mb oldest oldest_mtime file
  now="$(now_epoch)"
  log "scan monitor_dir $dir"

  while IFS= read -r -d '' file; do
    is_monitor_file "$file" || continue
    age_seconds=$((now - $(file_mtime "$file")))
    age_minutes=$((age_seconds / 60))
    age_hours=$((age_seconds / 3600))
    size_bytes="$(file_size_bytes "$file")"
    size_mb=$(((size_bytes + 1048575) / 1048576))
    if ((age_hours >= monitor_retention_hours)); then
      remove_path "$file" "stale_monitor_file"
      continue
    fi
    if ((age_minutes >= monitor_active_grace_minutes && size_mb > monitor_file_max_mb)); then
      remove_path "$file" "oversized_monitor_file"
    fi
  done < <(list_monitor_files "$dir")

  while :; do
    local current_mb
    current_mb="$(dir_size_mb "$dir")"
    [[ -n "$current_mb" ]] || break
    if ((current_mb <= monitor_total_max_mb)); then
      break
    fi

    oldest=""
    oldest_mtime=0
    while IFS= read -r -d '' file; do
      is_monitor_file "$file" || continue
      age_seconds=$((now - $(file_mtime "$file")))
      age_minutes=$((age_seconds / 60))
      if ((age_minutes < monitor_active_grace_minutes)); then
        continue
      fi
      if [[ -z "$oldest" || $(file_mtime "$file") -lt "$oldest_mtime" ]]; then
        oldest="$file"
        oldest_mtime="$(file_mtime "$file")"
      fi
    done < <(list_monitor_files "$dir")

    [[ -n "$oldest" ]] || break
    remove_path "$oldest" "monitor_dir_cap"
  done

  return 0
}

status_is_artifact_only() {
  local worktree="$1"
  local status line path
  status="$(git -C "$worktree" status --porcelain=v1 --untracked-files=all 2> /dev/null || true)"
  [[ -z "$status" ]] && return 0

  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    path="${line:3}"
    case "$path" in
      artifacts | artifacts/* | .mypy_cache | .mypy_cache/* | .pytest_cache | .pytest_cache/* | .ruff_cache | .ruff_cache/* | target | target/* | build | build/*) ;;
      *)
        return 1
        ;;
    esac
  done <<< "$status"

  return 0
}

has_recent_worktree_activity() {
  local worktree="$1"
  local retention_minutes=$((worktree_retention_hours * 60))

  find "$worktree" \
    \( -path '*/.git' -o -path '*/.venv' -o -path '*/node_modules' -o -path '*/target' \) -prune -o \
    -type f -mmin "-$retention_minutes" -print -quit 2> /dev/null | grep -q .
}

branch_is_merged() {
  local branch="$1"
  git -C "$repo_root" show-ref --verify --quiet "refs/heads/$branch" || return 1
  git -C "$repo_root" merge-base --is-ancestor "$branch" "$base_branch"
}

branch_is_cleanup_candidate() {
  case "$1" in
    codex/* | experiment/* | fix/* | cloudbet | control-plane-ux-followon)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

cleanup_worktrees() {
  local worktree="" branch="" line reason
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ -z "$line" ]]; then
      if [[ -n "$worktree" && -d "$worktree" && "$worktree" == "$worktree_root"* ]]; then
        if [[ "$worktree" == "$repo_root" ]]; then
          :
        elif has_recent_worktree_activity "$worktree"; then
          log "skip recent_worktree $worktree"
        elif ! status_is_artifact_only "$worktree"; then
          log "skip dirty_worktree $worktree"
        else
          reason=""
          if [[ -z "$branch" ]]; then
            reason="detached_worktree"
          elif mark_branch_cleanup_candidate "$branch"; then
            reason="merged_worktree"
          fi

          if [[ -n "$reason" ]]; then
            log "remove $reason $worktree"
            if [[ "$apply" == true ]]; then
              git -C "$repo_root" worktree remove --force "$worktree"
            fi
          fi
        fi
      fi
      worktree=""
      branch=""
      continue
    fi

    case "$line" in
      worktree\ *)
        worktree="${line#worktree }"
        ;;
      branch\ refs/heads/*)
        branch="${line#branch refs/heads/}"
        ;;
    esac
  done < <(git -C "$repo_root" worktree list --porcelain)

  if [[ "$apply" == true ]]; then
    git -C "$repo_root" worktree prune
  fi

  return 0
}

cleanup_regenerable_caches() {
  local dir now age_days owner_worktree
  now="$(now_epoch)"
  while IFS= read -r -d '' dir; do
    if [[ "$dir" == "$repo_root"* ]]; then
      continue
    fi
    if owner_worktree="$(codex_worktree_owner "$dir" 2> /dev/null)"; then
      if [[ "$owner_worktree" != "$repo_root" ]]; then
        continue
      fi
    fi
    age_days=$(((now - $(file_mtime "$dir")) / 86400))
    if ((age_days >= cache_retention_days)); then
      remove_path_if_stale "$dir" "stale_cache_dir"
    fi
  done < <(
    find "$worktree_root" "$personal_repo_root" -type d \
      \( -name build -o -name target -o -name .mypy_cache -o -name .pytest_cache -o -name .ruff_cache \) \
      -print0 2> /dev/null
  )

  return 0
}

while (($# > 0)); do
  case "$1" in
    --apply)
      apply=true
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

log "apply=$apply"
log "repo_root=$repo_root"
log "worktree_root=$worktree_root"
log "personal_repo_root=$personal_repo_root"
stat_flavor="$(detect_stat_flavor)"
log "stat_flavor=$stat_flavor"

if [[ -d "$personal_repo_root/artifacts/monitors" ]]; then
  cleanup_monitor_dir "$personal_repo_root/artifacts/monitors"
fi

while IFS= read -r -d '' dir; do
  cleanup_monitor_dir "$dir"
done < <(find "$worktree_root" -type d -path '*/artifacts/monitors' -print0 2> /dev/null) || true

cleanup_worktrees
cleanup_regenerable_caches

log "done"
