"""
Hermetic tests for the standing miner service.

The mine phase is always mocked; slim export and distribution run against real
rule stores on tmp dirs and are asserted with the real ``semantic_cache_status``.

"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import miner_service as mod

COMMAND_FILENAME_RE = re.compile(r"^miner-(\d{8}T\d{12}Z)-reload_semantic_cache\.json$")
DROPPED_KEY_PREFIXES = (
    "betting:semantic_rules:normalized",
    "betting:semantic_rules:snapshot",
    "betting:semantic_rules:candidate",
    "betting:semantic_rules:template:candidate",
    "betting:semantic_rules:template:support",
    "betting:semantic_rules:validation",
    "betting:semantic_rules:index:normalized",
    "betting:semantic_rules:index:snapshots",
    "betting:semantic_rules:index:candidates",
    "betting:semantic_rules:index:template_candidates",
    "betting:semantic_rules:index:template_support",
    "betting:semantic_rules:index:validation",
)


def _config(tmp_path: Path, **overrides: str) -> mod.MinerConfig:
    env = {
        "MINER_MASTER_DIR": str(tmp_path / "master"),
        "MINER_MANIFEST": str(tmp_path / "mine-manifest.json"),
        "MINER_NODES_ROOT": str(tmp_path / "nodes"),
        "MINER_INTERVAL_HOURS": "0.001",
    }
    env.update(overrides)
    return mod.MinerConfig.from_env(env)


def _make_seed(tmp_path: Path) -> Path:
    seed_dir = tmp_path / "built-seed"
    seed_dir.mkdir()
    (seed_dir / "payload.bin").write_bytes(b"seed-generation-1")
    (seed_dir / "keys.json").write_text("{}", encoding="utf-8")
    return seed_dir


def _make_node(nodes_root: Path, name: str, *, with_manifest: bool = True) -> Path:
    node_dir = nodes_root / name
    node_dir.mkdir(parents=True)
    if with_manifest:
        (node_dir / mod.NODE_MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
    return node_dir


def _prime_previous_generation(node_dir: Path) -> None:
    for dirname in (mod.NODE_STAGING_DIRNAME, mod.NODE_SEED_DIRNAME):
        previous = node_dir / dirname
        previous.mkdir()
        (previous / "old-generation.bin").write_bytes(b"old")


def _out_store(semantics_store: Any, out_dir: Path) -> Any:
    return semantics_store.RuleStore(semantics_store.FileRuleCache(out_dir))


# --- config ----------------------------------------------------------------


def test_config_defaults() -> None:
    config = mod.MinerConfig.from_env({})
    assert config.master_dir == Path("/opt/cloudbet/miner/master-cache")
    assert config.manifest_path == Path("/opt/cloudbet/miner/mine-manifest.json")
    assert config.nodes_root == Path("/opt/cloudbet/strategy-nodes")
    assert config.interval_hours == 6.0
    assert config.hot_swap is True
    assert config.template_stale_days == 14.0
    assert config.max_disk_gb == 10.0
    assert config.log_level == "INFO"


def test_config_overrides_and_bad_numbers() -> None:
    config = mod.MinerConfig.from_env(
        {"MINER_HOT_SWAP": "0", "MINER_INTERVAL_HOURS": "not-a-number"},
    )
    assert config.hot_swap is False
    assert config.interval_hours == 6.0


# --- loop orchestration ------------------------------------------------------


def test_run_cycle_stage_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    miner = mod.Miner(config)
    order: list[str] = []
    export_kwargs: dict[str, Any] = {}

    def fake_mine(config: mod.MinerConfig) -> dict[str, Any]:
        order.append("mine")
        return {"corpus_record_count": 1, "promoted_template_count": 1, "phase_timings_secs": {}}

    def fake_export(master_dir: Path, out_dir: Path, **kwargs: Any) -> mod.SlimExportResult:
        order.append("export")
        export_kwargs.update(kwargs)
        return mod.SlimExportResult(1, 0, 0, 1, 1, 1)

    def fake_distribute(config: mod.MinerConfig, seed_dir: Path) -> list[str]:
        order.append("distribute")
        return ["node-a"]

    monkeypatch.setattr(mod, "mine_master", fake_mine)
    monkeypatch.setattr(mod, "export_slim_seed", fake_export)
    monkeypatch.setattr(mod, "distribute_seed", fake_distribute)
    monkeypatch.setattr(
        mod.Miner,
        "_log_node_diagnostics",
        lambda self: order.append("telemetry"),
    )

    miner.run_cycle()

    assert order == ["mine", "export", "distribute", "telemetry"]
    assert export_kwargs == {"stale_days": config.template_stale_days}


def test_run_cycle_mine_failure_does_not_block_distribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    miner = mod.Miner(config)
    order: list[str] = []

    def failing_mine(config: mod.MinerConfig) -> dict[str, Any]:
        raise RuntimeError("venue outage")

    def fake_export(master_dir: Path, out_dir: Path, **kwargs: Any) -> mod.SlimExportResult:
        order.append("export")
        return mod.SlimExportResult(1, 0, 0, 1, 1, 1)

    def fake_distribute(config: mod.MinerConfig, seed_dir: Path) -> list[str]:
        order.append("distribute")
        return []

    monkeypatch.setattr(mod, "mine_master", failing_mine)
    monkeypatch.setattr(mod, "export_slim_seed", fake_export)
    monkeypatch.setattr(mod, "distribute_seed", fake_distribute)

    miner.run_cycle()

    assert order == ["export", "distribute"]


def test_run_cycle_export_failure_skips_distribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    miner = mod.Miner(config)
    distributed: list[Path] = []

    def failing_export(master_dir: Path, out_dir: Path, **kwargs: Any) -> mod.SlimExportResult:
        raise mod.ExportNotReadyError("not ready")

    monkeypatch.setattr(
        mod,
        "mine_master",
        lambda config: {
            "corpus_record_count": 0,
            "promoted_template_count": 0,
            "phase_timings_secs": {},
        },
    )
    monkeypatch.setattr(mod, "export_slim_seed", failing_export)
    monkeypatch.setattr(mod, "distribute_seed", lambda config, seed: distributed.append(seed))

    miner.run_cycle()

    assert distributed == []


def test_run_cycle_empty_master_never_distributes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Real export against an empty master must fail the readiness gate, so the
    # (mocked) distribution is never reached.
    config = _config(tmp_path)
    miner = mod.Miner(config)
    distributed: list[Path] = []
    monkeypatch.setattr(
        mod,
        "mine_master",
        lambda config: {
            "corpus_record_count": 0,
            "promoted_template_count": 0,
            "phase_timings_secs": {},
        },
    )
    monkeypatch.setattr(mod, "distribute_seed", lambda config, seed: distributed.append(seed))

    miner.run_cycle()

    assert distributed == []


def test_run_forever_exits_when_stop_requested_mid_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    miner = mod.Miner(_config(tmp_path))
    cycles: list[int] = []

    def fake_cycle() -> None:
        cycles.append(1)
        miner.request_stop()

    monkeypatch.setattr(miner, "run_cycle", fake_cycle)
    miner.run_forever()
    assert len(cycles) == 1


def test_signal_handler_stops_before_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    miner = mod.Miner(_config(tmp_path))
    cycles: list[int] = []
    monkeypatch.setattr(miner, "run_cycle", lambda: cycles.append(1))

    miner.handle_signal(signal.SIGTERM, None)
    miner.run_forever()

    assert cycles == []


def test_main_once_runs_single_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key, value in {
        "MINER_MASTER_DIR": str(tmp_path / "master"),
        "MINER_MANIFEST": str(tmp_path / "mine-manifest.json"),
        "MINER_NODES_ROOT": str(tmp_path / "nodes"),
    }.items():
        monkeypatch.setenv(key, value)
    cycles: list[int] = []
    monkeypatch.setattr(mod.Miner, "run_cycle", lambda self: cycles.append(1))

    assert mod.main(["--once"]) == 0
    assert cycles == [1]


# --- slim export (real rule store) -------------------------------------------


def test_export_slim_seed_contract(
    master_builder: Any,
    semantics_store: Any,
    semantic_cache: Any,
    days_ago: Any,
    tmp_path: Path,
) -> None:
    template = master_builder.add_promoted_template("template:fresh", last_seen_at=days_ago(1))
    proof, hyperedge = master_builder.add_coverage("cov-1")
    master_builder.add_manifest("manifest-1")
    master_builder.add_junk()
    master_builder.write_marker("scope-abc")
    out_dir = tmp_path / "seed"

    result = mod.export_slim_seed(master_builder.master_dir, out_dir, stale_days=14)

    assert result == mod.SlimExportResult(
        promoted_template_count=1,
        stale_template_count=0,
        filtered_template_count=0,
        coverage_proof_count=1,
        coverage_hyperedge_count=1,
        manifest_count=1,
    )

    # Acceptance invariant with the node runtime's own status check.
    status = semantic_cache.semantic_cache_status(out_dir)
    assert status.ready
    assert status.compatible
    assert status.compatibility_version == semantic_cache.SEMANTIC_CACHE_COMPATIBILITY_VERSION
    assert status.compatibility_scope == "scope-abc"
    assert status.promoted_template_count == 1
    assert status.coverage_proof_count == 1
    assert status.coverage_hyperedge_count == 1
    assert status.manifest_count == 1

    # Kept artifacts round-trip through the real store APIs.
    out_store = _out_store(semantics_store, out_dir)
    assert out_store.list_promoted_template_ids() == ["template:fresh"]
    assert out_store.load_promoted_template("template:fresh") == template
    assert out_store.list_coverage_proof_ids() == [proof.proof_id]
    assert out_store.load_coverage_proof(proof.proof_id) == proof
    assert out_store.list_coverage_hyperedge_ids() == [hyperedge.hyperedge_id]
    assert out_store.load_coverage_hyperedge(hyperedge.hyperedge_id) == hyperedge
    assert out_store.list_manifest_ids() == ["manifest-1"]
    assert out_store.load_manifest("manifest-1") is not None

    # Node-only prefixes are absent: their indexes and their objects.
    assert out_store.list_snapshot_ids() == []
    assert out_store.list_normalized_ids() == []
    assert out_store.list_candidate_ids() == []
    assert out_store.list_template_candidate_ids() == []
    assert out_store.list_validation_ids() == []
    assert out_store.list_template_support_ids() == []
    assert out_store.load_template_support("template:fresh") is None
    assert out_store.load_snapshot("snap-1") is None
    assert out_store.load_normalized_selection("rec-1") is None
    assert out_store.load_candidate("rule-1") is None

    # keys.json is restricted to kept keys and consistent with on-disk files.
    keys_index = json.loads((out_dir / "keys.json").read_text(encoding="utf-8"))
    assert keys_index
    for key, filename in keys_index.items():
        assert (out_dir / filename).is_file()
        assert not key.startswith(DROPPED_KEY_PREFIXES)
    assert {path.name for path in out_dir.glob("*.bin")} == set(keys_index.values())

    marker = json.loads(
        (out_dir / semantic_cache.SEMANTIC_CACHE_COMPATIBILITY_FILE).read_text(encoding="utf-8"),
    )
    assert marker == {
        "version": semantic_cache.SEMANTIC_CACHE_COMPATIBILITY_VERSION,
        "scope": "scope-abc",
    }
    assert (out_dir / semantic_cache.SEMANTIC_CACHE_SUMMARY_FILE).is_file()


def test_export_staleness_boundary(
    master_builder: Any,
    semantics_store: Any,
    days_ago: Any,
    tmp_path: Path,
) -> None:
    master_builder.add_promoted_template("template:fresh", last_seen_at=days_ago(13))
    master_builder.add_promoted_template("template:stale", last_seen_at=days_ago(15))
    master_builder.add_promoted_template("template:unknown-age", last_seen_at=None)
    master_builder.add_coverage("cov-1")
    master_builder.add_manifest("manifest-1")
    master_builder.write_marker()
    out_dir = tmp_path / "seed"

    result = mod.export_slim_seed(master_builder.master_dir, out_dir, stale_days=14)

    out_store = _out_store(semantics_store, out_dir)
    assert sorted(out_store.list_promoted_template_ids()) == [
        "template:fresh",
        "template:unknown-age",
    ]
    assert result.stale_template_count == 1


def test_export_sport_filter(
    master_builder: Any,
    semantics_store: Any,
    days_ago: Any,
    tmp_path: Path,
) -> None:
    master_builder.add_promoted_template(
        "template:soccer",
        sport="soccer",
        last_seen_at=days_ago(1),
    )
    master_builder.add_promoted_template(
        "template:basketball",
        sport="basketball",
        last_seen_at=days_ago(1),
    )
    soccer_proof, soccer_edge = master_builder.add_coverage("cov-soccer", sport="soccer")
    master_builder.add_coverage("cov-basketball", sport="basketball")
    master_builder.add_manifest("manifest-1")
    master_builder.write_marker()
    out_dir = tmp_path / "seed"

    result = mod.export_slim_seed(master_builder.master_dir, out_dir, sports=["Soccer"])

    out_store = _out_store(semantics_store, out_dir)
    assert out_store.list_promoted_template_ids() == ["template:soccer"]
    assert out_store.list_coverage_proof_ids() == [soccer_proof.proof_id]
    # Hyperedges survive only when their coverage proof survived.
    assert out_store.list_coverage_hyperedge_ids() == [soccer_edge.hyperedge_id]
    assert result.filtered_template_count == 1


def test_export_tier_filter(
    master_builder: Any,
    semantics_store: Any,
    days_ago: Any,
    tmp_path: Path,
) -> None:
    master_builder.add_promoted_template(
        "template:safe",
        tier="EXECUTION_SAFE",
        last_seen_at=days_ago(1),
    )
    master_builder.add_promoted_template(
        "template:audit",
        tier="AUDIT_ONLY",
        last_seen_at=days_ago(1),
    )
    safe_proof, _ = master_builder.add_coverage("cov-safe", tier="EXECUTION_SAFE")
    master_builder.add_coverage("cov-audit", tier="AUDIT_ONLY")
    master_builder.add_manifest("manifest-1")
    master_builder.write_marker()
    out_dir = tmp_path / "seed"

    mod.export_slim_seed(master_builder.master_dir, out_dir, tiers=["EXECUTION_SAFE"])

    out_store = _out_store(semantics_store, out_dir)
    assert out_store.list_promoted_template_ids() == ["template:safe"]
    assert out_store.list_coverage_proof_ids() == [safe_proof.proof_id]


def test_export_empty_master_fails_readiness_gate(tmp_path: Path) -> None:
    with pytest.raises(mod.ExportNotReadyError):
        mod.export_slim_seed(tmp_path / "master", tmp_path / "seed")


# --- distribution -------------------------------------------------------------


def test_distribute_swaps_dirs_and_writes_exact_command(tmp_path: Path) -> None:
    config = _config(tmp_path)
    seed_dir = _make_seed(tmp_path)
    node_a = _make_node(config.nodes_root, "node-a")
    _prime_previous_generation(node_a)
    node_b = _make_node(config.nodes_root, "node-b")
    _make_node(config.nodes_root, "not-a-node", with_manifest=False)

    distributed = mod.distribute_seed(config, seed_dir)

    assert distributed == ["node-a", "node-b"]
    for node_dir in (node_a, node_b):
        for dirname in (mod.NODE_STAGING_DIRNAME, mod.NODE_SEED_DIRNAME):
            swapped = node_dir / dirname
            assert (swapped / "payload.bin").read_bytes() == b"seed-generation-1"
            assert not (swapped / "old-generation.bin").exists()
        assert not list(node_dir.glob(".miner-tmp-*"))
        assert not list(node_dir.glob(".miner-old-*"))
    assert not (config.nodes_root / "not-a-node" / mod.NODE_STAGING_DIRNAME).exists()

    command_files = list((node_a / mod.COMMANDS_DIRNAME).glob("*.json"))
    assert len(command_files) == 1
    match = COMMAND_FILENAME_RE.fullmatch(command_files[0].name)
    assert match is not None
    payload = json.loads(command_files[0].read_text(encoding="utf-8"))
    assert payload == {
        "command": "reload_semantic_cache",
        "id": f"miner-{match.group(1)}",
        "staging_dir": "/var/lib/nautilus-node/semantic-rule-cache-staging",
    }


def test_distribute_hot_swap_disabled_writes_no_command(tmp_path: Path) -> None:
    config = _config(tmp_path, MINER_HOT_SWAP="0")
    seed_dir = _make_seed(tmp_path)
    node_a = _make_node(config.nodes_root, "node-a")

    distributed = mod.distribute_seed(config, seed_dir)

    assert distributed == ["node-a"]
    assert (node_a / mod.NODE_STAGING_DIRNAME / "payload.bin").is_file()
    commands_dir = node_a / mod.COMMANDS_DIRNAME
    assert not commands_dir.exists() or not list(commands_dir.iterdir())


def test_distribute_failed_swap_in_restores_previous_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    seed_dir = _make_seed(tmp_path)
    node_a = _make_node(config.nodes_root, "node-a")
    _prime_previous_generation(node_a)

    real_rename = os.rename
    calls = {"count": 0}

    def flaky_rename(src: Any, dst: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated crash between rename-aside and rename-in")
        real_rename(src, dst)

    monkeypatch.setattr(mod.os, "rename", flaky_rename)

    distributed = mod.distribute_seed(config, seed_dir)

    assert distributed == []
    staging = node_a / mod.NODE_STAGING_DIRNAME
    assert (staging / "old-generation.bin").read_bytes() == b"old"
    assert not (staging / "payload.bin").exists()
    assert not list(node_a.glob(".miner-tmp-*"))
    assert not list(node_a.glob(".miner-old-*"))
    assert not list((node_a / mod.COMMANDS_DIRNAME).glob("*.json"))


def test_distribute_partial_copy_leaves_staging_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    seed_dir = _make_seed(tmp_path)
    node_a = _make_node(config.nodes_root, "node-a")
    _prime_previous_generation(node_a)

    def failing_copytree(src: Any, dst: Any, **kwargs: Any) -> Any:
        Path(dst).mkdir()
        (Path(dst) / "partial.bin").write_bytes(b"partial")
        raise OSError("simulated copy failure")

    monkeypatch.setattr(mod.shutil, "copytree", failing_copytree)

    distributed = mod.distribute_seed(config, seed_dir)

    assert distributed == []
    staging = node_a / mod.NODE_STAGING_DIRNAME
    assert (staging / "old-generation.bin").read_bytes() == b"old"
    assert not (staging / "partial.bin").exists()
    assert not list(node_a.glob(".miner-tmp-*"))


def test_distribute_failing_node_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    seed_dir = _make_seed(tmp_path)
    _make_node(config.nodes_root, "node-a")
    node_b = _make_node(config.nodes_root, "node-b")

    real_copytree = shutil.copytree

    def selective_copytree(src: Any, dst: Any, **kwargs: Any) -> Any:
        if "node-a" in str(dst):
            raise OSError("simulated node-a failure")
        return real_copytree(src, dst, **kwargs)

    monkeypatch.setattr(mod.shutil, "copytree", selective_copytree)

    distributed = mod.distribute_seed(config, seed_dir)

    assert distributed == ["node-b"]
    assert (node_b / mod.NODE_STAGING_DIRNAME / "payload.bin").is_file()


# --- disk guard and telemetry --------------------------------------------------


def test_disk_guard_warns_above_threshold(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, MINER_MAX_DISK_GB="0.000001")
    config.master_dir.mkdir(parents=True)
    (config.master_dir / "blob.bin").write_bytes(b"x" * 1_000_000)

    with caplog.at_level(logging.WARNING, logger="miner"):
        size_gb = mod.check_master_disk(config)

    assert size_gb > config.max_disk_gb
    assert any("MINER_MAX_DISK_GB" in record.message for record in caplog.records)


def test_disk_guard_quiet_below_threshold(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.master_dir.mkdir(parents=True)
    (config.master_dir / "blob.bin").write_bytes(b"x")

    with caplog.at_level(logging.WARNING, logger="miner"):
        mod.check_master_disk(config)

    assert not caplog.records


def test_node_diagnostics_deltas(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    miner = mod.Miner(config)
    node_a = _make_node(config.nodes_root, "node-a")

    def write_status(unsupported: int) -> None:
        (node_a / mod.NODE_STATUS_FILENAME).write_text(
            json.dumps(
                {
                    "semanticDiagnostics": {
                        "unsupportedProviderPatternCount": unsupported,
                        "supportedProviderCoverageRatio": 0.75,
                    },
                },
            ),
            encoding="utf-8",
        )

    write_status(5)
    with caplog.at_level(logging.INFO, logger="miner"):
        miner._log_node_diagnostics()
    assert "delta=None" in caplog.text

    caplog.clear()
    write_status(3)
    with caplog.at_level(logging.INFO, logger="miner"):
        miner._log_node_diagnostics()
    assert "delta=-2.0" in caplog.text
    assert "supported_coverage_ratio=0.75" in caplog.text


# --- mine manifest ------------------------------------------------------------


def test_mine_manifest_parses_with_node_manifest_loader() -> None:
    manifest_path = Path(mod.__file__).resolve().parent / "mine-manifest.json"
    manifest = mod._node_builder().load_manifest(manifest_path)
    assert manifest.node_id == "standing-miner"
    enabled = [venue for venue in manifest.venues if venue.enabled]
    assert sorted(venue.venue for venue in enabled) == ["CLOUDBET", "POLYMARKET", "SXBET"]
