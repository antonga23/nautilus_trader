---
experiment: live-pain-point-sweep
date: 2026-07-10
mode: autonomous
status: complete
baseline: develop @ 7b9d9b6993
primary-metric: per-experiment (throughput, latency, coverage, reliability)
result: 9 PASS / 1 FAIL across 10 experiments; 9 fixes landed as PRs #270-#278
---

# Live pain-point sweep — 10 experiments against production diagnostics

Every hypothesis was sourced from measured numbers in the multi-venue node's live
runtime probe (captured 2026-07-10), not speculation. Each experiment ran as a
self-contained offline benchmark (baseline and variant in one process, seeded
inputs, no live network) and every PASS was re-run and adversarially verified by
an independent reviewer agent before landing.

## Scoreboard

| # | Experiment | Live symptom | Verdict | Result | Landed |
|---|---|---|---|---|---|
| 1 | Probe payload cost | 1.75MB status.json written every ~5min | PASS | −75.3% size, −76.1% round-trip (cap attribution honest; indent kept) | #270 |
| 2 | Subscription churn | 19,734 adds vs 1,074 removes in ~2 days | PASS | −99.2% spurious add ops at headline severity; 0 missed new markets | #271 |
| 3 | SXBET cycle watchdog | one poll cycle ran 35.5h (max_fetch 68,475s) | PASS | unbounded wedge → bounded ≤2× target cycle, zero healthy-market loss | #272 |
| 4 | Probe assembly cost | 54,271 proofs iterated per probe cycle | PASS | −68.0% assembly wall-time from one index hoist; payload byte-identical | #273 |
| 5 | Quote coalescing | pilot p50 quote→strategy 7.4min vs 5s SLO | PASS | −99.8% delivered-tick age p50/p95 under 5× backpressure; zero newest-tick loss | #274 |
| 6 | Settlement inference | 39,542 proofs blocked unknown_settlement; exec-safe=0 | PASS | CORRECT_SCORE unknowns −80%; 6 independent ambiguous cases stay blocked | #275 |
| 7 | Cloudbet line-fallback fanout | 56/64 requests per cycle are structurally-absent fallbacks | PASS | honest −58–87% requests/cycle (cadence-dependent); identical published ticks | #276 |
| 8 | Corpus fetch concurrency | fresh semantic mine fetch-bound (~35min) | PASS | −83.1% fetch wall-time at concurrency 8 (−91.1% at 16); output byte-identical | #277 |
| 9 | Semantic pattern coverage | coverage ratio 0.41; 880 nodes unsupported via duplicate params | FAIL* | +3.15pts (below the ≥10pt bar) — but the fix is real, zero false-support | #278 |
| 10 | Cloudbet poll concurrency sweep | 16/16 concurrency, cycle 6.38s vs 4.0s target | PASS | −38.6% cycle at concurrency 48 (characterization) | config rec only |

\* Graded FAIL strictly against the ambitious ≥10pt threshold. The duplicate-key
normalization defect is systemic (fires on 100% of a 72-row synthetic sweep
across 4 sports) and the measurable-from-fixture portion alone recovers 880
nodes with zero false support, so the fix landed with the honest +3.15pt claim.

## Verification rigor highlights

- The line-fallback experiment's first claim (87.5% request cut) was **refuted
  on re-run**: the variant's revalidation counter incremented per event-group
  fetch, so revalidation fired every ~2.5 poll cycles instead of every 20,
  making the mean 19.2 req/cycle rather than the reported median 8. The
  benchmark was corrected and re-verified; the landed change (#276) counts
  revalidation per poll cycle and the PR claims the honest range.
- The settlement-inference verifier independently constructed six ambiguous
  selections (Abandoned, Cancelled, Void, 3+, 1-0-or-2-0, Postponed) beyond the
  benchmark's own adversarial cases; all correctly remain unpromoted.
- The probe-payload claim was re-attributed: the original 85.8% figure bundled a
  separator change the fix does not ship; the landed cap-only change measures
  75.3%/76.1% and the PR claims only that.

## Not landed

- **Poll concurrency 16→48** (−38.6% cycle on a latency-matched mock): config
  recommendation only. Raising live concurrency 3× needs a rate-limit (429)
  check against the real Cloudbet API first.
- **WINNER/CORRECT_SCORE unsupported patterns** (560 of the top-10 unsupported
  nodes): confirmed a genuine corpus/mining gap, not a normalization bug — no
  template was ever mined for those pattern keys. A corpus re-mine, not a code
  change, is the fix.

## Incidental live findings

- The polymarket-sxbet pilot's SXBET poller sat wedged at cycle_id 3247 for
  ~54h during this sweep — a second live instance of the hang class #272
  eliminates.
- The multi-venue node (pre-#265 image) reports `subscribed_but_no_quotes,
  health: fail` with 120 subscriptions — the mirror-desync bug fixed on develop;
  it clears when the node next redeploys.
