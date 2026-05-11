from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


SCRIPT_PATH = Path("scripts/betting/runtime_probe_report.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("runtime_probe_report", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_status_payload() -> dict[str, object]:
    return {
        "nodeId": "cloudbet-single",
        "status": "probed",
        "semanticCache": {
            "ready": True,
            "source": "existing",
            "manifestCount": 3,
            "promotedTemplateCount": 1248,
            "executionSafeTemplateCount": 65,
            "sameVenueExecutionEligibleTemplateCount": 237,
            "promotedSafetyTierCounts": {"EXECUTION_SAFE": 65, "TOPOLOGY_SAFE": 900},
            "promotedMarketFamilyCounts": {
                "ASIAN_HANDICAP + ASIAN_HANDICAP": 120,
                "TOTALS + TOTALS": 80,
            },
            "executionSafeMarketFamilyCounts": {"TOTALS + TOTALS": 25},
            "sameVenueEligibleMarketFamilyCounts": {
                "ASIAN_HANDICAP + ASIAN_HANDICAP": 20,
            },
            "strictExecutionBlockerCounts": {"void_states_present": 40},
            "providerCorpusCoverage": {
                "SXBET": {
                    "sport_count": 6,
                    "sports_with_selections": 3,
                    "total_selection_count": 842,
                    "total_event_count": 114,
                    "total_market_count": 0,
                    "coverage_mode": "active_live",
                    "live_only": True,
                    "prefer_liquid_markets": True,
                    "requested_sports": [
                        "american_football",
                        "baseball",
                        "basketball",
                        "ice_hockey",
                        "soccer",
                        "tennis",
                    ],
                    "resolved_sports": ["basketball", "soccer", "tennis"],
                    "unresolved_requested_sports": ["american_football"],
                    "blocker_counts": {"no_active_markets_or_provider_data": 2},
                    "sparse_sports": ["american_football"],
                    "zero_selection_sports": ["baseball", "ice_hockey"],
                },
            },
        },
        "executionReadiness": {
            "validationMode": True,
            "autoExecute": False,
            "semanticCacheConfigured": True,
            "venues": [
                {
                    "venue": "CLOUDBET",
                    "executionEnabled": True,
                    "executionDryRun": True,
                    "environment": "paper",
                    "baseCurrency": "PLAY_EUR",
                },
                {
                    "venue": "SXBET",
                    "executionEnabled": False,
                    "executionDryRun": False,
                    "environment": "prod",
                    "baseCurrency": "USDC",
                },
            ],
        },
        "runtimeProbe": {
            "graphEngine": "rust",
            "topologySource": "rust_semantic",
            "semanticTemplateCount": 1248,
            "coverageProofCount": 5367,
            "coverageHyperedgeCount": 482,
            "coverageDiagnostics": {
                "executionSafeCoverageProofCount": 6,
                "executionSafeCoverageHyperedgeCount": 4,
                "sameVenueEligibleCoverageProofCount": 2,
                "proofSafetyTierCounts": {"EXECUTION_SAFE": 6},
                "hyperedgeSafetyTierCounts": {"EXECUTION_SAFE": 4},
                "proofBlockerReasonCounts": {"void_settlement": 3},
            },
            "feePolicy": {
                "venueTakerFeeRates": {"POLYMARKET": "0.03"},
                "venueMakerRebateRates": {"POLYMARKET": "0.0075"},
                "venueWinningProfitFeeRates": {},
                "venueBasketRebateRates": {"SXBET": "0.01"},
                "venueBasketBoostRates": {"CLOUDBET": "0.02"},
            },
            "graphNodes": 40,
            "graphEdges": 22,
            "graphQuoteStates": 15,
            "connectedNodes": 18,
            "semanticMatchInstruments": 20,
            "quotedSemanticMatchInstruments": 14,
            "executionSafeEdges": 9,
            "sameVenueExecutionEligibleEdges": 3,
            "quotedEdges": 8,
            "positiveMarginCandidates": {
                "executionSafe": 2,
                "sameVenueExecutionEligible": 1,
                "total": 3,
            },
            "thresholdMarginCandidates": {
                "executionSafe": 1,
                "sameVenueExecutionEligible": 1,
                "total": 2,
            },
            "providerQuotePollStats": {
                "CLOUDBET": {
                    "cycle_id": 12,
                    "source": "rest_poll",
                    "market_count": 10,
                    "quote_count": 8,
                    "cycle_elapsed_secs": 1.25,
                    "max_fetch_latency_secs": 0.2,
                    "fetch_latency_p50_secs": 0.1,
                    "fetch_latency_p95_secs": 0.18,
                    "fetch_latency_p99_secs": 0.2,
                    "quote_event_timestamp_source": "request_started",
                    "quote_init_timestamp_source": "response_received",
                    "backlog_count": 6,
                    "failure_count": 2,
                    "rate_limit_count": 1,
                    "backoff_secs": 1.0,
                },
            },
            "instrumentRefresh": {
                "requests": 3,
                "failures": 0,
                "added": 4,
                "removed": 2,
                "delistedRemoved": 2,
                "reconciles": 3,
                "graphRebuilds": 2,
                "staleQuoteTriggers": 1,
                "quoteUnsubscribeRequests": 2,
                "venues": {
                    "CLOUDBET": {"requests": 3, "added": 4, "removed": 2, "stale_triggers": 1},
                },
            },
            "semanticDiagnostics": {
                "supportedProviderNodeCount": 18,
                "unsupportedProviderNodeCount": 2,
                "supportedProviderCoverageRatio": 0.9,
                "commonPatternKeyCount": 14,
                "unsupportedProviderPatternCount": 1,
                "unsupportedProviderPatterns": [
                    {
                        "key": [
                            "POLYMARKET",
                            "soccer",
                            "full_time",
                            "TOTALS",
                            "TOTALS",
                            "OVER",
                            '[["line","3.5"]]',
                        ],
                        "value": 2,
                    },
                ],
                "unsupportedProviderPatternSamples": [
                    {
                        "provider": "POLYMARKET",
                        "sport": "soccer",
                        "scope": "full_time",
                        "marketType": "TOTALS",
                        "marketFamily": "TOTALS",
                        "selection": "OVER",
                        "paramsKey": '[["line","3.5"]]',
                        "count": 2,
                        "samples": [{"instrumentId": "poly-1"}],
                    },
                ],
            },
            "venueCoverage": {
                "enabledVenues": ["CLOUDBET", "SXBET"],
                "crossVenueCandidateCount": 2,
                "quoteSubscriptionCounts": {"CLOUDBET": 10, "SXBET": 8},
                "quoteSubscriptionLimits": {"CLOUDBET": 20, "SXBET": 10},
                "quoteSubscriptionGapCounts": {"CLOUDBET": 5},
                "quotedNodeCounts": {"CLOUDBET": 5, "SXBET": 9},
                "semanticMatchedNodeCounts": {"CLOUDBET": 7, "SXBET": 11},
                "quotedSemanticMatchedNodeCounts": {"CLOUDBET": 5, "SXBET": 9},
                "unquotedSemanticMatchedNodeCounts": {"CLOUDBET": 2, "SXBET": 2},
                "unquotedSemanticMatchedNodeSamples": {"CLOUDBET": [{"instrumentId": "cb-1"}]},
                "edgeCounts": {"CLOUDBET->SXBET": 4, "SXBET->SXBET": 2},
                "quotedEdgeCounts": {"CLOUDBET->SXBET": 3, "SXBET->SXBET": 1},
                "candidateCounts": {"CLOUDBET->SXBET": 2, "SXBET->SXBET": 1},
                "crossVenuePairsWithCandidates": ["CLOUDBET->SXBET"],
                "zeroCandidateBlockerCounts": {"fixture_identity_mismatch": 2},
                "zeroCandidateVenuePairs": [
                    {
                        "venuePair": "POLYMARKET->SXBET",
                        "reason": "no_common_fixture",
                        "blockerReason": "no_common_fixture",
                        "discoveryGapReason": "no_common_fixture_loaded",
                        "commonEventKeySamples": ["tennis:frances tiafoe:ignacio buse"],
                        "sampleBlockerCounts": {"no_common_fixture": 2},
                        "fixtureProofBlockerCounts": {"start_time_mismatch": 2},
                        "samples": [
                            {
                                "instrumentIdA": "poly-tiafoe",
                                "instrumentIdB": "sxbet-buse",
                                "blockerHint": "no_common_fixture",
                                "fixtureIdentityProof": {
                                    "sameFixture": False,
                                    "reason": "start_time_mismatch",
                                    "confidence": 0.62,
                                    "startTimeDeltaSeconds": 86400,
                                },
                            },
                        ],
                    },
                    {
                        "venuePair": "SXBET->POLYMARKET",
                        "fixtureProofBlockerCounts": {"ambiguous_fixture": 1},
                    },
                ],
            },
            "candidateQuality": {
                "marginBands": {"positive": 3, "< -5%": 5},
                "rejectionBuckets": {
                    "positive": 3,
                    "stale": 5,
                    "void_settlement": 2,
                },
                "semanticBlockedReasons": {
                    "equivalent_selection": 4,
                    "void_settlement": 2,
                },
                "semanticBlockedRelationships": {
                    "TOPOLOGY_SAFE:EQUIVALENT_SELECTION": 4,
                    "COVERAGE_SAFE:COMPLEMENTARY_COVERAGE": 2,
                },
                "venuePairs": {
                    "CLOUDBET->SXBET": {"positive": 2, "stale": 1},
                    "SXBET->SXBET": {"void_settlement": 2},
                },
                "marketFamilies": {
                    "TOTALS + TOTALS": {"positive": 2},
                    "MATCH_ODDS + MATCH_ODDS": {"stale": 5},
                },
                "latencyHistograms": {
                    "quoteAgeSeconds": {"count": 8, "p50": 0.5, "p95": 1.2, "p99": 1.4, "max": 1.5},
                    "fetchLatencySeconds": {
                        "count": 8,
                        "p50": 0.05,
                        "p95": 0.2,
                        "p99": 0.25,
                        "max": 0.3,
                    },
                    "pairSkewSeconds": {"count": 4, "p50": 0.2, "p95": 0.7, "p99": 0.8, "max": 0.9},
                },
                "liveQuoteAgeSlo": {
                    "maxQuoteAgeSeconds": 5.0,
                    "observations": 8,
                    "violations": 0,
                },
                "liveTimingSlo": {
                    "quoteAge": {
                        "thresholdSeconds": 5.0,
                        "observations": 8,
                        "violations": 0,
                    },
                    "fetchLatency": {
                        "thresholdMode": "per_candidate",
                        "minThresholdSeconds": 1.5,
                        "maxThresholdSeconds": 2.0,
                        "observations": 8,
                        "violations": 1,
                    },
                    "pairSkew": {
                        "thresholdMode": "per_candidate",
                        "minThresholdSeconds": 1.0,
                        "maxThresholdSeconds": 1.0,
                        "observations": 4,
                        "violations": 0,
                    },
                },
                "sameVenueDryRun": {
                    "passes": 2,
                    "failures": 1,
                    "failureReasons": {"freshQuotes": 1},
                },
                "feeAdjustment": {
                    "evaluatedEdges": 8,
                    "feeDragMargin": {
                        "count": 8,
                        "p50": 0.001,
                        "p95": 0.003,
                        "p99": 0.004,
                        "max": 0.004,
                    },
                    "impactBuckets": {
                        "fee_hurt": 6,
                        "fee_or_incentive_helped": 2,
                        "net_fee_drag": 6,
                        "net_rebate_or_boost": 2,
                        "raw_negative_fee_adjusted_positive": 1,
                    },
                },
                "devigDiagnostics": {
                    "evaluatedEdges": 8,
                    "completeBooks": 7,
                    "incompleteBooks": 1,
                    "methodCounts": {"shin": 5, "proportional": 3},
                    "methodReasonCounts": {"default_shin": 5, "balanced_two_way": 3},
                    "convergenceCounts": {"analytic": 3, "converged": 5},
                    "valueBuckets": {
                        "locked_execution_safe_arbitrage": 2,
                        "sportsbook_value_edge": 1,
                        "fee_or_vig_erased_edge": 1,
                        "vig_only_edge": 4,
                    },
                    "overround": {"count": 8, "p50": 1.02, "p95": 1.04, "p99": 1.05, "max": 1.05},
                    "vig": {"count": 8, "p50": 0.02, "p95": 0.04, "p99": 0.05, "max": 0.05},
                    "grossValueEdge": {
                        "count": 8,
                        "p50": 0.002,
                        "p95": 0.018,
                        "p99": 0.02,
                        "max": 0.02,
                    },
                    "feeAdjustedValueEdge": {
                        "count": 8,
                        "p50": 0.001,
                        "p95": 0.015,
                        "p99": 0.018,
                        "max": 0.018,
                    },
                },
                "coverageBookDevigDiagnostics": {
                    "sampledHyperedges": 3,
                    "quotedHyperedges": 2,
                    "incompleteHyperedges": 1,
                    "methodCounts": {"shin": 2},
                    "valueBuckets": {
                        "coverage_locked_execution_safe_arbitrage": 1,
                        "coverage_reference_book_incomplete": 1,
                    },
                    "overround": {"count": 2, "p50": 1.03, "p95": 1.04, "p99": 1.04, "max": 1.04},
                    "vig": {"count": 2, "p50": 0.03, "p95": 0.04, "p99": 0.04, "max": 0.04},
                    "rawProfitMargin": {
                        "count": 2,
                        "p50": 0.01,
                        "p95": 0.02,
                        "p99": 0.02,
                        "max": 0.02,
                    },
                    "feeAdjustedProfitMargin": {
                        "count": 2,
                        "p50": 0.009,
                        "p95": 0.018,
                        "p99": 0.018,
                        "max": 0.018,
                    },
                    "samples": [{"hyperedgeId": "h-1"}],
                },
                "blockerSamples": {
                    "void_settlement": [
                        {"instrumentIdA": "a", "instrumentIdB": "b"},
                        {"instrumentIdA": "c", "instrumentIdB": "d"},
                    ],
                },
                "zeroCandidateBlockerCounts": {"fixture_identity_mismatch": 2},
                "topPositiveCandidates": [{"instrumentIdA": "a"}],
                "topNegativeNearMisses": [{"instrumentIdA": "x"}],
                "topValueEdgeCandidates": [{"instrumentIdA": "v"}],
                "topVigErasedCandidates": [{"instrumentIdA": "e"}],
            },
            "latencyDiagnostics": {
                "quote_event_to_strategy": {
                    "count": 8,
                    "p50_ms": 10.0,
                    "p95_ms": 25.0,
                    "p99_ms": 28.0,
                    "max_ms": 30.0,
                },
                "quote_publish_to_strategy": {
                    "count": 8,
                    "p50_ms": 5.0,
                    "p95_ms": 12.0,
                    "p99_ms": 13.0,
                    "max_ms": 15.0,
                },
                "quote_fetch_latency": {
                    "count": 8,
                    "p50_ms": 90.0,
                    "p95_ms": 180.0,
                    "p99_ms": 190.0,
                    "max_ms": 200.0,
                },
                "by_venue": {
                    "SXBET": {
                        "quote_event_to_strategy": {
                            "count": 4,
                            "p50_ms": 8.0,
                            "p95_ms": 16.0,
                            "p99_ms": 18.0,
                            "max_ms": 20.0,
                        },
                        "quote_fetch_latency": {
                            "count": 4,
                            "p50_ms": 120.0,
                            "p95_ms": 280.0,
                            "p99_ms": 280.0,
                            "max_ms": 300.0,
                        },
                    },
                    "POLYMARKET": {
                        "quote_event_to_strategy": {
                            "count": 4,
                            "p50_ms": 5000.0,
                            "p95_ms": 42000.0,
                            "p99_ms": 43000.0,
                            "max_ms": 44000.0,
                        },
                        "quote_fetch_latency": {
                            "count": 4,
                            "p50_ms": 0.0,
                            "p95_ms": 0.0,
                            "p99_ms": 0.0,
                            "max_ms": 0.0,
                        },
                    },
                },
                "instrument_refresh_reconcile": {
                    "count": 2,
                    "p50_ms": 100.0,
                    "p95_ms": 120.0,
                    "p99_ms": 120.0,
                    "max_ms": 125.0,
                },
                "graph_scan": {
                    "count": 8,
                    "p50_ms": 1.2,
                    "p95_ms": 3.1,
                    "p99_ms": 3.5,
                    "max_ms": 4.0,
                },
                "candidate_decision": {
                    "count": 3,
                    "p50_ms": 0.8,
                    "p95_ms": 1.4,
                    "p99_ms": 1.5,
                    "max_ms": 1.6,
                },
                "runtime_probe_candidate_decision": {
                    "count": 8,
                    "p50_ms": 0.4,
                    "p95_ms": 0.9,
                    "p99_ms": 1.0,
                    "max_ms": 1.1,
                },
                "order_construction": {
                    "count": 0,
                    "p50_ms": 0.0,
                    "p95_ms": 0.0,
                    "p99_ms": 0.0,
                    "max_ms": 0.0,
                },
                "order_submit": {
                    "count": 0,
                    "p50_ms": 0.0,
                    "p95_ms": 0.0,
                    "p99_ms": 0.0,
                    "max_ms": 0.0,
                },
            },
        },
    }


def test_runtime_probe_report_summarizes_candidate_and_blocker_counts():
    module = _load_module()

    summary = module.summarize_payload(_runtime_status_payload(), top_limit=1)

    assert summary["semanticCache"]["promotedTemplateCount"] == 1248
    assert summary["semanticCache"]["promotedSafetyTierCounts"]["EXECUTION_SAFE"] == 65
    assert summary["semanticCache"]["executionSafeMarketFamilyCounts"]["TOTALS + TOTALS"] == 25
    assert summary["semanticCache"]["strictExecutionBlockerCounts"] == {"void_states_present": 40}
    assert summary["semanticCache"]["providerCorpusCoverage"]["SXBET"]["sport_count"] == 6
    assert summary["semanticCacheCorpusHealth"]["overall"] == "warn"
    assert summary["semanticCacheCorpusHealth"]["providers"]["SXBET"]["reasons"] == [
        "unresolved_requested_sports",
        "zero_selection_sports",
        "sparse_corpus_sports",
        "provider_corpus_blockers",
    ]
    assert summary["semanticCacheCorpusHealth"]["providers"]["SXBET"][
        "unresolvedRequestedSports"
    ] == ["american_football"]
    assert summary["semanticCacheCorpusHealth"]["providers"]["SXBET"]["coverageMode"] == (
        "active_live"
    )
    assert summary["semanticCacheCorpusHealth"]["providers"]["SXBET"]["liveOnly"] is True
    assert "inspect_unresolved_provider_sport_targets" in summary["recommendedActions"]
    assert "inspect_zero_selection_target_sports" in summary["recommendedActions"]
    assert summary["executionReadiness"]["validationMode"] is True
    assert summary["executionReadiness"]["autoExecute"] is False
    assert summary["executionReadiness"]["venues"][0]["environment"] == "paper"
    assert summary["executionReadiness"]["venues"][0]["executionDryRun"] is True
    assert summary["graph"]["engine"] == "rust"
    assert summary["graph"]["topologySource"] == "rust_semantic"
    assert summary["graph"]["coverageHyperedgeCount"] == 482
    assert summary["graph"]["coverageDiagnostics"]["executionSafeCoverageHyperedgeCount"] == 4
    assert summary["graph"]["coverageDiagnostics"]["sameVenueEligibleCoverageProofCount"] == 2
    assert summary["graph"]["topCoverageBlockerReasons"] == [
        {"key": "void_settlement", "value": 3},
    ]
    assert summary["graph"]["sampleCoverageProofs"] == []
    assert summary["graph"]["diagnosticWarnings"] == []
    assert summary["graph"]["quotedSemanticMatchInstruments"] == 14
    assert summary["graph"]["executionSafeEdges"] == 9
    assert summary["candidates"]["positiveTotal"] == 3
    assert summary["candidates"]["thresholdTotal"] == 2
    assert summary["candidates"]["crossVenueCandidateCount"] == 2
    assert summary["latencyDiagnostics"]["quoteEventToStrategy"]["p95_ms"] == 25.0
    assert summary["latencyDiagnostics"]["quoteFetchLatency"]["p95_ms"] == 180.0
    assert summary["latencyDiagnostics"]["byVenue"]["SXBET"]["quoteFetchLatency"]["p95_ms"] == (
        280.0
    )
    assert (
        summary["latencyDiagnostics"]["byVenue"]["POLYMARKET"]["quoteEventToStrategy"]["p95_ms"]
        == 42000.0
    )
    overlap = summary["candidateQuality"]["fixtureOverlapDiagnostics"][0]
    assert overlap["venuePair"] == "POLYMARKET->SXBET"
    assert overlap["discoveryGapReason"] == "no_common_fixture_loaded"
    assert overlap["fixtureProofBlockerCounts"] == {"start_time_mismatch": 2}
    assert overlap["sampleProofs"][0]["startTimeDeltaSeconds"] == 86400
    assert summary["latencyDiagnostics"]["graphScan"]["p95_ms"] == 3.1
    assert summary["latencyDiagnostics"]["instrumentRefreshReconcile"]["max_ms"] == 125.0
    assert summary["latencyDiagnostics"]["runtimeProbeCandidateDecision"]["count"] == 8
    assert summary["latencyDiagnostics"]["candidateDecisionSource"] == "strategy"
    assert summary["latencyDiagnostics"]["sloStatus"] == {
        "overall": "fail",
        "quoteAge": {
            "status": "pass",
            "observations": 8,
            "violations": 0,
            "violationRate": 0.0,
            "thresholdSeconds": 5.0,
            "minThresholdSeconds": None,
            "maxThresholdSeconds": None,
            "thresholdMode": None,
        },
        "fetchLatency": {
            "status": "fail",
            "observations": 8,
            "violations": 1,
            "violationRate": 0.125,
            "thresholdSeconds": None,
            "minThresholdSeconds": 1.5,
            "maxThresholdSeconds": 2.0,
            "thresholdMode": "per_candidate",
        },
        "pairSkew": {
            "status": "pass",
            "observations": 4,
            "violations": 0,
            "violationRate": 0.0,
            "thresholdSeconds": None,
            "minThresholdSeconds": 1.0,
            "maxThresholdSeconds": 1.0,
            "thresholdMode": "per_candidate",
        },
        "strategyLatency": {
            "graphScanObserved": True,
            "candidateDecisionObserved": True,
            "candidateDecisionSource": "strategy",
            "quoteReceiveObserved": True,
            "providerLatencyObserved": True,
        },
    }
    assert summary["latencyDiagnostics"]["diagnosticWarnings"] == []
    assert summary["candidateQuality"]["topRejectionBuckets"] == [
        {"key": "stale", "value": 5},
    ]
    assert summary["candidateQuality"]["topSemanticBlockedReasons"] == [
        {"key": "equivalent_selection", "value": 4},
    ]
    assert summary["candidateQuality"]["topSemanticBlockedRelationships"] == [
        {"key": "TOPOLOGY_SAFE:EQUIVALENT_SELECTION", "value": 4},
    ]
    assert summary["candidateQuality"]["blockerSamples"]["void_settlement"] == [
        {"instrumentIdA": "a", "instrumentIdB": "b"},
    ]
    assert summary["candidateQuality"]["zeroCandidateBlockerCounts"] == {
        "fixture_identity_mismatch": 2,
    }
    assert summary["candidateQuality"]["zeroCandidateFixtureProofBlockerCounts"] == {
        "ambiguous_fixture": 1,
        "start_time_mismatch": 2,
    }
    assert summary["candidateQuality"]["latencyHistograms"]["quoteAgeSeconds"]["p95"] == 1.2
    assert summary["candidateQuality"]["liveQuoteAgeSlo"]["violations"] == 0
    assert summary["candidateQuality"]["liveTimingSlo"]["fetchLatency"]["violations"] == 1
    assert summary["candidateQuality"]["sameVenueDryRun"]["passes"] == 2
    assert summary["candidateQuality"]["feeAdjustment"]["evaluatedEdges"] == 8
    assert summary["candidateQuality"]["feeAdjustment"]["feeDragMargin"]["p95"] == 0.003
    assert summary["candidateQuality"]["feeAdjustment"]["impactBuckets"] == {
        "fee_hurt": 6,
        "fee_or_incentive_helped": 2,
        "net_fee_drag": 6,
        "net_rebate_or_boost": 2,
        "raw_negative_fee_adjusted_positive": 1,
    }
    assert summary["candidateQuality"]["devigDiagnostics"]["evaluatedEdges"] == 8
    assert summary["candidateQuality"]["devigDiagnostics"]["methodCounts"] == {
        "shin": 5,
        "proportional": 3,
    }
    assert summary["candidateQuality"]["devigDiagnostics"]["valueBuckets"] == {
        "locked_execution_safe_arbitrage": 2,
        "sportsbook_value_edge": 1,
        "fee_or_vig_erased_edge": 1,
        "vig_only_edge": 4,
    }
    assert summary["candidateQuality"]["coverageBookDevigDiagnostics"]["quotedHyperedges"] == 2
    assert summary["candidateQuality"]["coverageBookDevigDiagnostics"]["valueBuckets"] == {
        "coverage_locked_execution_safe_arbitrage": 1,
        "coverage_reference_book_incomplete": 1,
    }
    assert summary["candidateQuality"]["topValueEdgeCandidates"] == [{"instrumentIdA": "v"}]
    assert summary["candidateQuality"]["topVigErasedCandidates"] == [{"instrumentIdA": "e"}]
    assert summary["feePolicy"]["venueTakerFeeRates"] == {"POLYMARKET": "0.03"}
    assert summary["feePolicy"]["venueMakerRebateRates"] == {"POLYMARKET": "0.0075"}
    assert summary["feePolicy"]["venueBasketRebateRates"] == {"SXBET": "0.01"}
    assert summary["feePolicy"]["venueBasketBoostRates"] == {"CLOUDBET": "0.02"}
    assert summary["candidateQuality"]["diagnosticWarnings"] == [
        "live_fetch_latency_slo_violations",
    ]
    assert summary["operatorHealth"]["overall"] == "fail"
    assert "latency_slo_failed" in summary["operatorHealth"]["reasons"]
    assert "candidate:live_fetch_latency_slo_violations" in summary["operatorHealth"]["reasons"]
    assert summary["providerQuotePollStats"]["CLOUDBET"]["cycle_id"] == 12
    assert summary["providerPollHealth"]["overall"] == "fail"
    assert summary["providerPollHealth"]["venues"]["CLOUDBET"]["status"] == "fail"
    assert summary["providerPollHealth"]["venues"]["CLOUDBET"]["cycleElapsedSeconds"] == 1.25
    assert summary["providerPollHealth"]["venues"]["CLOUDBET"]["reasons"] == [
        "provider_failures",
        "rate_limited",
        "poll_backlog",
    ]
    assert summary["venueCoverageHealth"]["overall"] == "warn"
    assert summary["venueCoverageHealth"]["venues"]["CLOUDBET"]["reasons"] == [
        "quote_subscription_gap",
    ]
    assert summary["venueCoverage"]["quoteSubscriptionLimits"] == {
        "CLOUDBET": 20,
        "SXBET": 10,
    }
    assert summary["venueCoverage"]["quoteCapacityPressure"]["CLOUDBET"] == {
        "status": "warn",
        "reasons": ["quote_subscription_gap", "unquoted_semantic_matches"],
        "subscriptionCount": 10,
        "subscriptionLimit": 20,
        "subscriptionUtilizationRatio": 0.5,
        "subscriptionGap": 5,
        "subscriptionLimitExceeded": 0,
        "quotedNodes": 5,
        "quoteYieldRatio": 0.5,
        "semanticMatchedNodes": 7,
        "quotedSemanticMatchedNodes": 5,
        "unquotedSemanticMatchedNodes": 2,
        "semanticQuoteCoverageRatio": 0.7143,
        "capacityPressureScore": 0.5,
    }
    assert summary["venueCoverage"]["unquotedSemanticMatchedNodeCounts"]["CLOUDBET"] == 2
    assert summary["venueCoverage"]["crossVenuePairsWithCandidates"] == ["CLOUDBET->SXBET"]
    assert summary["recommendedActions"] == [
        "audit_fixture_identity_normalization",
        "increase_poll_concurrency_or_reduce_subscriptions",
        "increase_quote_subscription_limit_or_refresh_quotes",
        "inspect_live_latency_slo_violations",
        "inspect_provider_corpus_blockers",
        "inspect_provider_poll_failures",
        "inspect_unresolved_provider_sport_targets",
        "inspect_zero_candidate_blockers",
        "inspect_zero_selection_target_sports",
        "prioritize_semantic_matched_quotes",
        "reduce_poll_rate_or_add_backoff",
        "widen_provider_corpus_window_or_limits",
    ]
    assert summary["instrumentRefresh"]["staleQuoteTriggers"] == 1
    assert summary["semanticDiagnostics"]["supportedProviderNodeCount"] == 18
    assert summary["semanticDiagnostics"]["unsupportedProviderNodeCount"] == 2
    assert summary["semanticDiagnostics"]["unsupportedProviderPatternCount"] == 1
    assert summary["semanticDiagnostics"]["unsupportedProviderPatternSamples"][0]["provider"] == (
        "POLYMARKET"
    )


def test_runtime_probe_report_flags_missing_coverage_diagnostics():
    module = _load_module()

    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "semanticTemplateCount": 5,
                "coverageProofCount": 0,
                "coverageHyperedgeCount": 0,
            },
        },
    )

    assert summary["graph"]["diagnosticWarnings"] == [
        "semantic_templates_without_coverage_proofs",
        "semantic_templates_without_coverage_hyperedges",
    ]


