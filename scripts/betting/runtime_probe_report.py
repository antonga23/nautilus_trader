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
import math
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
    execution_readiness = _as_dict(payload.get("executionReadiness"))
    candidate_quality = _as_dict(runtime.get("candidateQuality"))
    venue_coverage = _as_dict(runtime.get("venueCoverage"))
    provider_quote_poll_stats = _as_dict(runtime.get("providerQuotePollStats"))
    instrument_refresh = _as_dict(runtime.get("instrumentRefresh"))
    coverage_diagnostics = _as_dict(runtime.get("coverageDiagnostics"))
    latency_diagnostics = _normalized_latency_diagnostics(runtime.get("latencyDiagnostics"))
    semantic_diagnostics = _as_dict(runtime.get("semanticDiagnostics"))
    corpus_health = _semantic_cache_corpus_health(
        _as_dict(semantic_cache.get("providerCorpusCoverage")),
    )
    positive = runtime.get("positiveMarginCandidates")
    threshold = runtime.get("thresholdMarginCandidates")
    blocker_samples = _as_dict(candidate_quality.get("blockerSamples"))
    diagnostic_warnings = _diagnostic_warnings(candidate_quality)
    graph_diagnostic_warnings = _graph_diagnostic_warnings(
        {
            "semanticTemplateCount": runtime.get("semanticTemplateCount"),
            "coverageProofCount": runtime.get("coverageProofCount"),
            "coverageHyperedgeCount": runtime.get("coverageHyperedgeCount"),
            "coverageDiagnostics": coverage_diagnostics,
        },
    )

    latency_warnings = _merged_latency_warnings(
        latency_diagnostics,
        _latency_diagnostic_warnings(
            latency_diagnostics,
            quoted_edges=_int_value(runtime.get("quotedEdges")),
            positive_candidates=_candidate_total(positive),
            threshold_candidates=_candidate_total(threshold),
        ),
    )
    latency_block = {
        **latency_diagnostics,
        "sloStatus": _latency_slo_status(
            candidate_quality=candidate_quality,
            latency=latency_diagnostics,
        ),
        "diagnosticWarnings": latency_warnings,
    }
    candidate_warnings = diagnostic_warnings
    graph_warnings = graph_diagnostic_warnings
    summary = {
        "nodeId": payload.get("nodeId"),
        "status": payload.get("status") or payload.get("state"),
        "semanticCache": {
            "ready": semantic_cache.get("ready"),
            "source": semantic_cache.get("source"),
            "manifestCount": semantic_cache.get("manifestCount"),
            "promotedTemplateCount": semantic_cache.get("promotedTemplateCount"),
            "executionSafeTemplateCount": semantic_cache.get("executionSafeTemplateCount"),
            "sameVenueExecutionEligibleTemplateCount": semantic_cache.get(
                "sameVenueExecutionEligibleTemplateCount",
            ),
            "promotedSafetyTierCounts": semantic_cache.get("promotedSafetyTierCounts"),
            "promotedMarketFamilyCounts": semantic_cache.get("promotedMarketFamilyCounts"),
            "executionSafeMarketFamilyCounts": semantic_cache.get(
                "executionSafeMarketFamilyCounts",
            ),
            "sameVenueEligibleMarketFamilyCounts": semantic_cache.get(
                "sameVenueEligibleMarketFamilyCounts",
            ),
            "strictExecutionBlockerCounts": semantic_cache.get("strictExecutionBlockerCounts"),
            "summaryReused": semantic_cache.get("summaryReused"),
            "bootstrapPhaseTimingsSeconds": semantic_cache.get("bootstrapPhaseTimingsSeconds"),
            "providerCorpusCoverage": semantic_cache.get("providerCorpusCoverage"),
        },
        "executionReadiness": {
            "validationMode": execution_readiness.get("validationMode"),
            "autoExecute": execution_readiness.get("autoExecute"),
            "liveExecutionArmed": execution_readiness.get("liveExecutionArmed"),
            "liveExecutionEnvArmed": execution_readiness.get("liveExecutionEnvArmed"),
            "allowCrossCurrencyLiveExecution": execution_readiness.get(
                "allowCrossCurrencyLiveExecution",
            ),
            "semanticCacheConfigured": execution_readiness.get("semanticCacheConfigured"),
            "venues": execution_readiness.get("venues"),
        },
        "executionSafety": _execution_safety_health(execution_readiness),
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
            "topCoverageBlockerReasons": _top_items(
                _as_dict(coverage_diagnostics.get("proofBlockerReasonCounts")),
                limit=top_limit,
            ),
            "topCoverageGapReasons": _top_items(
                _as_dict(coverage_diagnostics.get("proofGapReasonCounts")),
                limit=top_limit,
            ),
            "topCoverageRiskReasons": _top_items(
                _as_dict(coverage_diagnostics.get("proofRiskReasonCounts")),
                limit=top_limit,
            ),
            "sampleCoverageProofs": (coverage_diagnostics.get("sampleProofs") or [])[:top_limit],
            "diagnosticWarnings": graph_diagnostic_warnings,
        },
        "candidates": {
            "positiveTotal": _candidate_total(positive),
            "thresholdTotal": _candidate_total(threshold),
            "positive": positive,
            "threshold": threshold,
            "crossVenueCandidateCount": venue_coverage.get("crossVenueCandidateCount"),
        },
        "feePolicy": _as_dict(runtime.get("feePolicy")),
        "latencyDiagnostics": latency_block,
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
            "liveTimingSlo": candidate_quality.get("liveTimingSlo"),
            "sameVenueDryRun": candidate_quality.get("sameVenueDryRun"),
            "feeAdjustment": candidate_quality.get("feeAdjustment"),
            "devigDiagnostics": candidate_quality.get("devigDiagnostics"),
            "coverageBookDevigDiagnostics": candidate_quality.get(
                "coverageBookDevigDiagnostics",
            ),
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
            "zeroCandidateFixtureProofBlockerCounts": _zero_fixture_proof_blocker_counts(
                venue_coverage.get("zeroCandidateVenuePairs"),
            ),
            "topPositiveCandidates": (candidate_quality.get("topPositiveCandidates") or [])[
                :top_limit
            ],
            "topNegativeNearMisses": (candidate_quality.get("topNegativeNearMisses") or [])[
                :top_limit
            ],
            "topValueEdgeCandidates": (candidate_quality.get("topValueEdgeCandidates") or [])[
                :top_limit
            ],
            "topVigErasedCandidates": (candidate_quality.get("topVigErasedCandidates") or [])[
                :top_limit
            ],
        },
        "providerQuotePollStats": provider_quote_poll_stats,
        "providerPollHealth": _provider_poll_health(provider_quote_poll_stats),
        "semanticCacheCorpusHealth": corpus_health,
        "instrumentRefresh": instrument_refresh,
        "semanticDiagnostics": {
            "supportedProviderNodeCount": semantic_diagnostics.get("supportedProviderNodeCount"),
            "unsupportedProviderNodeCount": semantic_diagnostics.get(
                "unsupportedProviderNodeCount",
            ),
            "supportedProviderCoverageRatio": semantic_diagnostics.get(
                "supportedProviderCoverageRatio",
            ),
            "commonPatternKeyCount": semantic_diagnostics.get("commonPatternKeyCount"),
            "unsupportedProviderPatternCount": semantic_diagnostics.get(
                "unsupportedProviderPatternCount",
            ),
            "unsupportedProviderPatterns": (
                semantic_diagnostics.get("unsupportedProviderPatterns") or []
            )[:top_limit],
            "unsupportedProviderPatternSamples": (
                semantic_diagnostics.get("unsupportedProviderPatternSamples") or []
            )[:top_limit],
        },
        "venueCoverage": {
            "enabledVenues": venue_coverage.get("enabledVenues"),
            "nodeCounts": venue_coverage.get("nodeCounts"),
            "quoteSubscriptionCounts": venue_coverage.get("quoteSubscriptionCounts"),
            "quoteSubscriptionLimits": venue_coverage.get("quoteSubscriptionLimits"),
            "quoteSubscriptionLimitExceededCounts": venue_coverage.get(
                "quoteSubscriptionLimitExceededCounts",
            ),
            "quoteSubscriptionGapCounts": venue_coverage.get("quoteSubscriptionGapCounts"),
            "venuesWithSubscriptionQuoteGap": venue_coverage.get(
                "venuesWithSubscriptionQuoteGap",
            ),
            "quotedNodeCounts": venue_coverage.get("quotedNodeCounts"),
            "semanticMatchedNodeCounts": venue_coverage.get("semanticMatchedNodeCounts"),
            "quotedSemanticMatchedNodeCounts": venue_coverage.get(
                "quotedSemanticMatchedNodeCounts",
            ),
            "unquotedSemanticMatchedNodeCounts": venue_coverage.get(
                "unquotedSemanticMatchedNodeCounts",
            ),
            "unquotedSemanticMatchedNodeSamples": venue_coverage.get(
                "unquotedSemanticMatchedNodeSamples",
            ),
            "edgeCounts": venue_coverage.get("edgeCounts"),
            "quotedEdgeCounts": venue_coverage.get("quotedEdgeCounts"),
            "candidateCounts": venue_coverage.get("candidateCounts"),
            "crossVenueCandidateCount": venue_coverage.get("crossVenueCandidateCount"),
            "crossVenuePairsWithCandidates": venue_coverage.get(
                "crossVenuePairsWithCandidates",
            ),
            "zeroCandidateVenuePairs": venue_coverage.get("zeroCandidateVenuePairs"),
            "zeroCandidateBlockerCounts": venue_coverage.get("zeroCandidateBlockerCounts"),
        },
        "venueCoverageHealth": _venue_coverage_health(venue_coverage),
        "operatorHealth": _operator_health(
            candidate_warnings=candidate_warnings,
            graph_warnings=graph_warnings,
            latency_warnings=latency_block["diagnosticWarnings"],
            latency_slo_status=_as_dict(latency_block.get("sloStatus")),
            execution_safety_status=_execution_safety_health(execution_readiness),
        ),
    }
    summary["recommendedActions"] = _recommended_actions(summary)
    return summary


