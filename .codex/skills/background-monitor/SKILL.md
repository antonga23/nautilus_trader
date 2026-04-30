---
name: background-monitor
description:
  Use when monitoring CI, logs, builds, remote scripts, deploys, or long-running
  commands, especially when a wait is likely to exceed 60 seconds; run a
  background watcher that exits only when the desired condition is met instead
  of polling in the conversation.
---

# Background Monitor

Use this skill whenever the task asks to monitor, watch, tail, wait for CI,
watch logs for errors, or supervise a long-running command.

## Rule

Do not poll from the model loop. Start a background watcher process whose exit
condition is the notification condition, then let Codex stop reasoning until the
background task completes.

For this repository, any expected wait longer than 60 seconds must use this
pattern. Do not use repeated `sleep && ...`, `gh ...` polling, or interactive
tail loops from the conversation.

## Pattern

1. Define the terminal condition before launching the watcher:
   - first CI job failure,
   - full CI terminal success/failure,
   - first log line matching an error pattern,
   - remote command exit,
   - service health becoming unhealthy or healthy.
2. Start the watcher as a background terminal task and make the watcher block
   until that condition occurs.
3. Write output to a durable log file if the task may outlive the current
   session.
4. Do not keep issuing `sleep && gh ...` or `tail` polling commands in chat.
5. When the watcher completes, inspect only the emitted summary and relevant
   logs.
6. For GitHub validation work in this repository, run the GCP-side
   `ci-preflight` slice before dispatching a new Actions run when the GCP CI
   runner is reachable.

## Active Waiting

When a watcher is expected to run for several minutes, keep useful work moving
without obscuring the watched task:

1. Write down the watched run/command, branch, terminal condition, and durable
   log path before switching context.
2. Continue only independent work that cannot corrupt the watched branch or
   deployed runtime. Prefer a separate worktree for stacked PRs, experiments, or
   Linear follow-ups.
3. Do not dispatch another run for the same branch or mutate the same release
   surface while its watcher is active, unless the watcher has failed or the run
   has been explicitly cancelled.
4. Post concise Linear progress comments for long waits and any independent
   work completed during the wait.
5. As soon as the watcher exits, pause the side task at a clean boundary,
   inspect the watcher result, and resume the original task before continuing
   experimentation.

## Runner Boundary

- Use GCP self-hosted runners for pre-commit, tests, Rust policy checks, wheel
  builds, and strategy-node image builds.
- Use EC2 only for strategy-node deploy, runtime lifecycle, health checks, and
  log inspection.
- If GCP auth expires or the GCP runner is unavailable, do not move CI/build
  work to EC2. Re-authenticate GCP, repair the GCP runner, or run the intended
  GitHub Actions workflow.
- Never persist plaintext cloud passwords in watcher commands, logs, skills, or
  repo files.

## Codex Desktop Call Shape

Use this shape for a watcher that should wake the agent only when it finishes:

```text
exec_command(
  cmd="<watcher command that exits only on the desired condition>",
  yield_time_ms=1000,
  max_output_tokens=12000
)
```

The watcher command is responsible for blocking until the signal happens. For
CI, that signal is usually first job failure or terminal run completion. For
remote scripts, that signal is command exit. For log monitoring, that signal is
the first matching error pattern.

## GitHub Actions

For this repository, prefer:

```sh
scripts/ci/wait_for_github_run_condition.sh \
  --repo antonga23/cloudbet-market-maker \
  --run-id "$RUN_ID" \
  --condition first-failure-or-terminal \
  --sleep 30
```

Default behavior exits with:

- `0` when the run completes successfully,
- `1` immediately when a job fails or the run completes unsuccessfully,
- `124` on explicit timeout.

## Remote Commands

For SSH or build commands, wrap the command in a shell block that exits only on
the signal you care about. Persist logs so another agent can inspect them after
a disconnect.

Example:

```text
exec_command(
  cmd="mkdir -p artifacts/monitors && log_path=artifacts/monitors/gcp-ci-preflight.log && set +e; /Users/alatha.ntonga/google-cloud-sdk/bin/gcloud compute ssh instance-20260415-214825 --project=shining-sol-493421-h6 --zone=europe-west4-c --command 'set -euo pipefail; cd /opt/actions-runner/_work/cloudbet-market-maker/cloudbet-market-maker; bash scripts/ci/run_ci_preflight.sh' > \"$log_path\" 2>&1; status=$?; set -e; if [ \"$status\" -ne 0 ]; then tail -n 200 \"$log_path\" >&2 || true; fi; exit \"$status\"",
  yield_time_ms=1000,
  max_output_tokens=12000
)
```

The condition is remote command completion. The background task returns `0`
when the GCP remote preflight exits `0`, and returns non-zero with the last 200
log lines when the preflight fails.

## Log Error Watch

For an existing log stream, make the process block until the pattern appears:

```sh
tail -n +1 -F "$log_path" | grep -m 1 -E "ERROR|Traceback|panic|Fatal Python error"
```

Use this only when the desired notification is the first matching log line. If
the desired notification is command completion, prefer the remote command
wrapper above.

## Required Discipline

- Surface the exact command and log path before leaving it in the background.
- Prefer fail-fast watchers: first failure should wake the agent immediately.
- Avoid notification spam: one watcher per condition, not one watcher per job
  unless needed.
- Kill or replace stale watchers before starting a new watcher for the same
  branch or run.
- If the watcher exists to guard a GitHub run, do not dispatch that run until
  local or GCP preflight has already covered the minimal reproducible slice.