def test_runtime_probe_report_formats_execution_readiness_line():
    module = _load_module()

    rendered = module._format_text(
        Path("status.json"),
        module.summarize_payload(_runtime_status_payload()),
    )

    assert (
        "execution_readiness validation_mode=True auto_execute=False semantic_cache=True"
        in rendered
    )
    assert "CLOUDBET(exec=True,dry_run=True,env=paper,base=PLAY_EUR)" in rendered
    assert (
        "fee_policy taker={'POLYMARKET': '0.03'} "
        "maker_rebate={'POLYMARKET': '0.0075'} winning_profit={} "
        "basket_rebate={'SXBET': '0.01'} basket_boost={'CLOUDBET': '0.02'}"
    ) in rendered
    assert "semantic_cache_execution_safe_families TOTALS + TOTALS=25" in rendered
    assert "corpus_coverage provider=SXBET mode=active_live sports=3/6 selections=842" in rendered


def test_runtime_probe_report_flags_legacy_semantic_blocked_artifacts():
    module = _load_module()

    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "candidateQuality": {
                    "rejectionBuckets": {"semantic_blocked": 12},
                },
            },
        },
    )

    assert summary["candidateQuality"]["diagnosticWarnings"] == [
        "semantic_blocked_without_reason_breakdown",
        "semantic_blocked_without_blocker_samples",
    ]


