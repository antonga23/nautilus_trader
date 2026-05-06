#!/usr/bin/env python3
"""
Summarize betting strategy-node runtime probe artifacts.

The release and runtime-verification workflows persist full ``status.json`` and
``runtime-summary.json`` files. This script extracts the operator-facing fields
needed to answer how many semantic matches/candidates were found and why quoted
semantic edges did not become executable candidates.

"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _top_items(mapping: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    items = sorted(mapping.items(), key=lambda item: (-_numeric(item[1]), item[0]))
    return [{"key": key, "value": value} for key, value in items[:limit]]


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return sum(_numeric(item) for item in value.values())
    return 0.0


def _candidate_total(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    total = value.get("total")
    if isinstance(total, int):
        return total
    return int(sum(item for item in value.values() if isinstance(item, int)))


def summarize_payload(payload: dict[str, Any], *, top_limit: int = 5) -> dict[str, Any]:
    runtime = _as_dict(payload.get("runtimeProbe"))
    semantic_cache = _as_dict(payload.get("semanticCache"))
    candidate_quality = _as_dict(runtime.get("candidateQuality"))
    venue_coverage = _as_dict(runtime.get("venueCoverage"))
    provider_quote_poll_stats = _as_dict(runtime.get("providerQuotePollStats"))
    instrument_refresh = _as_dict(runtime.get("instrumentRefresh"))
    coverage_diagnostics = _as_dict(runtime.get("coverageDiagnostics"))
    semantic_diagnostics = _as_dict(runtime.get("semanticDiagnostics"))
    positive = runtime.get("positiveMarginCandidates")
    threshold = runtime.get("thresholdMarginCandidates")
    blocker_samples = _as_dict(candidate_quality.get("blockerSamples"))
    diagnostic_warnings = _diagnostic_warnings(candidate_quality)

    return {
        "nodeId": payload.get("nodeId"),
        "status": payload.get("status"),
        "semanticCache": {
            "ready": semantic_cache.get("ready"),
            "source": semantic_cache.get("source"),
            "manifestCount": semantic_cache.get("manifestCount"),
            "promotedTemplateCount": semantic_cache.get("promotedTemplateCount"),
            "executionSafeTemplateCount": semantic_cache.get("executionSafeTemplateCount"),
            "sameVenueExecutionEligibleTemplateCount": semantic_cache.get(
                "sameVenueExecutionEligibleTemplateCount",
            ),
        },
        "graph": {
            "engine": runtime.get("graphEngine"),
            "topologySource": runtime.get("topologySource"),
            "semanticTemplateCount": runtime.get("semanticTemplateCount"),
            "coverageProofCount": runtime.get("coverageProofCount"),
            "coverageHyperedgeCount": runtime.get("coverageHyperedgeCount"),
            "nodes": runtime.get("graphNodes"),
            "edges": runtime.get("graphEdges"),
            "quoteStates": runtime.get("graphQuoteStates"),
            "connectedNodes": runtime.get("connectedNodes"),
            "semanticMatchInstruments": runtime.get("semanticMatchInstruments"),
            "quotedSemanticMatchInstruments": runtime.get("quotedSemanticMatchInstruments"),
            "executionSafeEdges": runtime.get("executionSafeEdges"),
            "sameVenueExecutionEligibleEdges": runtime.get("sameVenueExecutionEligibleEdges"),
            "quotedEdges": runtime.get("quotedEdges"),
            "coverageDiagnostics": coverage_diagnostics,
        },
        "candidates": {
            "positiveTotal": _candidate_total(positive),
            "thresholdTotal": _candidate_total(threshold),
            "positive": positive,
            "threshold": threshold,
            "crossVenueCandidateCount": venue_coverage.get("crossVenueCandidateCount"),
        },
        "candidateQuality": {
            "diagnosticWarnings": diagnostic_warnings,
            "marginBands": candidate_quality.get("marginBands"),
            "rejectionBuckets": candidate_quality.get("rejectionBuckets"),
            "semanticBlockedReasons": candidate_quality.get("semanticBlockedReasons"),
            "semanticBlockedRelationships": candidate_quality.get("semanticBlockedRelationships"),
            "venuePairs": candidate_quality.get("venuePairs"),
            "marketFamilies": candidate_quality.get("marketFamilies"),
            "latencyHistograms": candidate_quality.get("latencyHistograms"),
            "liveQuoteAgeSlo": candidate_quality.get("liveQuoteAgeSlo"),
            "sameVenueDryRun": candidate_quality.get("sameVenueDryRun"),
            "topRejectionBuckets": _top_items(
                _as_dict(candidate_quality.get("rejectionBuckets")),
                limit=top_limit,
            ),
            "topSemanticBlockedReasons": _top_items(
                _as_dict(candidate_quality.get("semanticBlockedReasons")),
                limit=top_limit,
            ),
            "topSemanticBlockedRelationships": _top_items(
                _as_dict(candidate_quality.get("semanticBlockedRelationships")),
                limit=top_limit,
            ),
            "topVenuePairs": _top_items(
                _as_dict(candidate_quality.get("venuePairs")),
                limit=top_limit,
            ),
            "topMarketFamilies": _top_items(
                _as_dict(candidate_quality.get("marketFamilies")),
                limit=top_limit,
            ),
            "blockerSamples": {
                key: samples[:top_limit] if isinstance(samples, list) else samples
                for key, samples in sorted(blocker_samples.items())
            },
            "zeroCandidateVenuePairSamples": (
                candidate_quality.get("zeroCandidateVenuePairSamples") or []
            )[:top_limit],
            "zeroCandidateBlockerCounts": candidate_quality.get("zeroCandidateBlockerCounts"),
            "topPositiveCandidates": candidate_quality.get("topPositiveCandidates"),
            "topNegativeNearMisses": candidate_quality.get("topNegativeNearMisses"),
        },
        "providerQuotePollStats": provider_quote_poll_stats,
        "instrumentRefresh": instrument_refresh,
        "semanticDiagnostics": {
            "supportedProviderNodeCount": semantic_diagnostics.get("supportedProviderNodeCount"),
            "unsupportedProviderNodeCount": semantic_diagnostics.get("unsupportedProviderNodeCount"),
            "supportedProviderCoverageRatio": semantic_diagnostics.get(
                "supportedProviderCoverageRatio",
            ),
            "commonPatternKeyCount": semantic_diagnostics.get("commonPatternKeyCount"),
            "unsupportedProviderPatternCount": semantic_diagnostics.get(
                "unsupportedProviderPatternCount",
            ),
            "unsupportedProviderPatterns": semantic_diagnostics.get(
                "unsupportedProviderPatterns",
            ),
            "unsupportedProviderPatternSamples": (
                semantic_diagnostics.get("unsupportedProviderPatternSamples") or []
            )[:top_limit],
        },
        "venueCoverage": {
            "enabledVenues": venue_coverage.get("enabledVenues"),
            "nodeCounts": venue_coverage.get("nodeCounts"),
            "quoteSubscriptionCounts": venue_coverage.get("quoteSubscriptionCounts"),
            "quotedNodeCounts": venue_coverage.get("quotedNodeCounts"),
            "edgeCounts": venue_coverage.get("edgeCounts"),
            "quotedEdgeCounts": venue_coverage.get("quotedEdgeCounts"),
            "candidateCounts": venue_coverage.get("candidateCounts"),
            "zeroCandidateVenuePairs": venue_coverage.get("zeroCandidateVenuePairs"),
        },
    }


def aggregate_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    warning_counts: dict[str, int] = {}
    engine_counts: dict[str, int] = {}
    topology_counts: dict[str, int] = {}
    for summary in summaries:
        graph = _as_dict(summary.get("graph"))
        engine = str(graph.get("engine") or "unknown")
        topology = str(graph.get("topologySource") or "unknown")
        engine_counts[engine] = engine_counts.get(engine, 0) + 1
        topology_counts[topology] = topology_counts.get(topology, 0) + 1
        quality = _as_dict(summary.get("candidateQuality"))
        for warning in quality.get("diagnosticWarnings") or []:
            warning_counts[str(warning)] = warning_counts.get(str(warning), 0) + 1

    return {
        "artifactCount": len(summaries),
        "statusCounts": _count_values(summary.get("status") for summary in summaries),
        "graphEngineCounts": dict(sorted(engine_counts.items())),
        "topologySourceCounts": dict(sorted(topology_counts.items())),
        "semanticMatchInstruments": sum(
            _int_value(_as_dict(summary.get("graph")).get("semanticMatchInstruments"))
            for summary in summaries
        ),
        "quotedSemanticMatchInstruments": sum(
            _int_value(_as_dict(summary.get("graph")).get("quotedSemanticMatchInstruments"))
            for summary in summaries
        ),
        "graphEdges": sum(
            _int_value(_as_dict(summary.get("graph")).get("edges")) for summary in summaries
        ),
        "quotedEdges": sum(
            _int_value(_as_dict(summary.get("graph")).get("quotedEdges")) for summary in summaries
        ),
        "positiveCandidates": sum(
            _int_value(_as_dict(summary.get("candidates")).get("positiveTotal"))
            for summary in summaries
        ),
        "thresholdCandidates": sum(
            _int_value(_as_dict(summary.get("candidates")).get("thresholdTotal"))
            for summary in summaries
        ),
        "crossVenueCandidates": sum(
            _int_value(_as_dict(summary.get("candidates")).get("crossVenueCandidateCount"))
            for summary in summaries
        ),
        "diagnosticWarningCounts": dict(sorted(warning_counts.items())),
    }


def _count_values(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _int_value(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _diagnostic_warnings(candidate_quality: dict[str, Any]) -> list[str]:
    rejection_buckets = _as_dict(candidate_quality.get("rejectionBuckets"))
    semantic_blocked = _numeric(rejection_buckets.get("semantic_blocked"))
    semantic_reasons = _as_dict(candidate_quality.get("semanticBlockedReasons"))
    warnings: list[str] = []
    if semantic_blocked > 0 and not semantic_reasons:
        warnings.append("semantic_blocked_without_reason_breakdown")
    if semantic_blocked > 0 and not candidate_quality.get("blockerSamples"):
        warnings.append("semantic_blocked_without_blocker_samples")
    if candidate_quality.get("zeroCandidateVenuePairSamples") and not candidate_quality.get(
        "zeroCandidateBlockerCounts",
    ):
        warnings.append("zero_candidate_blockers_without_counts")
    if not candidate_quality.get("topPositiveCandidates"):
        warnings.append("missing_top_positive_candidates")
    if not candidate_quality.get("topNegativeNearMisses"):
        warnings.append("missing_top_negative_near_misses")
    return warnings


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return payload


def _format_text(path: Path, summary: dict[str, Any]) -> str:
    graph = summary["graph"]
    candidates = summary["candidates"]
    quality = summary["candidateQuality"]
    lines = [
        f"{path}:",
        f"  status={summary.get('status')} node={summary.get('nodeId')}",
        f"  graph={graph.get('engine')}/{graph.get('topologySource')} "
        f"nodes={graph.get('nodes')} edges={graph.get('edges')} "
        f"quoted_edges={graph.get('quotedEdges')}",
        f"  matches semantic={graph.get('semanticMatchInstruments')} "
        f"quoted={graph.get('quotedSemanticMatchInstruments')} "
        f"execution_safe_edges={graph.get('executionSafeEdges')} "
        f"same_venue_edges={graph.get('sameVenueExecutionEligibleEdges')}",
        f"  coverage proofs={graph.get('coverageProofCount')} "
        f"hyperedges={graph.get('coverageHyperedgeCount')}",
        f"  candidates positive={candidates.get('positiveTotal')} "
        f"threshold={candidates.get('thresholdTotal')} "
        f"cross_venue={candidates.get('crossVenueCandidateCount')}",
    ]
    lines.extend(_format_coverage_lines(graph))
    lines.extend(_format_quality_lines(quality))
    provider_poll = _format_provider_poll_stats(summary.get("providerQuotePollStats"))
    if provider_poll:
        lines.append(f"  provider_poll {provider_poll}")
    lines.extend(_format_refresh_lines(summary.get("instrumentRefresh")))
    lines.extend(_format_semantic_diagnostic_lines(summary.get("semanticDiagnostics")))
    same_venue_dry_run = quality.get("sameVenueDryRun") or {}
    if isinstance(same_venue_dry_run, dict) and (
        same_venue_dry_run.get("passes") or same_venue_dry_run.get("failures")
    ):
        lines.append(
            "  same_venue_dry_run "
            f"passes={same_venue_dry_run.get('passes', 0)} "
            f"failures={same_venue_dry_run.get('failures', 0)} "
            f"failure_reasons={same_venue_dry_run.get('failureReasons', {})}",
        )
    warnings = quality.get("diagnosticWarnings") or []
    if warnings:
        lines.append(f"  warnings {', '.join(warnings)}")
    return "\n".join(lines)


def _format_coverage_lines(graph: dict[str, Any]) -> list[str]:
    coverage_diagnostics = graph.get("coverageDiagnostics") or {}
    lines: list[str] = []
    if isinstance(coverage_diagnostics, dict) and coverage_diagnostics:
        lines.append(
            "  coverage_execution_safe "
            f"proofs={coverage_diagnostics.get('executionSafeCoverageProofCount', 0)} "
            f"hyperedges={coverage_diagnostics.get('executionSafeCoverageHyperedgeCount', 0)}",
        )
    return lines


def _format_quality_lines(quality: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    top_rejections = quality.get("topRejectionBuckets") or []
    if top_rejections:
        rendered = ", ".join(f"{item['key']}={item['value']}" for item in top_rejections)
        lines.append(f"  top_rejections {rendered}")
    top_blockers = quality.get("topSemanticBlockedReasons") or []
    if top_blockers:
        rendered = ", ".join(f"{item['key']}={item['value']}" for item in top_blockers)
        lines.append(f"  top_semantic_blockers {rendered}")
    top_blocker_relationships = quality.get("topSemanticBlockedRelationships") or []
    if top_blocker_relationships:
        rendered = ", ".join(f"{item['key']}={item['value']}" for item in top_blocker_relationships)
        lines.append(f"  top_semantic_relationships {rendered}")
    zero_candidate_blockers = quality.get("zeroCandidateBlockerCounts") or {}
    if isinstance(zero_candidate_blockers, dict) and zero_candidate_blockers:
        rendered = ", ".join(
            f"{key}={value}" for key, value in sorted(zero_candidate_blockers.items())
        )
        lines.append(f"  zero_candidate_blockers {rendered}")
    latency = quality.get("latencyHistograms") or {}
    if isinstance(latency, dict) and latency:
        quote_age = _as_dict(latency.get("quoteAgeSeconds"))
        fetch_latency = _as_dict(latency.get("fetchLatencySeconds"))
        pair_skew = _as_dict(latency.get("pairSkewSeconds"))
        lines.append(
            "  latency "
            f"quote_age_p95={quote_age.get('p95', 0)}s "
            f"fetch_p95={fetch_latency.get('p95', 0)}s "
            f"pair_skew_p95={pair_skew.get('p95', 0)}s",
        )
    live_slo = quality.get("liveQuoteAgeSlo") or {}
    if isinstance(live_slo, dict) and live_slo.get("observations"):
        lines.append(
            "  live_quote_age_slo "
            f"observations={live_slo.get('observations')} "
            f"violations={live_slo.get('violations')} "
            f"max={live_slo.get('maxQuoteAgeSeconds')}s",
        )
    return lines


def _format_refresh_lines(value: Any) -> list[str]:
    instrument_refresh = value or {}
    lines: list[str] = []
    if isinstance(instrument_refresh, dict) and instrument_refresh:
        lines.append(
            "  instrument_refresh "
            f"requests={instrument_refresh.get('requests', 0)} "
            f"failures={instrument_refresh.get('failures', 0)} "
            f"added={instrument_refresh.get('added', 0)} "
            f"removed={instrument_refresh.get('removed', 0)} "
            f"stale_triggers={instrument_refresh.get('staleQuoteTriggers', 0)}",
        )
        venue_refresh = instrument_refresh.get("venues") or {}
        if isinstance(venue_refresh, dict) and venue_refresh:
            rendered = ", ".join(
                (
                    f"{venue}:req={stats.get('requests', 0)} add={stats.get('added', 0)} "
                    f"rm={stats.get('removed', 0)} stale={stats.get('stale_triggers', 0)}"
                )
                for venue, stats in sorted(venue_refresh.items())
                if isinstance(stats, dict)
            )
            if rendered:
                lines.append(f"  instrument_refresh_by_venue {rendered}")
    return lines


def _format_semantic_diagnostic_lines(value: Any) -> list[str]:
    diagnostics = value if isinstance(value, dict) else {}
    if not diagnostics:
        return []
    lines = [
        "  semantic_diagnostics "
        f"supported_nodes={diagnostics.get('supportedProviderNodeCount', 0)} "
        f"unsupported_nodes={diagnostics.get('unsupportedProviderNodeCount', 0)} "
        f"coverage_ratio={diagnostics.get('supportedProviderCoverageRatio', 0)} "
        f"common_patterns={diagnostics.get('commonPatternKeyCount', 0)} "
        f"unsupported_patterns={diagnostics.get('unsupportedProviderPatternCount', 0)}",
    ]
    top_patterns = diagnostics.get("unsupportedProviderPatterns") or []
    if isinstance(top_patterns, list) and top_patterns:
        rendered = ", ".join(
            f"{item.get('key')}={item.get('value')}"
            for item in top_patterns
            if isinstance(item, dict)
        )
        if rendered:
            lines.append(f"  unsupported_provider_patterns {rendered}")
    return lines


def _format_provider_poll_stats(value: Any) -> str:
    provider_poll_stats = value if isinstance(value, dict) else {}
    rendered = []
    for venue, stats in sorted(provider_poll_stats.items()):
        if not isinstance(stats, dict):
            continue
        rendered.append(
            f"{venue}:cycle={stats.get('cycle_id', 0)} "
            f"quotes={stats.get('quote_count', 0)} "
            f"markets={stats.get('market_count', 0)} "
            f"cycle_elapsed={stats.get('cycle_elapsed_secs', 0)}s "
            f"max_fetch={stats.get('max_fetch_latency_secs', 0)}s "
            f"ts={stats.get('quote_event_timestamp_source', '')}->{stats.get('quote_init_timestamp_source', '')} "
            f"backlog={stats.get('backlog_count', 0)} "
            f"failures={stats.get('failure_count', 0)} "
            f"rate_limits={stats.get('rate_limit_count', 0)} "
            f"backoff={stats.get('backoff_secs', 0)}s",
        )
    return "; ".join(rendered)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="status.json or runtime-summary.json paths",
    )
    parser.add_argument("--top-limit", type=int, default=5, help="Maximum top samples to include")
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Return non-zero when any artifact has diagnostic warnings",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Include aggregate counts across all input artifacts",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summaries = [
        {"path": str(path), **summarize_payload(_load_json(path), top_limit=args.top_limit)}
        for path in args.paths
    ]
    if args.format == "text":
        print("\n".join(_format_text(Path(item["path"]), item) for item in summaries))
        if args.aggregate:
            aggregate = aggregate_summaries(summaries)
            print(
                "\naggregate: "
                f"artifacts={aggregate['artifactCount']} "
                f"positive={aggregate['positiveCandidates']} "
                f"threshold={aggregate['thresholdCandidates']} "
                f"cross_venue={aggregate['crossVenueCandidates']} "
                f"warnings={aggregate['diagnosticWarningCounts']}",
            )
    else:
        payload: object = (
            {
                "artifacts": summaries,
                "aggregate": aggregate_summaries(summaries),
            }
            if args.aggregate
            else summaries
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
    if args.fail_on_warning and any(
        item["candidateQuality"].get("diagnosticWarnings") for item in summaries
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
