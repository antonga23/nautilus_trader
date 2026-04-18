#!/usr/bin/env bash
set -euo pipefail

origin_remote="${ORIGIN_REMOTE:-origin}"
upstream_remote="${UPSTREAM_REMOTE:-upstream-sync}"
upstream_url="${UPSTREAM_URL:-https://github.com/nautechsystems/nautilus_trader.git}"
mirror_develop_branch="${MIRROR_DEVELOP_BRANCH:-nautilus_develop}"
mirror_master_branch="${MIRROR_MASTER_BRANCH:-nautilus_master}"
summary_path="${GITHUB_STEP_SUMMARY:-}"
output_path="${GITHUB_OUTPUT:-}"

git remote remove "$upstream_remote" > /dev/null 2>&1 || true
git remote add "$upstream_remote" "$upstream_url"

git fetch "$origin_remote" --prune
git fetch "$upstream_remote" develop master --prune

develop_sha="$(git rev-parse "$upstream_remote/develop")"
master_sha="$(git rev-parse "$upstream_remote/master")"

git update-ref "refs/heads/$mirror_develop_branch" "$develop_sha"
git update-ref "refs/heads/$mirror_master_branch" "$master_sha"

git push --force-with-lease "$origin_remote" \
  "$develop_sha:refs/heads/$mirror_develop_branch"
git push --force-with-lease "$origin_remote" \
  "$master_sha:refs/heads/$mirror_master_branch"

if [[ -n "$summary_path" ]]; then
  {
    echo "## Upstream Mirror Sync"
    echo
    echo "- ${mirror_develop_branch}: ${develop_sha}"
    echo "- ${mirror_master_branch}: ${master_sha}"
  } >> "$summary_path"
fi

if [[ -n "$output_path" ]]; then
  {
    echo "develop_sha=$develop_sha"
    echo "master_sha=$master_sha"
  } >> "$output_path"
fi
