from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path("scripts/strategy_nodes/check_deploy_runner_status.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("check_deploy_runner_status", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fetch_runner_payload_reports_missing_gh_without_traceback(monkeypatch):
    module = _load_module()

    monkeypatch.setattr(module.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="gh executable not found"):
        module._fetch_runner_payload("owner/repo", token=None)


def test_fetch_runner_payload_uses_absolute_gh_path(monkeypatch):
    module = _load_module()
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stderr = ""
        stdout = '{"runners": [{"name": "ec2"}]}'

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return Completed()

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/local/bin/gh")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module._fetch_runner_payload("owner/repo", token=None) == {
        "runners": [{"name": "ec2"}],
    }
    assert captured["args"][:2] == ["/usr/local/bin/gh", "api"]
