# Upstream Reconciliation

This repository tracks the upstream Nautilus Trader project while preserving
Cloudbet-specific product work on separate branches.

## Branch Roles

- `cloudbet`
  - legacy/default branch during the migration
  - kept for history and to host scheduled workflows until the default branch is
    switched deliberately
- `develop`
  - main integration branch for product work
  - receives upstream reconciliation from `nautilus_develop`
  - receives feature work such as `sports-arbitrage`
- `production`
  - stable release branch
  - receives upstream reconciliation from `nautilus_master`
  - receives controlled promotion from `develop`
- `nautilus_develop`
  - strict mirror of `upstream/develop`
  - updated manually by an operator when upstream reconciliation is needed
- `nautilus_master`
  - strict mirror of `upstream/master`
  - updated manually by an operator when upstream reconciliation is needed

## Automation

Scheduled upstream mirror and nightly merge workflows are currently disabled.
Operators should update mirror branches and prepare reconciliation PRs manually
until a replacement automation path is explicitly approved.

## Reconciliation PR Rules

- PRs are created on stable sync branches:
  - `sync/upstream-develop-to-develop`
  - `sync/upstream-master-to-production`
- Sync PRs are labeled:
  - `upstream-sync`
  - `automated`
- Sync PRs are never auto-merged.
- Sync PRs are expected to land with a merge commit.
- Remote sync branches are preserved after merge.

## Conflict Handling

When a clean merge is not possible:

- do not push a conflicted sync branch
- open or update a GitHub issue instead
- record:
  - source branch
  - target branch
  - source SHA
  - target SHA
  - merge base

## Operator Notes

- The repository merge policy should remain:
  - merge commits enabled
  - squash disabled
  - rebase disabled
  - delete branch on merge disabled
- Branch protection for private repositories may depend on the current GitHub
  plan. If the API rejects branch protection changes, the sync workflow and repo
  merge policy still provide the baseline guardrails until rules can be enabled.
