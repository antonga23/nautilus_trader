from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


SCRIPT_PATH = Path("scripts/strategy_nodes/evaluate_live_pilot_soak.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("evaluate_live_pilot_soak", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status_payload(*, positive: int = 0, negative_band: int = 2) -> dict[str, object]:
    return {
        "nodeId": "polymarket-sxbet-live-pilot",
        "status": "running",
        "updatedAt": "2026-05-10T12:00:00Z",
        "runtimeProbe": {
            "graphEngine": "rust",
            "topologySource": "rust_semantic",
            "quotedSemanticMatchInstruments": 10,
            "quotedEdges": 4,
            "positiveMarginCandidates": {"total": positive},
            "thresholdMarginCandidates": {"total": positive},
            "venueCoverage": {
                "enabledVenues": ["POLYMARKET", "SXBET"],
                "quoteSubscriptionCounts": {"POLYMARKET": 2, "SXBET": 3},
                "quoteSubscriptionLimits": {"POLYMARKET": 3, "SXBET": 4},
                "quotedNodeCounts": {"POLYMARKET": 2, "SXBET": 3},
                "semanticMatchedNodeCounts": {"POLYMARKET": 3, "SXBET": 4},
                "quotedSemanticMatchedNodeCounts": {"POLYMARKET": 2, "SXBET": 3},
                "unquotedSemanticMatchedNodeCounts": {"POLYMARKET": 1, "SXBET": 1},
                "edgeCounts": {
                    "POLYMARKET->SXBET": 3,
                    "SXBET->POLYMARKET": 2,
                    "SXBET->SXBET": 9,
                },
                "quotedEdgeCounts": {"POLYMARKET->SXBET": 2},
                "candidateCounts": {"POLYMARKET->SXBET": positive},
            },
            "resolutionHorizon": {
                "enabled": True,
                "maxResolutionHorizonHours": 48.0,
                "eventsInsideHorizon": 5,
                "quotedCandidatesInsideHorizon": 2,
                "blockedCandidatesDueHorizon": 0,
            },
            "candidateQuality": {
                "marginBands": {
                    "positive": positive,
                    "0% to -1%": negative_band,
                    "-1% to -2%": 1,
                },
                "rejectionBuckets": {
                    "negative_margin": 1,
                    "same_market_params_mismatch": 2,
                },
                "blockerSamples": {
                    "same_market_params_mismatch": [
                        {
                            "venuePair": "POLYMARKET->SXBET",
                            "instrumentIdA": "poly-1",
                            "instrumentIdB": "sx-1",
                        },
                    ],
                },
                "topPositiveCandidates": [{"instrumentIdA": "poly-1"}] if positive else [],
                "topNegativeNearMisses": [{"instrumentIdA": "poly-2"}],
                "latencyHistograms": {
                    "quoteAgeSeconds": {"count": 10, "p95": 1.8},
                    "pairSkewSeconds": {"count": 10, "p95": 0.4},
                },
            },
            "latencyDiagnostics": {
                "graphScan": {"count": 10, "p95_ms": 6.0},
                "candidateDecision": {"count": 10, "p95_ms": 20.0},
            },
        },
    }


def test_evaluate_soak_passes_on_cross_venue_edges_blockers_and_latency(tmp_path):
    module = _load_module()
    first = tmp_path / "status-1.json"
    second = tmp_path / "status-2.json"
    first.write_text(json.dumps(_status_payload(negative_band=2)), encoding="utf-8")
    payload = _status_payload(positive=1, negative_band=5)
    payload["updatedAt"] = "2026-05-10T12:20:00Z"
    second.write_text(json.dumps(payload), encoding="utf-8")

    result = module.evaluate_soak([first, second])

    assert result["readiness"] == "pass"
    assert "cross_venue_semantic_edges_observed" in result["earlyPassReasons"]
    assert "positive_or_threshold_candidates_observed" in result["earlyPassReasons"]
    assert "exact_execution_blockers_observed" in result["earlyPassReasons"]
    assert result["movement"]["negativeMargin"]["moving"] is True
    assert result["latency"]["quoteAgeP95"]["status"] == "pass"
    assert result["latency"]["pairSkewP95"]["status"] == "pass"
    assert result["latency"]["graphScanP95"]["status"] == "pass"
    assert result["latency"]["decisionP95"]["status"] == "pass"
    assert result["totals"]["crossVenueEdges"] == 10
    assert result["totals"]["quotedCandidatesInsideHorizon"] == 4
    assert result["totals"]["maxQuoteCapacityPressureScore"] == 0.75
    assert result["totals"]["unquotedSemanticMatchedNodes"] == 4


def test_evaluate_soak_warns_when_latency_samples_are_missing(tmp_path):
    module = _load_module()
    status = tmp_path / "status.json"
    payload = _status_payload()
    payload["runtimeProbe"]["candidateQuality"]["latencyHistograms"] = {}
    payload["runtimeProbe"]["latencyDiagnostics"] = {}
    status.write_text(json.dumps(payload), encoding="utf-8")

    result = module.evaluate_soak([status])

    assert result["readiness"] == "warn"
    assert "latency_slo_incomplete_or_failed" in result["reasons"]
    assert result["latency"]["quoteAgeP95"]["status"] == "unknown_or_fail"
    assert result["latency"]["graphScanP95"]["status"] == "unknown_or_fail"


def test_cli_can_fail_unless_pass(tmp_path, monkeypatch, capsys):
    module = _load_module()
    status = tmp_path / "status.json"
    payload = _status_payload()
    payload["runtimeProbe"]["venueCoverage"] = {"edgeCounts": {}}
    payload["runtimeProbe"]["candidateQuality"]["blockerSamples"] = {}
    payload["runtimeProbe"]["candidateQuality"]["rejectionBuckets"] = {}
    payload["runtimeProbe"]["candidateQuality"]["marginBands"] = {}
    payload["runtimeProbe"]["candidateQuality"]["latencyHistograms"] = {}
    payload["runtimeProbe"]["latencyDiagnostics"] = {}
    status.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(status), "--fail-unless-pass", "--format", "text"],
    )

    assert module.main() == 2
    assert "readiness=warn" in capsys.readouterr().out
