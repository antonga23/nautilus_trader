from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


SCRIPT_PATH = Path("scripts/strategy_nodes/check_deploy_runner_status.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("check_deploy_runner_status", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner_payload(*, status: str = "online", busy: bool = False) -> dict[str, object]:
    return {
        "total_count": 2,
        "runners": [
            {
                "id": 1,
                "name": "gcp-runner",
                "status": "online",
                "busy": False,
                "labels": [{"name": "self-hosted"}, {"name": "gcp"}],
            },
            {
                "id": 2,
                "name": "ec2-deploy",
                "status": status,
                "busy": busy,
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "Linux"},
                    {"name": "X64"},
                    {"name": "ec2"},
                    {"name": "deploy"},
                    {"name": "trading"},
                ],
            },
        ],
    }


def test_runner_status_passes_when_matching_ec2_runner_is_online():
    module = _load_module()

    result = module.evaluate_runner_payload(_runner_payload())

    assert result["status"] == "pass"
    assert result["matchingRunnerCount"] == 1
    assert result["matchingOnlineRunnerCount"] == 1
    assert result["matchingAvailableRunnerCount"] == 1
    assert result["reasons"] == []


def test_runner_status_fails_when_matching_runner_is_offline():
    module = _load_module()

    result = module.evaluate_runner_payload(_runner_payload(status="offline"))

    assert result["status"] == "fail"
    assert result["matchingRunnerCount"] == 1
    assert result["matchingOnlineRunnerCount"] == 0
    assert result["reasons"] == ["matching_runner_offline"]


def test_runner_status_distinguishes_busy_from_offline():
    module = _load_module()

    result = module.evaluate_runner_payload(_runner_payload(busy=True))

    assert result["status"] == "pass"
    assert result["matchingOnlineRunnerCount"] == 1
    assert result["matchingAvailableRunnerCount"] == 0
    assert result["reasons"] == ["matching_runner_busy"]


def test_cli_can_fail_when_required_runner_is_missing(tmp_path, monkeypatch, capsys):
    module = _load_module()
    payload_path = tmp_path / "runners.json"
    payload_path.write_text(json.dumps({"runners": []}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--input-json",
            str(payload_path),
            "--require-online",
            "--format",
            "text",
        ],
    )

    assert module.main() == 3
    assert "status=fail" in capsys.readouterr().out