def test_runtime_probe_report_flags_missing_strategy_latency_diagnostics():
    module = _load_module()

    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "quotedEdges": 4,
                "positiveMarginCandidates": {"total": 1},
                "thresholdMarginCandidates": {"total": 1},
            },
        },
    )

    assert summary["latencyDiagnostics"]["diagnosticWarnings"] == [
        "missing_strategy_latency_diagnostics",
    ]


def test_runtime_probe_report_uses_latency_histograms_when_live_slo_missing():
    module = _load_module()

    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "quotedEdges": 4,
                "candidateQuality": {
                    "latencyHistograms": {
                        "quoteAgeSeconds": {"count": 8, "p95": 7.5, "max": 8.0},
                        "fetchLatencySeconds": {"count": 8, "p95": 0.4, "max": 0.5},
                        "pairSkewSeconds": {"count": 8, "p95": 0.2, "max": 0.4},
                    },
                },
                "latencyDiagnostics": {
                    "graph_scan": {"count": 4},
                    "quote_event_to_strategy": {"count": 4},
                },
            },
        },
    )

    slo = summary["latencyDiagnostics"]["sloStatus"]
    assert slo["overall"] == "fail"
    assert slo["quoteAge"] == {
        "status": "fail",
        "observations": 8,
        "violations": 8,
        "violationRate": 1.0,
        "thresholdSeconds": 5.0,
        "minThresholdSeconds": None,
        "maxThresholdSeconds": None,
        "thresholdMode": "histogram_p95_or_max",
    }
    assert slo["fetchLatency"]["status"] == "pass"
    assert slo["pairSkew"]["status"] == "pass"