def _zero_fixture_proof_blocker_counts(value: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(value, list):
        return counts
    for report in value:
        if not isinstance(report, dict):
            continue
        for reason, count in _as_dict(report.get("fixtureProofBlockerCounts")).items():
            key = str(reason)
            counts[key] = counts.get(key, 0) + _int_value(count)
    return dict(sorted(counts.items()))


def _normalized_latency_diagnostics(value: Any) -> dict[str, Any]:
    diagnostics = _as_dict(value)
    runtime_probe_candidate_decision = _as_dict(
        diagnostics.get("runtime_probe_candidate_decision")
        or diagnostics.get("runtimeProbeCandidateDecision"),
    )
    candidate_decision = _as_dict(
        diagnostics.get("candidate_decision") or diagnostics.get("candidateDecision"),
    )
    strategy_candidate_decision_observed = _int_value(candidate_decision.get("count")) > 0
    if not strategy_candidate_decision_observed and runtime_probe_candidate_decision:
        candidate_decision = runtime_probe_candidate_decision
    candidate_decision_source = diagnostics.get("candidate_decision_source") or diagnostics.get(
        "candidateDecisionSource",
    )
    if not candidate_decision_source:
        if strategy_candidate_decision_observed:
            candidate_decision_source = "strategy"
        elif runtime_probe_candidate_decision:
            candidate_decision_source = "runtime_probe"
        else:
            candidate_decision_source = "none"
    return {
        "quoteEventToStrategy": _as_dict(
            diagnostics.get("quote_event_to_strategy") or diagnostics.get("quoteEventToStrategy"),
        ),
        "quotePublishToStrategy": _as_dict(
            diagnostics.get("quote_publish_to_strategy")
            or diagnostics.get("quotePublishToStrategy"),
        ),
        "quoteFetchLatency": _as_dict(
            diagnostics.get("quote_fetch_latency") or diagnostics.get("quoteFetchLatency"),
        ),
        "instrumentRefreshReconcile": _as_dict(
            diagnostics.get("instrument_refresh_reconcile")
            or diagnostics.get("instrumentRefreshReconcile"),
        ),
        "graphScan": _as_dict(diagnostics.get("graph_scan") or diagnostics.get("graphScan")),
        "candidateDecision": candidate_decision,
        "runtimeProbeCandidateDecision": runtime_probe_candidate_decision,
        "candidateDecisionSource": str(candidate_decision_source),
        "rawSloStatus": _as_dict(diagnostics.get("sloStatus")),
        "rawDiagnosticWarnings": [
            str(item) for item in diagnostics.get("diagnosticWarnings") or []
        ],
        "orderConstruction": _as_dict(
            diagnostics.get("order_construction") or diagnostics.get("orderConstruction"),
        ),
        "orderSubmit": _as_dict(diagnostics.get("order_submit") or diagnostics.get("orderSubmit")),
        "byVenue": _normalized_latency_by_venue(
            diagnostics.get("by_venue") or diagnostics.get("byVenue"),
        ),
    }


def _normalized_latency_by_venue(value: Any) -> dict[str, dict[str, Any]]:
    payload = _as_dict(value)
    normalized: dict[str, dict[str, Any]] = {}
    for venue, raw_stats in sorted(payload.items()):
        stats = _as_dict(raw_stats)
        normalized[str(venue).upper()] = {
            "quoteEventToStrategy": _as_dict(
                stats.get("quote_event_to_strategy") or stats.get("quoteEventToStrategy"),
            ),
            "quotePublishToStrategy": _as_dict(
                stats.get("quote_publish_to_strategy") or stats.get("quotePublishToStrategy"),
            ),
            "quoteFetchLatency": _as_dict(
                stats.get("quote_fetch_latency") or stats.get("quoteFetchLatency"),
            ),
        }
    return normalized


def _merged_latency_warnings(
    latency: dict[str, Any],
    computed: list[str],
) -> list[str]:
    warnings = [str(item) for item in latency.get("rawDiagnosticWarnings") or []]
    warnings.extend(computed)
    return sorted(dict.fromkeys(warnings))


class _SummaryAggregate:
    def __init__(self) -> None:
        self.warning_counts: dict[str, int] = {}
        self.graph_warning_counts: dict[str, int] = {}
        self.latency_warning_counts: dict[str, int] = {}
        self.latency_slo_counts: dict[str, int] = {}
        self.engine_counts: dict[str, int] = {}
        self.topology_counts: dict[str, int] = {}
        self.health_counts: dict[str, int] = {}
        self.provider_poll_health_counts: dict[str, int] = {}
        self.corpus_health_counts: dict[str, int] = {}
        self.venue_coverage_health_counts: dict[str, int] = {}
        self.execution_safety_counts: dict[str, int] = {}
        self.quote_subscription_counts: dict[str, int] = {}
        self.quoted_node_counts: dict[str, int] = {}
        self.semantic_matched_node_counts: dict[str, int] = {}
        self.quoted_semantic_matched_node_counts: dict[str, int] = {}
        self.candidate_counts_by_venue_pair: dict[str, int] = {}
        self.edge_counts_by_venue_pair: dict[str, int] = {}
        self.quoted_edge_counts_by_venue_pair: dict[str, int] = {}
        self.rejection_bucket_counts: dict[str, int] = {}
        self.semantic_blocker_counts: dict[str, int] = {}
        self.zero_candidate_blocker_counts: dict[str, int] = {}
        self.fee_impact_bucket_counts: dict[str, int] = {}
        self.devig_method_counts: dict[str, int] = {}
        self.devig_value_bucket_counts: dict[str, int] = {}
        self.coverage_book_devig_method_counts: dict[str, int] = {}
        self.coverage_book_devig_value_bucket_counts: dict[str, int] = {}
        self.recommended_action_counts: dict[str, int] = {}
        self.provider_corpus_coverage: dict[str, dict[str, Any]] = {}
        self.coverage_book_devig_sampled = 0
        self.coverage_book_devig_quoted = 0
        self.coverage_book_devig_incomplete = 0

    def add(self, summary: dict[str, Any]) -> None:
        graph = _as_dict(summary.get("graph"))
        quality = _as_dict(summary.get("candidateQuality"))
        coverage = _as_dict(summary.get("venueCoverage"))
        self._add_graph_counts(graph)
        self._add_warning_counts(summary, quality)
        self._add_health_counts(summary)
        self._add_venue_coverage(coverage)
        self._add_candidate_quality(quality)
        self._add_provider_corpus(summary)
        self._add_recommended_actions(summary)

    def _add_graph_counts(self, graph: dict[str, Any]) -> None:
        _increment_count(self.engine_counts, graph.get("engine") or "unknown")
        _increment_count(self.topology_counts, graph.get("topologySource") or "unknown")

    def _add_warning_counts(self, summary: dict[str, Any], quality: dict[str, Any]) -> None:
        _increment_all(self.warning_counts, quality.get("diagnosticWarnings"))
        _increment_all(
            self.graph_warning_counts,
            _as_dict(summary.get("graph")).get("diagnosticWarnings"),
        )
        latency = _as_dict(summary.get("latencyDiagnostics"))
        _increment_all(self.latency_warning_counts, latency.get("diagnosticWarnings"))
        slo_status = _as_dict(latency.get("sloStatus"))
        _increment_count(self.latency_slo_counts, slo_status.get("overall") or "unknown")

    def _add_health_counts(self, summary: dict[str, Any]) -> None:
        _increment_count(
            self.health_counts,
            _as_dict(summary.get("operatorHealth")).get("overall") or "unknown",
        )
        _increment_count(
            self.provider_poll_health_counts,
            _as_dict(summary.get("providerPollHealth")).get("overall") or "unknown",
        )
        _increment_count(
            self.corpus_health_counts,
            _as_dict(summary.get("semanticCacheCorpusHealth")).get("overall") or "unknown",
        )
        _increment_count(
            self.venue_coverage_health_counts,
            _as_dict(summary.get("venueCoverageHealth")).get("overall") or "unknown",
        )
        _increment_count(
            self.execution_safety_counts,
            _as_dict(summary.get("executionSafety")).get("overall") or "unknown",
        )

    def _add_venue_coverage(self, coverage: dict[str, Any]) -> None:
        _merge_int_mapping(self.quote_subscription_counts, coverage.get("quoteSubscriptionCounts"))
        _merge_int_mapping(self.quoted_node_counts, coverage.get("quotedNodeCounts"))
        _merge_int_mapping(
            self.semantic_matched_node_counts,
            coverage.get("semanticMatchedNodeCounts"),
        )
        _merge_int_mapping(
            self.quoted_semantic_matched_node_counts,
            coverage.get("quotedSemanticMatchedNodeCounts"),
        )
        _merge_int_mapping(self.candidate_counts_by_venue_pair, coverage.get("candidateCounts"))
        _merge_int_mapping(self.edge_counts_by_venue_pair, coverage.get("edgeCounts"))
        _merge_int_mapping(
            self.quoted_edge_counts_by_venue_pair,
            coverage.get("quotedEdgeCounts"),
        )

    def _add_candidate_quality(self, quality: dict[str, Any]) -> None:
        _merge_nested_count_mapping(self.rejection_bucket_counts, quality.get("rejectionBuckets"))
        _merge_nested_count_mapping(
            self.semantic_blocker_counts,
            quality.get("semanticBlockedReasons"),
        )
        _merge_nested_count_mapping(
            self.zero_candidate_blocker_counts,
            quality.get("zeroCandidateBlockerCounts"),
        )
        _merge_nested_count_mapping(
            self.fee_impact_bucket_counts,
            _as_dict(quality.get("feeAdjustment")).get("impactBuckets"),
        )
        _merge_nested_count_mapping(
            self.devig_method_counts,
            _as_dict(quality.get("devigDiagnostics")).get("methodCounts"),
        )
        _merge_nested_count_mapping(
            self.devig_value_bucket_counts,
            _as_dict(quality.get("devigDiagnostics")).get("valueBuckets"),
        )
        self._add_coverage_book_devig(_as_dict(quality.get("coverageBookDevigDiagnostics")))

    def _add_coverage_book_devig(self, diagnostics: dict[str, Any]) -> None:
        self.coverage_book_devig_sampled += _int_value(diagnostics.get("sampledHyperedges"))
        self.coverage_book_devig_quoted += _int_value(diagnostics.get("quotedHyperedges"))
        self.coverage_book_devig_incomplete += _int_value(diagnostics.get("incompleteHyperedges"))
        _merge_nested_count_mapping(
            self.coverage_book_devig_method_counts,
            diagnostics.get("methodCounts"),
        )
        _merge_nested_count_mapping(
            self.coverage_book_devig_value_bucket_counts,
            diagnostics.get("valueBuckets"),
        )

    def _add_provider_corpus(self, summary: dict[str, Any]) -> None:
        _merge_provider_corpus_coverage(
            self.provider_corpus_coverage,
            _as_dict(_as_dict(summary.get("semanticCache")).get("providerCorpusCoverage")),
        )

    def _add_recommended_actions(self, summary: dict[str, Any]) -> None:
        _increment_all(self.recommended_action_counts, summary.get("recommendedActions"))

    def as_dict(self, summaries: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "artifactCount": len(summaries),
            "statusCounts": _count_values(summary.get("status") for summary in summaries),
            "graphEngineCounts": dict(sorted(self.engine_counts.items())),
            "topologySourceCounts": dict(sorted(self.topology_counts.items())),
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
                _int_value(_as_dict(summary.get("graph")).get("quotedEdges"))
                for summary in summaries
            ),
            "semanticTemplateCount": sum(
                _int_value(_as_dict(summary.get("graph")).get("semanticTemplateCount"))
                for summary in summaries
            ),
            "coverageProofCount": sum(
                _int_value(_as_dict(summary.get("graph")).get("coverageProofCount"))
                for summary in summaries
            ),
            "coverageHyperedgeCount": sum(
                _int_value(_as_dict(summary.get("graph")).get("coverageHyperedgeCount"))
                for summary in summaries
            ),
            "executionSafeEdges": sum(
                _int_value(_as_dict(summary.get("graph")).get("executionSafeEdges"))
                for summary in summaries
            ),
            "sameVenueExecutionEligibleEdges": sum(
                _int_value(_as_dict(summary.get("graph")).get("sameVenueExecutionEligibleEdges"))
                for summary in summaries
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
            "diagnosticWarningCounts": dict(sorted(self.warning_counts.items())),
            "graphDiagnosticWarningCounts": dict(sorted(self.graph_warning_counts.items())),
            "latencyDiagnosticWarningCounts": dict(sorted(self.latency_warning_counts.items())),
            "latencySloStatusCounts": dict(sorted(self.latency_slo_counts.items())),
            "operatorHealthCounts": dict(sorted(self.health_counts.items())),
            "providerPollHealthCounts": dict(sorted(self.provider_poll_health_counts.items())),
            "semanticCacheCorpusHealthCounts": dict(sorted(self.corpus_health_counts.items())),
            "venueCoverageHealthCounts": dict(sorted(self.venue_coverage_health_counts.items())),
            "executionSafetyCounts": dict(sorted(self.execution_safety_counts.items())),
            "quoteSubscriptionCountsByVenue": dict(sorted(self.quote_subscription_counts.items())),
            "quotedNodeCountsByVenue": dict(sorted(self.quoted_node_counts.items())),
            "semanticMatchedNodeCountsByVenue": dict(
                sorted(self.semantic_matched_node_counts.items()),
            ),
            "quotedSemanticMatchedNodeCountsByVenue": dict(
                sorted(self.quoted_semantic_matched_node_counts.items()),
            ),
            "candidateCountsByVenuePair": dict(sorted(self.candidate_counts_by_venue_pair.items())),
            "edgeCountsByVenuePair": dict(sorted(self.edge_counts_by_venue_pair.items())),
            "quotedEdgeCountsByVenuePair": dict(
                sorted(self.quoted_edge_counts_by_venue_pair.items()),
            ),
            "rejectionBucketCounts": dict(sorted(self.rejection_bucket_counts.items())),
            "semanticBlockerCounts": dict(sorted(self.semantic_blocker_counts.items())),
            "zeroCandidateBlockerCounts": dict(sorted(self.zero_candidate_blocker_counts.items())),
            "feeImpactBucketCounts": dict(sorted(self.fee_impact_bucket_counts.items())),
            "devigMethodCounts": dict(sorted(self.devig_method_counts.items())),
            "devigValueBucketCounts": dict(sorted(self.devig_value_bucket_counts.items())),
            "coverageBookDevigSampledHyperedges": self.coverage_book_devig_sampled,
            "coverageBookDevigQuotedHyperedges": self.coverage_book_devig_quoted,
            "coverageBookDevigIncompleteHyperedges": self.coverage_book_devig_incomplete,
            "coverageBookDevigMethodCounts": dict(
                sorted(self.coverage_book_devig_method_counts.items()),
            ),
            "coverageBookDevigValueBucketCounts": dict(
                sorted(self.coverage_book_devig_value_bucket_counts.items()),
            ),
            "providerCorpusCoverage": self.provider_corpus_coverage,
            "recommendedActionCounts": dict(sorted(self.recommended_action_counts.items())),
        }


def aggregate_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate = _SummaryAggregate()
    for summary in summaries:
        aggregate.add(summary)
    return aggregate.as_dict(summaries)


def _increment_count(target: dict[str, int], value: Any) -> None:
    key = str(value or "unknown")
    target[key] = target.get(key, 0) + 1


def _increment_all(target: dict[str, int], values: Any) -> None:
    for value in values or []:
        _increment_count(target, value)


def _merge_int_mapping(target: dict[str, int], value: Any) -> None:
    for key, raw_count in _as_dict(value).items():
        target[str(key)] = target.get(str(key), 0) + _int_value(raw_count)


def _merge_nested_count_mapping(target: dict[str, int], value: Any) -> None:
    for key, raw_count in _as_dict(value).items():
        target[str(key)] = target.get(str(key), 0) + int(_numeric(raw_count))


def _merge_provider_corpus_coverage(
    target: dict[str, dict[str, Any]],
    value: dict[str, Any],
) -> None:
    for provider, raw_report in sorted(value.items(), key=lambda item: str(item[0])):
        report = _as_dict(raw_report)
        provider_key = str(provider)
        existing = target.setdefault(
            provider_key,
            {
                "sportCount": 0,
                "sportsWithSelections": 0,
                "totalSelectionCount": 0,
                "totalEventCount": 0,
                "totalMarketCount": 0,
                "blockerCounts": {},
                "sparseSports": [],
                "zeroSelectionSports": [],
                "requestedSports": [],
                "resolvedSports": [],
                "unresolvedRequestedSports": [],
                "coverageModes": [],
                "liveOnlyObserved": False,
                "preferLiquidMarketsObserved": False,
            },
        )
        existing["sportCount"] = max(
            _int_value(existing.get("sportCount")),
            _int_value(report.get("sport_count")),
        )
        existing["sportsWithSelections"] = max(
            _int_value(existing.get("sportsWithSelections")),
            _int_value(report.get("sports_with_selections")),
        )
        existing["totalSelectionCount"] = max(
            _int_value(existing.get("totalSelectionCount")),
            _int_value(report.get("total_selection_count")),
        )
        existing["totalEventCount"] = max(
            _int_value(existing.get("totalEventCount")),
            _int_value(report.get("total_event_count")),
        )
        existing["totalMarketCount"] = max(
            _int_value(existing.get("totalMarketCount")),
            _int_value(report.get("total_market_count")),
        )
        blocker_counts = existing.get("blockerCounts")
        if not isinstance(blocker_counts, dict):
            blocker_counts = {}
            existing["blockerCounts"] = blocker_counts
        _merge_int_mapping(blocker_counts, report.get("blocker_counts"))
        existing["sparseSports"] = sorted(
            {
                *[str(item) for item in existing.get("sparseSports", [])],
                *[str(item) for item in report.get("sparse_sports", []) if item],
            },
        )
        existing["zeroSelectionSports"] = sorted(
            {
                *[str(item) for item in existing.get("zeroSelectionSports", [])],
                *[str(item) for item in report.get("zero_selection_sports", []) if item],
            },
        )
        existing["requestedSports"] = sorted(
            {
                *[str(item) for item in existing.get("requestedSports", [])],
                *[str(item) for item in report.get("requested_sports", []) if item],
            },
        )
        existing["resolvedSports"] = sorted(
            {
                *[str(item) for item in existing.get("resolvedSports", [])],
                *[str(item) for item in report.get("resolved_sports", []) if item],
            },
        )
        existing["unresolvedRequestedSports"] = sorted(
            {
                *[str(item) for item in existing.get("unresolvedRequestedSports", [])],
                *[str(item) for item in report.get("unresolved_requested_sports", []) if item],
            },
        )
        coverage_mode = report.get("coverage_mode")
        if coverage_mode:
            existing["coverageModes"] = sorted(
                {
                    *[str(item) for item in existing.get("coverageModes", [])],
                    str(coverage_mode),
                },
            )
        existing["liveOnlyObserved"] = bool(existing.get("liveOnlyObserved")) or bool(
            report.get("live_only"),
        )
        existing["preferLiquidMarketsObserved"] = bool(
            existing.get("preferLiquidMarketsObserved"),
        ) or bool(report.get("prefer_liquid_markets"))


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
    margin_bands = _as_dict(candidate_quality.get("marginBands"))
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
    live_timing = _as_dict(candidate_quality.get("liveTimingSlo"))
    quote_age_slo = _as_dict(live_timing.get("quoteAge"))
    fetch_latency_slo = _as_dict(live_timing.get("fetchLatency"))
    pair_skew_slo = _as_dict(live_timing.get("pairSkew"))
    if _int_value(quote_age_slo.get("violations")) > 0:
        warnings.append("live_quote_age_slo_violations")
    if _int_value(fetch_latency_slo.get("violations")) > 0:
        warnings.append("live_fetch_latency_slo_violations")
    if _int_value(pair_skew_slo.get("violations")) > 0:
        warnings.append("live_pair_skew_slo_violations")
    positive_observations = _numeric(rejection_buckets.get("positive")) + _numeric(
        margin_bands.get("positive"),
    )
    near_miss_observations = sum(
        _numeric(rejection_buckets.get(key)) for key in ("negative_margin", "below_threshold")
    ) + sum(_numeric(margin_bands.get(key)) for key in ("0% to -1%", "-1% to -2%", "-2% to -5%"))
    if positive_observations > 0 and not candidate_quality.get("topPositiveCandidates"):
        warnings.append("missing_top_positive_candidates")
    if near_miss_observations > 0 and not candidate_quality.get("topNegativeNearMisses"):
        warnings.append("missing_top_negative_near_misses")
    return warnings


def _graph_diagnostic_warnings(graph: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    semantic_template_count = _int_value(graph.get("semanticTemplateCount"))
    coverage_proof_count = _int_value(graph.get("coverageProofCount"))
    coverage_hyperedge_count = _int_value(graph.get("coverageHyperedgeCount"))
    coverage_diagnostics = _as_dict(graph.get("coverageDiagnostics"))
    if semantic_template_count > 0 and coverage_proof_count == 0:
        warnings.append("semantic_templates_without_coverage_proofs")
    if semantic_template_count > 0 and coverage_hyperedge_count == 0:
        warnings.append("semantic_templates_without_coverage_hyperedges")
    if coverage_proof_count > 0 and not coverage_diagnostics:
        warnings.append("coverage_proofs_without_diagnostics")
    return warnings


def _latency_diagnostic_warnings(
    latency: dict[str, Any],
    *,
    quoted_edges: int,
    positive_candidates: int,
    threshold_candidates: int,
) -> list[str]:
    warnings: list[str] = []
    has_candidate_activity = quoted_edges > 0 or positive_candidates > 0 or threshold_candidates > 0
    has_any_latency = any(isinstance(section, dict) and section for section in latency.values())
    if has_candidate_activity and not has_any_latency:
        return ["missing_strategy_latency_diagnostics"]
    if has_candidate_activity and _int_value(_as_dict(latency.get("graphScan")).get("count")) == 0:
        warnings.append("missing_graph_scan_latency")
    if (
        has_candidate_activity
        and _int_value(_as_dict(latency.get("candidateDecision")).get("count")) == 0
    ):
        warnings.append("missing_candidate_decision_latency")
    if (
        quoted_edges > 0
        and _int_value(_as_dict(latency.get("quoteEventToStrategy")).get("count")) == 0
    ):
        warnings.append("missing_quote_event_to_strategy_latency")
    if (
        quoted_edges > 0
        and _int_value(_as_dict(latency.get("quotePublishToStrategy")).get("count")) == 0
    ):
        warnings.append("missing_quote_publish_to_strategy_latency")
    return warnings


def _latency_slo_status(
    *,
    candidate_quality: dict[str, Any],
    latency: dict[str, Any],
) -> dict[str, Any]:
    live_timing = _as_dict(candidate_quality.get("liveTimingSlo"))
    histograms = _as_dict(candidate_quality.get("latencyHistograms"))
    quote_age = _slo_section_status(
        _as_dict(live_timing.get("quoteAge")),
        fallback=_histogram_slo_status(
            _as_dict(histograms.get("quoteAgeSeconds")),
            threshold_seconds=5.0,
        ),
    )
    fetch_latency = _slo_section_status(
        _as_dict(live_timing.get("fetchLatency")),
        fallback=_histogram_slo_status(
            _as_dict(histograms.get("fetchLatencySeconds")),
            threshold_seconds=5.0,
            ms_histogram=_as_dict(latency.get("quoteFetchLatency")),
        ),
    )
    pair_skew = _slo_section_status(
        _as_dict(live_timing.get("pairSkew")),
        fallback=_histogram_slo_status(
            _as_dict(histograms.get("pairSkewSeconds")),
            threshold_seconds=1.0,
        ),
    )
    strategy_latency = {
        "graphScanObserved": _int_value(_as_dict(latency.get("graphScan")).get("count")) > 0,
        "candidateDecisionObserved": _int_value(
            _as_dict(latency.get("candidateDecision")).get("count"),
        )
        > 0,
        "candidateDecisionSource": latency.get("candidateDecisionSource"),
        "quoteReceiveObserved": (
            _int_value(_as_dict(latency.get("quoteEventToStrategy")).get("count")) > 0
            or _int_value(_as_dict(latency.get("quotePublishToStrategy")).get("count")) > 0
        ),
        "providerLatencyObserved": (
            _int_value(_as_dict(histograms.get("fetchLatencySeconds")).get("count")) > 0
            or _int_value(_as_dict(latency.get("quoteFetchLatency")).get("count")) > 0
        ),
    }
    statuses = [
        quote_age["status"],
        fetch_latency["status"],
        pair_skew["status"],
    ]
    if any(status == "fail" for status in statuses):
        overall = "fail"
    elif any(status == "pass" for status in statuses):
        overall = "pass"
    else:
        overall = "no_observations"
    return {
        "overall": overall,
        "quoteAge": quote_age,
        "fetchLatency": fetch_latency,
        "pairSkew": pair_skew,
        "strategyLatency": strategy_latency,
    }


def _operator_health(
    *,
    candidate_warnings: list[str],
    graph_warnings: list[str],
    latency_warnings: list[str],
    latency_slo_status: dict[str, Any],
    execution_safety_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if latency_slo_status.get("overall") == "fail":
        reasons.append("latency_slo_failed")
    execution_safety = _as_dict(execution_safety_status)
    if execution_safety.get("overall") in {"warn", "fail"}:
        reasons.extend(f"execution:{reason}" for reason in execution_safety.get("reasons") or [])
    reasons.extend(f"candidate:{warning}" for warning in candidate_warnings)
    reasons.extend(f"graph:{warning}" for warning in graph_warnings)
    reasons.extend(f"latency:{warning}" for warning in latency_warnings)
    if latency_slo_status.get("overall") == "fail" or execution_safety.get("overall") == "fail":
        overall = "fail"
    elif reasons:
        overall = "warn"
    else:
        overall = "pass"
    return {
        "overall": overall,
        "reasonCount": len(reasons),
        "reasons": reasons,
    }


def _execution_safety_health(execution_readiness: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    validation_mode = bool(execution_readiness.get("validationMode"))
    auto_execute = bool(execution_readiness.get("autoExecute"))
    live_execution_armed = bool(execution_readiness.get("liveExecutionArmed"))
    live_execution_env_armed = bool(execution_readiness.get("liveExecutionEnvArmed"))
    if auto_execute:
        if live_execution_armed and not live_execution_env_armed:
            reasons.append("auto_execute_env_gate_unarmed")
        else:
            reasons.append("auto_execute_enabled")
    venues = execution_readiness.get("venues") or []
    if isinstance(venues, list):
        for venue in venues:
            if not isinstance(venue, dict):
                continue
            if (
                validation_mode
                and bool(venue.get("executionEnabled"))
                and not bool(venue.get("executionDryRun"))
            ):
                reasons.append(f"{venue.get('venue')}:validation_execution_not_dry_run")
    auto_execute_blocked_by_env_gate = (
        auto_execute and live_execution_armed and not live_execution_env_armed
    )
    overall = (
        "fail"
        if auto_execute and not auto_execute_blocked_by_env_gate
        else "warn"
        if reasons
        else "pass"
    )
    return {
        "overall": overall,
        "reasons": reasons,
        "validationMode": validation_mode,
        "autoExecute": auto_execute,
        "liveExecutionArmed": live_execution_armed,
        "liveExecutionEnvArmed": live_execution_env_armed,
    }


def _histogram_slo_status(
    histogram: dict[str, Any],
    *,
    threshold_seconds: float,
    ms_histogram: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observations = _int_value(histogram.get("count"))
    p95 = _float_value(histogram.get("p95"))
    max_value = _float_value(histogram.get("max"))
    if observations <= 0 and ms_histogram:
        observations = _int_value(ms_histogram.get("count"))
        p95 = _float_value(ms_histogram.get("p95_ms")) / 1000.0
        max_value = _float_value(ms_histogram.get("max_ms")) / 1000.0
    violations = observations if observations > 0 and max(p95, max_value) > threshold_seconds else 0
    return {
        "status": "fail" if violations else "pass" if observations else "no_observations",
        "observations": observations,
        "violations": violations,
        "violationRate": 1.0 if violations else 0.0,
        "thresholdSeconds": threshold_seconds,
        "minThresholdSeconds": None,
        "maxThresholdSeconds": None,
        "thresholdMode": "histogram_p95_or_max",
    }


def _slo_section_status(
    section: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observations = _int_value(section.get("observations"))
    if observations <= 0 and fallback is not None and _int_value(fallback.get("observations")) > 0:
        return fallback
    violations = _int_value(section.get("violations"))
    if observations <= 0:
        status = "no_observations"
    elif violations > 0:
        status = "fail"
    else:
        status = "pass"
    return {
        "status": status,
        "observations": observations,
        "violations": violations,
        "violationRate": round((violations / observations) if observations else 0.0, 6),
        "thresholdSeconds": section.get("thresholdSeconds"),
        "minThresholdSeconds": section.get("minThresholdSeconds"),
        "maxThresholdSeconds": section.get("maxThresholdSeconds"),
        "thresholdMode": section.get("thresholdMode"),
    }


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
        _format_operator_health_line(summary.get("operatorHealth")),
        _format_execution_readiness_line(summary.get("executionReadiness")),
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
    execution_safety = _format_execution_safety_line(summary.get("executionSafety"))
    if execution_safety:
        lines.append(execution_safety)
    lines.extend(_format_fee_policy_lines(summary.get("feePolicy")))
    lines.extend(_format_semantic_cache_family_lines(summary.get("semanticCache")))
    lines.extend(_format_coverage_lines(graph))
    lines.extend(_format_quality_lines(quality))
    lines.extend(_format_latency_lines(summary.get("latencyDiagnostics")))
    provider_poll = _format_provider_poll_stats(summary.get("providerQuotePollStats"))
    if provider_poll:
        lines.append(f"  provider_poll {provider_poll}")
    provider_poll_health = _format_provider_poll_health(summary.get("providerPollHealth"))
    if provider_poll_health:
        lines.append(f"  provider_poll_health {provider_poll_health}")
    venue_coverage_health = _format_venue_coverage_health(summary.get("venueCoverageHealth"))
    if venue_coverage_health:
        lines.append(f"  venue_coverage_health {venue_coverage_health}")
    lines.extend(
        _format_semantic_cache_corpus_health_lines(summary.get("semanticCacheCorpusHealth")),
    )
    lines.extend(_format_refresh_lines(summary.get("instrumentRefresh")))
    lines.extend(_format_semantic_diagnostic_lines(summary.get("semanticDiagnostics")))
    lines.extend(_format_provider_corpus_coverage_lines(summary.get("semanticCache")))
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
    graph_warnings = graph.get("diagnosticWarnings") or []
    if warnings:
        lines.append(f"  warnings {', '.join(warnings)}")
    if graph_warnings:
        lines.append(f"  graph_warnings {', '.join(graph_warnings)}")
    latency_warnings = _as_dict(summary.get("latencyDiagnostics")).get("diagnosticWarnings") or []
    if latency_warnings:
        lines.append(f"  latency_warnings {', '.join(latency_warnings)}")
    recommended_actions = summary.get("recommendedActions") or []
    if isinstance(recommended_actions, list) and recommended_actions:
        lines.append(
            "  recommended_actions " + ", ".join(str(action) for action in recommended_actions),
        )
    return "\n".join(lines)


def _format_operator_health_line(value: Any) -> str:
    health = value if isinstance(value, dict) else {}
    reasons = health.get("reasons") or []
    rendered_reasons = ", ".join(str(reason) for reason in reasons[:5])
    return (
        "  operator_health "
        f"overall={health.get('overall')} "
        f"reason_count={health.get('reasonCount', 0)} "
        f"reasons=[{rendered_reasons}]"
    )


def _format_fee_policy_lines(value: Any) -> list[str]:
    fee_policy = value if isinstance(value, dict) else {}
    taker = _as_dict(fee_policy.get("venueTakerFeeRates"))
    maker_rebate = _as_dict(fee_policy.get("venueMakerRebateRates"))
    winning = _as_dict(fee_policy.get("venueWinningProfitFeeRates"))
    basket_rebate = _as_dict(fee_policy.get("venueBasketRebateRates"))
    basket_boost = _as_dict(fee_policy.get("venueBasketBoostRates"))
    if not taker and not maker_rebate and not winning and not basket_rebate and not basket_boost:
        return []
    return [
        "  fee_policy "
        f"taker={dict(sorted(taker.items()))} "
        f"maker_rebate={dict(sorted(maker_rebate.items()))} "
        f"winning_profit={dict(sorted(winning.items()))} "
        f"basket_rebate={dict(sorted(basket_rebate.items()))} "
        f"basket_boost={dict(sorted(basket_boost.items()))}",
    ]


def _format_execution_readiness_line(value: Any) -> str:
    readiness = value if isinstance(value, dict) else {}
    venues = readiness.get("venues") or []
    rendered_venues: list[str] = []
    if isinstance(venues, list):
        for venue in venues:
            if not isinstance(venue, dict):
                continue
            rendered_venues.append(
                (
                    f"{venue.get('venue')}("
                    f"exec={venue.get('executionEnabled', False)},"
                    f"dry_run={venue.get('executionDryRun', False)},"
                    f"env={venue.get('environment')},"
                    f"base={venue.get('baseCurrency')})"
                ),
            )
    return (
        "  execution_readiness "
        f"validation_mode={readiness.get('validationMode')} "
        f"auto_execute={readiness.get('autoExecute')} "
        f"semantic_cache={readiness.get('semanticCacheConfigured')} "
        f"venues=[{', '.join(rendered_venues)}]"
    )


def _format_execution_safety_line(value: Any) -> str:
    safety = value if isinstance(value, dict) else {}
    if not safety:
        return ""
    reasons = safety.get("reasons") or []
    return (
        "  execution_safety "
        f"overall={safety.get('overall')} "
        f"auto_execute={safety.get('autoExecute')} "
        f"reasons=[{', '.join(str(reason) for reason in reasons[:5])}]"
    )


def _format_semantic_cache_family_lines(value: Any) -> list[str]:
    semantic_cache = value if isinstance(value, dict) else {}
    family_sections = (
        ("families", semantic_cache.get("promotedMarketFamilyCounts")),
        ("execution_safe_families", semantic_cache.get("executionSafeMarketFamilyCounts")),
        ("same_venue_families", semantic_cache.get("sameVenueEligibleMarketFamilyCounts")),
    )
    lines: list[str] = []
    for label, counts in family_sections:
        top_counts = _top_items(_as_dict(counts), limit=5)
        if not top_counts:
            continue
        rendered = " ".join(f"{item['key']}={item['value']}" for item in top_counts)
        lines.append(f"  semantic_cache_{label} {rendered}")
    return lines


def _format_coverage_lines(graph: dict[str, Any]) -> list[str]:
    coverage_diagnostics = graph.get("coverageDiagnostics") or {}
    lines: list[str] = []
    if isinstance(coverage_diagnostics, dict) and coverage_diagnostics:
        lines.append(
            "  coverage_execution_safe "
            f"proofs={coverage_diagnostics.get('executionSafeCoverageProofCount', 0)} "
            f"hyperedges={coverage_diagnostics.get('executionSafeCoverageHyperedgeCount', 0)}",
        )
        blocker_counts = _as_dict(coverage_diagnostics.get("proofBlockerReasonCounts"))
        if blocker_counts:
            rendered = ", ".join(
                f"{item['key']}={item['value']}" for item in _top_items(blocker_counts, limit=5)
            )
            lines.append(f"  coverage_blockers {rendered}")
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
    lines.extend(_format_zero_fixture_proof_blocker_lines(quality))
    fee_adjustment = _as_dict(quality.get("feeAdjustment"))
    fee_impact = _as_dict(fee_adjustment.get("impactBuckets"))
    if fee_impact:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(fee_impact.items()))
        lines.append(f"  fee_impact {rendered}")
    lines.extend(_format_devig_quality_lines(quality.get("devigDiagnostics")))
    lines.extend(_format_coverage_book_devig_lines(quality.get("coverageBookDevigDiagnostics")))
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
    live_timing_slo = quality.get("liveTimingSlo") or {}
    if isinstance(live_timing_slo, dict) and live_timing_slo:
        quote_age = _as_dict(live_timing_slo.get("quoteAge"))
        fetch_latency = _as_dict(live_timing_slo.get("fetchLatency"))
        pair_skew = _as_dict(live_timing_slo.get("pairSkew"))
        if any(
            _int_value(item.get("observations"))
            for item in (quote_age, fetch_latency, pair_skew)
            if isinstance(item, dict)
        ):
            lines.append(
                "  live_timing_slo "
                f"quote_age={quote_age.get('violations', 0)}/{quote_age.get('observations', 0)} "
                f"fetch_latency={fetch_latency.get('violations', 0)}/{fetch_latency.get('observations', 0)} "
                f"pair_skew={pair_skew.get('violations', 0)}/{pair_skew.get('observations', 0)}",
            )
    return lines


def _format_zero_fixture_proof_blocker_lines(quality: dict[str, Any]) -> list[str]:
    zero_fixture_blockers = quality.get("zeroCandidateFixtureProofBlockerCounts") or {}
    if not isinstance(zero_fixture_blockers, dict) or not zero_fixture_blockers:
        return []
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(zero_fixture_blockers.items()))
    return [f"  zero_candidate_fixture_proof_blockers {rendered}"]


def _format_devig_quality_lines(value: Any) -> list[str]:
    devig = _as_dict(value)
    if not devig:
        return []
    lines: list[str] = []
    value_buckets = _as_dict(devig.get("valueBuckets"))
    if value_buckets:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(value_buckets.items()))
        lines.append(f"  devig_value {rendered}")
    method_counts = _as_dict(devig.get("methodCounts"))
    if method_counts:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(method_counts.items()))
        lines.append(f"  devig_methods {rendered}")
    return lines


def _format_coverage_book_devig_lines(value: Any) -> list[str]:
    coverage_devig = _as_dict(value)
    if not coverage_devig:
        return []
    sampled = _int_value(coverage_devig.get("sampledHyperedges"))
    quoted = _int_value(coverage_devig.get("quotedHyperedges"))
    if sampled <= 0 and quoted <= 0:
        return []
    lines = [
        "  coverage_book_devig "
        f"sampled={sampled} "
        f"quoted={quoted} "
        f"incomplete={coverage_devig.get('incompleteHyperedges', 0)}",
    ]
    value_buckets = _as_dict(coverage_devig.get("valueBuckets"))
    if value_buckets:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(value_buckets.items()))
        lines.append(f"  coverage_book_value {rendered}")
    return lines


def _format_latency_lines(value: Any) -> list[str]:
    latency = value if isinstance(value, dict) else {}
    if not latency:
        return []
    lines: list[str] = []
    summary_bits: list[str] = []
    for label, payload in (
        ("quote_event", _as_dict(latency.get("quoteEventToStrategy"))),
        ("quote_publish", _as_dict(latency.get("quotePublishToStrategy"))),
        ("quote_fetch", _as_dict(latency.get("quoteFetchLatency"))),
        ("graph_scan", _as_dict(latency.get("graphScan"))),
        ("candidate_decision", _as_dict(latency.get("candidateDecision"))),
        ("order_construction", _as_dict(latency.get("orderConstruction"))),
        ("order_submit", _as_dict(latency.get("orderSubmit"))),
    ):
        if _int_value(payload.get("count")) <= 0:
            continue
        summary_bits.append(
            f"{label}_p95={payload.get('p95_ms', 0)}ms "
            f"p99={payload.get('p99_ms', 0)}ms "
            f"max={payload.get('max_ms', 0)}ms",
        )
    if summary_bits:
        lines.append(f"  strategy_latency {'; '.join(summary_bits)}")
    by_venue = _as_dict(latency.get("byVenue"))
    venue_bits: list[str] = []
    for venue, raw_payload in sorted(by_venue.items()):
        payload = _as_dict(raw_payload)
        quote_event = _as_dict(payload.get("quoteEventToStrategy"))
        quote_fetch = _as_dict(payload.get("quoteFetchLatency"))
        if _int_value(quote_event.get("count")) <= 0 and _int_value(quote_fetch.get("count")) <= 0:
            continue
        venue_bits.append(
            f"{venue}:quote_event_p95={quote_event.get('p95_ms', 0)}ms "
            f"quote_fetch_p95={quote_fetch.get('p95_ms', 0)}ms "
            f"quote_fetch_max={quote_fetch.get('max_ms', 0)}ms",
        )
    if venue_bits:
        lines.append(f"  strategy_latency_by_venue {'; '.join(venue_bits)}")
    slo_status = _as_dict(latency.get("sloStatus"))
    if slo_status:
        quote_age = _as_dict(slo_status.get("quoteAge"))
        fetch_latency = _as_dict(slo_status.get("fetchLatency"))
        pair_skew = _as_dict(slo_status.get("pairSkew"))
        lines.append(
            "  latency_slo "
            f"overall={slo_status.get('overall')} "
            f"quote_age={quote_age.get('status')} "
            f"fetch_latency={fetch_latency.get('status')} "
            f"pair_skew={pair_skew.get('status')}",
        )
    reconcile = _as_dict(latency.get("instrumentRefreshReconcile"))
    if _int_value(reconcile.get("count")) > 0:
        lines.append(
            "  refresh_reconcile_latency "
            f"p95={reconcile.get('p95_ms', 0)}ms "
            f"p99={reconcile.get('p99_ms', 0)}ms "
            f"max={reconcile.get('max_ms', 0)}ms",
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
            f"{item.get('key')}={item.get('value', item.get('count', item.get('total', 0)))}"
            for item in top_patterns
            if isinstance(item, dict)
        )
        if rendered:
            lines.append(f"  unsupported_provider_patterns {rendered}")
    return lines


def _format_provider_corpus_coverage_lines(value: Any) -> list[str]:
    semantic_cache = value if isinstance(value, dict) else {}
    coverage = semantic_cache.get("providerCorpusCoverage")
    if not isinstance(coverage, dict) or not coverage:
        return []
    lines: list[str] = []
    for provider, report in sorted(coverage.items(), key=lambda item: str(item[0])):
        if not isinstance(report, dict):
            continue
        blocker_counts = report.get("blocker_counts")
        sparse_sports = report.get("sparse_sports")
        zero_selection_sports = report.get("zero_selection_sports")
        unresolved_requested_sports = report.get("unresolved_requested_sports")
        lines.append(
            "  corpus_coverage "
            f"provider={provider} "
            f"mode={report.get('coverage_mode') or 'unknown'} "
            f"sports={report.get('sports_with_selections', 0)}/{report.get('sport_count', 0)} "
            f"selections={report.get('total_selection_count', 0)} "
            f"events={report.get('total_event_count', 0)} "
            f"markets={report.get('total_market_count', 0)} "
            f"blockers={blocker_counts if isinstance(blocker_counts, dict) else {}} "
            "unresolved="
            f"{unresolved_requested_sports if isinstance(unresolved_requested_sports, list) else []} "
            f"sparse={sparse_sports if isinstance(sparse_sports, list) else []} "
            f"zero={zero_selection_sports if isinstance(zero_selection_sports, list) else []}",
        )
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
            f"requests={stats.get('request_count', 0)} "
            f"event_requests={stats.get('event_request_count', 0)} "
            f"line_requests={stats.get('line_request_count', 0)} "
            f"pruned={stats.get('pruned_subscription_count', 0)} "
            f"refilled={stats.get('refilled_subscription_count', 0)} "
            f"markets={stats.get('market_count', 0)} "
            f"cycle_elapsed={stats.get('cycle_elapsed_secs', 0)}s "
            f"target={stats.get('poll_target_cycle_secs', 0)}s "
            f"next_sleep={stats.get('next_poll_sleep_secs', 0)}s "
            f"max_fetch={stats.get('max_fetch_latency_secs', 0)}s "
            f"fetch_p95={stats.get('fetch_latency_p95_secs', 0)}s "
            f"concurrency={stats.get('concurrency', 0)}/{stats.get('max_concurrency', 0)} "
            f"ts={stats.get('quote_event_timestamp_source', '')}->{stats.get('quote_init_timestamp_source', '')} "
            f"backlog={stats.get('backlog_count', 0)} "
            f"failures={stats.get('failure_count', 0)} "
            f"rate_limits={stats.get('rate_limit_count', 0)} "
            f"backoff={stats.get('backoff_secs', 0)}s",
        )
    return "; ".join(rendered)


