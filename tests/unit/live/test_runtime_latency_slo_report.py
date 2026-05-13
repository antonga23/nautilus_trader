# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Tests for runtime latency SLO audit reporting.
"""

from pathlib import Path
import importlib.util
import json
import sys
from types import ModuleType
from typing import Any


SCRIPT_PATH = Path("scripts/betting/runtime_latency_slo_report.py")


def load_report() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_latency_slo_report", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def payload(overall: str = "pass") -> dict:
    return {
        "nodeId": "node-a",
        "runtimeProbe": {
            "latencyDiagnostics": {
                "sloStatus": {
                    "overall": overall,
                    "quoteAge": {"status": "pass", "observations": 3},
                    "fetchLatency": {"status": overall, "observations": 2},
                    "pairSkew": {"status": "pass", "observations": 1},
                    "strategyLatency": {
                        "graphScanObserved": True,
                        "candidateDecisionObserved": False,
                        "quoteReceiveObserved": True,
                        "providerLatencyObserved": True,
                    },
                },
                "diagnosticWarnings": ["candidate_decision_missing"],
            },
        },
    }


def test_latency_slo_report_extracts_missing_stages() -> None:
    module = load_report()

    summary = module.summarize_payload(payload("warn"), path=Path("status.json"))

    assert summary["nodeId"] == "node-a"
    assert summary["overall"] == "warn"
    assert summary["stages"]["fetchLatency"] == "warn"
    assert summary["observations"]["quoteAge"] == 3
    assert summary["missingStages"] == ["candidate_decision"]
    assert summary["warnings"] == ["candidate_decision_missing"]


def test_latency_slo_report_aggregates_files(tmp_path: Path) -> None:
    module = load_report()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(payload("pass")), encoding="utf-8")
    second.write_text(json.dumps(payload("fail")), encoding="utf-8")

    summary = module.summarize_files([first, second])

    assert summary["artifactCount"] == 2
    assert summary["overallCounts"] == {"fail": 1, "pass": 1}
    assert summary["stageCounts"]["fetchLatency"] == {"fail": 1, "pass": 1}
    assert len(summary["failingNodes"]) == 2


def test_latency_slo_report_cli_outputs_json(tmp_path: Path, capsys: Any) -> None:
    module = load_report()
    artifact = tmp_path / "status.json"
    artifact.write_text(json.dumps(payload()), encoding="utf-8")

    exit_code = module.main(["--json", str(artifact)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["nodes"][0]["nodeId"] == "node-a"