def test_runtime_probe_report_uses_quote_fetch_latency_when_candidate_histogram_missing():
    module = _load_module()

    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "quotedEdges": 4,
                "candidateQuality": {
                    "latencyHistograms": {
                        "quoteAgeSeconds": {"count": 8, "p95": 1.0, "max": 1.2},
                        "pairSkewSeconds": {"count": 8, "p95": 0.2, "max": 0.4},
                    },
                },
                "latencyDiagnostics": {
                    "graph_scan": {"count": 4},
                    "quote_event_to_strategy": {"count": 4},
                    "quote_fetch_latency": {
                        "count": 4,
                        "p95_ms": 450.0,
                        "max_ms": 600.0,
                    },
                },
            },
        },
    )

    slo = summary["latencyDiagnostics"]["sloStatus"]
    assert slo["fetchLatency"]["status"] == "pass"
    assert slo["fetchLatency"]["observations"] == 4
    assert slo["strategyLatency"]["providerLatencyObserved"] is True


def test_runtime_probe_report_aggregates_multiple_artifacts():
    module = _load_module()
    first = module.summarize_payload(_runtime_status_payload())
    second = module.summarize_payload(
        {
            "status": "probed",
            "runtimeProbe": {
                "graphEngine": "rust",
                "topologySource": "rust_semantic",
                "graphEdges": 10,
                "quotedEdges": 4,
                "semanticMatchInstruments": 8,
                "quotedSemanticMatchInstruments": 5,
                "positiveMarginCandidates": {"total": 2},
                "thresholdMarginCandidates": {"total": 1},
                "venueCoverage": {"crossVenueCandidateCount": 1},
                "candidateQuality": {
                    "rejectionBuckets": {"semantic_blocked": 1},
                },
            },
        },
    )

    aggregate = module.aggregate_summaries([first, second])

    assert aggregate["artifactCount"] == 2
    assert aggregate["statusCounts"] == {"probed": 2}
    assert aggregate["graphEngineCounts"] == {"rust": 2}
    assert aggregate["topologySourceCounts"] == {"rust_semantic": 2}
    assert aggregate["semanticMatchInstruments"] == 28
    assert aggregate["quotedSemanticMatchInstruments"] == 19
    assert aggregate["graphEdges"] == 32
    assert aggregate["quotedEdges"] == 12
    assert aggregate["semanticTemplateCount"] == 1248
    assert aggregate["coverageProofCount"] == 5367
    assert aggregate["coverageHyperedgeCount"] == 482
    assert aggregate["executionSafeEdges"] == 9
    assert aggregate["sameVenueExecutionEligibleEdges"] == 3
    assert aggregate["positiveCandidates"] == 5
    assert aggregate["thresholdCandidates"] == 3
    assert aggregate["crossVenueCandidates"] == 3
    assert aggregate["diagnosticWarningCounts"] == {
        "live_fetch_latency_slo_violations": 1,
        "semantic_blocked_without_blocker_samples": 1,
        "semantic_blocked_without_reason_breakdown": 1,
    }
    assert aggregate["graphDiagnosticWarningCounts"] == {}
    assert aggregate["latencyDiagnosticWarningCounts"] == {
        "missing_strategy_latency_diagnostics": 1,
    }
    assert aggregate["latencySloStatusCounts"] == {
        "fail": 1,
        "no_observations": 1,
    }
    assert aggregate["operatorHealthCounts"] == {
        "fail": 1,
        "warn": 1,
    }
    assert aggregate["providerPollHealthCounts"] == {
        "fail": 1,
        "unknown": 1,
    }
    assert aggregate["providerPollUtilizationByVenue"]["CLOUDBET"] == {
        "maxPollUtilizationRatio": 0.625,
        "maxFetchLatencyUtilizationRatio": 0.09,
        "maxConcurrencyUtilizationRatio": 0.5,
        "minCycleHeadroomSeconds": 0.75,
    }
    assert aggregate["quoteCapacityPressureByVenue"]["CLOUDBET"] == {
        "maxSubscriptionUtilizationRatio": 0.5,
        "minSemanticQuoteCoverageRatio": 0.7143,
        "maxCapacityPressureScore": 0.5,
        "maxUnquotedSemanticMatchedNodes": 2.0,
    }
    assert aggregate["semanticCacheCorpusHealthCounts"] == {
        "unknown": 1,
        "warn": 1,
    }
    assert aggregate["venueCoverageHealthCounts"] == {
        "unknown": 1,
        "warn": 1,
    }
    assert aggregate["executionSafetyCounts"] == {"pass": 2}
    assert aggregate["quoteSubscriptionCountsByVenue"] == {"CLOUDBET": 10, "SXBET": 8}
    assert aggregate["quotedNodeCountsByVenue"] == {"CLOUDBET": 5, "SXBET": 9}
    assert aggregate["semanticMatchedNodeCountsByVenue"] == {"CLOUDBET": 7, "SXBET": 11}
    assert aggregate["quotedSemanticMatchedNodeCountsByVenue"] == {"CLOUDBET": 5, "SXBET": 9}
    assert aggregate["candidateCountsByVenuePair"] == {"CLOUDBET->SXBET": 2, "SXBET->SXBET": 1}
    assert aggregate["edgeCountsByVenuePair"] == {"CLOUDBET->SXBET": 4, "SXBET->SXBET": 2}
    assert aggregate["quotedEdgeCountsByVenuePair"] == {"CLOUDBET->SXBET": 3, "SXBET->SXBET": 1}
    assert aggregate["rejectionBucketCounts"] == {
        "positive": 3,
        "semantic_blocked": 1,
        "stale": 5,
        "void_settlement": 2,
    }
    assert aggregate["semanticBlockerCounts"] == {
        "equivalent_selection": 4,
        "void_settlement": 2,
    }
    assert aggregate["zeroCandidateBlockerCounts"] == {"fixture_identity_mismatch": 2}
    assert aggregate["feeImpactBucketCounts"] == {
        "fee_hurt": 6,
        "fee_or_incentive_helped": 2,
        "net_fee_drag": 6,
        "net_rebate_or_boost": 2,
        "raw_negative_fee_adjusted_positive": 1,
    }
    assert aggregate["devigMethodCounts"] == {"proportional": 3, "shin": 5}
    assert aggregate["devigValueBucketCounts"] == {
        "fee_or_vig_erased_edge": 1,
        "locked_execution_safe_arbitrage": 2,
        "sportsbook_value_edge": 1,
        "vig_only_edge": 4,
    }
    assert aggregate["coverageBookDevigSampledHyperedges"] == 3
    assert aggregate["coverageBookDevigQuotedHyperedges"] == 2
    assert aggregate["coverageBookDevigIncompleteHyperedges"] == 1
    assert aggregate["coverageBookDevigMethodCounts"] == {"shin": 2}
    assert aggregate["coverageBookDevigValueBucketCounts"] == {
        "coverage_locked_execution_safe_arbitrage": 1,
        "coverage_reference_book_incomplete": 1,
    }
    assert aggregate["providerCorpusCoverage"]["SXBET"]["sportsWithSelections"] == 3
    assert aggregate["providerCorpusCoverage"]["SXBET"]["zeroSelectionSports"] == [
        "baseball",
        "ice_hockey",
    ]
    assert aggregate["recommendedActionCounts"]["inspect_zero_candidate_blockers"] == 1