def _format_provider_poll_health(value: Any) -> str:
    health = value if isinstance(value, dict) else {}
    venues = _as_dict(health.get("venues"))
    if not venues:
        return ""
    rendered = []
    for venue, raw_payload in sorted(venues.items()):
        payload = _as_dict(raw_payload)
        reasons = payload.get("reasons") or []
        rendered.append(
            f"{venue}:status={payload.get('status')} "
            f"shards={payload.get('estimatedShardsForTarget', 1)} "
            f"headroom={payload.get('cycleHeadroomSeconds', 0)}s "
            f"fanout={payload.get('requestFanoutPerQuote', 0)} "
            f"yield={payload.get('quoteYieldRatio', 0)} "
            f"reasons={','.join(str(r) for r in reasons)}",
        )
    return f"overall={health.get('overall')} " + "; ".join(rendered)


def _format_semantic_cache_corpus_health(value: Any) -> str:
    health = value if isinstance(value, dict) else {}
    providers = _as_dict(health.get("providers"))
    if not providers:
        return ""
    rendered = []
    for provider, raw_payload in sorted(providers.items()):
        payload = _as_dict(raw_payload)
        reasons = payload.get("reasons") or []
        rendered.append(
            f"{provider}:status={payload.get('status')} reasons={','.join(str(r) for r in reasons)}",
        )
    return f"overall={health.get('overall')} " + "; ".join(rendered)


