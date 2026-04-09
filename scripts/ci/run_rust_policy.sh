#!/usr/bin/env bash
set -euo pipefail

artifact_dir="${CI_ARTIFACTS_DIR:-artifacts/rust-policy}"
summary_path="${GITHUB_STEP_SUMMARY:-}"
overall_status=0
mkdir -p "$artifact_dir"

write_summary() {
  local label="$1"
  local status="$2"
  local log_file="$3"

  if [[ -n "$summary_path" ]]; then
    {
      echo "### ${label}"
      echo
      echo "- status: ${status}"
      echo "- log: ${log_file}"
      echo
    } >> "$summary_path"
  fi
}

run_and_capture() {
  local label="$1"
  shift
  local log_file="$artifact_dir/${label}.log"
  local status_text="success"

  echo "== ${label} =="
  set +e
  "$@" 2>&1 | tee "$log_file"
  local status=${PIPESTATUS[0]}
  set -e

  if [[ $status -ne 0 ]]; then
    status_text="failure"
    overall_status=1
    echo "::error title=${label} failed::See ${log_file} for details"
  fi

  write_summary "$label" "$status_text" "$log_file"
}

hooks=(
  check-anyhow-usage
  check-logging-macro-usage
  check-tokio-usage
  check-pyo3-conventions
  check-testing-conventions
  check-nautilus-conventions
  fmt
  cargo-clippy
)

for hook in "${hooks[@]}"; do
  run_and_capture "pre-commit-${hook}" pre-commit run --hook-stage manual --all-files "$hook"
done

run_and_capture cargo-vet cargo vet --locked
run_and_capture cargo-deny cargo deny --all-features check advisories licenses sources bans

if [[ "${RUN_CAPNP_SCHEMAS:-false}" == "true" ]] && [[ -f Makefile ]]; then
  run_and_capture capnp-schemas make check-capnp-schemas
fi

exit "$overall_status"
