---
name: ci-preflight
description:
  Run the same custom and full Python validation path on the GCP CI runner
  before a GitHub Actions run, using the repo's reusable preflight script.
---

# CI Preflight

Use this skill when you need a fast operator-side validation pass on the GCP
self-hosted CI runner before spending a GitHub Actions run.

EC2 is not a CI preflight host for this repository. EC2 is reserved for
strategy-node deploy, runtime, lifecycle, health, and log inspection work.

## Goals

- Reproduce the PR Python validation path on the GCP CI runner.
- Use the same custom and full suite commands as CI.
- Keep the result diagnostic only; this is not a formal merge gate.
- Preserve the GCP/EC2 split: CI/build/test work on GCP, deploy/runtime work on
  EC2.

## Required Files

- Preflight runner script:
  - `scripts/ci/run_ci_preflight.sh`
- Shared test helpers:
  - `scripts/ci/initialize_database_schema.sh`
  - `scripts/ci/run_python_test_suites.sh`
  - `scripts/ci/run_pytest_with_reporting.sh`

## Workflow

1. Verify GCP access before any remote CI preflight:
   - active account: `info@unlimitedgames.shop`,
   - project: `shining-sol-493421-h6`,
   - runner host: `cloudbet-gcp-ci-runner-20260426`,
   - zone: `us-central1-a`.
2. Sync or fetch the candidate checkout on the GCP CI host.
3. If the preflight is expected to take longer than 60 seconds, use the
   `background-monitor` skill and run the GCP preflight as a background watcher
   task.
4. Run `scripts/ci/run_ci_preflight.sh` on the GCP host from the repository
   root.
5. Inspect `tests/results/` for:
   - JUnit XML,
   - raw pytest logs,
   - coverage output for the custom logic suite.
6. Only after the GCP preflight is green should you spend a GitHub Actions run.

## Runner Boundary

- GCP CI runner:
  - pre-commit,
  - Python tests,
  - Rust policy checks,
  - wheel builds,
  - strategy-node image builds.
- EC2 deploy/trading host:
  - strategy-node deployment,
  - process lifecycle,
  - runtime health,
  - persisted logs.

If GCP auth expires or the GCP runner is unavailable, do not fall back to EC2
for CI/build work. Re-authenticate GCP, repair the GCP runner, or use the
intended GitHub Actions workflow.

## Background Monitor Example

For long-running preflights, use one blocking watcher command instead of chat
polling:

```text
exec_command(
  cmd="mkdir -p artifacts/monitors && log_path=artifacts/monitors/gcp-ci-preflight.log && set +e; /Users/alatha.ntonga/google-cloud-sdk/bin/gcloud compute ssh cloudbet-gcp-ci-runner-20260426 --project=shining-sol-493421-h6 --zone=us-central1-a --command 'set -euo pipefail; cd /opt/actions-runner/_work/cloudbet-market-maker/cloudbet-market-maker; git config --global --add safe.directory \"$PWD\"; bash scripts/ci/run_ci_preflight.sh' > \"$log_path\" 2>&1; status=$?; set -e; if [ \"$status\" -ne 0 ]; then tail -n 200 \"$log_path\" >&2 || true; fi; exit \"$status\"",
  yield_time_ms=1000,
  max_output_tokens=12000
)
```

## Notes

- The script uses the same `ci-runner` container image as GitHub's self-hosted
  jobs.
- The script initializes Postgres with `psql`, not `nautilus database init`.
- The script reuses the self-hosted wheel cache when available.
- Do not store cloud passwords in this skill, command templates, logs, or repo
  files. Use interactive GCP auth or approved secret storage.
- Do not use repeated `sleep && ssh ...` or other model-loop polling for
  long-running preflights.
