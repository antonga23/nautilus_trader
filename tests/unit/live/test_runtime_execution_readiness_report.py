# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Tests for runtime execution-readiness audit reporting.
"""

from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import sys
from types import ModuleType
from typing import Any


SCRIPT_PATH = Path("scripts/betting/runtime_execution_readiness_report.py")


def load_report() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_execution_readiness_report", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def payload(*, env_armed: bool = False, halt_reason: str | None = None) -> dict[str, Any]:
    return {
        "nodeId": "pilot-node",
        "executionReadiness": {
            "validationMode": False,
            "autoExecute": True,
            "liveExecutionArmed": True,
            "liveExecutionEnvArmed": env_armed,
            "semanticCacheConfigured": True,
            "executionVenueMode": "cross_venue",
            "riskCaps": {
                "maxLegStake": "15",
                "maxDailyNotional": "100",
                "maxDailyLoss": "25",
            },
            "venues": [
                {
                    "venue": "CLOUDBET",
                    "executionEnabled": True,
                    "executionDryRun": True,
                    "environment": "paper",
                    "baseCurrency": "USDC",
                },
                {
                    "venue": "SXBET",
                    "executionEnabled": True,
                    "executionDryRun": True,
                    "environment": "testnet",
                    "baseCurrency": "USDC",
                },
            ],
        },
        "runtimeProbe": {
            "fxState": {"fxSource": "hyperliquid", "stale": False},
            "latencyDiagnostics": {"sloStatus": {"overall": "pass"}},
            "liveExecution": {
                "auto_execute": True,
                "manifest_armed": True,
                "env_armed": env_armed,
                "kill_switch_active": False,
                "halt_reason": halt_reason,
                "notional_used": "0",
                "realized_loss": "0",
                "submissions": 0,
                "unhedged_exposures": 0,
                "order_lifecycle_counts_by_venue": {},
                "block_reasons": {},
            },
        },
    }


def test_execution_readiness_report_classifies_ready_unarmed() -> None:
    module = load_report()

    summary = module.summarize_payload(payload(), path=Path("status.json"))

    assert summary["overall"] == "ready_unarmed"
    assert summary["autoExecute"] is True
    assert summary["manifestArmed"] is True
    assert summary["envArmed"] is False
    assert "env_gate_unarmed" in summary["reasons"]
    assert summary["latencyOverall"] == "pass"


def test_execution_readiness_report_flags_halt_and_unhedged() -> None:
    module = load_report()
    status = payload(env_armed=True, halt_reason="partial_submit_unhedged_exposure")
    status["runtimeProbe"]["liveExecution"]["unhedged_exposures"] = 1

    summary = module.summarize_payload(status)

    assert summary["overall"] == "fail"
    assert "live_execution_armed" in summary["reasons"]
    assert "halt:partial_submit_unhedged_exposure" in summary["reasons"]
    assert "unhedged_exposure" in summary["reasons"]


def test_execution_readiness_report_aggregates_files(tmp_path: Path) -> None:
    module = load_report()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(payload()), encoding="utf-8")
    second.write_text(json.dumps(payload(env_armed=True)), encoding="utf-8")

    summary = module.summarize_files([first, second])

    assert summary["artifactCount"] == 2
    assert summary["overallCounts"] == {"armed": 1, "ready_unarmed": 1}
    assert summary["reasonCounts"]["live_execution_armed"] == 1


def test_execution_readiness_report_cli_enforces_unarmed(tmp_path: Path, capsys: Any) -> None:
    module = load_report()
    artifact = tmp_path / "status.json"
    artifact.write_text(json.dumps(payload(env_armed=True)), encoding="utf-8")

    exit_code = module.main(["--json", "--require-unarmed", str(artifact)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["nodes"][0]["overall"] == "armed"
