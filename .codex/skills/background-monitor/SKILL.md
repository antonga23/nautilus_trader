---
name: background-monitor
description:
  Use when monitoring CI, logs, builds, remote scripts, deploys, or long-running
  commands; run a background watcher that exits only when the desired condition
  is met instead of polling in the conversation.
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
  cmd="mkdir -p artifacts/monitors && log_path=artifacts/monitors/ci-preflight.log && set +e; ssh -i \"$EC2_KEY_PATH\" -o StrictHostKeyChecking=no \"$EC2_USER@$EC2_HOST\" 'cd /home/ubuntu/pr14-ci-clone && bash scripts/ci/run_ci_preflight.sh' > \"$log_path\" 2>&1; status=$?; set -e; if [ \"$status\" -ne 0 ]; then tail -n 200 \"$log_path\" >&2 || true; fi; exit \"$status\"",
  yield_time_ms=1000,
  max_output_tokens=12000
)
```

The condition is remote command completion. The background task returns `0`
when the remote preflight exits `0`, and returns non-zero with the last 200 log
lines when the preflight fails.

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
