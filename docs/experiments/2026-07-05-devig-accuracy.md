---
experiment: devig-accuracy
date: 2026-07-05
mode: checkpoint
status: complete
branch: experiment/2026-07-05-devig-accuracy
baseline_commit: b4bd5de8dd78
primary_metric: meanMAE of recovered fair probabilities (lower is better)
result: FINDING — proportional best 9/9 regimes; auto (default) 3rd due to logarithmic over-routing; validate on real data before changing default
---

## Hypothesis

**Question:** which devig method (`proportional`, `shin`, `logarithmic`, or the default `auto`) most accurately recovers fair probabilities from overround-inflated odds, and is the shipped default (`auto`) optimal?

**Testable claim:** across a grid of 2-way and 3-way markets whose quoted odds are built from known fair probabilities with a **favorite-longshot-biased** margin (a real vig structure that matches no single method's assumption), the methods produce measurably different recovery error (`meanMAE`), and the default `auto` is at least as accurate as the best fixed method.

| Component | Value |
| --- | --- |
| Independent variable | devig method passed to `devig_probabilities` |
| Dependent variable | `meanMAE` (and worst-case) of `no_vig_probabilities` vs the true fair probs; sum-to-1 error; convergence failures |
| Success threshold | rank methods by `meanMAE`; if a fixed method beats `auto` by a material margin, recommend adjusting `auto`'s regime selection or the default |

## Discovery

- `devig_probabilities(odds, *, method="auto") -> DeviggedBook` at `nautilus_trader/adapters/betting/common/odds.py:135`. Methods: `proportional` (linear scale), `shin` (insider-parameter, analytic 2-way / iterative multi-way), `logarithmic` (power exponent via binary search), `auto` (regime selector: underround→proportional, extreme odds→logarithmic, balanced 2-way→proportional, else→shin).
- `DeviggedBook.no_vig_probabilities` (sum to 1) is the recovered fair vector; `.convergence_status` ∈ {not_required, analytic, converged, failed}.
- Config default `devig_method="auto"` (betting_arbitrage.py); feeds `fee_adjusted_coverage_basket` + value-edge diagnostics (min_value_edge).
- Module is pure-Python but the package import pulls the compiled extension, so the benchmark runs on the EC2 build host.

## Verification Stack

| Layer | Command | Purpose |
| --- | --- | --- |
| Baseline (regression) | `uv run pytest tests/unit/adapters/test_odds_utilities.py -q` | devig unit behavior intact |
| Hypothesis (measurement) | `uv run python scripts/betting/devig_accuracy_benchmark.py --out <artifact>` | per-method meanMAE / robustness |
| Diff | compare per-method JSON against baseline `auto` | which method wins, by how much |

## Abort Conditions

| Signal | Threshold | Status |
| --- | --- | --- |
| Max iterations | 5 | 0 |
| Time budget | 40 min | — |
| Convergence | 2 × <5% meanMAE improvement | — |
| Consecutive failures | 2 | 0 |

Checkpoint mode: pause after the baseline comparison for `continue` / `pivot` / `stop`.

## Baseline (per-method, favorite_longshot base=0.06 skew=0.8)

| Method | meanMAE | worstMAE | rank |
| --- | --- | --- | --- |
| **proportional** | **0.00224** | 0.00429 | 1 |
| shin | 0.00895 | 0.02400 | 2 |
| auto (default) | 0.01109 | 0.04539 | 3 |
| logarithmic | 0.01588 | 0.04539 | 4 |

All methods: `maxSumToOneError = 0`, `convergenceFailures = 0` (robust; the difference is pure accuracy). The default `auto` is ~5× less accurate than `proportional` here.

## Iteration 1 — margin-regime robustness sweep

Swept `base_margin ∈ {0.03, 0.06, 0.12}` × `skew ∈ {0.0, 0.8, 2.0}` (9 regimes). meanMAE per method:

| base | skew | auto | proportional | shin | logarithmic | best |
| --- | --- | --- | --- | --- | --- | --- |
| 0.03 | 0.0 | 0.00501 | 0.00000 | 0.00441 | 0.00672 | proportional |
| 0.03 | 0.8 | 0.00525 | 0.00116 | 0.00448 | 0.00748 | proportional |
| 0.03 | 2.0 | 0.00564 | 0.00286 | 0.00458 | 0.00867 | proportional |
| 0.06 | 0.0 | 0.01062 | 0.00000 | 0.00882 | 0.01407 | proportional |
| 0.06 | 0.8 | 0.01109 | 0.00224 | 0.00895 | 0.01588 | proportional |
| 0.06 | 2.0 | 0.01184 | 0.00544 | 0.00914 | 0.01886 | proportional |
| 0.12 | 0.0 | 0.01964 | 0.00026 | 0.01618 | 0.02805 | proportional |
| 0.12 | 0.8 | 0.02361 | 0.00452 | 0.01600 | 0.03198 | proportional |
| 0.12 | 2.0 | 0.02574 | 0.01043 | 0.01572 | 0.03904 | proportional |

**Verdict:** PASS (robust). `proportional` wins **9/9**; ranking `proportional < shin < auto < logarithmic` holds in every regime. `auto` is worse than *both* fixed alternatives everywhere.

## Conclusion

**Root cause of `auto`'s underperformance:** its regime selector routes any book with an outcome near odds `1.10` or `10.0` to `logarithmic` — but this grid's mild favorites (p≈0.90 → odds≈1.11) and 3-way longshots (p≈0.10 → odds≈10) trip that boundary, and `logarithmic` is the **worst** method in all 9 regimes. So `auto` frequently selects its least-accurate branch for ordinary books.

**Robust (model-independent) finding:** `logarithmic` is consistently the least accurate of the four here, and `auto` over-triggers it.

**Model-dependent finding (caveat):** `proportional` ranks first, but the ground truth is a *synthetic* favorite-longshot model; its near-uniform vig at low skew favors `proportional`, and real books may carry structure that favors `shin`. Recovery accuracy against **real closing-line-vs-outcome frequencies** is required before treating "proportional is best" as definitive.

**Recommendation:**
1. Narrow `auto`'s `logarithmic` trigger (raise the odds thresholds so only genuinely extreme books use it) and prefer `proportional`/`shin` for normal books — a plausibly-safe accuracy win.
2. **Do not change the production devig default on synthetic evidence alone** (it is a money-path input to value-edge detection). Validate a candidate `auto` policy against real market data first.
3. Ship `scripts/betting/devig_accuracy_benchmark.py` as a reusable devig-accuracy gate for that validation.

**Lessons:** deliberately choosing a method-neutral (favorite-longshot) generator kept the comparison honest and surfaced that `auto`'s regime boundaries — not any single method's math — are the weak point. The correct output is a *finding + validation plan*, not a blind default flip.
