#!/usr/bin/env bash
set -euo pipefail

PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

usage() {
  cat <<'EOF' >&2
Usage: wait_for_github_run_condition.sh --repo <owner/repo> --run-id <id> [options]

Options:
  --condition <value>      terminal | first-failure | first-failure-or-terminal
  --sleep <seconds>        Poll interval inside the watcher process (default: 30)
  --timeout-seconds <n>    Exit 124 if the watcher exceeds this duration
  --no-log-failed          Do not print failed job logs on early failure
EOF
  exit 64
}

repo=""
run_id=""
condition="first-failure-or-terminal"
sleep_seconds=30
timeout_seconds=0
log_failed=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      repo="${2:-}"
      shift 2
      ;;
    --run-id)
      run_id="${2:-}"
      shift 2
      ;;
    --condition)
      condition="${2:-}"
      shift 2
      ;;
    --sleep)
      sleep_seconds="${2:-}"
      shift 2
      ;;
    --timeout-seconds)
      timeout_seconds="${2:-}"
      shift 2
      ;;
    --no-log-failed)
      log_failed=0
      shift
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$repo" ] || usage
[ -n "$run_id" ] || usage

case "$condition" in
  terminal|first-failure|first-failure-or-terminal) ;;
  *)
    echo "Unsupported condition: $condition" >&2
    exit 64
    ;;
esac

command -v gh >/dev/null 2>&1 || {
  echo "gh is required" >&2
  exit 69
}

command -v jq >/dev/null 2>&1 || {
  echo "jq is required" >&2
  exit 69
}

started_at_epoch="$(date +%s)"
last_signature=""

is_bad_conclusion() {
  case "$1" in
    failure|cancelled|timed_out|action_required|startup_failure)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

fetch_json() {
  gh run view -R "$repo" "$run_id" --json \
    databaseId,status,conclusion,url,workflowName,displayTitle,headSha,createdAt,jobs
}

print_summary() {
  local json="$1"
  jq -r '
    [
      "run_id=\(.databaseId)",
      "workflow=\(.workflowName // "unknown")",
      "title=\(.displayTitle // "unknown")",
      "status=\(.status // "unknown")",
      "conclusion=\(.conclusion // "null")",
      "head_sha=\(.headSha // "unknown")",
      "url=\(.url // "unknown")"
    ] | .[]
  ' <<<"$json"
}

emit_failed_job_logs() {
  local job_id="$1"
  [ "$log_failed" -eq 1 ] || return 0
  gh run view -R "$repo" "$run_id" --job "$job_id" --log-failed || true
}

while true; do
  if [ "$timeout_seconds" -gt 0 ]; then
    now_epoch="$(date +%s)"
    if [ $(( now_epoch - started_at_epoch )) -ge "$timeout_seconds" ]; then
      echo "Timed out waiting for run $run_id after ${timeout_seconds}s" >&2
      exit 124
    fi
  fi

  json="$(fetch_json)"
  signature="$(
    jq -c '{status,conclusion,jobs:[.jobs[]? | {name,status,conclusion,startedAt,completedAt}]}' <<< "$json"
  )"

  if [ "$signature" != "$last_signature" ]; then
    print_summary "$json"
    jq -r '.jobs[]? | "- " + (.name // "unknown") + ": status=" + (.status // "unknown") + ", conclusion=" + (.conclusion // "null")' <<<"$json"
    last_signature="$signature"
  fi

  failed_job="$(jq -c '
    [.jobs[]? | select(.status == "completed" and (.conclusion != null)) |
      select(.conclusion == "failure" or .conclusion == "cancelled" or .conclusion == "timed_out" or .conclusion == "action_required" or .conclusion == "startup_failure")
    ] | first
  ' <<<"$json")"

  if [ "$failed_job" != "null" ] && [ "$failed_job" != "" ]; then
    if [ "$condition" = "first-failure" ] || [ "$condition" = "first-failure-or-terminal" ]; then
      job_id="$(jq -r '.databaseId // empty' <<<"$failed_job")"
      job_name="$(jq -r '.name // "unknown"' <<<"$failed_job")"
      job_conclusion="$(jq -r '.conclusion // "unknown"' <<<"$failed_job")"
      echo "Detected failing job: $job_name ($job_conclusion)" >&2
      if [ -n "$job_id" ]; then
        emit_failed_job_logs "$job_id"
      fi
      exit 1
    fi
  fi

  run_status="$(jq -r '.status // "unknown"' <<<"$json")"
  run_conclusion="$(jq -r '.conclusion // empty' <<<"$json")"
  if [ "$run_status" = "completed" ]; then
    case "$condition" in
      terminal|first-failure-or-terminal)
        if [ "$run_conclusion" = "success" ]; then
          exit 0
        fi
        echo "Run completed with conclusion=$run_conclusion" >&2
        exit 1
        ;;
      first-failure)
        exit 0
        ;;
    esac
  fi

  sleep "$sleep_seconds"
done