def test_runtime_probe_report_cli_outputs_json_and_text(tmp_path, monkeypatch, capsys):
    module = _load_module()
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_runtime_status_payload()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--top-limit", "1", "--aggregate"],
    )
    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifacts"][0]["path"] == str(status_path)
    assert payload["artifacts"][0]["candidates"]["positiveTotal"] == 3
    assert payload["aggregate"]["positiveCandidates"] == 3

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--format", "text", "--aggregate"],
    )
    assert module.main() == 0
    text_output = capsys.readouterr().out
    assert "graph=rust/rust_semantic" in text_output
    assert "operator_health overall=fail" in text_output
    assert "coverage proofs=5367 hyperedges=482" in text_output
    assert "semantic_cache_execution_safe_families TOTALS + TOTALS=25" in text_output
    assert "coverage_blockers void_settlement=3" in text_output
    assert "candidates positive=3 threshold=2 cross_venue=2" in text_output
    assert "aggregate: artifacts=1 positive=3 threshold=2 cross_venue=2" in text_output
    assert "quoted_semantic=14" in text_output
    assert "coverage_proofs=5367 hyperedges=482" in text_output
    assert "execution_safety={'pass': 1}" in text_output
    assert "health={'fail': 1}" in text_output
    assert "provider_poll_health={'fail': 1}" in text_output
    assert "provider_poll_utilization={'CLOUDBET':" in text_output
    assert "quote_capacity_pressure={'CLOUDBET':" in text_output
    assert "quote_capacity_pressure CLOUDBET:status=warn" in text_output
    assert "corpus_health={'warn': 1}" in text_output
    assert "coverage_book_devig_quoted=2" in text_output
    assert "venue_coverage_health={'warn': 1}" in text_output
    assert "top_semantic_blockers" in text_output
    assert "top_semantic_relationships" in text_output
    assert "zero_candidate_blockers fixture_identity_mismatch=2" in text_output
    assert "zero_candidate_fixture_proof_blockers" in text_output
    assert "start_time_mismatch=2" in text_output
    assert "fee_impact fee_hurt=6" in text_output
    assert "raw_negative_fee_adjusted_positive=1" in text_output
    assert "strategy_latency quote_event_p95=25.0ms" in text_output
    assert "strategy_latency_by_venue POLYMARKET:quote_event_p95=42000.0ms" in text_output
    assert "SXBET:quote_event_p95=16.0ms quote_fetch_p95=280.0ms" in text_output
    assert "fixture_overlap POLYMARKET->SXBET reason=no_common_fixture" in text_output
    assert "fixture_overlap_sample POLYMARKET->SXBET reason=start_time_mismatch" in text_output
    assert "quote_fetch_p95=180.0ms" in text_output
    assert (
        "latency_slo overall=fail quote_age=pass fetch_latency=fail pair_skew=pass" in text_output
    )
    assert "refresh_reconcile_latency p95=120.0ms" in text_output
    assert "provider_poll CLOUDBET:cycle=12" in text_output
    assert "fetch_p95=0.18s" in text_output
    assert "provider_poll_health overall=fail CLOUDBET:status=fail" in text_output
    assert "poll_util=0.625" in text_output
    assert "fetch_util=0.09" in text_output
    assert "concurrency_util=0.5" in text_output
    assert "next_sleep=0.2s" in text_output
    assert "semantic_cache_corpus_health overall=warn SXBET:status=warn" in text_output
    assert (
        "corpus_coverage provider=SXBET mode=active_live sports=3/6 selections=842" in text_output
    )
    assert "unresolved=['american_football']" in text_output
    assert "venue_coverage_health overall=warn CLOUDBET:status=warn" in text_output
    assert "recommended_actions" in text_output
    assert "inspect_live_latency_slo_violations" in text_output
    assert "increase_quote_subscription_limit_or_refresh_quotes" in text_output
    assert "ts=request_started->response_received" in text_output
    assert "failures=2 rate_limits=1 backoff=1.0s" in text_output
    assert "instrument_refresh_by_venue CLOUDBET:req=3 add=4 rm=2 stale=1" in text_output
    assert "semantic_diagnostics supported_nodes=18 unsupported_nodes=2" in text_output
    assert "unsupported_provider_patterns" in text_output
    assert "=2" in text_output


