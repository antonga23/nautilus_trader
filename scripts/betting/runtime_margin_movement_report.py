#!/usr/bin/env python3
"""
Compare betting runtime status snapshots for margin movement during soak tests.

The live-pilot soak can pass without a positive arb only when artifacts prove the
node is live: quoted semantic candidates exist, negative margins move over time,
and blockers/latency are explicit. This helper turns a sequence of status.json
snapshots into that operator-facing evidence.

"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


NEAR_MISS_BANDS = ("0% to -1%", "-1% to -2%", "-2% to -5%", "< -5%")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _candidate_total(value: Any) -> int:
    payload = _as_dict(value)
    total = payload.get("total")
    if isinstance(total, int):
        return total
    return sum(_int(item) for item in payload.values())


def _counter_payload(value: Any) -> dict[str, int]:
    return {str(key): _int(item) for key, item in _as_dict(value).items()}


def _venue_pair_candidate_counts(
    runtime: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, int]:
    coverage = _as_dict(runtime.get("venueCoverage"))
    counts = _counter_payload(coverage.get("candidateCounts"))
    if counts:
        return counts
    venue_pairs = _as_dict(quality.get("venuePairs"))
    return {
        str(pair): sum(_int(count) for count in _as_dict(bucket_counts).values())
        for pair, bucket_counts in venue_pairs.items()
    }


def _cross_venue_candidate_count(runtime: dict[str, Any], quality: dict[str, Any]) -> int:
    coverage = _as_dict(runtime.get("venueCoverage"))
    value = coverage.get("crossVenueCandidateCount")
    if isinstance(value, int):
        return value
    return sum(
        count
        for pair, count in _venue_pair_candidate_counts(runtime, quality).items()
        if "->" in pair and pair.split("->", maxsplit=1)[0] != pair.split("->", maxsplit=1)[1]
    )


def _margin_value(candidate: dict[str, Any]) -> float | None:
    for key in (
        "profitMargin",
        "profit_margin",
        "feeAdjustedMargin",
        "fee_adjusted_margin",
        "netEdge",
        "net_edge",
        "margin",
    ):
        value = candidate.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.rstrip("%")) / (100.0 if value.endswith("%") else 1.0)
            except ValueError:
                continue
    return None


def _snapshot_summary(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    runtime = _as_dict(payload.get("runtimeProbe"))
    quality = _as_dict(runtime.get("candidateQuality"))
    coverage = _as_dict(runtime.get("venueCoverage"))
    horizon = _as_dict(runtime.get("resolutionHorizon"))
    margin_bands = _as_dict(quality.get("marginBands"))
    near_misses = [
        item for item in quality.get("topNegativeNearMisses") or [] if isinstance(item, dict)
    ]
    near_miss_margins = [
        margin for item in near_misses if (margin := _margin_value(item)) is not None
    ]
    return {
        "source": source,
        "nodeId": payload.get("nodeId"),
        "timestamp": payload.get("updatedAt")
        or payload.get("timestamp")
        or payload.get("createdAt"),
        "quotedEdges": _int(runtime.get("quotedEdges")),
        "quotedSemanticMatchInstruments": _int(runtime.get("quotedSemanticMatchInstruments")),
        "positiveCandidates": _candidate_total(runtime.get("positiveMarginCandidates")),
        "thresholdCandidates": _candidate_total(runtime.get("thresholdMarginCandidates")),
        "crossVenueCandidates": _cross_venue_candidate_count(runtime, quality),
        "venuePairCandidates": _venue_pair_candidate_counts(runtime, quality),
        "zeroCandidateBlockers": _counter_payload(coverage.get("zeroPairBlockerCounts")),
        "nearTermQuotedCandidates": _int(horizon.get("quotedCandidatesInsideHorizon")),
        "insideHorizonEvents": _int(horizon.get("eventsInsideHorizon")),
        "outsideHorizonEvents": _int(horizon.get("eventsOutsideHorizon")),
        "unknownResolutionEvents": _int(horizon.get("eventsUnknownResolution")),
        "marginBands": {key: _int(margin_bands.get(key)) for key in ("positive", *NEAR_MISS_BANDS)},
        "nearMissCount": len(near_misses),
        "bestNearMissMargin": max(near_miss_margins) if near_miss_margins else None,
        "worstNearMissMargin": min(near_miss_margins) if near_miss_margins else None,
        "latencySlo": _as_dict(_as_dict(runtime.get("latencyDiagnostics")).get("sloStatus")),
    }


def summarize_snapshots(paths: list[Path]) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            continue
        snapshots.append(_snapshot_summary(payload, source=str(path)))

    margin_band_series = [snapshot["marginBands"] for snapshot in snapshots]
    positive_series = [snapshot["positiveCandidates"] for snapshot in snapshots]
    threshold_series = [snapshot["thresholdCandidates"] for snapshot in snapshots]
    cross_venue_candidate_series = [snapshot["crossVenueCandidates"] for snapshot in snapshots]
    near_term_quoted_candidate_series = [
        snapshot["nearTermQuotedCandidates"] for snapshot in snapshots
    ]
    best_near_miss_series = [
        snapshot["bestNearMissMargin"]
        for snapshot in snapshots
        if snapshot["bestNearMissMargin"] is not None
    ]
    margin_band_movement = (
        len({json.dumps(item, sort_keys=True) for item in margin_band_series}) > 1
    )
    candidate_movement = len(set(positive_series)) > 1 or len(set(threshold_series)) > 1
    cross_venue_candidate_movement = len(set(cross_venue_candidate_series)) > 1
    near_term_candidate_movement = len(set(near_term_quoted_candidate_series)) > 1
    near_miss_movement = len(set(best_near_miss_series)) > 1
    any_activity = any(
        snapshot["quotedEdges"] > 0
        or snapshot["quotedSemanticMatchInstruments"] > 0
        or snapshot["positiveCandidates"] > 0
        or snapshot["thresholdCandidates"] > 0
        or snapshot["crossVenueCandidates"] > 0
        or snapshot["nearTermQuotedCandidates"] > 0
        or snapshot["nearMissCount"] > 0
        for snapshot in snapshots
    )
    return {
        "snapshotCount": len(snapshots),
        "nodeId": next((snapshot["nodeId"] for snapshot in snapshots if snapshot["nodeId"]), None),
        "firstSource": snapshots[0]["source"] if snapshots else None,
        "lastSource": snapshots[-1]["source"] if snapshots else None,
        "activityObserved": any_activity,
        "movementDetected": (
            margin_band_movement
            or candidate_movement
            or cross_venue_candidate_movement
            or near_term_candidate_movement
            or near_miss_movement
        ),
        "movementReasons": [
            reason
            for reason, present in (
                ("margin_band_movement", margin_band_movement),
                ("candidate_count_movement", candidate_movement),
                ("cross_venue_candidate_movement", cross_venue_candidate_movement),
                ("near_term_candidate_movement", near_term_candidate_movement),
                ("near_miss_margin_movement", near_miss_movement),
            )
            if present
        ],
        "positiveCandidateSeries": positive_series,
        "thresholdCandidateSeries": threshold_series,
        "crossVenueCandidateSeries": cross_venue_candidate_series,
        "nearTermQuotedCandidateSeries": near_term_quoted_candidate_series,
        "bestNearMissMarginSeries": best_near_miss_series,
        "latestZeroCandidateBlockers": (
            snapshots[-1]["zeroCandidateBlockers"] if snapshots else {}
        ),
        "latestVenuePairCandidates": snapshots[-1]["venuePairCandidates"] if snapshots else {},
        "snapshots": snapshots,
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "runtime margin movement",
        f"  node={summary.get('nodeId') or 'unknown'} snapshots={summary['snapshotCount']}",
        f"  activity_observed={summary['activityObserved']} movement_detected={summary['movementDetected']}",
        f"  movement_reasons={summary['movementReasons']}",
        f"  positive_series={summary['positiveCandidateSeries']}",
        f"  threshold_series={summary['thresholdCandidateSeries']}",
        f"  cross_venue_series={summary['crossVenueCandidateSeries']}",
        f"  near_term_quoted_series={summary['nearTermQuotedCandidateSeries']}",
        f"  best_near_miss_margin_series={summary['bestNearMissMarginSeries']}",
        f"  latest_zero_candidate_blockers={summary['latestZeroCandidateBlockers']}",
        f"  latest_venue_pair_candidates={summary['latestVenuePairCandidates']}",
    ]
    for snapshot in summary["snapshots"]:
        lines.append(
            "  snapshot "
            f"source={snapshot['source']} quoted_edges={snapshot['quotedEdges']} "
            f"quoted_semantic={snapshot['quotedSemanticMatchInstruments']} "
            f"positive={snapshot['positiveCandidates']} threshold={snapshot['thresholdCandidates']} "
            f"cross_venue={snapshot['crossVenueCandidates']} "
            f"near_term_quoted={snapshot['nearTermQuotedCandidates']} "
            f"near_misses={snapshot['nearMissCount']} bands={snapshot['marginBands']}",
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status_json", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--require-activity",
        action="store_true",
        help="Return non-zero when no quoted/candidate/near-miss activity is observed",
    )
    parser.add_argument(
        "--require-movement",
        action="store_true",
        help="Return non-zero when no margin/candidate/near-miss movement is observed",
    )
    args = parser.parse_args(argv)

    summary = summarize_snapshots(args.status_json)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_summary(summary))
    if args.require_activity and not summary["activityObserved"]:
        return 2
    if args.require_movement and not summary["movementDetected"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
