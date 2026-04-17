---
name: parallel-orchestrator
description:
  Use when a task should be split across multiple workers with explicit file
  ownership, aggressive parallel delegation, iterative follow-up, and monitor
  handoff for long waits instead of chat polling.
---

# Parallel Orchestrator

Use this skill when the work is large enough to benefit from multiple workers or
when the user explicitly asks for coordinated delegation.

## Core Rules

- Delegate aggressively when workstreams are independent.
- Give every worker an explicit ownership boundary before it edits anything.
- Default spawned agents to `gpt-5.4` with `xhigh` reasoning unless the task has
  a stronger constraint.
- Follow up iteratively with `send_input`; do not treat delegation as a one-shot
  handoff.
- Each subagent may spawn at most one child agent.
- Do not use chat-loop polling for CI, SSH, build, or deploy waits.
- For waits expected to exceed 60 seconds, require
  [$background-monitor](../background-monitor/SKILL.md).
- Workers must hand long waits to a monitor, capture the command/log path, and
  return when the monitor reports an event.

## When To Use

Use this skill when one or more of these are true:

1. The task spans multiple directories, systems, or validation tracks.
2. The user has already assigned worker identities or ownership slices.
3. A single agent would spend noticeable time serializing independent work.
4. Some workers can continue while another worker waits on CI, SSH, or builds.

Do not spawn workers for trivial single-file edits or when ownership cannot be
made clear.

## Orchestration Loop

1. Break the task into independent slices.
2. Assign each slice to one worker with:
   - exact files/directories it owns,
   - the objective,
   - required validation,
   - forbidden areas.
3. Launch workers in parallel as early as possible.
4. Keep a live coordinator ledger:
   - worker name,
   - ownership,
   - status,
   - blockers,
   - validation state.
5. Revisit workers with `send_input` when:
   - requirements change,
   - another worker exposes an interface dependency,
   - validation fails,
   - a monitor returns an event.
6. Merge outcomes only after checking that ownership boundaries were respected.

## Worker Contract

Every worker instruction should include:

- `Ownership`: the only files, directories, or systems that worker may modify.
- `Goal`: the concrete deliverable.
- `Validate`: the minimum checks to run.
- `Escalate`: what to do if blocked or if another worker owns the needed area.

Require workers to state assumptions early, avoid touching unowned files, and
report exact outputs: changed files, validation status, blockers, and follow-up
requests.

## Follow-Up Discipline

Use iterative `send_input` instead of only one initial prompt. Good follow-ups
are short and specific:

- tighten or expand ownership,
- answer a blocker,
- hand over an interface contract from another worker,
- request a retry after a dependent change lands,
- redirect a long wait to a monitor.

Do not resend the full task unless the worker lost necessary context.

## Long-Running Waits

If a worker reaches CI, SSH, build, deploy, or remote-script waiting:

1. Stop interactive polling.
2. Hand the wait to [$background-monitor](../background-monitor/SKILL.md).
3. Persist logs when the process may outlive the current interaction.
4. Return control to the coordinator.
5. Resume only when the monitor reports failure, success, or the requested log
   event.

## Child-Agent Limit

Subagents may spawn at most one child agent and must pass down a narrower
ownership boundary than their own. Do not create deep trees for work that can be
coordinated from the top level.

## Output

The coordinator should finish with:

- completed workers and ownership slices,
- changed files per worker,
- validation results,
- open blockers or follow-up needs,
- any monitor commands or log paths still in effect.
