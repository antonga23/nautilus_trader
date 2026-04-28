#!/usr/bin/env bash
set -euo pipefail

null_sha='0000000000000000000000000000000000000000'
base_ref="${GITHUB_BASE_REF:-}"
base_sha="${GITHUB_BASE_SHA:-}"
event_name="${GITHUB_EVENT_NAME:-}"
before_sha="${GITHUB_EVENT_BEFORE:-}"
current_sha="${GITHUB_SHA:-HEAD}"
output_path="${GITHUB_OUTPUT:-}"
summary_path="${GITHUB_STEP_SUMMARY:-}"
changes_file="${RUNNER_TEMP:-/tmp}/changed-files.txt"
mkdir -p "$(dirname "$changes_file")"

if [[ "$event_name" == "pull_request" ]]; then
  if [[ -z "$base_ref" ]]; then
    echo "GITHUB_BASE_REF is required for pull_request events" >&2
    exit 1
  fi
  if git rev-parse --verify --quiet "refs/remotes/origin/$base_ref" > /dev/null; then
    diff_target="refs/remotes/origin/$base_ref...HEAD"
  elif [[ -n "$base_sha" ]]; then
    diff_target="$base_sha...HEAD"
  else
    echo "Unable to determine pull request base commit for $base_ref" >&2
    exit 1
  fi
elif [[ -n "$before_sha" && "$before_sha" != "$null_sha" ]]; then
  diff_target="$before_sha...$current_sha"
elif git rev-parse --verify --quiet "${current_sha}^1" > /dev/null; then
  # workflow_dispatch and similar manual runs do not provide a base SHA.
  # Fall back to the current commit against its first parent instead of linting
  # the entire repository.
  diff_target="${current_sha}^1...$current_sha"
else
  diff_target=""
fi

if [[ -n "$diff_target" ]]; then
  git diff --name-only "$diff_target" | sed '/^$/d' > "$changes_file"
else
  git ls-files > "$changes_file"
fi

rust_pattern='^(crates/|Cargo\.toml$|Cargo\.lock$|deny\.toml$|supply-chain/|rust-toolchain\.toml$|capnp-version$|schema/|\.pre-commit-config\.yaml$|\.pre-commit-hooks/|\.github/workflows/(pr-validation|rust-policy|release-publish)\.yml$|\.github/actions/(common-setup|cargo-tool-install)/action\.yml$|scripts/ci/(detect_changed_surfaces|install-rust|run_pre_commit_fast|run_rust_policy)\.sh$)'
python_pattern='^(nautilus_trader/|tests/|crates/|Cargo\.toml$|Cargo\.lock$|pyproject\.toml$|uv\.lock$|build\.py$|schema/|capnp-version$|rust-toolchain\.toml$|\.github/workflows/(pr-validation|develop-branch-guard)\.yml$|\.github/actions/common-setup/action\.yml$|scripts/ci/(detect_changed_surfaces|run_pytest_with_reporting|run_python_test_suites|run_installed_wheel_smoke_test)\.sh$|scripts/ci/enforce_develop_push_policy\.py$|scripts/test(-coverage|-performance)?\.sh$)'
full_suite_pattern='^(nautilus_trader/|tests/|crates/|Cargo\.toml$|Cargo\.lock$|pyproject\.toml$|uv\.lock$|build\.py$|schema/|capnp-version$|rust-toolchain\.toml$)'
package_builds_pattern='^(nautilus_trader/|crates/|Cargo\.toml$|Cargo\.lock$|pyproject\.toml$|build\.py$|schema/|capnp-version$|rust-toolchain\.toml$)'

if grep -Eq "$rust_pattern" "$changes_file"; then
  rust_policy=true
else
  rust_policy=false
fi

if grep -Eq "$python_pattern" "$changes_file"; then
  python_tests=true
else
  python_tests=false
fi

if grep -Eq "$full_suite_pattern" "$changes_file"; then
  full_suite=true
else
  full_suite=false
fi

if grep -Eq "$package_builds_pattern" "$changes_file"; then
  package_builds=true
else
  package_builds=false
fi

if [[ -n "$output_path" ]]; then
  {
    echo "changed_files_path=$changes_file"
    echo "rust_policy=$rust_policy"
    echo "python_tests=$python_tests"
    echo "full_suite=$full_suite"
    echo "package_builds=$package_builds"
  } >> "$output_path"
fi

printf 'Changed files (%s):\n' "$(wc -l < "$changes_file" | tr -d ' ')"
cat "$changes_file"

if [[ -n "$summary_path" ]]; then
  {
    echo "## Changed Surfaces"
    echo
    echo "- rust_policy: $rust_policy"
    echo "- python_tests: $python_tests"
    echo "- full_suite: $full_suite"
    echo "- package_builds: $package_builds"
    echo
    echo "### Files"
    echo '```text'
    cat "$changes_file"
    echo '```'
  } >> "$summary_path"
fi
