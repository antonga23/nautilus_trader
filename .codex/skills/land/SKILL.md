---
name: land
description:
  Land a PR by resolving drift against its current base branch, watching CI with
  a background monitor, and merge-committing only when checks and review
  feedback are fully clear.
---

# Land

## Goals

- Keep the PR conflict-free with its current base branch.
- Ensure CI and review feedback are fully resolved.
- Merge with a normal merge commit, without auto-merge or branch deletion.
- Preserve the runner split: PR/develop validation and image builds run on GCP;
  EC2 is only for post-merge strategy-node deployment/runtime verification.

## Preconditions

- `gh` is authenticated.
- The working tree is clean.
- The current branch already has an open PR.

## Steps

1. Identify the PR for the current branch.
2. Confirm the latest local validation for the branch still passes.
   Use local checks or the GCP-side `ci-preflight` skill for expensive CI-like
   validation; do not move pre-commit, build, test, or image-build work to EC2.
3. Check mergeability and base branch:
   - `gh pr view --json mergeable -q .mergeable`
4. If the PR is conflicting, merge or rebase from the PR base branch, rerun
   validation, and push.
5. Run the `review` skill. Do not merge until it reports no unresolved blocking
   items.
6. Reply inline before changing code when feedback requires action.
7. If CI will take longer than 60 seconds, use the `background-monitor` skill
   and `scripts/ci/wait_for_github_run_condition.sh` instead of `gh pr checks
   --watch` or `sleep && gh ...` polling.
8. If checks fail:
   - inspect failing runs,
   - fix the issue,
   - commit, push, and restart the watcher.
9. When checks are green and all review feedback is resolved, merge the PR with
   a normal merge commit.
10. Keep the remote branch intact after merge.
11. Move the Linear issue to `Done`.

## Commands

```sh
branch=$(git branch --show-current)
pr_number=$(gh pr view --json number -q .number)
mergeable=$(gh pr view --json mergeable -q .mergeable)
base_branch=$(gh pr view --json baseRefName -q .baseRefName)

if [ "$mergeable" = "CONFLICTING" ]; then
  echo "Merge or rebase from origin/$base_branch, validate, and push before merging." >&2
  exit 1
fi

gh pr merge "$pr_number" --merge
```

## Notes

- Infer the base branch from the PR; do not hard-code `sports-arbitrage`.
- Never use squash merge.
- Never enable auto-merge.
- Never delete the remote branch as part of the merge flow.
- Treat unresolved human or bot review comments as blocking.
- Do not use model-loop polling for CI. Use the background watcher script for
  any wait expected to exceed 60 seconds.
