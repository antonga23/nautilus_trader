#!/usr/bin/env python3
"""
Experiment: probe-payload-cost.

Benchmarks the write/parse cost of the betting-arbitrage runner's status.json
(``_write_json`` -> ``json.dumps(payload, indent=2)``) against the REAL
1.75MB multivenue fixture, and evaluates a trimmed variant.

Grounding note (deviation from the original hypothesis): the task brief named
coverageDiagnostics.sampleHyperedges/sampleProofs, semanticDiagnostics
.unsupportedProviderPatternSamples, candidateQuality arrays, and venueCoverage
lists as the heavy, already-capped-at-N sample arrays to try capping to N=10.
Reading the real source (runner.py) shows those are *already* capped at 10
(`_coverage_sample_hyperedges(..., limit=10)`, `proof_payloads[:10]`,
`_unsupported_provider_patterns(..., limit=10)`). Measuring the real fixture
top-down shows the actual dominant cost is two *uncapped* full lists inside
semanticDiagnostics that are missing the same capping pattern applied
everywhere else in the file:

    semanticDiagnostics.sameVenueEligibleTemplates  (1008 entries, 717,219 bytes)
    semanticDiagnostics.executionSafeTemplates       (259 entries, 178,487 bytes)

Together these two fields are 895,706 bytes -- 51.1% of the 1,753,741-byte
fixture -- and neither of the two required readers
(scripts/betting/runtime_probe_report.py summarize_payload,
tools/nodeops/server.py build_sample_row) reads either key (verified by
grep + by running both readers' real extraction code against the trimmed
payload below and diffing outputs). This experiment adapts the plan to cap
those two fields instead, using the same style already used for their sibling
sample fields elsewhere in the same file.
"""

from __future__ import annotations

import gzip
import json
import statistics
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = Path(
    "/private/tmp/claude-501/-Users-alatha-ntonga-pencil/"
    "f0128054-1bfc-4af4-96db-c2f1eef7a228/scratchpad/fixtures-status-multivenue.json",
)
ARTIFACT_PATH = Path(
    "/private/tmp/claude-501/-Users-alatha-ntonga-pencil/"
    "f0128054-1bfc-4af4-96db-c2f1eef7a228/scratchpad/exp-artifacts/exp4_probe-payload-cost.json",
)

REPEATS = 9
CAP_N = 10
TEMPLATE_FIELDS = ("executionSafeTemplates", "sameVenueEligibleTemplates")

sys.path.insert(0, str(REPO_ROOT / "scripts" / "betting"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "nodeops"))
import runtime_probe_report as rpr  # noqa: E402
import server as nodeops  # noqa: E402


def load_fixture() -> tuple[dict, int]:
    with FIXTURE_PATH.open("rb") as f:
        raw = f.read()
    return json.loads(raw), len(raw)


def make_trimmed(payload: dict, *, cap: int) -> dict:
    """
    Mirror the existing `[:10]` / `limit=10` sample-capping pattern already used for
    sibling fields in runner.py (_coverage_sample_hyperedges, proof_payloads[:10],
    _unsupported_provider_patterns limit=10) and apply it to the two fields that are
    missing it.
    """
    trimmed = deepcopy(payload)
    semantic_diagnostics = trimmed.get("runtimeProbe", {}).get("semanticDiagnostics", {})
    for key in TEMPLATE_FIELDS:
        value = semantic_diagnostics.get(key)
        if isinstance(value, list) and len(value) > cap:
            semantic_diagnostics[key] = value[:cap]
    return trimmed


def encode_baseline(payload: dict) -> bytes:
    # Mirrors runner.py `_write_json`: json.dumps(payload, indent=2) + "\n"
    return (json.dumps(payload, indent=2) + "\n").encode("utf8")


