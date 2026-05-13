# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Tests for the fixture proof audit report.
"""

from pathlib import Path
import importlib.util
import json
import sys
from types import ModuleType
from typing import Any


SCRIPT_PATH = Path("scripts/betting/fixture_proof_audit_report.py")


def load_report() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fixture_proof_audit_report", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def runtime_payload() -> dict[str, Any]:
    return {
        "nodeId": "polymarket-sxbet-cross-venue-live-pilot",
        "runtimeProbe": {
            "candidateQuality": {
                "zeroCandidateFixtureProofBlockerCounts": {
                    "start_time_mismatch": 2,
                },
                "zeroCandidateBlockerCounts": {
                    "fixture_identity_mismatch": 1,
                    "negative_margin": 4,
                },
                "fixtureOverlapDiagnostics": [
                    {
                        "venuePair": "POLYMARKET->SXBET",
                        "fixtureProofBlockerCounts": {
                            "participant_mismatch": 3,
                            "ambiguous_fixture": 1,
                        },
                        "sampleProofs": [
                            {
                                "canonicalEventKeyA": "tennis:tiafoe:buse",
                                "canonicalEventKeyB": "tennis:frances tiafoe:ignacio buse",
                                "startTimeDeltaSeconds": 0,
                            },
                        ],
                    },
                ],
                "blockerSamples": {
                    "participant_mismatch": [
                        {
                            "instrumentIdA": "event:home.POLYMARKET",
                            "instrumentIdB": "event:away.SXBET",
                            "fixtureProofBlockerReason": "participant_mismatch",
                        },
                    ],
                    "negative_margin": [
                        {
                            "instrumentIdA": "ignored.POLYMARKET",
                            "instrumentIdB": "ignored.SXBET",
                        },
                    ],
                },
            },
        },
    }


def test_fixture_proof_audit_aggregates_counts_and_samples() -> None:
    module = load_report()

    summary = module.summarize_payload(runtime_payload(), path=Path("status.json"))

    assert summary["nodeId"] == "polymarket-sxbet-cross-venue-live-pilot"
    assert summary["fixtureProofBlockerCounts"] == {
        "ambiguous_fixture": 1,
        "fixture_identity_mismatch": 1,
        "participant_mismatch": 4,
        "start_time_mismatch": 2,
    }
    assert summary["venuePairBlockerCounts"]["POLYMARKET->SXBET"] == {
        "ambiguous_fixture": 1,
        "participant_mismatch": 4,
    }
    assert summary["samples"][0]["blockerReason"] == "participant_mismatch"
    assert "audit_participant_aliases_and_provider_suffixes" in summary["recommendations"]


def test_fixture_proof_audit_summarizes_multiple_files(tmp_path: Path) -> None:
    module = load_report()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(runtime_payload()), encoding="utf-8")
    second.write_text(
        json.dumps(
            {
                "nodeId": "cloudbet-sxbet",
                "runtimeProbe": {
                    "candidateQuality": {
                        "zeroCandidateFixtureProofBlockerCounts": {
                            "no_common_fixture": 2,
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    summary = module.summarize_files([first, second])

    assert summary["artifactCount"] == 2
    assert summary["fixtureProofBlockerCounts"]["no_common_fixture"] == 2
    assert "increase_common_fixture_discovery_or_quote_capacity" in summary["recommendations"]


def test_fixture_proof_audit_cli_outputs_json(tmp_path: Path, capsys: Any) -> None:
    module = load_report()
    artifact = tmp_path / "status.json"
    artifact.write_text(json.dumps(runtime_payload()), encoding="utf-8")

    exit_code = module.main(["--json", str(artifact)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["nodes"][0]["nodeId"] == "polymarket-sxbet-cross-venue-live-pilot"