def _format_semantic_cache_corpus_health_lines(value: Any) -> list[str]:
    rendered = _format_semantic_cache_corpus_health(value)
    return [f"  semantic_cache_corpus_health {rendered}"] if rendered else []


def _format_venue_coverage_health(value: Any) -> str:
    health = value if isinstance(value, dict) else {}
    venues = _as_dict(health.get("venues"))
    if not venues:
        return ""
    rendered = []
    for venue, raw_payload in sorted(venues.items()):
        payload = _as_dict(raw_payload)
        reasons = payload.get("reasons") or []
        rendered.append(
            f"{venue}:status={payload.get('status')} reasons={','.join(str(r) for r in reasons)}",
        )
    return f"overall={health.get('overall')} " + "; ".join(rendered)


def _format_aggregate_line(aggregate: dict[str, Any]) -> str:
    return (
        "\naggregate: "
        f"artifacts={aggregate['artifactCount']} "
        f"positive={aggregate['positiveCandidates']} "
        f"threshold={aggregate['thresholdCandidates']} "
        f"cross_venue={aggregate['crossVenueCandidates']} "
        f"quoted_semantic={aggregate['quotedSemanticMatchInstruments']} "
        f"edges={aggregate['graphEdges']} "
        f"quoted_edges={aggregate['quotedEdges']} "
        f"coverage_proofs={aggregate['coverageProofCount']} "
        f"hyperedges={aggregate['coverageHyperedgeCount']} "
        f"warnings={aggregate['diagnosticWarningCounts']} "
        f"latency_slo={aggregate['latencySloStatusCounts']} "
        f"health={aggregate['operatorHealthCounts']} "
        f"execution_safety={aggregate['executionSafetyCounts']} "
        f"provider_poll_health={aggregate['providerPollHealthCounts']} "
        f"corpus_health={aggregate['semanticCacheCorpusHealthCounts']} "
        f"venue_coverage_health={aggregate['venueCoverageHealthCounts']} "
        f"devig_methods={aggregate['devigMethodCounts']} "
        f"devig_values={aggregate['devigValueBucketCounts']} "
        f"coverage_book_devig_quoted={aggregate['coverageBookDevigQuotedHyperedges']} "
        f"coverage_book_devig_values={aggregate['coverageBookDevigValueBucketCounts']} "
        f"actions={aggregate['recommendedActionCounts']}"
    )


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
        "--fail-on-latency-slo",
        action="store_true",
        help="Return non-zero when any artifact has live latency SLO violations",
    )
    parser.add_argument(
        "--fail-on-operator-health",
        choices=("warn", "fail"),
        default=None,
        help="Return non-zero when operator health is at or above the chosen severity",
    )
    parser.add_argument(
        "--fail-on-provider-poll-health",
        choices=("warn", "fail"),
        default=None,
        help="Return non-zero when provider poll health is at or above the chosen severity",
    )
    parser.add_argument(
        "--fail-on-venue-coverage-health",
        choices=("warn", "fail"),
        default=None,
        help="Return non-zero when venue coverage health is at or above the chosen severity",
    )
    parser.add_argument(
        "--require-auto-execute-false",
        action="store_true",
        help="Return non-zero if any artifact allows strategy auto execution",
    )
    parser.add_argument(
        "--require-validation-mode",
        action="store_true",
        help="Return non-zero if any artifact is not running in validation mode",
    )
    parser.add_argument(
        "--require-live-execution-env-unarmed",
        action="store_true",
        help="Return non-zero if any artifact has the live execution environment gate armed",
    )
    parser.add_argument(
        "--require-cross-currency-live-blocked",
        action="store_true",
        help="Return non-zero if any artifact permits cross-currency live execution",
    )
    parser.add_argument(
        "--require-rust-semantic",
        action="store_true",
        help="Return non-zero unless every artifact uses rust/rust_semantic topology",
    )
    parser.add_argument(
        "--require-coverage-runtime",
        action="store_true",
        help="Return non-zero unless every artifact exposes nonzero coverage proofs and hyperedges",
    )
    parser.add_argument(
        "--min-positive-candidates",
        type=int,
        default=None,
        help="Return non-zero unless aggregate positive candidates meet this count",
    )
    parser.add_argument(
        "--min-threshold-candidates",
        type=int,
        default=None,
        help="Return non-zero unless aggregate threshold candidates meet this count",
    )
    parser.add_argument(
        "--min-cross-venue-candidates",
        type=int,
        default=None,
        help="Return non-zero unless aggregate cross-venue candidates meet this count",
    )
    parser.add_argument(
        "--min-quoted-semantic-instruments",
        type=int,
        default=None,
        help="Return non-zero unless aggregate quoted semantic instruments meet this count",
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
    aggregate = aggregate_summaries(summaries)
    if args.format == "text":
        print("\n".join(_format_text(Path(item["path"]), item) for item in summaries))
        if args.aggregate:
            print(_format_aggregate_line(aggregate))
    else:
        payload: object = (
            {
                "artifacts": summaries,
                "aggregate": aggregate,
            }
            if args.aggregate
            else summaries
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
    return _gate_exit_code(args, summaries=summaries, aggregate=aggregate)


def _gate_exit_code(
    args: argparse.Namespace,
    *,
    summaries: list[dict[str, Any]],
    aggregate: dict[str, Any],
) -> int:
    gate_checks = [
        (2, args.fail_on_warning, _has_candidate_warnings(summaries)),
        (3, args.fail_on_latency_slo, _has_latency_slo_failure(summaries)),
        (
            4,
            args.fail_on_operator_health,
            _has_health_at_or_above(summaries, "operatorHealth", args.fail_on_operator_health),
        ),
        (
            5,
            args.fail_on_provider_poll_health,
            _has_health_at_or_above(
                summaries,
                "providerPollHealth",
                args.fail_on_provider_poll_health,
            ),
        ),
        (
            6,
            args.fail_on_venue_coverage_health,
            _has_health_at_or_above(
                summaries,
                "venueCoverageHealth",
                args.fail_on_venue_coverage_health,
            ),
        ),
        (7, args.require_auto_execute_false, _has_auto_execute_enabled(summaries)),
        (8, args.require_validation_mode, _has_non_validation_mode(summaries)),
        (
            14,
            args.require_live_execution_env_unarmed,
            _has_live_execution_env_armed(summaries),
        ),
        (
            15,
            args.require_cross_currency_live_blocked,
            _has_cross_currency_live_execution_enabled(summaries),
        ),
        (9, args.require_rust_semantic, _has_non_rust_semantic_topology(summaries)),
        (10, args.require_coverage_runtime, _has_missing_runtime_coverage(summaries)),
        (
            11,
            args.min_positive_candidates is not None,
            aggregate["positiveCandidates"] < (args.min_positive_candidates or 0),
        ),
        (
            12,
            args.min_threshold_candidates is not None,
            aggregate["thresholdCandidates"] < (args.min_threshold_candidates or 0),
        ),
        (
            13,
            args.min_cross_venue_candidates is not None,
            aggregate["crossVenueCandidates"] < (args.min_cross_venue_candidates or 0),
        ),
        (
            14,
            args.min_quoted_semantic_instruments is not None,
            aggregate["quotedSemanticMatchInstruments"]
            < (args.min_quoted_semantic_instruments or 0),
        ),
    ]
    for code, enabled, failed in gate_checks:
        if enabled and failed:
            return code
    return 0


def _has_candidate_warnings(summaries: list[dict[str, Any]]) -> bool:
    return any(item["candidateQuality"].get("diagnosticWarnings") for item in summaries)


def _has_latency_slo_failure(summaries: list[dict[str, Any]]) -> bool:
    return any(
        _as_dict(item["latencyDiagnostics"].get("sloStatus")).get("overall") == "fail"
        for item in summaries
    )


def _has_health_at_or_above(
    summaries: list[dict[str, Any]],
    key: str,
    threshold: str | None,
) -> bool:
    if threshold is None:
        return False
    return any(
        _operator_health_at_or_above(
            str(_as_dict(item.get(key)).get("overall") or "unknown"),
            threshold,
        )
        for item in summaries
    )


def _has_auto_execute_enabled(summaries: list[dict[str, Any]]) -> bool:
    return any(
        bool(_as_dict(item.get("executionReadiness")).get("autoExecute")) for item in summaries
    )


def _has_non_validation_mode(summaries: list[dict[str, Any]]) -> bool:
    return any(
        not bool(_as_dict(item.get("executionReadiness")).get("validationMode"))
        for item in summaries
    )


def _has_live_execution_env_armed(summaries: list[dict[str, Any]]) -> bool:
    return any(
        bool(_as_dict(item.get("executionReadiness")).get("liveExecutionEnvArmed"))
        for item in summaries
    )


def _has_cross_currency_live_execution_enabled(summaries: list[dict[str, Any]]) -> bool:
    return any(
        bool(
            _as_dict(item.get("executionReadiness")).get(
                "allowCrossCurrencyLiveExecution",
            ),
        )
        for item in summaries
    )


def _has_non_rust_semantic_topology(summaries: list[dict[str, Any]]) -> bool:
    return any(
        _as_dict(item.get("graph")).get("engine") != "rust"
        or _as_dict(item.get("graph")).get("topologySource") != "rust_semantic"
        for item in summaries
    )


def _has_missing_runtime_coverage(summaries: list[dict[str, Any]]) -> bool:
    return any(
        _int_value(_as_dict(item.get("graph")).get("coverageProofCount")) <= 0
        or _int_value(_as_dict(item.get("graph")).get("coverageHyperedgeCount")) <= 0
        for item in summaries
    )


def _operator_health_at_or_above(actual: str, threshold: str) -> bool:
    severity = {"pass": 0, "warn": 1, "fail": 2}
    return severity.get(actual, 0) >= severity.get(threshold, 0)


def _provider_poll_health(provider_quote_poll_stats: dict[str, Any]) -> dict[str, Any]:
    venues: dict[str, dict[str, Any]] = {}
    overall = "pass"
    for venue, raw_stats in sorted(provider_quote_poll_stats.items()):
        stats = _as_dict(raw_stats)
        failure_count = _int_value(stats.get("failure_count"))
        rate_limit_count = _int_value(stats.get("rate_limit_count"))
        backlog_count = _int_value(stats.get("backlog_count"))
        request_count = _int_value(stats.get("request_count"))
        event_request_count = _int_value(stats.get("event_request_count"))
        line_request_count = _int_value(stats.get("line_request_count"))
        pruned_subscription_count = _int_value(stats.get("pruned_subscription_count"))
        refilled_subscription_count = _int_value(stats.get("refilled_subscription_count"))
        quote_count = _int_value(stats.get("quote_count"))
        concurrency = _int_value(stats.get("concurrency"))
        max_concurrency = _int_value(stats.get("max_concurrency"))
        poll_target_cycle_secs = _float_value(stats.get("poll_target_cycle_secs"))
        max_fetch_latency_secs = _float_value(stats.get("max_fetch_latency_secs"))
        fetch_latency_p50_secs = _float_value(stats.get("fetch_latency_p50_secs"))
        fetch_latency_p95_secs = _float_value(stats.get("fetch_latency_p95_secs"))
        fetch_latency_p99_secs = _float_value(stats.get("fetch_latency_p99_secs"))
        cycle_elapsed_secs = _float_value(stats.get("cycle_elapsed_secs"))
        source = str(stats.get("source") or "")
        request_fanout_per_quote = _ratio(request_count, quote_count)
        line_fallback_ratio = _ratio(line_request_count, request_count)
        cycle_headroom_secs = poll_target_cycle_secs - cycle_elapsed_secs
        estimated_shards_for_target = (
            max(1, math.ceil(cycle_elapsed_secs / poll_target_cycle_secs))
            if poll_target_cycle_secs > 0
            else 1
        )
        reasons = _provider_poll_reasons(
            source=source,
            failure_count=failure_count,
            rate_limit_count=rate_limit_count,
            backlog_count=backlog_count,
            request_count=request_count,
            line_request_count=line_request_count,
            pruned_subscription_count=pruned_subscription_count,
            refilled_subscription_count=refilled_subscription_count,
            quote_count=quote_count,
            concurrency=concurrency,
            max_concurrency=max_concurrency,
            poll_target_cycle_secs=poll_target_cycle_secs,
            max_fetch_latency_secs=max_fetch_latency_secs,
            fetch_latency_p95_secs=fetch_latency_p95_secs,
            cycle_elapsed_secs=cycle_elapsed_secs,
            request_fanout_per_quote=request_fanout_per_quote,
            line_fallback_ratio=line_fallback_ratio,
        )
        status = _provider_poll_status(
            reasons,
            failure_count=failure_count,
            rate_limit_count=rate_limit_count,
        )
        if _operator_health_at_or_above(status, overall):
            overall = status
        venues[str(venue)] = {
            "status": status,
            "reasons": reasons,
            "failureCount": failure_count,
            "rateLimitCount": rate_limit_count,
            "backlogCount": backlog_count,
            "requestCount": request_count,
            "eventRequestCount": event_request_count,
            "lineRequestCount": line_request_count,
            "prunedSubscriptionCount": pruned_subscription_count,
            "refilledSubscriptionCount": refilled_subscription_count,
            "quoteCount": quote_count,
            "requestFanoutPerQuote": round(request_fanout_per_quote, 4),
            "lineFallbackRatio": round(line_fallback_ratio, 4),
            "quoteYieldRatio": round(_ratio(quote_count, request_count), 4),
            "requestsPerSecond": round(_ratio(request_count, cycle_elapsed_secs), 4),
            "quotesPerSecond": round(_ratio(quote_count, cycle_elapsed_secs), 4),
            "cycleHeadroomSeconds": round(cycle_headroom_secs, 4),
            "estimatedShardsForTarget": estimated_shards_for_target,
            "source": source,
            "concurrency": concurrency,
            "maxConcurrency": max_concurrency,
            "adaptiveConcurrency": bool(stats.get("adaptive_concurrency")),
            "pollTargetCycleSeconds": poll_target_cycle_secs,
            "maxFetchLatencySeconds": max_fetch_latency_secs,
            "fetchLatencyP50Seconds": fetch_latency_p50_secs,
            "fetchLatencyP95Seconds": fetch_latency_p95_secs,
            "fetchLatencyP99Seconds": fetch_latency_p99_secs,
            "cycleElapsedSeconds": cycle_elapsed_secs,
        }
    return {
        "overall": overall if venues else "unknown",
        "venues": venues,
    }


def _provider_poll_reasons(
    *,
    source: str,
    failure_count: int,
    rate_limit_count: int,
    backlog_count: int,
    request_count: int,
    line_request_count: int,
    pruned_subscription_count: int,
    refilled_subscription_count: int,
    quote_count: int,
    concurrency: int,
    max_concurrency: int,
    poll_target_cycle_secs: float,
    max_fetch_latency_secs: float,
    fetch_latency_p95_secs: float,
    cycle_elapsed_secs: float,
    request_fanout_per_quote: float,
    line_fallback_ratio: float,
) -> list[str]:
    reasons: list[str] = []
    if failure_count > 0:
        reasons.append("provider_failures")
    if rate_limit_count > 0:
        reasons.append("rate_limited")
    if backlog_count > 0:
        reasons.append("poll_backlog")
    reasons.extend(
        _provider_poll_latency_reasons(
            cycle_elapsed_secs=cycle_elapsed_secs,
            poll_target_cycle_secs=poll_target_cycle_secs,
            max_fetch_latency_secs=max_fetch_latency_secs,
            fetch_latency_p95_secs=fetch_latency_p95_secs,
            concurrency=concurrency,
            max_concurrency=max_concurrency,
        ),
    )
    reasons.extend(
        _provider_poll_fanout_reasons(
            source=source,
            request_count=request_count,
            line_request_count=line_request_count,
            pruned_subscription_count=pruned_subscription_count,
            refilled_subscription_count=refilled_subscription_count,
            quote_count=quote_count,
            request_fanout_per_quote=request_fanout_per_quote,
            line_fallback_ratio=line_fallback_ratio,
        ),
    )
    return reasons


def _provider_poll_latency_reasons(
    *,
    cycle_elapsed_secs: float,
    poll_target_cycle_secs: float,
    max_fetch_latency_secs: float,
    fetch_latency_p95_secs: float,
    concurrency: int,
    max_concurrency: int,
) -> list[str]:
    reasons: list[str] = []
    target_missed = poll_target_cycle_secs > 0 and cycle_elapsed_secs > poll_target_cycle_secs
    if target_missed:
        reasons.append("poll_target_missed")
    if max(max_fetch_latency_secs, fetch_latency_p95_secs) > 5.0:
        reasons.append("slow_fetch_latency")
    if cycle_elapsed_secs > 5.0:
        reasons.append("poll_cycle_exceeds_live_quote_slo")
    if cycle_elapsed_secs > 30.0:
        reasons.append("slow_poll_cycle")
    if target_missed and max_concurrency > 0 and concurrency >= max_concurrency:
        reasons.append("at_max_concurrency")
    return reasons


def _provider_poll_fanout_reasons(
    *,
    source: str,
    request_count: int,
    line_request_count: int,
    pruned_subscription_count: int,
    refilled_subscription_count: int,
    quote_count: int,
    request_fanout_per_quote: float,
    line_fallback_ratio: float,
) -> list[str]:
    reasons: list[str] = []
    if "event" in source and line_request_count > 0 and line_fallback_ratio > 0.25:
        reasons.append("line_fallback_fanout")
    if "line" in source and request_count >= 20:
        reasons.append("event_batching_disabled")
    if quote_count > 0 and request_fanout_per_quote > 1.5:
        reasons.append("request_fanout_high")
    if pruned_subscription_count > 0:
        reasons.append("stale_subscription_pruned")
        if refilled_subscription_count <= 0:
            reasons.append("stale_subscription_refill_gap")
    return reasons


def _provider_poll_status(
    reasons: list[str],
    *,
    failure_count: int,
    rate_limit_count: int,
) -> str:
    if failure_count > 0 or rate_limit_count > 0:
        return "fail"
    return "warn" if reasons else "pass"


def _semantic_cache_corpus_health(provider_corpus_coverage: dict[str, Any]) -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    overall = "unknown"
    if not provider_corpus_coverage:
        return {"overall": overall, "providers": providers}

    overall = "pass"
    for provider, raw_report in sorted(
        provider_corpus_coverage.items(),
        key=lambda item: str(item[0]),
    ):
        report = _as_dict(raw_report)
        reasons: list[str] = []
        sport_count = _int_value(report.get("sport_count"))
        sports_with_selections = _int_value(report.get("sports_with_selections"))
        total_selection_count = _int_value(report.get("total_selection_count"))
        zero_selection_sports = _as_list_of_strings(report.get("zero_selection_sports"))
        sparse_sports = _as_list_of_strings(report.get("sparse_sports"))
        unresolved_requested_sports = _as_list_of_strings(
            report.get("unresolved_requested_sports"),
        )
        blocker_counts = _as_dict(report.get("blocker_counts"))
        if sport_count <= 0:
            reasons.append("no_corpus_sports")
        if sports_with_selections <= 0 or total_selection_count <= 0:
            reasons.append("no_corpus_selections")
        if unresolved_requested_sports:
            reasons.append("unresolved_requested_sports")
        if zero_selection_sports:
            reasons.append("zero_selection_sports")
        if sparse_sports:
            reasons.append("sparse_corpus_sports")
        if blocker_counts:
            reasons.append("provider_corpus_blockers")

        status = "fail" if "no_corpus_selections" in reasons else ("warn" if reasons else "pass")
        if _operator_health_at_or_above(status, overall):
            overall = status
        providers[str(provider)] = {
            "status": status,
            "reasons": reasons,
            "sportCount": sport_count,
            "sportsWithSelections": sports_with_selections,
            "totalSelectionCount": total_selection_count,
            "coverageMode": str(report.get("coverage_mode") or ""),
            "liveOnly": bool(report.get("live_only")),
            "preferLiquidMarkets": bool(report.get("prefer_liquid_markets")),
            "unresolvedRequestedSports": unresolved_requested_sports,
            "zeroSelectionSports": zero_selection_sports,
            "sparseSports": sparse_sports,
            "blockerCounts": blocker_counts,
        }
    return {"overall": overall, "providers": providers}


def _recommended_actions(summary: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    execution_safety = _as_dict(summary.get("executionSafety"))
    if execution_safety.get("overall") == "fail":
        actions.append("disable_auto_execute_until_approved")
    latency_slo = _as_dict(_as_dict(summary.get("latencyDiagnostics")).get("sloStatus"))
    if latency_slo.get("overall") == "fail":
        actions.append("inspect_live_latency_slo_violations")
    latency_warnings = set(
        _as_list_of_strings(_as_dict(summary.get("latencyDiagnostics")).get("diagnosticWarnings")),
    )
    if "missing_candidate_decision_latency" in latency_warnings:
        actions.append("inspect_candidate_decision_latency_instrumentation")
    if "missing_graph_scan_latency" in latency_warnings:
        actions.append("inspect_graph_scan_latency_instrumentation")
    if (
        "missing_quote_event_to_strategy_latency" in latency_warnings
        or "missing_quote_publish_to_strategy_latency" in latency_warnings
    ):
        actions.append("inspect_quote_timestamp_instrumentation")
    actions.extend(
        _actions_from_reason_payloads(
            _as_dict(_as_dict(summary.get("providerPollHealth")).get("venues")).values(),
            {
                "provider_failures": "inspect_provider_poll_failures",
                "rate_limited": "reduce_poll_rate_or_add_backoff",
                "slow_fetch_latency": "profile_provider_rest_latency",
                "poll_target_missed": "profile_provider_poll_cycle_target",
                "poll_cycle_exceeds_live_quote_slo": (
                    "increase_poll_concurrency_or_reduce_subscriptions"
                ),
                "slow_poll_cycle": "reduce_subscription_count_or_raise_poll_concurrency",
                "poll_backlog": "increase_poll_concurrency_or_reduce_subscriptions",
                "line_fallback_fanout": "inspect_provider_event_batching_mapping",
                "event_batching_disabled": "enable_event_batched_provider_polling",
                "request_fanout_high": "reduce_provider_request_fanout",
                "at_max_concurrency": "reduce_subscription_count_or_shard_provider_polling",
                "stale_subscription_pruned": "inspect_pruned_provider_subscriptions",
                "stale_subscription_refill_gap": "refresh_provider_market_catalog",
            },
        ),
    )
    actions.extend(
        _actions_from_reason_payloads(
            _as_dict(_as_dict(summary.get("venueCoverageHealth")).get("venues")).values(),
            {
                "no_quote_subscription": "refresh_market_subscriptions",
                "no_quoted_nodes": "refresh_market_subscriptions",
                "no_semantic_edges": "inspect_semantic_template_coverage",
                "quote_subscription_gap": "increase_quote_subscription_limit_or_refresh_quotes",
                "quote_subscription_limit_exceeded": ("reduce_semantic_quote_subscription_load"),
            },
        ),
    )
    actions.extend(
        _actions_from_reason_payloads(
            _as_dict(_as_dict(summary.get("semanticCacheCorpusHealth")).get("providers")).values(),
            {
                "no_corpus_sports": "inspect_provider_corpus_discovery",
                "no_corpus_selections": "inspect_provider_corpus_discovery",
                "unresolved_requested_sports": "inspect_unresolved_provider_sport_targets",
                "zero_selection_sports": "inspect_zero_selection_target_sports",
                "sparse_corpus_sports": "widen_provider_corpus_window_or_limits",
                "provider_corpus_blockers": "inspect_provider_corpus_blockers",
            },
        ),
    )
    if _as_dict(summary.get("candidateQuality")).get("zeroCandidateBlockerCounts"):
        actions.append("inspect_zero_candidate_blockers")
        zero_candidate_blockers = _as_dict(summary.get("candidateQuality")).get(
            "zeroCandidateBlockerCounts",
        )
        actions.extend(
            _actions_from_reason_names(
                _as_dict(zero_candidate_blockers),
                {
                    "no_common_fixture": "improve_cross_venue_fixture_discovery",
                    "quotes_missing_for_semantic_edges": "increase_quote_subscription_limit_or_refresh_quotes",
                    "no_semantic_edge": "inspect_semantic_template_coverage",
                    "fixture_identity_mismatch": "audit_fixture_identity_normalization",
                    "same_market_params_mismatch": "audit_market_param_normalization",
                    "provider_scope_mismatch": "audit_provider_scope_rules",
                },
            ),
        )
    return sorted(set(actions))


def _actions_from_reason_payloads(
    payloads: Iterable[Any],
    mapping: dict[str, str],
) -> list[str]:
    actions: list[str] = []
    for payload in payloads:
        reasons = set(_as_list_of_strings(_as_dict(payload).get("reasons")))
        actions.extend(action for reason, action in mapping.items() if reason in reasons)
    return actions


def _actions_from_reason_names(
    counts: dict[str, Any],
    mapping: dict[str, str],
) -> list[str]:
    return [action for reason, action in mapping.items() if _numeric(counts.get(reason)) > 0]


def _venue_coverage_health(venue_coverage: dict[str, Any]) -> dict[str, Any]:
    enabled_venues = venue_coverage.get("enabledVenues") or []
    quote_subscriptions = _as_dict(venue_coverage.get("quoteSubscriptionCounts"))
    quote_subscription_limits = _as_dict(venue_coverage.get("quoteSubscriptionLimits"))
    quote_limit_exceeded = _as_dict(venue_coverage.get("quoteSubscriptionLimitExceededCounts"))
    quote_subscription_gaps = _as_dict(venue_coverage.get("quoteSubscriptionGapCounts"))
    quoted_nodes = _as_dict(venue_coverage.get("quotedNodeCounts"))
    edge_counts = _as_dict(venue_coverage.get("edgeCounts"))
    venues: dict[str, dict[str, Any]] = {}
    overall = "pass"
    for venue in sorted(str(item) for item in enabled_venues if item):
        reasons: list[str] = []
        quote_subscription_count = _int_value(quote_subscriptions.get(venue))
        quote_subscription_limit = quote_subscription_limits.get(venue)
        quote_subscription_limit_exceeded = _int_value(quote_limit_exceeded.get(venue))
        quote_subscription_gap = _int_value(quote_subscription_gaps.get(venue))
        quoted_node_count = _int_value(quoted_nodes.get(venue))
        edge_count = _venue_pair_total(edge_counts, venue)
        if quote_subscription_count <= 0:
            reasons.append("no_quote_subscription")
        if quote_subscription_limit_exceeded > 0:
            reasons.append("quote_subscription_limit_exceeded")
        if quote_subscription_gap > 0:
            reasons.append("quote_subscription_gap")
        if quoted_node_count <= 0:
            reasons.append("no_quoted_nodes")
        if edge_count <= 0:
            reasons.append("no_semantic_edges")
        status = "warn" if reasons else "pass"
        if _operator_health_at_or_above(status, overall):
            overall = status
        venues[venue] = {
            "status": status,
            "reasons": reasons,
            "quoteSubscriptionCount": quote_subscription_count,
            "quoteSubscriptionLimit": quote_subscription_limit,
            "quoteSubscriptionLimitExceeded": quote_subscription_limit_exceeded,
            "quoteSubscriptionGap": quote_subscription_gap,
            "quotedNodeCount": quoted_node_count,
            "edgeCount": edge_count,
        }
    return {
        "overall": overall if venues else "unknown",
        "venues": venues,
    }


def _venue_pair_total(counts: dict[str, Any], venue: str) -> int:
    direct = _int_value(counts.get(venue))
    if direct > 0:
        return direct
    total = 0
    prefix = f"{venue}->"
    suffix = f"->{venue}"
    for key, value in counts.items():
        rendered_key = str(key)
        if (
            rendered_key == venue
            or rendered_key.startswith(prefix)
            or rendered_key.endswith(suffix)
        ):
            total += _int_value(value)
    return total


def _as_list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _float_value(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, float(numerator) / float(denominator))


if __name__ == "__main__":
    raise SystemExit(main())
