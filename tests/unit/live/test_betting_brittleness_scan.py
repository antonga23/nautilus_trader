# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Tests for the betting brittleness scanner.
"""

from pathlib import Path
import importlib.util
import json
import sys
from types import ModuleType


SCRIPT_PATH = Path("scripts/betting/scan_betting_brittleness.py")


def load_scanner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_betting_brittleness", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_brittleness_scan_passes_clean_fixture(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text("def ok():\n    return 'fixture proof'\n", encoding="utf-8")

    findings = load_scanner().scan((clean,))

    assert findings == []


def test_brittleness_scan_default_repo_paths_are_clean() -> None:
    scanner = load_scanner()

    findings = scanner.scan(scanner.DEFAULT_SCAN_PATHS)

    assert findings == []


def test_brittleness_scan_flags_legacy_mapper_symbol(tmp_path: Path) -> None:
    brittle = tmp_path / "brittle.py"
    brittle.write_text("MARKET_MAPPER = {}\n", encoding="utf-8")

    findings = load_scanner().scan((brittle,))

    assert [finding.rule for finding in findings] == ["banned-symbol:MARKET_MAPPER"]


def test_brittleness_scan_flags_direct_matcher_event_gate(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    matcher = root / "nautilus_trader/adapters/betting/market_matcher.py"
    matcher.parent.mkdir(parents=True)
    matcher.write_text("if instrument.matches_event(candidate):\n    pass\n", encoding="utf-8")

    findings = load_scanner().scan((matcher,))

    assert [finding.rule for finding in findings] == ["direct-matches-event-gate"]


def test_brittleness_scan_json_output_lists_findings(tmp_path: Path, capsys) -> None:
    brittle = tmp_path / "brittle.py"
    brittle.write_text("BaseRiskEngine = object\n", encoding="utf-8")

    exit_code = load_scanner().main(["--json", str(brittle)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["findings"][0]["rule"] == "banned-symbol:BaseRiskEngine"
