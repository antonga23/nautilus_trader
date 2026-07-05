---
experiment: cross-venue-fixture-precision
date: 2026-07-05
mode: checkpoint
status: complete
branch: experiment/2026-07-05-cross-venue-fixture-precision
baseline_commit: 46e375f2e1d8
primary_metric: doubleheaderFalseMatches (target 0) with recall held at 1.0
result: PASS (precision 0.75->1.0, doubleheader false matches 1->0, recall held 1.0)
---

## Hypothesis

**Original question:** resolve #231/#237 (cross-venue fixture-identity ambiguity) with measured evidence and extend cross-venue matching quality.

**Testable claim:** The cross-venue hedge-match path asserts a match when both instruments carry start times (`market_matcher._hedge_event_match_decision`, the `matched=True` "cross_venue_fixture_proof" branch) **without any set-level ambiguity check**. So a doubleheader — the same teams playing twice the same day, both games within the 12h cross-venue soft start-time tolerance of a single opposing-venue fixture — produces a false hedge match: the target resolves as `same_fixture` against *both* source-venue games. Adding a set-level cluster-count guard on that branch (the deferred #231/#237 fix) eliminates the false match without reducing recall on genuine cross-venue fixtures.

| Component | Value |
| --- | --- |
| Independent variable | the set-level ambiguity guard on the both-start-times cross-venue branch |
| Dependent variable | precision (esp. doubleheader false matches) and recall on a labeled corpus |
| Success threshold | `doubleheaderFalseMatches` → 0, recall stays 1.0, all fixture-identity + market-matcher unit suites green |

## Discovery

- **Match entrypoint (pure):** `MarketMatcher.explain_hedge_event_match(instrument, candidate, candidates) -> dict` (market_matcher.py:372) exposes `matched`/`reason`/`ambiguous`/`confidence`. Wraps `_hedge_event_match_decision` (392).
- **The unguarded branch:** `_hedge_event_match_decision` line 478 — `if instrument.parsed_start_time() and candidate.parsed_start_time(): return matched=True` with no candidate-pool ambiguity check. The missing-start-time branch (485) *does* call `_has_ambiguous_missing_fixture_evidence`; the start-time-conflict branch (511) calls `_is_cross_venue_unique_start_time_conflict`. The both-times branch is the gap.
- **Set-level ambiguity helpers:** `_has_ambiguous_missing_fixture_evidence` (527) and `_fixture_cluster_count` (585) count distinct fixture clusters on the source venue that resolve against the target. `proof.ambiguous` (fixture_identity.py:53) is never set (dead — #237); the guards at market_matcher.py:464 and 639 that read it never fire.
- **Resolver:** `FixtureIdentityResolver.resolve(a, b)` (fixture_identity.py:477); cross-venue soft tolerance `DEFAULT_SOFT_CROSS_VENUE_START_TIME_TOLERANCE_SECS = 12h`.
- **Test infra:** `uv run pytest tests/unit/adapters/test_fixture_identity.py tests/unit/adapters/test_market_matcher.py`; instrument builder pattern from test_fixture_identity.py `_instrument()`.

## Verification Stack

| Layer | Command | Purpose |
| --- | --- | --- |
| Baseline (regression) | `uv run pytest tests/unit/adapters/test_fixture_identity.py tests/unit/adapters/test_market_matcher.py -q` | did the change break existing matching? |
| Hypothesis (measurement) | `uv run python scripts/betting/fixture_match_benchmark.py --out <artifact>` | recall/precision + doubleheaderFalseMatches on the labeled corpus |
| Diff | compare benchmark JSON before/after against the Phase-0 baseline snapshot | metric delta |

Runs on the EC2 build host (compiled `CryptoBettingInstrument` required); orchestrated from the workstation.

## Abort Conditions

| Signal | Threshold | Status |
| --- | --- | --- |
| Max iterations | 6 | 0 |
| Time budget | 45 min | — |
| Convergence | 2 × <5% on primary metric | — |
| Consecutive baseline failures | 2 | 0 |
| Custom: recall regresses below baseline | any drop | — |

Checkpoint mode: pause after iteration 1 for `continue` / `pivot` / `stop`.

## Baseline

Commit `46e375f2e1d8`, measured on the EC2 build host.

| Metric | Value |
| --- | --- |
| totalScenarios | 6 |
| recall | 1.0 |
| precision | **0.75** |
| falsePositives | 1 |
| doubleheaderFalseMatches | **1** |
| correctScenarios | 5 / 6 |
| regression suites (fixture_identity + market_matcher) | 66 passed |

The `doubleheader_both_times` scenario matched (`matched=True`, reason `cross_venue_fixture_proof`, `ambiguous=false`, confidence 0.99) when it should not — the bug reproduces exactly.

## Iteration 1

### Hypothesis

**Claim:** Calling the existing set-level ambiguity check (`_has_ambiguous_missing_fixture_evidence`, which already counts start-time-aware fixture clusters) on the both-start-times cross-venue branch eliminates the doubleheader false match with no recall loss.
**What changed:** `market_matcher._hedge_event_match_decision` — the `matched=True` "cross_venue_fixture_proof" branch now returns `matched=False, reason="ambiguous_fixture"` with `replace(proof, ambiguous=True)` when the target is ambiguous across ≥2 source-venue start-time clusters.
**Should improve:** precision / doubleheaderFalseMatches. **Should NOT regress:** recall, unit suites.

### Design

Single variable: the ambiguity guard on the both-times branch (market_matcher.py). No change to the resolver, the missing-time branch, or the start-time-conflict branch. `proof.ambiguous` is now populated on this path (resolving the #237 dead-guard on the branch that actually needed it).

### Execution

- Applied to `nautilus_trader/adapters/betting/market_matcher.py` (+15 −5). Added regression test `test_match_events_cross_venue_skips_same_day_doubleheader_with_start_times`.
- Baseline verification: ✅ 67 passed (66 prior + new test).

### Verification

**Command:** `fixture_match_benchmark.py` + `pytest test_fixture_identity.py test_market_matcher.py`
**Runs:** deterministic (no timing variance)

| Metric | Before | After | Delta | Meets threshold? |
|--------|--------|-------|-------|-----------------|
| recall | 1.0 | 1.0 | 0 | ✅ (held) |
| precision | 0.75 | 1.0 | +0.25 | ✅ |
| doubleheaderFalseMatches | 1 | 0 | −1 | ✅ (target 0) |
| correctScenarios | 5/6 | 6/6 | +1 | ✅ |
| regression suites | 66 | 67 | +1 test | ✅ green |

**Verdict:** PASS

### Analysis

**Decision:** KEEP. **Reasoning:** primary metric hit target (0 doubleheader false matches), precision +0.25, recall held at 1.0, zero regressions, and the change resolves #231 (start-time-aware cluster count now consulted on the both-times branch) and the live half of #237 (`proof.ambiguous` populated where it governs a real match decision).
**Abort conditions:** none fired; success threshold met on iteration 1.

## Conclusion

**Summary:** The cross-venue hedge path asserted a match whenever both legs carried start times, skipping the set-level ambiguity check that the missing-time and start-time-conflict branches already ran. A same-day doubleheader within the 12h cross-venue soft tolerance therefore produced a false hedge match. Consulting the existing start-time-aware cluster count on that branch (and flagging `proof.ambiguous`) closes the gap.

**Accumulated effect:** precision 0.75 → 1.0; doubleheader false matches 1 → 0; recall unchanged at 1.0; 66 → 67 unit tests, all green.

**Recommendation:** Graduate to a PR fixing #231 + #237 (squash). The benchmark harness (`scripts/betting/fixture_match_benchmark.py`) ships as a reusable fixture-match precision/recall gate.

**Lessons:** The ambiguity machinery already existed and was correct (hour-precision cluster keys); the defect was purely that one of three decision branches never invoked it. Reading the code before iterating (Discovery) prevented a wasted "wire proof.ambiguous everywhere" iteration that would have added inert code.
