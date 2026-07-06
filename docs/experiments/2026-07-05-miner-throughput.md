---
experiment: miner-throughput
date: 2026-07-05
mode: checkpoint
status: complete
branch: experiment/2026-07-05-miner-throughput
baseline_commit: a4094843e0
primary_metric: FileRuleCache write throughput (records/sec) during a bulk mine
result: PASS (84.2x write throughput, content identical, tests green)
---

## Hypothesis

**Question:** the fresh semantic mine takes ~35 min; how much of that is the storage hot path, and can it be cut without changing the mined output?

**Testable claim:** `FileRuleCache._atomic_write_bytes` calls `os.fsync` on **every** record, and a mine persists thousands (normalized selections + candidates + templates + coverage proofs). On EBS the per-record fsync dominates the write path. Deferring per-record fsync to a single directory fsync at the end of a bulk rebuild (`bulk_writes()`) raises write throughput by ~2 orders of magnitude while leaving byte-identical cache content.

| Component | Value |
| --- | --- |
| Independent variable | per-record fsync (on) vs deferred-to-one-dir-fsync (`bulk_writes()`) |
| Dependent variable | write throughput (records/sec); cache content must be identical |
| Success threshold | ≥10× write throughput, content byte-identical, store/miner unit suites green |

## Discovery

- `FileRuleCache.add` → `_atomic_write_bytes` (store.py) does `mkstemp` → write → **`os.fsync`** → `os.replace` per record.
- `defer_index_writes` (already used by the bootstrap) batches only the `keys.json` index, **not** the per-record `.bin` fsync — so the hot path was un-optimised.
- Bootstrap `_bootstrap_semantic_cache` (semantic_cache.py) mines under `defer_index_writes`; a fresh mine was observed writing 5000+ `.bin` files.

**Microbenchmark (EC2 EBS, 5000 atomic writes of 900B, baseline evidence):**

| path | time | throughput |
| --- | --- | --- |
| fsync per write | 19.42s | 257/s |
| deferred (one dir fsync) | 0.24s | 20,930/s |

→ ~**80×**. Confirms per-record fsync is the dominant, eliminable write cost.

## Iteration 1 — `FileRuleCache.bulk_writes()`

**Change:** added a `bulk_writes()` context to `FileRuleCache` (and a delegating `RuleStore.bulk_writes()`), entered by the mine bootstrap alongside `defer_index_writes`. Inside it, `_atomic_write_bytes` skips the per-record `os.fsync`; on exit it flushes the key index and fsyncs the directory once. `os.replace` still makes each file atomically visible, and the cache is a derived artifact (crash mid-mine → re-mine), so per-record durability is unnecessary.

**Verification:** `scripts/betting/rulecache_write_benchmark.py` (normal vs bulk on the real `FileRuleCache`, content-identity check) + `test_file_rule_cache_bulk_writes_defers_fsync_and_preserves_content` (counts fsyncs, asserts identical content) + the existing store/miner suites.

## Verification Stack

| Layer | Command |
| --- | --- |
| Baseline (regression) | `uv run pytest tests/unit/adapters/test_betting_semantics_miner.py tests/unit/strategies/test_opportunity_graph.py -q` |
| Hypothesis (measurement) | `uv run python scripts/betting/rulecache_write_benchmark.py --out <artifact>` |

## Abort Conditions

| Signal | Threshold | Status |
| --- | --- | --- |
| Max iterations | 4 | 0 |
| Time budget | 40 min | — |
| speedup < 10× | abort/rethink | — |
| content NOT identical | hard fail | — |

## Result — PASS

`FileRuleCache` benchmark (real class, EC2 EBS, 5000 records):

| path | time | throughput |
| --- | --- | --- |
| normal (fsync/record) | 20.83s | 240/s |
| `bulk_writes()` | 0.247s | 20,224/s |

**Speedup ~84.2×**, `contentIdentical: true`. Miner/store suite: 11 passed incl. the new fsync/content test.

**Conclusion:** the mine's per-record fsync was the dominant storage cost; `bulk_writes()` removes it safely (atomic visibility preserved, one dir fsync at end, derived-cache semantics). Directly reduces the fresh-mine wall time behind PR #246's `fresh` default. KEPT.

**Next directions (not run — checkpoint):** profile the mining CPU stages (`mine_store`/`mine_templates`/`mine_coverage`) and node warmup/instrument-fetch separately; these need a fixed captured corpus to be reproducible.
