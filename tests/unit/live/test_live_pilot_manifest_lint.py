from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


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
            "opportunity_graph_enabled": True,
            "opportunity_graph_engine": "semantic_rust",
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


def test_lint_manifest_requires_semantic_rust_runtime(tmp_path):
    module = _load_module()
    manifest = _manifest()
    manifest["strategy"]["opportunity_graph_engine"] = "auto"
    manifest["strategy"]["opportunity_graph_enabled"] = False
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = module.lint_manifest(path)

    assert result["status"] == "fail"
    assert "opportunity_graph_disabled" in result["issues"]
    assert "opportunity_graph_engine_not_semantic_rust" in result["issues"]


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


def test_betting_node_manifests_pin_semantic_rust_topology() -> None:
    manifest_paths = sorted(Path("deploy/strategy_nodes/betting_arbitrage").glob("*.json"))

    assert manifest_paths
    for path in manifest_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        strategy = payload.get("strategy")
        assert isinstance(strategy, dict), path
        assert strategy.get("opportunity_graph_engine") == "semantic_rust", path


PER_SPORT_SHARDS = {
    "soccer": 5,
    "tennis": 6,
    "basketball": 1,
}

# CLOUDBET quote-subscription budget per shard. shardplan `budget --apply` cut the
# over-provisioned caps on baseball/basketball/soccer (gap-heavy: most subscriptions had no
# cross-venue common fixture). Tennis keeps its 600 budget and instead enables tiered
# quote-poll scheduling (below) as the canary for cutting CLOUDBET REST poll latency without
# shrinking the budget.
EXPECTED_CLOUDBET_QUOTE_LIMIT = {
    "soccer": 250,
    "tennis": 600,
    "basketball": 150,
}
EXPECTED_CLOUDBET_TIER_SCHEDULING = {
    "soccer": False,
    "tennis": True,
    "basketball": False,
}


@pytest.mark.parametrize("sport", sorted(PER_SPORT_SHARDS))
def test_per_sport_shard_is_scoped_all_venue_and_unarmed(sport: str) -> None:
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import (
        build_trading_node_config,
    )
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import load_manifest

    path = Path(
        f"deploy/strategy_nodes/betting_arbitrage/cloudbet-sxbet-polymarket-{sport}.json",
    )
    manifest = load_manifest(path)
    config = build_trading_node_config(manifest)

    # Sport-filtered to exactly one sport.
    assert manifest.strategy.sport_filter == sport

    # All three venues co-located so cross-venue edges still form.
    venues_by_name = {venue.venue: venue for venue in manifest.venues}
    assert set(venues_by_name) == {"CLOUDBET", "SXBET", "POLYMARKET"}
    assert len(config.data_clients) == 3

    # Structurally unarmed: no execution clients and every execution flag is off.
    assert len(config.exec_clients) == 0
    assert manifest.validation_mode is True
    assert manifest.strategy.live_execution_armed is False
    assert manifest.strategy.auto_execute is False
    assert manifest.strategy.value_execution_enabled is False
    assert all(not venue.execution_enabled for venue in manifest.venues)
    assert manifest.strategy.opportunity_graph_engine == "semantic_rust"

    # Cross-venue sequencer wiring is present.
    assert manifest.strategy.cross_venue_sequential_execution is True
    assert manifest.strategy.cross_venue_anchor_venue == "CLOUDBET"

    # Each venue is bounded by the live-subscription caps.
    for venue in manifest.venues:
        assert venue.instrument_load_limit is not None
        assert venue.market_discovery_limit is not None
        assert venue.quote_subscription_limit is not None
        assert venue.top_markets_by_depth is not None

    # CLOUDBET quote budget is per-shard after the shardplan gap-heavy re-tune: baseball/
    # basketball/soccer shrink the over-provisioned cap (most CLOUDBET subscriptions had no
    # cross-venue common fixture); tennis keeps 600 and enables tiered quote-poll scheduling
    # so hot cross-venue legs poll every 1.0s cycle while warm/cold instruments poll less,
    # cutting effective REST latency without dropping the budget.
    assert (
        venues_by_name["CLOUDBET"].quote_subscription_limit == EXPECTED_CLOUDBET_QUOTE_LIMIT[sport]
    )
    assert venues_by_name["CLOUDBET"].order_book_poll_interval_secs == 1.0
    assert (
        venues_by_name["CLOUDBET"].order_book_quote_tier_scheduling_enabled
        is EXPECTED_CLOUDBET_TIER_SCHEDULING[sport]
    )

    # SXBET quotes stream (Centrifugo) so subscribed legs clear the 5s live age gate that
    # the 10s REST poll fallback cannot.
    assert venues_by_name["SXBET"].order_book_transport == "stream"

    # The REST fallback must be able to burst through the reseed/poll backlog when the
    # stream drops: adaptive concurrency from the base 4 up to 16.
    assert venues_by_name["SXBET"].order_book_concurrency == 4
    assert venues_by_name["SXBET"].order_book_max_concurrency == 16
    assert venues_by_name["SXBET"].order_book_adaptive_concurrency is True

    # Void-compatible middles STAGE for manual approval, with the middle floor strictly
    # above the ordinary arb floor. Staging only: the armed flags asserted false above are
    # untouched by the flag.
    assert manifest.strategy.execute_void_compatible_middles is True
    assert manifest.strategy.min_middle_profit_margin > manifest.strategy.min_profit_margin

    # Per-venue sport scoping uses the venue-native key/id space.
    assert venues_by_name["CLOUDBET"].sport_keys == frozenset({sport})
    assert venues_by_name["POLYMARKET"].sport_keys == frozenset({sport})
    assert venues_by_name["SXBET"].sport_ids == frozenset({PER_SPORT_SHARDS[sport]})

    # Total live-instrument budget stays near baseball scale (<= ~2000).
    total_cap = sum(int(venue.instrument_load_limit or 0) for venue in manifest.venues)
    assert total_cap <= 2000


