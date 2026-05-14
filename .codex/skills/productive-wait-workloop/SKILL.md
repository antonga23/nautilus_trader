---
name: productive-wait-workloop
description:
  Use when a CI, release, deploy, runtime verification, or remote monitor is
  expected to run long enough that independent work should continue in a safe
  separate worktree, with Linear updates and an immediate return to the watched
  task when the monitor exits.
---

# Productive Wait Workloop

Use this skill after starting a `background-monitor` watcher when the wait is
likely to last several minutes.

## Preconditions

- A watcher is already running or about to run.
- Its terminal condition, run id/command, branch, and durable log path are
  known.
- The side work is independent from the watched branch, workflow, deployed
  runtime, and secrets surface.
- The side worktree and any validation/build/test/pre-commit workload are on a
  dedicated remote code-dev VM, GCP runner, or GitHub Actions. The local Mac is
  edit/light-inspection only.

## Workflow

1. Record a short wait ledger:
   - watcher target and terminal condition,
   - branch/run id,
   - durable monitor log path,
   - original task to resume,
   - side task and worktree.
2. Post a Linear comment before switching context.
3. Work only on a separate remote worktree or a clearly disjoint file set.
4. Keep the side task bounded to one clean stopping point: diagnostics, tests,
   docs, a small stacked branch, or an isolated implementation slice.
5. Do not dispatch a second workflow for the watched release surface while the
   watcher is active.
6. When the watcher exits, stop the side task immediately after the current
   command or patch finishes.
7. Inspect the watcher log, post a Linear update, and resume the original task
   before continuing experimentation.

## Safety Rules

- Never enable real-money execution or place live trades while filling wait
  time.
- Keep `auto_execute=false` for trading-node validation.
- Do not print or commit secrets.
- Do not run local build, pre-commit, pytest, ruff, semantic completion, Rust,
  wheel, Docker, or image-build workloads from the Mac.
- Use GCP runners for CI/build/test/image work and EC2 only for deployed node
  runtime inspection.
