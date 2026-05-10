from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.error import URLError

import pytest


SCRIPT_PATH = Path("scripts/strategy_nodes/check_deploy_runner_status.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("check_deploy_runner_status", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fetch_runner_payload_reports_url_errors_without_traceback(monkeypatch):
    module = _load_module()

    def fail_urlopen(*args, **kwargs):
        raise URLError("certificate verify failed")

    def fail_fallback(*args, **kwargs):
        raise RuntimeError("gh unavailable")

    monkeypatch.setattr(module, "urlopen", fail_urlopen)
    monkeypatch.setattr(module, "_fetch_runner_payload_with_gh", fail_fallback)

    with pytest.raises(RuntimeError, match=r"certificate verify failed.*gh unavailable"):
        module._fetch_runner_payload("owner/repo", token=None)


def test_fetch_runner_payload_falls_back_to_gh_on_url_error(monkeypatch):
    module = _load_module()

    def fail_urlopen(*args, **kwargs):
        raise URLError("certificate verify failed")

    monkeypatch.setattr(module, "urlopen", fail_urlopen)
    monkeypatch.setattr(
        module,
        "_fetch_runner_payload_with_gh",
        lambda repo: {"runners": [{"name": "ec2"}]},
    )

    assert module._fetch_runner_payload("owner/repo", token=None) == {
        "runners": [{"name": "ec2"}],
    }
