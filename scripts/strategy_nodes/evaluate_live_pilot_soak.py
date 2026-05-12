#!/usr/bin/env python3
"""
Evaluate unarmed live-pilot strategy-node soak artifacts.

The live-pilot rollout should not require manual SSH log inspection to decide
whether a node is live enough to arm. This script consumes persisted
``status.json`` or runtime-summary JSON artifacts and checks for the evidence
required by the six-hour unarmed soak:

- cross-venue semantic participation,
- quoted candidates inside the near-term resolution horizon,
- positive or threshold candidates when available,
- exact blockers when execution did not proceed,
- moving near-miss / negative-margin observations across snapshots,
- latency SLO evidence for quote age, pair skew, graph scan, and decisions.

"""

from __future__ import annotations

import argparse
from datetime import UTC
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
from statistics import pstdev
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_REPORT_PATH = _REPO_ROOT / "scripts" / "betting" / "runtime_probe_report.py"

_NEGATIVE_MARGIN_BANDS = ("0% to -1%", "-1% to -2%", "-2% to -5%", "< -5%")
_EXACT_BLOCKER_KEYS = (
    "stale",
    "cross_cycle",
    "fetch_latency",
    "liquidity",
    "topology_only",
    "equivalent_selection",
    "void_settlement",
    "partial_settlement",
    "same_venue_policy",
    "same_market_params_mismatch",
    "provider_scope_mismatch",
    "fixture_identity_mismatch",
    "unsupported_market_family",
    "unknown_settlement",
    "ambiguous_resolution",
    "no_semantic_edge",
    "no_common_fixture",
    "incomplete_book_no_devig",
    "devig_method_failed",
    "stale_fx",
    "missing_fx",
    "cap_breach",
    "venue_degraded",
    "semantic_cache_unready",
)


