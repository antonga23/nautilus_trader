---
name: review
description:
  Sweep a pull request for human and bot feedback, and drive the push-review-fix
  loop until the PR is clear.
---

# Review

## Goals

- Treat PR review as a repeatable control loop, not a one-off check.
- Capture all actionable feedback from humans, CI, and `qodo-code-review`.
- Keep Qodo review optional unless the user or repo policy explicitly requires
  it.

## When To Use

Use this skill:

1. Immediately after every push that changes code.
2. Before moving a Linear issue to `Human Review`.
3. Before moving a Linear issue to `Merging`.
4. Whenever CI fails or new review activity appears on the PR.

## Review Sources

Sweep these channels every time:

- top-level PR comments:
  - `gh pr view --comments`
- issue comments on the PR:
  - `gh api repos/<owner>/<repo>/issues/<pr>/comments --paginate`
- inline review comments:
  - `gh api repos/<owner>/<repo>/pulls/<pr>/comments --paginate`
- review summaries:
  - `gh pr view --json reviews`
- check runs / CI:
  - `gh pr checks`

## Qodo Rules

- Reviewer login is `qodo-code-review`.
- Only request a fresh Qodo review when the user explicitly asks for it or the
  active repo policy requires it.
- Do not assume an older Qodo review on a previous SHA is sufficient if a fresh
  Qodo pass is actually required.
- Treat Qodo items labeled or described as bugs, rule violations, or required
  actions as blocking only when a Qodo review is in scope.
- Treat recommendation-only comments as non-blocking if the concern is verified
  false or explicitly out of scope.
- Never dismiss a Qodo finding silently. Either:
  - fix it in code,
  - reply with the validation that disproves it, or
  - document why it is not a real issue.

## Steps

1. Identify the current branch, PR number, and head SHA.
2. Pull the full feedback set from every source above.
3. Normalize the findings into a short checklist:
   - source,
   - author,
   - URL or comment id,
   - blocking vs non-blocking,
   - disposition: open, fixed, rejected with rationale.
4. Prioritize blocking feedback first:
   - CI failures,
   - human defect reports,
   - `qodo-code-review` bug/rule/action items when Qodo is in scope,
   - mergeability problems.
5. For each blocking item:
   - verify whether the issue is real,
   - implement the smallest correct fix if real,
   - otherwise reply with concrete rationale and evidence.
6. Run targeted validation for each batch of fixes.
7. Push the branch update.
8. If Qodo is in scope, request a fresh Qodo review on the latest SHA.
9. Re-run this skill and stop only when:
   - there are no unresolved blocking comments,
   - CI is green,
   - mergeability is clean,
   - if Qodo is in scope, the latest SHA has a fresh Qodo pass or no new Qodo
     issues.

## Commands

```sh
branch=$(git branch --show-current)
pr_number=$(gh pr view --json number -q .number)
head_sha=$(gh pr view "$pr_number" --json headRefOid -q .headRefOid)

gh pr view "$pr_number" --comments
gh api "repos/antonga23/cloudbet-market-maker/issues/$pr_number/comments?per_page=100"
gh api "repos/antonga23/cloudbet-market-maker/pulls/$pr_number/comments?per_page=100"
gh pr view "$pr_number" --json reviews,comments,latestReviews,headRefOid
gh pr checks "$pr_number"
```

## Output

Record the current sweep in the Linear `## Codex Workpad` comment:

- PR head SHA
- whether Qodo is in scope for that SHA
- open blocking findings
- fixed findings in this pass
- validation run
- whether another push-review loop is required
