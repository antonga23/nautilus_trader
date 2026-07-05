---
experiment: devig-accuracy
date: 2026-07-05
mode: checkpoint
status: running
branch: experiment/2026-07-05-devig-accuracy
baseline_commit: b4bd5de8dd78
primary_metric: meanMAE of recovered fair probabilities (lower is better)
result: pending
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

## Baseline

_pending — per-method comparison measured next._
