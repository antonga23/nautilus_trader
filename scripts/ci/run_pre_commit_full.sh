#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat << 'EOF' >&2
Usage: run_pre_commit_full.sh [--exclude-rust]
EOF
  exit 64
}

exclude_rust=0
tracked_count=0
rust_skipped=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --exclude-rust)
      exclude_rust=1
      shift
      ;;
    -h | --help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

skip_hooks="cargo-deny,cargo-vet,check-anyhow-usage,check-logging-macro-usage,check-tokio-usage,check-pyo3-conventions,check-testing-conventions,check-nautilus-conventions,fmt,cargo-clippy"
if [ "$exclude_rust" -eq 1 ]; then
  # This hook ignores the filtered file list (`pass_filenames: false`) and scans
  # repo-wide, which would reintroduce Rust/global debt into the PR-only lane.
  skip_hooks="${skip_hooks},check-copyright-year"
fi
export SKIP="$skip_hooks"

pre_commit_cmd=(pre-commit)
if ! command -v pre-commit > /dev/null 2>&1; then
  pre_commit_cmd=(uv run pre-commit)
fi

tracked_files=()
while IFS= read -r -d '' tracked_file; do
  tracked_count=$((tracked_count + 1))
  if [ "$exclude_rust" -eq 1 ] && [[ "$tracked_file" == *.rs ]]; then
    rust_skipped=$((rust_skipped + 1))
    continue
  fi
  tracked_files+=("$tracked_file")
done < <(git ls-files -z)

if ((${#tracked_files[@]} == 0)); then
  echo "No tracked files matched the requested full pre-commit scope"
  exit 0
fi

echo "tracked_files=$tracked_count"
echo "selected_files=${#tracked_files[@]}"
echo "rust_skipped=$rust_skipped"

"${pre_commit_cmd[@]}" run --show-diff-on-failure --color=always --files "${tracked_files[@]}"