def test_venue_coverage_health_counts_venue_pair_edges():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "nodeId": "cloudbet-single",
            "status": "running",
            "runtimeProbe": {
                "venueCoverage": {
                    "enabledVenues": ["CLOUDBET"],
                    "quoteSubscriptionCounts": {"CLOUDBET": 10},
                    "quotedNodeCounts": {"CLOUDBET": 7},
                    "edgeCounts": {"CLOUDBET->CLOUDBET": 12},
                },
            },
        },
    )

    assert summary["venueCoverageHealth"]["overall"] == "pass"
    assert summary["venueCoverageHealth"]["venues"]["CLOUDBET"]["edgeCount"] == 12


def test_venue_coverage_health_flags_quote_subscription_limit_overrun():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "venueCoverage": {
                    "enabledVenues": ["CLOUDBET"],
                    "quoteSubscriptionCounts": {"CLOUDBET": 1826},
                    "quoteSubscriptionLimits": {"CLOUDBET": 80},
                    "quoteSubscriptionLimitExceededCounts": {"CLOUDBET": 1746},
                    "quotedNodeCounts": {"CLOUDBET": 80},
                    "edgeCounts": {"CLOUDBET->CLOUDBET": 12},
                },
            },
        },
    )

    venue = summary["venueCoverageHealth"]["venues"]["CLOUDBET"]
    assert summary["venueCoverageHealth"]["overall"] == "warn"
    assert venue["quoteSubscriptionLimit"] == 80
    assert venue["quoteSubscriptionLimitExceeded"] == 1746
    assert "quote_subscription_limit_exceeded" in venue["reasons"]
    assert "reduce_semantic_quote_subscription_load" in summary["recommendedActions"]


