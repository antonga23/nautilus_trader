---
name: end-to-end-completion
description: Use for multi-part implementation plans, recovery work, CI/release/runtime tasks, or any user request that says to proceed relentlessly, avoid stopping for decisions, or drive work to completion. Enforces a requirements ledger, safe workaround attempts before escalation, and end-to-end verification before handing work back.
---

# End-To-End Completion

Use this skill when a task has multiple requirements, an explicit plan, release/runtime validation, or the user asks not to stop until completion.

## Operating Rule

Do not hand work back while feasible implementation, validation, or workaround paths remain.

Before stopping, produce evidence that every requested outcome is either:

- completed and verified,
- superseded by a newer user requirement,
- intentionally out of scope by explicit user instruction, or
- blocked after exhausting safe alternatives.

## Requirements Ledger

At task start and after each major change, maintain a concise ledger:

- User-requested outcomes.
- Required files, systems, workflows, tickets, hosts, and PRs.
- Current status for each outcome: `pending`, `in_progress`, `verified`, `superseded`, or `blocked`.
- Evidence needed to mark each outcome verified.

Newer user requirements supersede older requirements where they conflict. Preserve non-conflicting older requirements.

## Blocker Policy

Do not ask the user to decide when a safe, bounded engineering path is available.

For blockers, try multiple practical approaches before escalating. Prefer at least five when the surface allows it:

1. Retry with corrected parameters or repaired local state.
2. Use an equivalent existing resource.
3. Move to another zone/host/runner/storage path within the approved boundary.
4. Patch the workflow/script/test so the path is deterministic.
5. Split the task so independent work continues while the blocked lane is remediated.

Escalate only for irreversible destructive action, material unapproved cost increase, unavailable secrets that cannot be sourced from approved secret stores, or policy/security boundaries.

## Validation Discipline

- Use `background-monitor` for waits expected to exceed 60 seconds.
- During long watcher waits, continue bounded independent work in a separate
  worktree when available, then pause that side work and return to the original
  task as soon as the watcher fires.
- Use GCP CI runners for pre-commit, tests, wheel builds, Rust policy, and strategy-node image builds.
- Use EC2 only for strategy-node deploy/runtime/lifecycle/health/logs.
- Prefer `ci-preflight` before spending GitHub Actions runs when the GCP runner is reachable.
- Do not rely on “container is running” as runtime success. Verify process, persisted logs, heartbeat/status, and task-specific success lines.
- For release-only runtime bugs, add tests at the boundary that failed: adapter command shape, DataEngine integration, strategy-node smoke, deploy input validation, or artifact handoff.

## Completion Gate

Before final response:

- Re-read the latest user plan and compare it to the ledger.
- Check `git status`, PR state, CI state, and runtime state when relevant.
- Confirm changes are committed/pushed/merged when the user requested published branch state.
- Provide exact residual risks only after verified work is complete.

If any item remains incomplete, continue working unless escalation is required by the blocker policy.
