---
name: ci-preflight
description:
  Run the same custom and full Python validation path on the EC2 runner before a
  GitHub Actions run, using the repo's reusable preflight script.
---

# CI Preflight

Use this skill when you need a fast operator-side validation pass on the EC2
runner before spending a GitHub Actions run.

## Goals

- Reproduce the PR Python validation path on the EC2 runner.
- Use the same custom and full suite commands as CI.
- Keep the result diagnostic only; this is not a formal merge gate.

## Required Files

- Preflight runner script:
  - `scripts/ci/run_ci_preflight.sh`
- Shared test helpers:
  - `scripts/ci/initialize_database_schema.sh`
  - `scripts/ci/run_python_test_suites.sh`
  - `scripts/ci/run_pytest_with_reporting.sh`

## Workflow

1. Sync the candidate checkout to the EC2 host.
2. If the preflight is expected to take longer than 60 seconds, use the
   `background-monitor` skill and run the EC2 preflight as a background watcher
   task.
3. Run `scripts/ci/run_ci_preflight.sh` on the EC2 host from the repository
   root.
4. Inspect `tests/results/` for:
   - JUnit XML,
   - raw pytest logs,
   - coverage output for the custom logic suite.
5. Only after the preflight is green should you spend a GitHub Actions run.

## Background Monitor Example

For long-running preflights, use one blocking watcher command instead of chat
polling:

```text
exec_command(
  cmd="mkdir -p artifacts/monitors && log_path=artifacts/monitors/ci-preflight.log && set +e; ssh -i \"$EC2_KEY_PATH\" -o StrictHostKeyChecking=no \"$EC2_USER@$EC2_HOST\" 'cd /home/ubuntu/pr14-ci-clone && bash scripts/ci/run_ci_preflight.sh' > \"$log_path\" 2>&1; status=$?; set -e; if [ \"$status\" -ne 0 ]; then tail -n 200 \"$log_path\" >&2 || true; fi; exit \"$status\"",
  yield_time_ms=1000,
  max_output_tokens=12000
)
```

## Notes

- The script uses the same `ci-runner` container image as GitHub's self-hosted
  jobs.
- The script initializes Postgres with `psql`, not `nautilus database init`.
- The script reuses the self-hosted wheel cache when available.
- Do not use repeated `sleep && ssh ...` or other model-loop polling for
  long-running preflights.
