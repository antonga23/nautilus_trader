---
name: land
description:
  Land a PR by resolving drift against `sports-arbitrage`, watching CI, and
  merge-committing only when checks and review feedback are fully clear.
---

# Land

## Goals

- Keep the PR conflict-free with `sports-arbitrage`.
- Ensure CI and review feedback are fully resolved.
- Merge with a normal merge commit, without auto-merge or branch deletion.

## Preconditions

- `gh` is authenticated.
- The working tree is clean.
- The current branch already has an open PR.

## Steps

1. Identify the PR for the current branch.
2. Confirm the latest local validation for the branch still passes.
3. Check mergeability:
   - `gh pr view --json mergeable -q .mergeable`
4. If the PR is conflicting, merge `origin/sports-arbitrage`, rerun validation,
   and push.
5. Run the `review` skill. Do not merge until it reports no unresolved blocking
   items, including `qodo-code-review` findings on the latest SHA.
6. Reply inline before changing code when feedback requires action.
7. Watch checks until complete:
   - `gh pr checks --watch`
8. If checks fail:
   - inspect failing runs,
   - fix the issue,
   - commit, push, request a fresh Qodo review, and restart the watch loop.
9. When checks are green and all review feedback is resolved, merge the PR with
   a normal merge commit.
10. Keep the remote branch intact after merge.
11. Move the Linear issue to `Done`.

## Commands

```sh
branch=$(git branch --show-current)
pr_number=$(gh pr view --json number -q .number)
mergeable=$(gh pr view --json mergeable -q .mergeable)

if [ "$mergeable" = "CONFLICTING" ]; then
  echo "Merge origin/sports-arbitrage, validate, and push before merging." >&2
  exit 1
fi

gh pr checks --watch
gh pr merge "$pr_number" --merge
```

## Notes

- Base branch is `sports-arbitrage`.
- Never use squash merge.
- Never enable auto-merge.
- Never delete the remote branch as part of the merge flow.
- Treat unresolved human or bot review comments as blocking.
