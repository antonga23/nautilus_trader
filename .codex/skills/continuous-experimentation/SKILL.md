---
name: continuous-experimentation
description:
  Use when continuing after a completed task into the next logical experimental
  implementation branch, especially for strategy, adapter, semantic mining,
  arbitrage, trading-node, or runtime validation work where the user wants
  autonomous follow-through until CI/release evidence is available.
---

# Continuous Experimentation

Use this skill only after the current assigned task is implemented, verified,
committed, pushed, and updated in Linear.

## Purpose

Turn the next highest-value idea into a bounded experimental branch without
waiting for a new human prompt. This is for follow-on engineering work, not for
changing the acceptance criteria of the current task.

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

## Implementation Loop

1. Build the smallest end-to-end slice that proves or disproves the hypothesis.
2. Preserve public APIs unless the experiment explicitly owns an API change.
3. Use mocks, fixtures, simulators, or validation-mode trading nodes for any
   behavior that would otherwise require real-money execution.
4. Keep provider-specific behavior isolated behind existing adapters,
   normalizers, strategy config, or risk-gating seams.
5. Add tests at the boundary the experiment changes.
6. Before pushing, run the narrow local validation slice.

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
