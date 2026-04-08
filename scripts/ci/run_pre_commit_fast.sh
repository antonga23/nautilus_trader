#!/usr/bin/env bash
set -euo pipefail

export SKIP="cargo-deny,cargo-vet,check-anyhow-usage,check-logging-macro-usage,check-tokio-usage,check-pyo3-conventions,check-testing-conventions,check-nautilus-conventions,fmt,cargo-clippy"

detect_output="$(mktemp)"
detect_summary="$(mktemp)"
trap 'rm -f "$detect_output" "$detect_summary"' EXIT

GITHUB_OUTPUT="$detect_output" GITHUB_STEP_SUMMARY="$detect_summary" \
  bash scripts/ci/detect_changed_surfaces.sh > /dev/null

changed_files_path="$(awk -F= '/^changed_files_path=/{print $2}' "$detect_output")"

if [[ -z "$changed_files_path" || ! -f "$changed_files_path" ]]; then
  echo "Unable to determine changed files for pre-commit-fast" >&2
  exit 1
fi

changed_files=()
while IFS= read -r changed_file; do
  [[ -n "$changed_file" ]] || continue
  changed_files+=("$changed_file")
done < "$changed_files_path"

if ((${#changed_files[@]} == 0)); then
  echo "No changed files detected; skipping pre-commit-fast"
  exit 0
fi

pre_commit_cmd=(pre-commit)
if ! command -v pre-commit > /dev/null 2>&1; then
  pre_commit_cmd=(uv run pre-commit)
fi

"${pre_commit_cmd[@]}" run --show-diff-on-failure --files "${changed_files[@]}"
