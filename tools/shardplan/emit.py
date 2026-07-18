"""
Emit deployable shard manifests from packed bins.

Each bin becomes one manifest derived from the checked-in per-sport shard
template (post-quoted-edge-priority shape: CLOUDBET quote cap 400 at 1s poll,
SXBET stream transport, staged void-compatible middles). The emitted manifest
is always structurally unarmed: validation_mode true, auto_execute /
live_execution_armed / value_execution_enabled false, execution disabled on
every venue.

Budget scaling rule: every per-venue budget (instrument_load_limit,
market_discovery_limit, quote_subscription_limit, top_markets_by_depth) is
``max(template_value, ceil(template_value * bin_weight / capacity))`` -- the
template values are the proven per-sport floor, and budgets only grow
linearly once a bin's measured weight exceeds the capacity (over-capacity
dedicated bins awaiting a league-level split).

"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

from tools.shardplan.pack import Bin


TEMPLATE_PATH = Path(
    "deploy/strategy_nodes/betting_arbitrage/cloudbet-sxbet-polymarket-tennis.json",
)
LINT_SCRIPT_PATH = Path("scripts/strategy_nodes/lint_live_pilot_manifest.py")
SCALED_BUDGET_FIELDS = (
    "instrument_load_limit",
    "market_discovery_limit",
    "quote_subscription_limit",
    "top_markets_by_depth",
)
UNARMED_LINT_MARKERS = frozenset(
    {
        "live_pilot_manifest_in_validation_mode",
        "auto_execute_not_enabled_for_live_pilot",
        "manifest_live_execution_gate_not_declared",
    },
)


class ManifestValidationError(RuntimeError):
    """
    Raised when an emitted manifest fails load/build/lint validation.
    """


def _sxbet_sport_ids(sports: tuple[str, ...]) -> list[int]:
    from nautilus_trader.adapters.sxbet.constants import SXBET_SPORT_IDS

    by_name = {name: sport_id for sport_id, name in SXBET_SPORT_IDS.items()}
    return sorted(by_name[sport] for sport in sports if sport in by_name)


def node_id_for(shard_bin: Bin) -> str:
    return shard_bin.name


def trader_id_for(shard_bin: Bin) -> str:
    tag = "-".join(sport.upper().replace("_", "") for sport in shard_bin.sports)
    return f"BETARB-SHARD-{tag}-001"


def container_name_for(shard_bin: Bin) -> str:
    return f"betting-arbitrage-node-{node_id_for(shard_bin)}"


def scale_budget(template_value: int, weight: int, capacity: int) -> int:
    return max(template_value, math.ceil(template_value * weight / capacity))


def load_template(template_path: Path = TEMPLATE_PATH) -> dict[str, Any]:
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Template manifest {template_path} must be a JSON object")
    return payload


def _build_venues(shard_bin: Bin, entries: list[Any]) -> list[dict[str, Any]]:
    venues: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        venue = copy.deepcopy(entry)
        if venue.get("venue") == "SXBET":
            sport_ids = _sxbet_sport_ids(shard_bin.sports)
            if not sport_ids:
                continue  # No SXBET listing for any sport in this bin
            venue["sport_ids"] = sport_ids
        elif "sport_keys" in venue:
            venue["sport_keys"] = sorted(shard_bin.sports)
        venue["execution_enabled"] = False
        for budget_field in SCALED_BUDGET_FIELDS:
            template_value = venue.get(budget_field)
            if isinstance(template_value, int):
                venue[budget_field] = scale_budget(
                    template_value,
                    shard_bin.weight,
                    shard_bin.capacity,
                )
        venues.append(venue)
    return venues


def build_manifest(shard_bin: Bin, template: dict[str, Any]) -> dict[str, Any]:
    """
    Derive one shard manifest from the template for the given bin.
    """
    manifest = copy.deepcopy(template)
    node_id = node_id_for(shard_bin)
    manifest["node_id"] = node_id
    manifest["trader_id"] = trader_id_for(shard_bin)
    manifest["validation_mode"] = True
    manifest["rendered_config_path"] = (
        f"artifacts/strategy-nodes/{node_id}/trading-node-config.json"
    )
    manifest["status_path"] = f"artifacts/strategy-nodes/{node_id}/status.json"
    manifest["heartbeat_path"] = f"artifacts/strategy-nodes/{node_id}/heartbeat.json"
    manifest["semantic_rule_cache_dir"] = f"artifacts/semantic-rule-cache/{node_id}"
    manifest["semantic_rule_cache_seed_dir"] = (
        f"artifacts/strategy-nodes/{node_id}/semantic-rule-cache-seed"
    )
    manifest["semantic_rule_cache_mode"] = "reuse"
    manifest["semantic_rule_cache_seed_allow_scope_mismatch"] = True

    strategy = manifest.get("strategy")
    if not isinstance(strategy, dict):
        raise ValueError("Template manifest has no strategy object")
    # sport_filter is a post-filter on the merged topology, so it can only be
    # set when the bin holds exactly one sport; grouped bins scope per venue.
    strategy["sport_filter"] = shard_bin.sports[0] if shard_bin.dedicated else None
    strategy["auto_execute"] = False
    strategy["live_execution_armed"] = False
    strategy["value_execution_enabled"] = False

    venues = _build_venues(shard_bin, manifest.get("venues") or [])
    manifest["venues"] = venues

    venue_names = {str(venue.get("venue")) for venue in venues}
    for list_field in ("semantic_unmatched_quote_probe_venues", "devig_reference_venues"):
        declared = strategy.get(list_field)
        if isinstance(declared, list):
            strategy[list_field] = [name for name in declared if name in venue_names]

    manifest["metadata"] = {
        "phase": "smart-sharding-validation",
        "track": node_id,
        "generated_by": "tools/shardplan",
        "sports": ",".join(shard_bin.sports),
        "bin_weight": str(shard_bin.weight),
        "bin_capacity": str(shard_bin.capacity),
        "note": (
            "Validation-mode only, data-only, structurally unarmed. "
            "Whole-sport shard with all venues co-located so cross-venue edges "
            "still form. Generated by tools/shardplan from measured per-sport "
            "instrument weights; budgets floor at the per-sport template values."
        ),
    }
    return manifest


def emit_manifests(
    bins: tuple[Bin, ...] | list[Bin],
    out_dir: Path,
    template_path: Path = TEMPLATE_PATH,
) -> list[Path]:
    """
    Write one manifest per bin into ``out_dir`` and return the paths.
    """
    template = load_template(template_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for shard_bin in bins:
        manifest = build_manifest(shard_bin, template)
        path = out_dir / f"{node_id_for(shard_bin)}.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def _load_lint_module(lint_script_path: Path = LINT_SCRIPT_PATH) -> ModuleType:
    spec = importlib.util.spec_from_file_location("lint_live_pilot_manifest", lint_script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load lint script {lint_script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _structural_problems(manifest: Any, config: Any) -> list[str]:
    problems: list[str] = []
    if not manifest.validation_mode:
        problems.append("validation_mode_not_true")
    if manifest.strategy.auto_execute:
        problems.append("auto_execute_armed")
    if manifest.strategy.live_execution_armed:
        problems.append("live_execution_armed")
    if manifest.strategy.value_execution_enabled:
        problems.append("value_execution_enabled")
    if any(venue.execution_enabled for venue in manifest.venues):
        problems.append("venue_execution_enabled")
    if len(config.exec_clients) != 0:
        problems.append("exec_clients_built")
    if manifest.strategy.opportunity_graph_engine != "semantic_rust":
        problems.append("opportunity_graph_engine_not_semantic_rust")
    return problems


def validate_manifest_file(
    path: Path,
    template_path: Path = TEMPLATE_PATH,
    lint_script_path: Path = LINT_SCRIPT_PATH,
) -> dict[str, Any]:
    """
    Validate an emitted manifest end-to-end.

    Loads it through the repo ``load_manifest`` + ``build_trading_node_config``
    (so venue scoping, budgets, and cache wiring are the real deployable
    config), asserts it is structurally unarmed, and runs the live-pilot lint
    script: the unarmed markers must be present (proving the lint sees it as
    NOT live-armed) and no lint issue may appear that the checked-in template
    shard does not already carry.

    """
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import (
        build_trading_node_config,
    )
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import load_manifest

    manifest = load_manifest(path)
    config = build_trading_node_config(manifest)
    problems = _structural_problems(manifest, config)

    lint = _load_lint_module(lint_script_path)
    result = lint.lint_manifest(path)
    issues = set(result.get("issues") or [])
    template_issues = set(lint.lint_manifest(template_path).get("issues") or [])
    missing_markers = UNARMED_LINT_MARKERS - issues
    if missing_markers:
        problems.append(f"missing_unarmed_lint_markers:{sorted(missing_markers)}")
    new_issues = issues - template_issues
    if new_issues:
        problems.append(f"lint_issues_beyond_template:{sorted(new_issues)}")

    if problems:
        raise ManifestValidationError(f"{path}: {problems}")
    return {
        "path": str(path),
        "node_id": manifest.node_id,
        "venues": [venue.venue for venue in manifest.venues],
        "data_clients": len(config.data_clients),
        "exec_clients": len(config.exec_clients),
        "lint_issues": sorted(issues),
    }