def test_baseball_shard_raises_cloudbet_budget_streams_sxbet_and_stages_middles() -> None:
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import (
        build_trading_node_config,
    )
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import load_manifest

    path = Path("deploy/strategy_nodes/betting_arbitrage/cloudbet-sxbet-baseball.json")
    manifest = load_manifest(path)
    config = build_trading_node_config(manifest)

    venues_by_name = {venue.venue: venue for venue in manifest.venues}
    assert set(venues_by_name) == {"CLOUDBET", "SXBET"}

    # CLOUDBET quote budget cut to 180 by the shardplan gap-heavy re-tune (most CLOUDBET
    # subscriptions had no cross-venue common fixture); arb-relevant legs stay in-cap and
    # refresh under the 1.0s poll (inside the 3s CLOUDBET live quote-age gate).
    assert venues_by_name["CLOUDBET"].quote_subscription_limit == 180
    assert venues_by_name["CLOUDBET"].order_book_poll_interval_secs == 1.0

    # SXBET streams so subscribed legs clear the 5s live age gate; the REST fallback
    # bursts adaptively from the base 4 up to 16 when the stream drops.
    assert venues_by_name["SXBET"].order_book_transport == "stream"
    assert venues_by_name["SXBET"].order_book_concurrency == 4
    assert venues_by_name["SXBET"].order_book_max_concurrency == 16
    assert venues_by_name["SXBET"].order_book_adaptive_concurrency is True

    # Middles STAGE (unarmed): flag on, middle floor above arb floor, every armed flag false
    # and no execution client built.
    assert manifest.strategy.execute_void_compatible_middles is True
    assert manifest.strategy.min_middle_profit_margin > manifest.strategy.min_profit_margin
    assert manifest.strategy.live_execution_armed is False
    assert manifest.strategy.auto_execute is False
    assert manifest.strategy.value_execution_enabled is False
    assert len(config.exec_clients) == 0
    assert all(not venue.execution_enabled for venue in manifest.venues)


def test_soccer_shard_is_partial_lock_canary_and_stays_unarmed() -> None:
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import (
        build_trading_node_config,
    )
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import load_manifest

    path = Path("deploy/strategy_nodes/betting_arbitrage/cloudbet-sxbet-polymarket-soccer.json")
    manifest = load_manifest(path)
    config = build_trading_node_config(manifest)

    # Soccer is the partial-compatible-lock canary: both opt-ins on, partial-lock floor
    # strictly above the arb floor.
    assert manifest.strategy.allow_partial_compatible_locks is True
    assert manifest.strategy.execute_partial_compatible_locks is True
    assert manifest.strategy.min_partial_lock_profit_margin > manifest.strategy.min_profit_margin

    # Staging only: every armed flag stays false and no execution client is built.
    assert manifest.validation_mode is True
    assert manifest.strategy.live_execution_armed is False
    assert manifest.strategy.auto_execute is False
    assert manifest.strategy.value_execution_enabled is False
    assert len(config.exec_clients) == 0
    assert all(not venue.execution_enabled for venue in manifest.venues)