def _load_runtime_report_module() -> Any:
    spec = importlib.util.spec_from_file_location("runtime_probe_report", _RUNTIME_REPORT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import runtime probe report from {_RUNTIME_REPORT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return sum(_numeric(item) for item in value.values())
    return 0.0


def _int_value(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _snapshot_time(path: Path, payload: dict[str, Any]) -> datetime | None:
    for key in ("updatedAt", "checkedAt", "timestamp", "createdAt"):
        parsed = _parse_timestamp(payload.get(key))
        if parsed is not None:
            return parsed
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _is_cross_venue_pair(pair: str) -> bool:
    if "->" not in pair:
        return False
    source, target = pair.split("->", maxsplit=1)
    return bool(source and target and source != target)


def _cross_venue_count(mapping: Any) -> int:
    return sum(
        _int_value(value)
        for key, value in _as_dict(mapping).items()
        if _is_cross_venue_pair(str(key))
    )


def _latency_metric(latency: dict[str, Any], name: str) -> dict[str, Any]:
    value = latency.get(name)
    return value if isinstance(value, dict) else {}


def _latency_histogram_metric(quality: dict[str, Any], *names: str) -> dict[str, Any]:
    histograms = _as_dict(quality.get("latencyHistograms"))
    for name in names:
        value = histograms.get(name)
        if isinstance(value, dict):
            return value
    return {}


def _latency_pass(metric: dict[str, Any], *, threshold: float) -> dict[str, Any]:
    count = _int_value(metric.get("count"))
    p95 = metric.get("p95_ms")
    if count <= 0:
        return {"status": "unknown", "count": count, "p95_ms": None, "threshold_ms": threshold}
    if not isinstance(p95, int | float) or math.isnan(float(p95)):
        return {"status": "unknown", "count": count, "p95_ms": None, "threshold_ms": threshold}
    return {
        "status": "pass" if float(p95) <= threshold else "fail",
        "count": count,
        "p95_ms": float(p95),
        "threshold_ms": threshold,
    }


def _candidate_total(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    total = value.get("total")
    if isinstance(total, int):
        return total
    return int(sum(item for item in value.values() if isinstance(item, int)))


def _margin_band_total(quality: dict[str, Any], *bands: str) -> int:
    margin_bands = _as_dict(quality.get("marginBands"))
    return int(sum(_numeric(margin_bands.get(band)) for band in bands))


def _sample_exact_blockers(quality: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    samples = _as_dict(quality.get("blockerSamples"))
    rendered: list[dict[str, Any]] = []
    for reason in _EXACT_BLOCKER_KEYS:
        reason_samples = _as_list(samples.get(reason))
        if not reason_samples:
            continue
        rendered.append({"reason": reason, "samples": reason_samples[:limit]})
        if len(rendered) >= limit:
            break
    return rendered


def _exact_blocker_count(quality: dict[str, Any]) -> int:
    counts = 0
    for source_key in ("rejectionBuckets", "semanticBlockedReasons", "zeroCandidateBlockerCounts"):
        source = _as_dict(quality.get(source_key))
        counts += int(sum(_numeric(source.get(key)) for key in _EXACT_BLOCKER_KEYS))
    return counts


def _quality_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(summary.get("candidateQuality"))


def _raw_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(payload.get("runtimeProbe"))


def _snapshot_payload(
    *,
    path: Path,
    payload: dict[str, Any],
    summary: dict[str, Any],
    top_limit: int,
) -> dict[str, Any]:
    runtime = _raw_runtime(payload)
    quality = _quality_from_summary(summary)
    venue_coverage = _as_dict(runtime.get("venueCoverage"))
    summary_venue_coverage = _as_dict(summary.get("venueCoverage"))
    horizon = _as_dict(runtime.get("resolutionHorizon"))
    latency = _as_dict(summary.get("latencyDiagnostics"))
    timestamp = _snapshot_time(path, payload)
    positive_total = _int_value(_as_dict(summary.get("candidates")).get("positiveTotal"))
    threshold_total = _int_value(_as_dict(summary.get("candidates")).get("thresholdTotal"))
    cross_venue_edges = _cross_venue_count(venue_coverage.get("edgeCounts"))
    cross_venue_quoted_edges = _cross_venue_count(venue_coverage.get("quotedEdgeCounts"))
    cross_venue_candidates = _cross_venue_count(venue_coverage.get("candidateCounts"))
    negative_band_total = _margin_band_total(quality, *_NEGATIVE_MARGIN_BANDS)
    exact_blockers = _sample_exact_blockers(quality, limit=top_limit)
    return {
        "path": str(path),
        "timestamp": timestamp.isoformat() if timestamp is not None else None,
        "nodeId": summary.get("nodeId"),
        "status": summary.get("status"),
        "graphEngine": _as_dict(summary.get("graph")).get("engine"),
        "topologySource": _as_dict(summary.get("graph")).get("topologySource"),
        "quotedSemanticMatchInstruments": _as_dict(summary.get("graph")).get(
            "quotedSemanticMatchInstruments",
        ),
        "quotedEdges": _as_dict(summary.get("graph")).get("quotedEdges"),
        "crossVenueEdges": cross_venue_edges,
        "crossVenueQuotedEdges": cross_venue_quoted_edges,
        "crossVenueCandidates": cross_venue_candidates,
        "positiveCandidates": positive_total,
        "thresholdCandidates": threshold_total,
        "nearMissCandidates": _numeric(
            _as_dict(quality.get("rejectionBuckets")).get("below_threshold"),
        ),
        "negativeMarginObservations": negative_band_total
        + _numeric(_as_dict(quality.get("rejectionBuckets")).get("negative_margin")),
        "exactBlockerCount": _exact_blocker_count(quality),
        "exactBlockerSamples": exact_blockers,
        "resolutionHorizon": {
            "enabled": horizon.get("enabled"),
            "maxResolutionHorizonHours": horizon.get("maxResolutionHorizonHours"),
            "eventsInsideHorizon": horizon.get("eventsInsideHorizon"),
            "quotedCandidatesInsideHorizon": horizon.get("quotedCandidatesInsideHorizon"),
            "blockedCandidatesDueHorizon": horizon.get("blockedCandidatesDueHorizon"),
        },
        "quoteCapacityPressure": summary_venue_coverage.get("quoteCapacityPressure") or {},
        "fxPolicy": summary.get("fxPolicy") or {},
        "latency": {
            "quoteAge": _latency_histogram_metric(quality, "quoteAgeSeconds"),
            "pairSkew": _latency_histogram_metric(quality, "pairSkewSeconds", "quoteDeltaSeconds"),
            "graphScan": _latency_metric(latency, "graphScan"),
            "candidateDecision": _latency_metric(latency, "candidateDecision"),
            "sloStatus": _as_dict(latency.get("sloStatus")),
            "warnings": latency.get("diagnosticWarnings") or [],
        },
        "topPositiveCandidates": _as_list(quality.get("topPositiveCandidates"))[:top_limit],
        "topNegativeNearMisses": _as_list(quality.get("topNegativeNearMisses"))[:top_limit],
    }


def _series_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "stddev": None}
    mean = sum(values) / len(values)
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": mean,
        "stddev": pstdev(values) if len(values) > 1 else 0.0,
    }


def _movement_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    negative_values = [
        float(item["negativeMarginObservations"])
        for item in snapshots
        if _numeric(item.get("negativeMarginObservations")) > 0
    ]
    near_miss_values = [
        float(item["nearMissCandidates"])
        for item in snapshots
        if _numeric(item.get("nearMissCandidates")) > 0
    ]
    positive_values = [
        float(item["positiveCandidates"])
        for item in snapshots
        if _numeric(item.get("positiveCandidates")) > 0
    ]
    return {
        "negativeMargin": {
            **_series_stats(negative_values),
            "moving": len(set(negative_values)) > 1,
        },
        "nearMiss": {
            **_series_stats(near_miss_values),
            "moving": len(set(near_miss_values)) > 1,
        },
        "positive": {
            **_series_stats(positive_values),
            "moving": len(set(positive_values)) > 1,
        },
    }


def _latency_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    quote_age = _latency_metric_series(snapshots, "quoteAge", "p95", scale=1000.0)
    pair_skew = _latency_metric_series(snapshots, "pairSkew", "p95", scale=1000.0)
    graph_scan = _latency_metric_series(snapshots, "graphScan", "p95_ms")
    decision = _latency_metric_series(snapshots, "candidateDecision", "p95_ms")
    return {
        "quoteAgeP95": {
            **_series_stats(quote_age),
            "threshold_ms": 5000.0,
            "status": "pass" if quote_age and max(quote_age) <= 5000.0 else "unknown_or_fail",
        },
        "pairSkewP95": {
            **_series_stats(pair_skew),
            "threshold_ms": 1000.0,
            "status": "pass" if pair_skew and max(pair_skew) <= 1000.0 else "unknown_or_fail",
        },
        "graphScanP95": {
            **_series_stats(graph_scan),
            "threshold_ms": 50.0,
            "status": "pass" if graph_scan and max(graph_scan) <= 50.0 else "unknown_or_fail",
        },
        "decisionP95": {
            **_series_stats(decision),
            "threshold_ms": 250.0,
            "status": "pass" if decision and max(decision) <= 250.0 else "unknown_or_fail",
        },
    }


def _latency_metric_series(
    snapshots: list[dict[str, Any]],
    metric: str,
    field: str,
    *,
    scale: float = 1.0,
) -> list[float]:
    values: list[float] = []
    for item in snapshots:
        value = _as_dict(_as_dict(item.get("latency")).get(metric)).get(field)
        if isinstance(value, int | float):
            values.append(float(value) * scale)
    return values


def _load_snapshots(paths: list[Path], *, top_limit: int) -> list[dict[str, Any]]:
    runtime_report = _load_runtime_report_module()
    loaded: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for path in paths:
        payload = _load_json(path)
        summary = runtime_report.summarize_payload(payload, top_limit=top_limit)
        loaded.append((path, payload, summary))
    loaded.sort(
        key=lambda item: _snapshot_time(item[0], item[1]) or datetime.min.replace(tzinfo=UTC),
    )
    return [
        _snapshot_payload(path=path, payload=payload, summary=summary, top_limit=top_limit)
        for path, payload, summary in loaded
    ]


def _snapshot_elapsed_hours(snapshots: list[dict[str, Any]]) -> float:
    timestamps: list[datetime] = []
    for item in snapshots:
        timestamp = item.get("timestamp")
        if not timestamp:
            continue
        parsed = _parse_timestamp(str(timestamp))
        if parsed is not None:
            timestamps.append(parsed)
    if len(timestamps) < 2:
        return 0.0
    return (max(timestamps) - min(timestamps)).total_seconds() / 3600


def _soak_totals(
    snapshots: list[dict[str, Any]],
    *,
    max_soak_hours: float,
    elapsed_hours: float,
) -> dict[str, int | float]:
    return {
        "artifactCount": len(snapshots),
        "elapsedHours": elapsed_hours,
        "maxSoakHours": max_soak_hours,
        "crossVenueEdges": int(sum(_numeric(item.get("crossVenueEdges")) for item in snapshots)),
        "crossVenueQuotedEdges": int(
            sum(_numeric(item.get("crossVenueQuotedEdges")) for item in snapshots),
        ),
        "crossVenueCandidates": int(
            sum(_numeric(item.get("crossVenueCandidates")) for item in snapshots),
        ),
        "positiveCandidates": int(
            sum(_numeric(item.get("positiveCandidates")) for item in snapshots),
        ),
        "thresholdCandidates": int(
            sum(_numeric(item.get("thresholdCandidates")) for item in snapshots),
        ),
        "quotedCandidatesInsideHorizon": int(
            sum(
                _numeric(
                    _as_dict(item.get("resolutionHorizon")).get(
                        "quotedCandidatesInsideHorizon",
                    ),
                )
                for item in snapshots
            ),
        ),
        "exactBlockers": int(sum(_numeric(item.get("exactBlockerCount")) for item in snapshots)),
        "negativeMarginObservations": int(
            sum(_numeric(item.get("negativeMarginObservations")) for item in snapshots),
        ),
        "maxQuoteCapacityPressureScore": _max_quote_capacity_pressure_score(snapshots),
        "unquotedSemanticMatchedNodes": _sum_unquoted_semantic_matched_nodes(snapshots),
    }


def _max_quote_capacity_pressure_score(snapshots: list[dict[str, Any]]) -> float:
    scores: list[float] = []
    for snapshot in snapshots:
        for payload in _as_dict(snapshot.get("quoteCapacityPressure")).values():
            score = _as_dict(payload).get("capacityPressureScore")
            if isinstance(score, int | float):
                scores.append(float(score))
    return max(scores) if scores else 0.0


def _sum_unquoted_semantic_matched_nodes(snapshots: list[dict[str, Any]]) -> int:
    total = 0
    for snapshot in snapshots:
        for payload in _as_dict(snapshot.get("quoteCapacityPressure")).values():
            total += _int_value(_as_dict(payload).get("unquotedSemanticMatchedNodes"))
    return total


def _early_pass_reasons(
    *,
    totals: dict[str, int | float],
    movement: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if totals["crossVenueEdges"] > 0:
        reasons.append("cross_venue_semantic_edges_observed")
    if totals["crossVenueQuotedEdges"] > 0 or totals["quotedCandidatesInsideHorizon"] > 0:
        reasons.append("quoted_cross_venue_or_horizon_candidates_observed")
    if totals["positiveCandidates"] > 0 or totals["thresholdCandidates"] > 0:
        reasons.append("positive_or_threshold_candidates_observed")
    if totals["exactBlockers"] > 0:
        reasons.append("exact_execution_blockers_observed")
    if movement["negativeMargin"]["moving"] or movement["nearMiss"]["moving"]:
        reasons.append("near_miss_or_negative_margin_movement_observed")
    return reasons


def _latency_ready(latency: dict[str, Any]) -> bool:
    return all(
        _as_dict(latency[key]).get("status") == "pass"
        for key in ("quoteAgeP95", "pairSkewP95", "graphScanP95", "decisionP95")
    )


def _readiness_reasons(
    *,
    early_pass_reasons: list[str],
    latency_pass: bool,
    elapsed_hours: float,
    max_soak_hours: float,
) -> list[str]:
    reasons: list[str] = []
    if not early_pass_reasons:
        reasons.append("missing_cross_venue_edge_or_blocker_evidence")
    if not latency_pass:
        reasons.append("latency_slo_incomplete_or_failed")
    if elapsed_hours > max_soak_hours:
        reasons.append("soak_window_exceeded")
    return reasons


def evaluate_soak(
    paths: list[Path],
    *,
    max_soak_hours: float = 6.0,
    top_limit: int = 5,
) -> dict[str, Any]:
    snapshots = _load_snapshots(paths, top_limit=top_limit)
    elapsed_hours = _snapshot_elapsed_hours(snapshots)
    totals = _soak_totals(
        snapshots,
        max_soak_hours=max_soak_hours,
        elapsed_hours=elapsed_hours,
    )
    movement = _movement_summary(snapshots)
    latency = _latency_summary(snapshots)
    early_pass_reasons = _early_pass_reasons(totals=totals, movement=movement)
    latency_pass = _latency_ready(latency)
    readiness = "pass" if early_pass_reasons and latency_pass else "warn"
    reasons = _readiness_reasons(
        early_pass_reasons=early_pass_reasons,
        latency_pass=latency_pass,
        elapsed_hours=elapsed_hours,
        max_soak_hours=max_soak_hours,
    )
    return {
        "readiness": readiness,
        "reasons": reasons,
        "earlyPassReasons": early_pass_reasons,
        "totals": totals,
        "movement": movement,
        "latency": latency,
        "snapshots": snapshots,
    }


def _format_text(payload: dict[str, Any]) -> str:
    totals = _as_dict(payload.get("totals"))
    movement = _as_dict(payload.get("movement"))
    latency = _as_dict(payload.get("latency"))
    lines = [
        f"live-pilot-soak readiness={payload.get('readiness')} reasons={payload.get('reasons')}",
        "  totals "
        f"artifacts={totals.get('artifactCount')} elapsed_hours={totals.get('elapsedHours'):.2f} "
        f"cross_venue_edges={totals.get('crossVenueEdges')} "
        f"quoted_cross_venue_edges={totals.get('crossVenueQuotedEdges')} "
        f"positive={totals.get('positiveCandidates')} threshold={totals.get('thresholdCandidates')} "
        f"inside_horizon={totals.get('quotedCandidatesInsideHorizon')} "
        f"exact_blockers={totals.get('exactBlockers')}",
        "  quote_capacity "
        f"max_pressure={totals.get('maxQuoteCapacityPressureScore')} "
        f"unquoted_semantic={totals.get('unquotedSemanticMatchedNodes')}",
        "  movement "
        f"negative={_as_dict(movement.get('negativeMargin')).get('moving')} "
        f"near_miss={_as_dict(movement.get('nearMiss')).get('moving')}",
        "  latency "
        f"quote_age={_as_dict(latency.get('quoteAgeP95')).get('status')} "
        f"pair_skew={_as_dict(latency.get('pairSkewP95')).get('status')} "
        f"graph_scan={_as_dict(latency.get('graphScanP95')).get('status')} "
        f"decision={_as_dict(latency.get('decisionP95')).get('status')}",
    ]
    if payload.get("earlyPassReasons"):
        lines.append("  early_pass " + ", ".join(str(item) for item in payload["earlyPassReasons"]))
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="status/runtime JSON artifacts")
    parser.add_argument(
        "--max-soak-hours",
        type=float,
        default=6.0,
        help="Maximum intended unarmed soak window",
    )
    parser.add_argument("--top-limit", type=int, default=5, help="Maximum samples per bucket")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format",
    )
    parser.add_argument(
        "--fail-unless-pass",
        action="store_true",
        help="Return non-zero unless soak readiness is pass",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = evaluate_soak(
        args.paths,
        max_soak_hours=args.max_soak_hours,
        top_limit=args.top_limit,
    )
    if args.format == "text":
        print(_format_text(payload))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if args.fail_unless_pass and payload.get("readiness") != "pass":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
