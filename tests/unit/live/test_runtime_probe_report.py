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
            "strictExecutionBlockerCounts": {"void_states_present": 40},
            "providerCorpusCoverage": {
                "SXBET": {
                    "sport_count": 6,
                    "sports_with_selections": 3,
                    "total_selection_count": 842,
                    "total_event_count": 114,
                    "total_market_count": 0,
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
                "blockerSamples": {
                    "void_settlement": [
                        {"instrumentIdA": "a", "instrumentIdB": "b"},
                        {"instrumentIdA": "c", "instrumentIdB": "d"},
                    ],
                },
                "zeroCandidateBlockerCounts": {"fixture_identity_mismatch": 2},
                "topPositiveCandidates": [{"instrumentIdA": "a"}],
                "topNegativeNearMisses": [{"instrumentIdA": "x"}],
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
    assert summary["semanticCache"]["strictExecutionBlockerCounts"] == {"void_states_present": 40}
    assert summary["semanticCache"]["providerCorpusCoverage"]["SXBET"]["sport_count"] == 6
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
    assert summary["candidateQuality"]["latencyHistograms"]["quoteAgeSeconds"]["p95"] == 1.2
    assert summary["candidateQuality"]["liveQuoteAgeSlo"]["violations"] == 0
    assert summary["candidateQuality"]["liveTimingSlo"]["fetchLatency"]["violations"] == 1
    assert summary["candidateQuality"]["sameVenueDryRun"]["passes"] == 2
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
    assert summary["venueCoverage"]["unquotedSemanticMatchedNodeCounts"]["CLOUDBET"] == 2
    assert summary["venueCoverage"]["crossVenuePairsWithCandidates"] == ["CLOUDBET->SXBET"]
    assert summary["recommendedActions"] == [
        "audit_fixture_identity_normalization",
        "increase_poll_concurrency_or_reduce_subscriptions",
        "increase_quote_subscription_limit_or_refresh_quotes",
        "inspect_live_latency_slo_violations",
        "inspect_provider_poll_failures",
        "inspect_zero_candidate_blockers",
        "reduce_poll_rate_or_add_backoff",
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
    assert "corpus_coverage provider=SXBET sports=3/6 selections=842" in rendered


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
    assert "coverage_blockers void_settlement=3" in text_output
    assert "candidates positive=3 threshold=2 cross_venue=2" in text_output
    assert "aggregate: artifacts=1 positive=3 threshold=2 cross_venue=2" in text_output
    assert "quoted_semantic=14" in text_output
    assert "coverage_proofs=5367 hyperedges=482" in text_output
    assert "execution_safety={'pass': 1}" in text_output
    assert "health={'fail': 1}" in text_output
    assert "provider_poll_health={'fail': 1}" in text_output
    assert "venue_coverage_health={'warn': 1}" in text_output
    assert "top_semantic_blockers" in text_output
    assert "top_semantic_relationships" in text_output
    assert "zero_candidate_blockers fixture_identity_mismatch=2" in text_output
    assert "strategy_latency quote_event_p95=25.0ms" in text_output
    assert (
        "latency_slo overall=fail quote_age=pass fetch_latency=fail pair_skew=pass" in text_output
    )
    assert "refresh_reconcile_latency p95=120.0ms" in text_output
    assert "provider_poll CLOUDBET:cycle=12" in text_output
    assert "fetch_p95=0.18s" in text_output
    assert "provider_poll_health overall=fail CLOUDBET:status=fail" in text_output
    assert "corpus_coverage provider=SXBET sports=3/6 selections=842" in text_output
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
                        "max_fetch_latency_secs": 0.8,
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
