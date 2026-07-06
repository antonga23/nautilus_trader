# skipcq
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the miner CPU benchmark's synthetic corpus and stage timings.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleMiner
from nautilus_trader.adapters.betting.semantics import RuleStore


SCRIPT_PATH = Path("scripts/betting/miner_cpu_benchmark.py")


def _load_benchmark() -> ModuleType:
    spec = importlib.util.spec_from_file_location("miner_cpu_benchmark", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_synthetic_corpus_is_deterministic_and_minable(tmp_path) -> None:
    bench = _load_benchmark()

    first = bench.build_corpus(records=300, fixtures=40, seed=7)
    second = bench.build_corpus(records=300, fixtures=40, seed=7)

    assert len(first) == 300
    assert [record.record_id for record in first] == [record.record_id for record in second]
    assert first == second
    assert len({record.record_id for record in first}) == 300
    assert {record.selection.sport for record in first} == {"soccer", "basketball", "baseball"}
    assert {record.provider for record in first} == {"CLOUDBET", "SXBET", "POLYMARKET"}

    miner = RuleMiner(RuleStore(FileRuleCache(tmp_path)))
    candidates = miner.mine_event_candidates(first, persist=False)
    templates = miner.mine_templates(first, persist=False, persist_event_candidates=False)

    assert len(candidates) > 0
    assert len(templates) > 0


def test_stage_timings_report_expected_stages(tmp_path) -> None:
    bench = _load_benchmark()
    records = bench.build_corpus(records=300, fixtures=40, seed=7)

    payload = bench.run_benchmark(records, repeats=1, store_dir=tmp_path / "store", profile=True)

    assert payload["stageErrors"] == {}
    assert set(payload["perStage"]) == set(bench.STAGE_NAMES)
    assert set(bench.STAGE_NAMES) == {
        "mine_event_candidates",
        "mine_templates",
        "store_load_records",
        "mine_store",
        "mine_templates_from_store",
        "mine_coverage_from_store",
    }
    for stage in payload["perStage"].values():
        assert stage["medianSecs"] > 0
        assert stage["recordsPerSec"] > 0
        assert stage["variancePct"] >= 0
    assert payload["perStage"]["store_load_records"]["resultCount"] == 300
    assert payload["perStage"]["mine_event_candidates"]["resultCount"] > 0
    assert payload["perStage"]["mine_store"]["resultCount"] > 0
    assert payload["perStage"]["mine_templates"]["resultCount"] > 0
    assert payload["perStage"]["mine_templates_from_store"]["resultCount"] > 0
    assert payload["populateSecs"] > 0

    assert payload["profiledStage"] in payload["perStage"]
    assert 0 < len(payload["profileTop"]) <= 20
    for row in payload["profileTop"]:
        assert row["function"]
        assert row["cumtimeSecs"] >= 0
        assert row["ncalls"] >= 1

    assert json.loads(json.dumps(payload)) is not None