def encode_compact(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf8")


def round_trip_time(encode_fn, payload: dict) -> tuple[float, int]:
    t0 = time.perf_counter()
    blob = encode_fn(payload)
    _ = json.loads(blob)
    t2 = time.perf_counter()
    return (t2 - t0), len(blob)


def timed_repeats(encode_fn, payload: dict, repeats: int) -> tuple[list[float], int]:
    times = []
    size = 0
    for _ in range(repeats):
        dt, size = round_trip_time(encode_fn, payload)
        times.append(dt)
    return times, size


def median_variance(samples: list[float]) -> tuple[float, float]:
    med = statistics.median(samples)
    var = statistics.pvariance(samples) if len(samples) > 1 else 0.0
    return med, var


def call_runtime_probe_report(payload: dict) -> dict:
    return rpr.summarize_payload(payload, top_limit=5)


def call_nodeops_sample_row(payload: dict) -> dict:
    now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
    heartbeat = {"at": "2026-07-10T11:59:30Z"}
    inspect = {"state": "running", "started_at": "2026-07-10T00:00:00Z"}
    stats = {"mem_mb": 512.0, "cpu_pct": 12.5}
    return nodeops.build_sample_row("multivenue-1", payload, heartbeat, inspect, stats, now)


def main() -> None:
    payload, fixture_bytes_on_disk = load_fixture()
    assert fixture_bytes_on_disk == 1_753_741, (
        f"expected the documented 1,753,741-byte fixture, got {fixture_bytes_on_disk}"
    )

    semantic_diagnostics = payload["runtimeProbe"]["semanticDiagnostics"]
    field_lengths = {k: len(semantic_diagnostics.get(k) or []) for k in TEMPLATE_FIELDS}
    print("Real uncapped field lengths:", field_lengths)

    # Confirm already-capped sample fields named in the original brief really
    # are already at N=10 (grounding check; documents the plan deviation).
    coverage_diag = payload["runtimeProbe"]["coverageDiagnostics"]
    already_capped = {
        "coverageDiagnostics.sampleHyperedges": len(coverage_diag.get("sampleHyperedges") or []),
        "coverageDiagnostics.sampleProofs": len(coverage_diag.get("sampleProofs") or []),
        "semanticDiagnostics.unsupportedProviderPatternSamples": len(
            semantic_diagnostics.get("unsupportedProviderPatternSamples") or [],
        ),
    }
    print("Already-capped sample fields (grounding check):", already_capped)

    trimmed_payload = make_trimmed(payload, cap=CAP_N)

    # ---- Correctness: both real readers must extract identical values ----
    baseline_report = call_runtime_probe_report(payload)
    trimmed_report = call_runtime_probe_report(trimmed_payload)
    report_identical = baseline_report == trimmed_report

    baseline_row = call_nodeops_sample_row(payload)
    trimmed_row = call_nodeops_sample_row(trimmed_payload)
    row_identical = baseline_row == trimmed_row

    correctness_ok = report_identical and row_identical
    print(f"runtime_probe_report.summarize_payload identical: {report_identical}")
    print(f"nodeops.build_sample_row identical: {row_identical}")
    if not report_identical:
        for k in baseline_report:
            if baseline_report.get(k) != trimmed_report.get(k):
                print("  DIFF in report key:", k)
    if not row_identical:
        for k in baseline_row:
            if baseline_row.get(k) != trimmed_row.get(k):
                print("  DIFF in row key:", k)

    # ---- Performance: baseline vs compact vs compact+capped ----
    baseline_times, baseline_size = timed_repeats(encode_baseline, payload, REPEATS)
    compact_times, compact_size = timed_repeats(encode_compact, payload, REPEATS)
    capped_times, capped_size = timed_repeats(encode_compact, trimmed_payload, REPEATS)

    baseline_med, baseline_var = median_variance(baseline_times)
    compact_med, compact_var = median_variance(compact_times)
    capped_med, capped_var = median_variance(capped_times)

    gzip_baseline_size = len(gzip.compress(encode_baseline(payload), compresslevel=6))
    gzip_capped_size = len(gzip.compress(encode_compact(trimmed_payload), compresslevel=6))

    size_reduction = 1 - (capped_size / baseline_size)
    time_reduction = 1 - (capped_med / baseline_med)
    combined_reduction = 1 - ((capped_size / baseline_size) * (capped_med / baseline_med)) ** 0.5
    # also report the simple average of the two reductions, since "combined"
    # is ambiguous; use the more conservative (smaller) of the two framings
    # for the pass/fail call.
    avg_reduction = (size_reduction + time_reduction) / 2
    conservative_combined = min(combined_reduction, avg_reduction)

    threshold = 0.60
    verdict = "PASS" if (conservative_combined >= threshold and correctness_ok) else "FAIL"

    result = {
        "slug": "probe-payload-cost",
        "fixture_path": str(FIXTURE_PATH),
        "fixture_bytes": fixture_bytes_on_disk,
        "repeats": REPEATS,
        "cap_n": CAP_N,
        "grounding": {
            "already_capped_sample_fields": already_capped,
            "uncapped_dominant_fields": field_lengths,
            "uncapped_dominant_fields_bytes": {
                k: len(json.dumps(semantic_diagnostics.get(k))) for k in TEMPLATE_FIELDS
            },
        },
        "correctness": {
            "runtime_probe_report_identical": report_identical,
            "nodeops_build_sample_row_identical": row_identical,
            "overall_ok": correctness_ok,
        },
        "sizes_bytes": {
            "baseline_indent2": baseline_size,
            "compact_no_trim": compact_size,
            "compact_trimmed_capN": capped_size,
            "gzip_baseline_indent2": gzip_baseline_size,
            "gzip_compact_trimmed_capN": gzip_capped_size,
        },
        "round_trip_seconds": {
            "baseline_indent2": {
                "median": baseline_med,
                "variance": baseline_var,
                "samples": baseline_times,
            },
            "compact_no_trim": {
                "median": compact_med,
                "variance": compact_var,
                "samples": compact_times,
            },
            "compact_trimmed_capN": {
                "median": capped_med,
                "variance": capped_var,
                "samples": capped_times,
            },
        },
        "reductions": {
            "size_reduction_fraction": size_reduction,
            "time_reduction_fraction": time_reduction,
            "combined_geometric_reduction_fraction": combined_reduction,
            "combined_average_reduction_fraction": avg_reduction,
            "conservative_combined_used_for_verdict": conservative_combined,
        },
        "threshold": threshold,
        "verdict": verdict,
    }

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(result, indent=2))

    print()
    print("=== SUMMARY ===")
    print(
        f"baseline (indent=2, full):        {baseline_size:>10} bytes, median {baseline_med * 1000:.2f} ms",
    )
    print(
        f"compact (no trim):                 {compact_size:>10} bytes, median {compact_med * 1000:.2f} ms",
    )
    print(
        f"compact + capped(N={CAP_N}) templates:  {capped_size:>10} bytes, median {capped_med * 1000:.2f} ms",
    )
    print(f"gzip baseline:                      {gzip_baseline_size:>10} bytes")
    print(f"gzip compact+capped:                {gzip_capped_size:>10} bytes")
    print(f"size reduction:      {size_reduction * 100:.1f}%")
    print(f"time reduction:      {time_reduction * 100:.1f}%")
    print(f"combined (geo mean): {combined_reduction * 100:.1f}%")
    print(f"combined (avg):      {avg_reduction * 100:.1f}%")
    print(f"correctness ok:      {correctness_ok}")
    print(f"VERDICT: {verdict}")
    print(f"Artifact written to {ARTIFACT_PATH}")


if __name__ == "__main__":
    main()
