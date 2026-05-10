---
name: continuous-experimentation
description:
  Use when continuing into the next logical experimental implementation branch,
  including while a CI, release, deploy, or runtime watcher is running, especially
  for strategy, adapter, semantic mining, arbitrage, trading-node, or runtime
  validation work where the user wants autonomous follow-through until evidence
  is available.
---

# Continuous Experimentation

Use this skill after the current assigned task is at a safe boundary, or while a
background watcher is active and waiting. It may start bounded side work before
the primary task is landed, provided the side work is isolated and immediately
pausable.

## Purpose

Turn the next highest-value idea into a bounded experimental branch without
waiting for a new human prompt. During long waits, use it to keep useful work
moving without changing the acceptance criteria or release surface of the
watched task.

## Hard Safety Boundaries

- Never spend real funds, place live trades, or enable live execution without
  explicit human approval in the current thread.
- Implement execution paths up to dry-run, validation-mode, simulator, or mock
  execution when real funds would be required.
- Keep `auto_execute=false` for strategy-node validation unless explicitly
  instructed otherwise by a human.
- Do not persist secrets in committed files, logs, skill files, PR bodies, or
  Linear comments. Use approved secret stores or local secret files.
- Use GCP runners for CI/build/test/image work and EC2 only for deployed
  strategy-node runtime inspection.

## Experiment Selection

1. Re-read the latest user goals, open PR status, Linear issue, and recent
   runtime evidence.
2. Choose one small experimental objective that improves the current strategy
   surface. Prefer:
   - full-featured single-venue arbitrage,
   - multi-venue arbitrage,
   - Cloudbet or Polymarket adapter integration,
   - semantic matching coverage,
   - risk-engine execution gating,
   - trading-node validation observability.
3. Create or update a Linear issue before coding. Include:
   - hypothesis,
   - scope and explicit non-goals,
   - affected providers/venues,
   - safety boundary,
   - validation plan,
   - rollback plan.
4. Branch from the current target base, normally `origin/develop`, with a
   clearly experimental branch name.

## Starting Before The Primary Task Lands

This mode is allowed when a primary CI, release, deploy, or runtime watcher is
already running or the primary task is otherwise blocked on external completion.

Requirements:

1. Record the primary watcher run id or command, branch, terminal condition, and
   durable log path before starting side work.
2. Create a separate worktree from the appropriate base branch. Do not mutate
   the watched branch, deployed runtime, or release artifacts.
3. Pick work that is independent of the watched gate and can be stopped after a
   single command or patch if the watcher fires.
4. Do not merge, arm live execution, dispatch a conflicting deploy, or mark the
   side task complete while the primary gate is unresolved.
5. When the watcher exits, pause side work immediately at the next clean
   boundary, inspect the watcher result, and resume the primary task before
   continuing experimentation.

## Implementation Loop

1. Build the smallest end-to-end slice that proves or disproves the hypothesis.
2. Preserve public APIs unless the experiment explicitly owns an API change.
3. Use mocks, fixtures, simulators, or validation-mode trading nodes for any
   behavior that would otherwise require real-money execution.
4. Keep provider-specific behavior isolated behind existing adapters,
   normalizers, strategy config, or risk-gating seams.
5. Add tests at the boundary the experiment changes.
6. Before pushing, run the narrow local validation slice.

## While A Monitor Runs

If a CI, release, deploy, or runtime watcher is active for the current task:

1. Treat the watcher result as the highest-priority interrupt.
2. Work only on independent experimental branches or separate worktrees while
   waiting.
3. Keep the side work small enough that it can be paused immediately when the
   watcher exits.
4. Record side-task progress in Linear, including the watched run id and the
   point where original-task work must resume.
5. Do not merge or make an experimental PR ready while the original task's
   release/runtime proof is unresolved.
6. If the watcher is expected to run for a full release/deploy cycle, create a
   short productive-wait note before switching contexts: watched run, durable
   log path, selected side task, files/worktree involved, and the condition
   that interrupts side work.

## Required Validation

- PR branch must pass at least `pr-validation`.
- When strategy-node behavior changes, run `strategy-node-release` in
  validation mode and inspect the runtime evidence.
- After merge, confirm `develop-validation` on the merge commit.
- For semantic mining or matcher changes, run the
  `semantic-rule-mining-completion` skill with the active provider scope.
- Use `background-monitor` for any CI, deploy, remote command, or log watch
  expected to exceed 60 seconds.

## Completion Before Handoff

Do not hand back until:

- Linear has a final comment with commit/PR/run links and remaining risks.
- The PR body or comment includes the validation evidence.
- Blocking CI/review feedback has been addressed or captured in Linear with a
  concrete blocker and attempted workarounds.
- Any long-running monitor has completed or has a durable log path and a clear
  owner.
- The next experiment candidate is recorded in Linear if more work remains.
