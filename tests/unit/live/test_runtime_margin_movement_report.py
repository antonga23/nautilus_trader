from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path("scripts/betting/runtime_margin_movement_report.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("runtime_margin_movement_report", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_status(tmp_path: Path, name: str, *, positive: int, near_band: int, near_margin: float):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "nodeId": "polymarket-sxbet-cross-venue-live-pilot",
                "runtimeProbe": {
                    "quotedEdges": 3,
                    "quotedSemanticMatchInstruments": 12,
                    "positiveMarginCandidates": {"executionSafe": positive, "total": positive},
                    "thresholdMarginCandidates": {"executionSafe": positive, "total": positive},
                    "latencyDiagnostics": {"sloStatus": {"overall": "pass"}},
                    "candidateQuality": {
                        "marginBands": {
                            "positive": positive,
                            "0% to -1%": near_band,
                            "-1% to -2%": 0,
                            "-2% to -5%": 0,
                            "< -5%": 1,
                        },
                        "topNegativeNearMisses": [{"profitMargin": near_margin}],
                    },
                },
            },
        ),
    )
    return path


def test_runtime_margin_movement_report_detects_activity_and_movement(tmp_path: Path):
    module = _load_module()
    first = _write_status(tmp_path, "first.json", positive=0, near_band=3, near_margin=-0.031)
    second = _write_status(tmp_path, "second.json", positive=1, near_band=1, near_margin=-0.004)

    summary = module.summarize_snapshots([first, second])

    assert summary["activityObserved"] is True
    assert summary["movementDetected"] is True
    assert summary["movementReasons"] == [
        "margin_band_movement",
        "candidate_count_movement",
        "near_miss_margin_movement",
    ]
    assert summary["positiveCandidateSeries"] == [0, 1]
    assert summary["bestNearMissMarginSeries"] == [-0.031, -0.004]


def test_runtime_margin_movement_report_can_fail_without_movement(tmp_path: Path):
    module = _load_module()
    first = _write_status(tmp_path, "first.json", positive=0, near_band=2, near_margin=-0.02)
    second = _write_status(tmp_path, "second.json", positive=0, near_band=2, near_margin=-0.02)

    assert module.main(["--require-activity", str(first), str(second)]) == 0
    assert module.main(["--require-movement", str(first), str(second)]) == 3
