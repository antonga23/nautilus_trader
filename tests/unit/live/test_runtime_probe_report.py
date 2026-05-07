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
                "quotedNodeCounts": {"CLOUDBET": 5, "SXBET": 9},
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
    assert summary["providerPollHealth"]["venues"]["CLOUDBET"]["reasons"] == [
        "provider_failures",
        "rate_limited",
        "poll_backlog",
    ]
    assert summary["venueCoverageHealth"]["overall"] == "warn"
    assert summary["venueCoverageHealth"]["venues"]["CLOUDBET"]["reasons"] == [
        "no_quote_subscription",
        "no_semantic_edges",
    ]
    assert summary["recommendedActions"] == [
        "increase_poll_concurrency_or_reduce_subscriptions",
        "inspect_live_latency_slo_violations",
        "inspect_provider_poll_failures",
        "inspect_semantic_template_coverage",
        "inspect_zero_candidate_blockers",
        "reduce_poll_rate_or_add_backoff",
        "refresh_market_subscriptions",
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
        "missing_top_positive_candidates",
        "missing_top_negative_near_misses",
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
    assert aggregate["positiveCandidates"] == 5
    assert aggregate["thresholdCandidates"] == 3
    assert aggregate["crossVenueCandidates"] == 3
    assert aggregate["diagnosticWarningCounts"] == {
        "live_fetch_latency_slo_violations": 1,
        "missing_top_negative_near_misses": 1,
        "missing_top_positive_candidates": 1,
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
    assert "provider_poll_health overall=fail CLOUDBET:status=fail" in text_output
    assert "venue_coverage_health overall=warn CLOUDBET:status=warn" in text_output
    assert "recommended_actions" in text_output
    assert "inspect_live_latency_slo_violations" in text_output
    assert "ts=request_started->response_received" in text_output
    assert "failures=2 rate_limits=1 backoff=1.0s" in text_output
    assert "instrument_refresh_by_venue CLOUDBET:req=3 add=4 rm=2 stale=1" in text_output
    assert "semantic_diagnostics supported_nodes=18 unsupported_nodes=2" in text_output
    assert "unsupported_provider_patterns" in text_output


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
