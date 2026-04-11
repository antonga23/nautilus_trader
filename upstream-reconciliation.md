# Upstream Reconciliation Policy

This document is the repository memory for any future upstream reconciliation
work. It exists so agents do not relearn the same rules on each pass and do not
reintroduce already-rejected merge behavior.

## Current Status

- Upstream reconciliation implementation is paused pending a product decision.
- Do not resume upstream merge automation or broad upstream code ingestion
  without an explicit Linear instruction.
- Documentation may be updated to preserve decisions, rules, and conflict
  history.

## Hard Rules

### CI/CD ownership

- This repository owns its CI/CD pipeline.
- Upstream Nautilus CI/CD must not replace, overwrite, or silently expand this
  repository's workflows.
- In any upstream conflict involving CI/CD or repository automation, prefer this
  repository's version by default.
- Relevant protected surfaces include:
  - `.github/workflows/**`
  - `.github/actions/**`
  - `.pre-commit-config.yaml`
  - repository-specific CI helper scripts under `scripts/ci/**`
  - any release, publish, tagging, or branch automation that is specific to
    this repository

### Dependency merges

- Dependency or packaging updates from upstream are not auto-accepted.
- Evaluate dependency changes for:
  - whether they are required for adapters or modules we actually use,
  - whether they break this repository's build or runtime behavior,
  - whether they introduce security or compatibility pressure,
  - whether the lowest working version can be retained instead of upgrading.
- Prefer the lowest version that works unless there is a concrete security,
  compatibility, or upstream integration reason to move higher.

### Script merges

- Preserve repository-specific scripts and operational flow.
- Compare upstream script changes only for genuinely useful logic, bug fixes, or
  new required dependencies.
- Do not overwrite local operator workflows or CI helpers just because upstream
  changed them.

### Makefile policy

- Treat `Makefile` as a mixed surface:
  - preserve local targets and repository-specific operational behavior,
  - selectively inherit clearly useful upstream improvements,
  - reject targets that reintroduce upstream-only release or validation flows we
    do not want.
- Any adopted upstream `Makefile` change must be justified by concrete value:
  dependency installation, compatibility, or required build behavior.

## Established Conflict Rules

### Conflict set 11

Conflict files previously identified:

- `.github/actions/common-setup/action.yml`
- `.github/workflows/build.yml`
- `.pre-commit-config.yaml`
- `Makefile`

Resolution rule:

- Keep this repository's CI/CD and pre-commit behavior.
- Ignore upstream workflow and action changes for these files unless a change is
  deliberately reviewed and ported.
- For `Makefile`, inspect upstream changes selectively and port only useful
  improvements that do not damage local workflows.

### Conflict set 12

Conflict files previously identified:

- `.github/workflows/build.yml`
- `.github/workflows/codeql-analysis.yml`
- `.github/workflows/coverage.yml`
- `.github/workflows/docker.yml`
- `.github/workflows/docs.yml`
- `.github/workflows/release.yml`
- `.gitignore`
- `Makefile`
- `nautilus_trader/common/providers.py`
- `nautilus_trader/model/currencies.py`
- `nautilus_trader/test_kit/providers.py`
- `poetry.lock`
- `pyproject.toml`
- `scripts/test-coverage.sh`
- `scripts/test.sh`
- `tests/integration_tests/adapters/betfair/test_betfair_client.py`
- `tests/unit_tests/model/test_model_instrument.py`

Resolution rule:

- Preserve this repository's CI/CD workflows and script layout.
- Review packaging and dependency files (`pyproject.toml`, `poetry.lock`) for
  truly needed upstream changes; do not accept broad version drift by default.
- Treat application and adapter files as code-review conflicts, not mechanical
  merges.
- For tests, keep repository-relevant coverage and avoid inheriting upstream
  validation paths that do not serve this product.

## Operational Guidance For Future Agents

- Start by reading this file before touching any upstream sync branch or
  reconciliation workflow.
- Check the active Linear issue to confirm whether upstream reconciliation is
  paused, exploratory, or approved for execution.
- If upstream work is paused, do not open sync PRs, update mirror branches, or
  land reconciliation branches.
- If upstream work resumes later, document each conflict cluster and resolution
  rule here before repeating the same merge pattern.

## Background Monitoring Rule

- Any monitoring, CI waiting, log watching, or long-running remote command
  expected to exceed 60 seconds must use a background watcher process.
- Do not consume conversation turns with `sleep && poll` loops.
- For GitHub Actions runs in this repository, use
  `scripts/ci/wait_for_github_run_condition.sh`.

## Open Decision

The repository still needs an explicit product decision on whether to:

- continue selective upstream reconciliation, or
- rewrite the custom adapter layer from scratch on top of current upstream
  Nautilus.

Until that decision is made, treat upstream reconciliation as paused and this
document as the policy baseline.