def test_provider_poll_health_flags_slow_poll_cycles():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "providerQuotePollStats": {
                    "CLOUDBET": {
                        "cycle_elapsed_secs": 84.0,
                        "max_fetch_latency_secs": 0.5,
                        "backlog_count": 0,
                        "failure_count": 0,
                        "rate_limit_count": 0,
                    },
                },
            },
        },
    )

    cloudbet = summary["providerPollHealth"]["venues"]["CLOUDBET"]
    assert cloudbet["status"] == "warn"
    assert cloudbet["cycleElapsedSeconds"] == 84.0
    assert "slow_poll_cycle" in cloudbet["reasons"]
    assert "poll_cycle_exceeds_live_quote_slo" in cloudbet["reasons"]
    assert "reduce_subscription_count_or_raise_poll_concurrency" in summary["recommendedActions"]


def test_provider_poll_health_flags_live_quote_slo_cycle_before_slow_cycle():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "providerQuotePollStats": {
                    "CLOUDBET": {
                        "cycle_elapsed_secs": 7.5,
                        "max_fetch_latency_secs": 0.4,
                        "backlog_count": 0,
                        "failure_count": 0,
                        "rate_limit_count": 0,
                    },
                },
            },
        },
    )

    cloudbet = summary["providerPollHealth"]["venues"]["CLOUDBET"]
    assert cloudbet["status"] == "warn"
    assert cloudbet["reasons"] == ["poll_cycle_exceeds_live_quote_slo"]
    assert "increase_poll_concurrency_or_reduce_subscriptions" in summary["recommendedActions"]


def test_provider_poll_health_explains_cloudbet_poll_fanout_bottlenecks():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "providerQuotePollStats": {
                    "CLOUDBET": {
                        "source": "rest_event_poll",
                        "cycle_elapsed_secs": 8.2,
                        "poll_target_cycle_secs": 4.0,
                        "next_poll_sleep_secs": 0.0,
                        "max_fetch_latency_secs": 0.8,
                        "fetch_latency_p95_secs": 0.72,
                        "request_count": 40,
                        "event_request_count": 10,
                        "line_request_count": 30,
                        "pruned_subscription_count": 3,
                        "refilled_subscription_count": 0,
                        "quote_count": 16,
                        "concurrency": 16,
                        "max_concurrency": 16,
                        "adaptive_concurrency": True,
                        "backlog_count": 0,
                        "failure_count": 0,
                        "rate_limit_count": 0,
                    },
                },
            },
        },
    )

    cloudbet = summary["providerPollHealth"]["venues"]["CLOUDBET"]
    assert cloudbet["status"] == "warn"
    assert cloudbet["requestFanoutPerQuote"] == 2.5
    assert cloudbet["lineFallbackRatio"] == 0.75
    assert cloudbet["quoteYieldRatio"] == 0.4
    assert cloudbet["requestsPerSecond"] == 4.878
    assert cloudbet["quotesPerSecond"] == 1.9512
    assert cloudbet["cycleHeadroomSeconds"] == -4.2
    assert cloudbet["pollUtilizationRatio"] == 2.05
    assert cloudbet["fetchLatencyUtilizationRatio"] == 0.9
    assert cloudbet["concurrencyUtilizationRatio"] == 1.0
    assert cloudbet["estimatedShardsForTarget"] == 3
    assert cloudbet["nextPollSleepSeconds"] == 0.0
    assert cloudbet["prunedSubscriptionCount"] == 3
    assert cloudbet["refilledSubscriptionCount"] == 0
    assert cloudbet["pollTargetCycleSeconds"] == 4.0
    assert "poll_target_missed" in cloudbet["reasons"]
    assert "line_fallback_fanout" in cloudbet["reasons"]
    assert "request_fanout_high" in cloudbet["reasons"]
    assert "at_max_concurrency" in cloudbet["reasons"]
    assert "stale_subscription_pruned" in cloudbet["reasons"]
    assert "stale_subscription_refill_gap" in cloudbet["reasons"]
    assert "inspect_provider_event_batching_mapping" in summary["recommendedActions"]
    assert "reduce_provider_request_fanout" in summary["recommendedActions"]
    assert "reduce_subscription_count_or_shard_provider_polling" in summary["recommendedActions"]
    assert "inspect_pruned_provider_subscriptions" in summary["recommendedActions"]
    assert "refresh_provider_market_catalog" in summary["recommendedActions"]


def test_zero_candidate_blockers_map_to_specific_actions():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "candidateQuality": {
                    "zeroCandidateBlockerCounts": {
                        "no_common_fixture": 3,
                        "quotes_missing_for_semantic_edges": 2,
                        "same_market_params_mismatch": 1,
                    },
                },
            },
        },
    )

    assert "inspect_zero_candidate_blockers" in summary["recommendedActions"]
    assert "improve_cross_venue_fixture_discovery" in summary["recommendedActions"]
    assert "increase_quote_subscription_limit_or_refresh_quotes" in summary["recommendedActions"]
    assert "audit_market_param_normalization" in summary["recommendedActions"]


