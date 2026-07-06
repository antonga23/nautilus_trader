---
experiment: betting-node-experiment-loop
date: 2026-07-05
mode: autonomous
status: complete (wave 1)
baseline_commit: 225eced818
primary_metric: per-experiment measured delta (accuracy / memory / CPU / throughput / reliability)
result: 12 experiments run + adversarially verified; 3 clean deployable wins, 2 overfit-measurements (→#251), 6 informative negatives, 1 behavior-relevant candidate
---

## Hypothesis

Can we demonstrably improve performance / accuracy / reliability / throughput of a deployed betting-arbitrage
node via offline, deterministic, single-variable experiments — measured against a captured baseline, with each
PASS adversarially re-verified?

## Method

- Baseline env: `cbmm-exp-base` (git-detached at develop `225eced818`), local nautilus_pyo3 build (py3.14),
  hot import ~2s. All benchmarks deterministic (seeded), no live venue/network/settlement data.
- Each experiment: a self-contained benchmark implementing BOTH baseline and variant in one process
  (monkeypatch / mirror / functools) so relative deltas are robust to ambient CPU load and require no source
  edit to measure. ≥5 repeats, median + variance. Every PASS re-run + fairness/overfit/correctness-audited by
  an independent adversarial verifier (opus).
- Hypotheses were grounded against the real code first (an initial blind-path discovery pass produced several
  hallucinated APIs — those were dropped: promotion thresholds do not exist in `classifier.py`; the cloudbet
  `get_event` cache target is commented out at `providers.py:124`).

## Results

| # | Experiment | Area | Verdict | Measured | Deployable? |
|---|---|---|---|---|---|
| 1 | devig-balanced-band | accuracy | PASS | auto meanMAE −25.8% (0.01109→0.00823) | NO — overfit; money-path (#251) |
| 2 | devig-extreme-threshold | accuracy | FAIL | 19.3% possible but the only LOW that achieves it breaks a passing unit-test fixture (single-grid effect) | no |
| 3 | mem-slots-opportunity | memory | FAIL* | per-instance −27% (ArbitrageOpportunity) / −39% (HedgeCandidate); aggregate peak under threshold (50k wrappers share 8 instruments) | YES* — per-instance win is real & safe |
| 4 | mem-intern-instrument-strings | memory | PASS | −67% of the low-cardinality string slice (5k instruments); identity-probed, reproduced 2nd process | YES |
| 5 | graph-markettype-fromstring-cache | CPU | PASS | −92% wall-time on `MarketType.from_string` (real linear scan, called 2×/pair at 4 hot sites) | YES (bounded vocabulary) |
| 6 | graph-bucket-by-event | throughput | FAIL | N/A — production `build()` is already Rust-backed | no (informative negative) |
| 7 | miner-deser-cache | throughput | FAIL | 0% cache hit-rate: stored rules are read once, not re-deserialized (slight regression) | no (informative negative) |
| 8 | miner-json-encode-reuse | throughput | FAIL | reused `JSONEncoder` = +9.7% encode-only (< 15% gate; negligible on total mine) | no |
| 9 | match-cluster-tolerance | accuracy | PASS | F1 +20pts on a synthetic doubleheader corpus (real harness saturated at 1.0); fixes a real same-hour ambiguity | CANDIDATE — behavior-relevant (touches #249) |
| 10 | mem-hedge-candidate-event-cache | memory/CPU | FAIL | −90% latency on the real O(n) `_fixture_bucket_for_pair` rescan BUT +227% memory (naive full-object cache) | RETUNE — lighter index (wave 2) |
| 11 | devig-shin-default-vs-proportional | accuracy | PASS | auto meanMAE −41.8% overall / −70.8% isolated | NO — measurement only; money-path (#251) |
| 12 | graph-build-scaling-profile | throughput | PASS | build scaling exponent k≈1.02 (≈linear); no super-linear hotspot | CHARACTERIZATION |

## Findings

1. **Three clean, safe deployable wins** (#3 reframed, #4, #5): `slots=True` on the arbitrage/hedge
   dataclasses; `sys.intern` on low-cardinality instrument strings; `lru_cache` on `MarketType.from_string`.
   All pure optimizations, correctness holds by construction, verified against the real unit tests.
2. **The devig accuracy "wins" (#1, #11) are overfit by construction** — the synthetic favorite-longshot
   corpus structurally favors `proportional` at all two-way odds, so routing more books to it necessarily
   "improves" the synthetic Brier. This confirms #251's direction but is NOT evidence for live markets;
   the `auto` routing default is a money-path change requiring real closing-line-vs-settlement validation
   (the #253 devig-calibration harness is the mechanism).
3. **Informative negatives** (real signal, prevent wasted effort): the opportunity graph is already Rust-backed
   (#6); mined rules are read once with no re-deserialization to cache (#7); the fixture-match harness is
   already saturated at precision/recall 1.0 (#9); JSON-encoder reuse is a negligible fraction of mine time
   (#8). The node is already well-optimized in the large-cost areas (consistent with #252/#254).
4. **Retune candidate** (#10): the real `_fixture_bucket_for_pair` O(n) rescan is a genuine per-pair hotspot
   (−90% latency available), but the naive cache blew memory +227%; a keys-only / resolve()-memoized index
   should capture the latency win without the footprint — wave 2.
5. **Behavior-relevant candidate** (#9): a half-hour (vs hour) fixture-cluster bucket fixes a real same-hour
   doubleheader ambiguity; needs its own scrutiny since it touches the #249 doubleheader guard.

## Merge recommendation

- LAND: #3 + #4 + #5 as a focused "betting-node micro-optimizations" PR (verified, safe, no behavior change).
- SEPARATE PR + scrutiny: #9 (matching precision; behavior-relevant).
- WAVE 2: retune #10; add a reliability win (status.json inf/NaN sanitization at runner.py:728, failure-injectable).
- DO NOT LAND on synthetic evidence: #1, #11 — route to #251 live validation instead.
