from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


SCRIPT_PATH = Path("scripts/strategy_nodes/lint_live_pilot_manifest.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("lint_live_pilot_manifest", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(*, mode: str = "cross_venue") -> dict[str, object]:
    venues = [
        {
            "venue": "CLOUDBET",
            "execution_enabled": True,
            "execution_dry_run": False,
            "environment": "prod",
            "base_currency": "USDT",
            "metadata": {"accept_price_change": "BETTER"},
        },
        {
            "venue": "SXBET",
            "execution_enabled": True,
            "execution_dry_run": False,
            "environment": "prod",
            "base_currency": "USDC",
            "metadata": {"execution_mode": "taker_fill"},
        },
    ]
    if mode == "same_venue":
        venues = venues[:1]
    return {
        "node_id": "pilot",
        "validation_mode": False,
        "strategy": {
            "auto_execute": True,
            "live_execution_armed": True,
            "execution_venue_mode": mode,
            "allow_same_venue_live_execution": mode == "same_venue",
            "allow_cross_currency_live_execution": False,
            "portfolio_base_currency": "USD",
            "stablecoin_currencies": ["USD", "USDC", "USDT"],
            "value_execution_enabled": False,
            "execution_price_change_policy": "better",
            "live_quote_age_slo_secs": 5.0,
            "quote_max_pair_skew_secs": 1.0,
            "max_total_stake": "25",
            "max_leg_stake": "15",
            "max_daily_notional": "100",
            "max_daily_loss": "25",
        },
        "venues": venues,
    }


def test_lint_manifest_accepts_cross_venue_tiny_pilot(tmp_path):
    module = _load_module()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    result = module.lint_manifest(path, expected_mode="cross_venue")

    assert result["status"] == "pass"
    assert result["issues"] == []


def test_lint_manifest_blocks_same_venue_enabled_in_cross_venue(tmp_path):
    module = _load_module()
    manifest = _manifest()
    manifest["strategy"]["allow_same_venue_live_execution"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = module.lint_manifest(path, expected_mode="cross_venue")

    assert result["status"] == "fail"
    assert "cross_venue_mode_allows_same_venue_execution" in result["issues"]


def test_lint_manifest_blocks_cross_currency_without_gate(tmp_path):
    module = _load_module()
    manifest = _manifest()
    manifest["venues"][0]["base_currency"] = "EUR"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = module.lint_manifest(path)

    assert result["status"] == "fail"
    assert "CLOUDBET:non_usd_live_currency_without_cross_currency_gate" in result["issues"]


def test_cli_fails_on_issue(tmp_path, monkeypatch, capsys):
    module = _load_module()
    manifest = _manifest()
    manifest["strategy"]["max_daily_loss"] = "50"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT_PATH), str(path), "--fail-on-issue", "--format", "text"],
    )

    assert module.main() == 2
    assert "max_daily_loss_above_tiny_pilot_limit" in capsys.readouterr().out