def test_latency_report_uses_runtime_probe_candidate_decision_fallback():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "quotedEdges": 3,
                "latencyDiagnostics": {
                    "graph_scan": {"count": 3},
                    "candidate_decision": {"count": 0},
                    "runtime_probe_candidate_decision": {
                        "count": 3,
                        "p50_ms": 0.3,
                        "p95_ms": 0.5,
                        "p99_ms": 0.6,
                        "max_ms": 0.7,
                    },
                },
            },
        },
    )

    assert summary["latencyDiagnostics"]["candidateDecision"]["count"] == 3
    assert summary["latencyDiagnostics"]["candidateDecisionSource"] == "runtime_probe"
    assert summary["latencyDiagnostics"]["diagnosticWarnings"] == [
        "missing_quote_event_to_strategy_latency",
        "missing_quote_publish_to_strategy_latency",
    ]


def test_latency_report_preserves_camel_case_strategy_candidate_decision_source():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "runtimeProbe": {
                "quotedEdges": 3,
                "latencyDiagnostics": {
                    "graphScan": {"count": 3},
                    "candidateDecision": {
                        "count": 3,
                        "p50_ms": 0.8,
                        "p95_ms": 1.1,
                    },
                    "runtimeProbeCandidateDecision": {
                        "count": 3,
                        "p50_ms": 0.3,
                        "p95_ms": 0.5,
                    },
                },
            },
        },
    )

    assert summary["latencyDiagnostics"]["candidateDecision"]["p95_ms"] == 1.1
    assert summary["latencyDiagnostics"]["candidateDecisionSource"] == "strategy"


def test_runtime_probe_report_flags_execution_safety_regression():
    module = _load_module()
    summary = module.summarize_payload(
        {
            "executionReadiness": {
                "validationMode": True,
                "autoExecute": True,
                "venues": [
                    {
                        "venue": "CLOUDBET",
                        "executionEnabled": True,
                        "executionDryRun": False,
                    },
                ],
            },
        },
    )

    assert summary["executionSafety"]["overall"] == "fail"
    assert "auto_execute_enabled" in summary["executionSafety"]["reasons"]
    assert "CLOUDBET:validation_execution_not_dry_run" in summary["executionSafety"]["reasons"]
    assert summary["operatorHealth"]["overall"] == "fail"
    assert "execution:auto_execute_enabled" in summary["operatorHealth"]["reasons"]
    assert "disable_auto_execute_until_approved" in summary["recommendedActions"]


def test_runtime_probe_report_treats_unarmed_live_pilot_as_warn_not_fail():
    module = _load_module()
    payload = _runtime_status_payload()
    payload["runtimeProbe"]["candidateQuality"]["liveTimingSlo"]["fetchLatency"]["violations"] = 0
    payload["executionReadiness"].update(
        {
            "validationMode": False,
            "autoExecute": True,
            "liveExecutionArmed": True,
            "liveExecutionEnvArmed": False,
            "allowCrossCurrencyLiveExecution": False,
        },
    )

    summary = module.summarize_payload(payload)

    assert summary["executionSafety"]["overall"] == "warn"
    assert "auto_execute_env_gate_unarmed" in summary["executionSafety"]["reasons"]
    assert summary["operatorHealth"]["overall"] == "warn"


def test_runtime_probe_report_cli_can_fail_on_incomplete_diagnostics(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    status_path = tmp_path / "legacy-status.json"
    status_path.write_text(
        json.dumps(
            {
                "runtimeProbe": {
                    "candidateQuality": {
                        "rejectionBuckets": {"semantic_blocked": 12},
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--fail-on-warning"],
    )

    assert module.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert (
        "semantic_blocked_without_reason_breakdown"
        in (payload[0]["candidateQuality"]["diagnosticWarnings"])
    )


def test_runtime_probe_report_cli_can_fail_on_live_latency_slo(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_runtime_status_payload()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--fail-on-latency-slo"],
    )

    assert module.main() == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["latencyDiagnostics"]["sloStatus"]["overall"] == "fail"


def test_runtime_probe_report_cli_can_fail_on_operator_health(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_runtime_status_payload()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--fail-on-operator-health", "warn"],
    )

    assert module.main() == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["operatorHealth"]["overall"] == "fail"


def test_runtime_probe_report_cli_can_fail_on_provider_poll_health(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_runtime_status_payload()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--fail-on-provider-poll-health", "warn"],
    )

    assert module.main() == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["providerPollHealth"]["overall"] == "fail"


def test_runtime_probe_report_cli_can_fail_on_venue_coverage_health(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_runtime_status_payload()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--fail-on-venue-coverage-health", "warn"],
    )

    assert module.main() == 6
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["venueCoverageHealth"]["overall"] == "warn"


def test_runtime_probe_report_cli_can_enforce_runtime_acceptance_gates(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_runtime_status_payload()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            str(status_path),
            "--require-auto-execute-false",
            "--require-validation-mode",
            "--require-rust-semantic",
            "--require-coverage-runtime",
            "--min-positive-candidates",
            "3",
            "--min-threshold-candidates",
            "2",
            "--min-cross-venue-candidates",
            "2",
            "--min-quoted-semantic-instruments",
            "14",
        ],
    )

    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)[0]["graph"]["engine"] == "rust"


def test_runtime_probe_report_cli_fails_auto_execute_gate(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    payload = _runtime_status_payload()
    payload["executionReadiness"]["autoExecute"] = True
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--require-auto-execute-false"],
    )

    assert module.main() == 7
    assert json.loads(capsys.readouterr().out)[0]["executionSafety"]["overall"] == "fail"


def test_runtime_probe_report_cli_enforces_live_pilot_env_and_currency_gates(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    payload = _runtime_status_payload()
    payload["executionReadiness"].update(
        {
            "validationMode": False,
            "autoExecute": True,
            "liveExecutionArmed": True,
            "liveExecutionEnvArmed": False,
            "allowCrossCurrencyLiveExecution": False,
        },
    )
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            str(status_path),
            "--require-live-execution-env-unarmed",
            "--require-cross-currency-live-blocked",
        ],
    )

    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)[0]["executionSafety"]["overall"] == "warn"


def test_runtime_probe_report_cli_fails_when_live_pilot_env_armed(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    payload = _runtime_status_payload()
    payload["executionReadiness"]["liveExecutionEnvArmed"] = True
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--require-live-execution-env-unarmed"],
    )

    assert module.main() == 14


def test_runtime_probe_report_cli_fails_when_cross_currency_live_enabled(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    payload = _runtime_status_payload()
    payload["executionReadiness"]["allowCrossCurrencyLiveExecution"] = True
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--require-cross-currency-live-blocked"],
    )

    assert module.main() == 15


def test_runtime_probe_report_cli_fails_rust_semantic_gate(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    payload = _runtime_status_payload()
    payload["runtimeProbe"]["graphEngine"] = "python"
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--require-rust-semantic"],
    )

    assert module.main() == 9
    assert json.loads(capsys.readouterr().out)[0]["graph"]["engine"] == "python"


def test_runtime_probe_report_cli_fails_aggregate_candidate_gate(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_runtime_status_payload()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status_path), "--min-threshold-candidates", "3"],
    )

    assert module.main() == 12
    assert json.loads(capsys.readouterr().out)[0]["candidates"]["thresholdTotal"] == 2
