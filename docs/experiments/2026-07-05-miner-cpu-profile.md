---
experiment: miner-cpu-profile
date: 2026-07-05
mode: checkpoint
status: complete
branch: experiment/2026-07-05-miner-cpu-profile
baseline_commit: 402ef1eff5
primary_metric: per-stage mining wall-time / throughput (records/sec)
result: FINDING — mining CPU is not the bottleneck (~14s/20k records); storage (fixed by #252) + corpus fetch dominate
---

## Hypothesis

**Question:** after the storage fsync fix (#252, 84× write throughput), where does the fresh mine's remaining time go — mining CPU, storage, or corpus fetch — and is any mining stage worth optimizing?

**Testable claim:** with per-record fsync removed, the mining *compute* stages (`mine_event_candidates` → `mine_templates` → `mine_coverage`) are not the bottleneck; the mine's wall time is dominated by corpus fetch (network) and the now-fixed storage I/O, so no mining-algorithm optimization is warranted.

| Component | Value |
| --- | --- |
| Independent variable | mining stage (measured separately) |
| Dependent variable | per-stage median wall-time / throughput (records/sec) on a fixed synthetic corpus |
| Success threshold | attribute the cost; recommend optimization only if a compute stage dominates |

## Discovery / method

Deterministic seeded synthetic corpus (no live fetch, reproducible): 20,000 `NormalizedSelectionRecord`, 2,500 fixtures, 3 sports (soccer 3-way MATCH_ODDS; basketball/baseball 2-way WINNER exercising the WINNER×±0.5 spread projection), per-venue cutoff skew 0/20/40 min (inside the 2h cluster tolerance), multiple market families + complementary pairs. Each stage timed with `perf_counter`, median of 3 repeats; the largest stage profiled with cProfile. Store population uses `bulk_writes()` (post-#252) so storage doesn't pollute the compute numbers. `scripts/betting/miner_cpu_benchmark.py`.

## Result — FINDING

Store population (`bulk_writes`): **10.44s** for 20k records (was 93.7s with per-record fsync pre-#252 — the fix in situ). Per-stage mining:

| stage | median | throughput |
| --- | --- | --- |
| mine_event_candidates | 2.12s | 9,416 rec/s |
| mine_templates | 2.97s | 6,724 rec/s |
| store_load_records | 0.75s | 26,695 rec/s |
| mine_store | 2.96s | 6,768 rec/s |
| mine_templates_from_store | 3.73s | 5,357 rec/s |
| mine_coverage_from_store | 1.71s | 11,664 rec/s |

Total mining compute ≈ **14s for 20k records**. cProfile of the largest stage (`mine_templates_from_store`) shows no single dominating hotspot — the largest leaves are JSON ser/deser (`types:from_rule` 40k calls, `__init__:dumps` / `encoder:encode` ~280k calls) and `_with_evidence` — all cheap in absolute terms.

**Conclusion:** mining CPU is **not** the bottleneck. Of the original ~35-min fresh mine: storage was the dominant local cost and is fixed by #252 (`bulk_writes`, 93.7s→10.4s here); mining compute is ~14s/20k; the remaining bulk is **live corpus fetch** (venue-API/network-bound, not addressed here). **No mining-algorithm optimization is warranted** — the profile explicitly rules it out. If the mine must be faster still, the lever is fetch concurrency/caching, not the miner. KEPT as tooling + finding (no production code change).

## Abort Conditions

| Signal | Threshold | Status |
| --- | --- | --- |
| Max iterations | 4 | done at baseline profile |
| a compute stage dominates | would trigger an optimization iteration | not triggered |

## Next directions (not run — checkpoint)

- Corpus-fetch profiling (needs live venue access; network/rate-limit bound).
- JSON ser/deser micro-opt (`types.from_rule` / `to_json_bytes`) — measurable but low absolute payoff vs fetch; only worth it at much larger corpora.
