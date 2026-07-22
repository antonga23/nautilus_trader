# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Hermetic unit tests for the smart-sharding allocator (``tools/shardplan``).

Synthetic weight tables and status fixtures only; emitted manifests are validated
through the real repo manifest loader, trading-node config builder, and the live-pilot
lint script.

"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.shardplan import plan
from tools.shardplan.budget import capacity_bound
from tools.shardplan.budget import extract_signals
from tools.shardplan.budget import plan_budgets
from tools.shardplan.budget import propose
from tools.shardplan.collect import apportion
from tools.shardplan.collect import collect_weights
from tools.shardplan.collect import load_static_weights
from tools.shardplan.collect import merge_weights
from tools.shardplan.emit import ManifestValidationError
from tools.shardplan.emit import TEMPLATE_PATH
from tools.shardplan.emit import build_manifest
from tools.shardplan.emit import emit_manifests
from tools.shardplan.emit import load_template
from tools.shardplan.emit import scale_budget
from tools.shardplan.emit import validate_manifest_file
from tools.shardplan.pack import pack


LIVE_WEIGHTS = {
    "tennis": 1886,
    "basketball": 1840,
    "soccer": 940,
    "baseball": 477,
    "american_football": 0,
    "ice_hockey": 0,
}


def _status_payload(
    node_counts: dict[str, int],
    event_sport_counts: dict[str, dict[str, int]],
    exceeded_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "nodeId": "fixture-node",
        "status": "running",
        "runtimeProbe": {
            "venueCoverage": {
                "enabledVenues": sorted(node_counts),
                "nodeCounts": node_counts,
                "eventSportCounts": event_sport_counts,
                "quoteSubscriptionCounts": dict.fromkeys(node_counts, 0),
                "quoteSubscriptionLimits": dict.fromkeys(node_counts, 400),
                "quoteSubscriptionLimitExceededCounts": exceeded_counts or {},
            },
        },
    }


def _budget_status_payload(
    node_id: str,
    counts: dict[str, int],
    limits: dict[str, int],
    exceeded: dict[str, int] | None = None,
    gaps: dict[str, int] | None = None,
    readiness: list[dict[str, Any]] | None = None,
    poll_stats: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "nodeId": node_id,
        "status": "running",
        "runtimeProbe": {
            "venueCoverage": {
                "enabledVenues": sorted(limits),
                "quoteSubscriptionCounts": counts,
                "quoteSubscriptionLimits": limits,
                "quoteSubscriptionLimitExceededCounts": exceeded or {},
                "quoteSubscriptionGapCounts": gaps or {},
                "crossVenueQuoteReadiness": readiness or [],
            },
            "providerQuotePollStats": poll_stats or {},
        },
    }


# Generous CLOUDBET poll cycle: 1200 requests in 20s at a 30s target serving
# 600 instruments -> capacity bound 900.
GENEROUS_POLL_STATS = {
    "CLOUDBET": {
        "request_count": 1200,
        "cycle_elapsed_secs": 20.0,
        "poll_target_cycle_secs": 30.0,
        "subscribed_instrument_count": 600,
    },
}


class TestPack:
    def test_live_weights_pack_to_dedicated_big_sports_and_one_grouped_bin(self) -> None:
        result = pack(LIVE_WEIGHTS, capacity=2000)

        assert [b.sports for b in result.bins] == [
            ("tennis",),
            ("basketball",),
            ("soccer", "baseball"),
        ]
        assert [b.name for b in result.bins] == [
            "shard-tennis",
            "shard-basketball",
            "shard-soccer-baseball",
        ]
        assert [b.weight for b in result.bins] == [1886, 1840, 1417]
        assert [b.dedicated for b in result.bins] == [True, True, False]
        assert all(not b.over_capacity for b in result.bins)
        assert result.dropped == ("american_football", "ice_hockey")

    def test_zero_weight_sports_produce_no_bin(self) -> None:
        result = pack({"american_football": 0, "ice_hockey": 0}, capacity=2000)

        assert result.bins == ()
        assert result.dropped == ("american_football", "ice_hockey")

    def test_over_capacity_sport_gets_dedicated_flagged_bin_without_co_tenants(self) -> None:
        result = pack({"tennis": 2600, "cricket": 150}, capacity=2000)

        assert [b.sports for b in result.bins] == [("tennis",), ("cricket",)]
        assert result.bins[0].over_capacity
        assert result.bins[0].dedicated
        assert not result.bins[1].over_capacity

    def test_small_sport_joins_first_bin_with_room(self) -> None:
        result = pack({"soccer": 940, "baseball": 477, "cricket": 400}, capacity=2000)

        assert [b.sports for b in result.bins] == [("soccer", "baseball", "cricket")]
        assert result.bins[0].weight == 1817

    def test_deterministic_across_input_order_and_ties(self) -> None:
        weights = {"soccer": 500, "cricket": 500, "baseball": 500, "mma": 200}
        reordered = dict(reversed(list(weights.items())))

        first = pack(weights, capacity=1000)
        second = pack(reordered, capacity=1000)

        assert first == second
        # Equal weights tie-break alphabetically.
        assert [b.sports for b in first.bins] == [
            ("baseball", "cricket"),
            ("soccer", "mma"),
        ]

    def test_capacity_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            pack({"tennis": 1}, capacity=0)


class TestApportion:
    def test_sums_are_exact_with_largest_remainder(self) -> None:
        parts = apportion(100, {"soccer": 1, "baseball": 1, "cricket": 1})

        assert sum(parts.values()) == 100
        assert parts == {"baseball": 34, "cricket": 33, "soccer": 33}

    def test_zero_total_or_no_shares_is_empty(self) -> None:
        assert apportion(0, {"soccer": 1}) == {}
        assert apportion(10, {"soccer": 0}) == {}


class TestCollect:
    def test_collects_weights_and_starvation_from_status_fixtures(self, tmp_path: Path) -> None:
        tennis_dir = tmp_path / "shard-tennis"
        tennis_dir.mkdir()
        (tennis_dir / "status.json").write_text(
            json.dumps(
                _status_payload(
                    node_counts={"CLOUDBET": 900, "SXBET": 500, "POLYMARKET": 486},
                    event_sport_counts={
                        "CLOUDBET": {"tennis": 120},
                        "SXBET": {"tennis": 80},
                        "POLYMARKET": {"tennis": 60},
                    },
                    exceeded_counts={"CLOUDBET": 30},
                ),
            ),
            encoding="utf-8",
        )
        # Flat status file with a grouped node: instruments apportioned by
        # per-sport event share (soccer 2/3, baseball 1/3 of 300).
        (tmp_path / "grouped.json").write_text(
            json.dumps(
                _status_payload(
                    node_counts={"CLOUDBET": 300},
                    event_sport_counts={"CLOUDBET": {"soccer": 2, "baseball": 1}},
                ),
            ),
            encoding="utf-8",
        )

        table = collect_weights(tmp_path)

        assert table["tennis"].total == 1886
        assert table["tennis"].venues == {"CLOUDBET": 900, "SXBET": 500, "POLYMARKET": 486}
        assert table["tennis"].starvation == 30
        assert table["soccer"].total == 200
        assert table["baseball"].total == 100
        assert table["soccer"].starvation == 0

    def test_ignores_payloads_without_venue_coverage(self, tmp_path: Path) -> None:
        (tmp_path / "empty.json").write_text(json.dumps({"status": "starting"}))

        assert collect_weights(tmp_path) == {}

    def test_static_weights_accept_scalar_and_venue_map_and_override(
        self,
        tmp_path: Path,
    ) -> None:
        static_path = tmp_path / "weights.json"
        static_path.write_text(
            json.dumps(
                {
                    "cricket": 150,
                    "tennis": {"CLOUDBET": 100, "SXBET": 50},
                },
            ),
        )

        static = load_static_weights(static_path)
        assert static["cricket"].total == 150
        assert static["tennis"].total == 150
        assert static["tennis"].venues == {"CLOUDBET": 100, "SXBET": 50}

        measured = load_static_weights(static_path)
        measured["tennis"].total = 999
        merged = merge_weights({"tennis": measured["tennis"]}, static)
        assert merged["tennis"].total == 150
        assert merged["cricket"].total == 150


class TestEmit:
    def test_scale_budget_floors_at_template_and_grows_over_capacity(self) -> None:
        assert scale_budget(500, 1417, 2000) == 500  # Under capacity: template floor
        assert scale_budget(500, 2000, 2000) == 500
        assert scale_budget(500, 3000, 2000) == 750  # Over capacity: linear growth
        assert scale_budget(150, 3000, 2000) == 225

    def test_grouped_bin_manifest_loads_builds_and_is_unarmed(self, tmp_path: Path) -> None:
        from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import (
            build_trading_node_config,
        )
        from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import load_manifest

        result = pack({"soccer": 940, "baseball": 477}, capacity=2000)
        paths = emit_manifests(result.bins, tmp_path)

        assert paths == [tmp_path / "shard-soccer-baseball.json"]
        manifest = load_manifest(paths[0])
        config = build_trading_node_config(manifest)

        assert manifest.node_id == "shard-soccer-baseball"
        assert manifest.trader_id == "BETARB-SHARD-SOCCER-BASEBALL-001"
        # Grouped bins must not post-filter the merged topology to one sport.
        assert manifest.strategy.sport_filter is None
        # All venues co-located so cross-venue edges survive.
        venues_by_name = {venue.venue: venue for venue in manifest.venues}
        assert set(venues_by_name) == {"CLOUDBET", "SXBET", "POLYMARKET"}
        assert len(config.data_clients) == 3
        # Per-venue scoping in each venue's native key/id space.
        assert venues_by_name["CLOUDBET"].sport_keys == frozenset({"soccer", "baseball"})
        assert venues_by_name["POLYMARKET"].sport_keys == frozenset({"soccer", "baseball"})
        assert venues_by_name["SXBET"].sport_ids == frozenset({3, 5})
        # Structurally unarmed.
        assert manifest.validation_mode is True
        assert manifest.strategy.auto_execute is False
        assert manifest.strategy.live_execution_armed is False
        assert manifest.strategy.value_execution_enabled is False
        assert all(not venue.execution_enabled for venue in manifest.venues)
        assert len(config.exec_clients) == 0
        # Post-quoted-edge-priority template shape is preserved.
        assert manifest.strategy.opportunity_graph_engine == "semantic_rust"
        assert manifest.strategy.cross_venue_sequential_execution is True
        assert manifest.strategy.cross_venue_anchor_venue == "CLOUDBET"
        assert manifest.strategy.execute_void_compatible_middles is True
        assert venues_by_name["CLOUDBET"].quote_subscription_limit == 600
        assert venues_by_name["SXBET"].order_book_transport == "stream"
        # Cache reuse wiring.
        assert manifest.semantic_rule_cache_mode == "reuse"
        assert manifest.semantic_rule_cache_seed_allow_scope_mismatch is True

    def test_single_sport_bin_keeps_sport_filter_string(self) -> None:
        result = pack({"tennis": 1886}, capacity=2000)
        manifest = build_manifest(result.bins[0], load_template())

        assert manifest["strategy"]["sport_filter"] == "tennis"
        assert manifest["node_id"] == "shard-tennis"

    def test_budgets_floor_at_template_under_capacity(self, tmp_path: Path) -> None:
        template = load_template()
        result = pack({"soccer": 940, "baseball": 477}, capacity=2000)
        manifest = build_manifest(result.bins[0], template)

        emitted = {venue["venue"]: venue for venue in manifest["venues"]}
        for entry in template["venues"]:
            for budget_field in (
                "instrument_load_limit",
                "market_discovery_limit",
                "quote_subscription_limit",
                "top_markets_by_depth",
            ):
                assert emitted[entry["venue"]][budget_field] == entry[budget_field]

    def test_budgets_scale_linearly_for_over_capacity_bin(self) -> None:
        result = pack({"tennis": 3000}, capacity=2000)
        manifest = build_manifest(result.bins[0], load_template())

        cloudbet = next(v for v in manifest["venues"] if v["venue"] == "CLOUDBET")
        assert cloudbet["instrument_load_limit"] == 1125  # ceil(750 * 3000/2000)
        assert cloudbet["quote_subscription_limit"] == 900  # ceil(600 * 3000/2000)

    def test_bin_without_sxbet_listing_drops_sxbet_venue(self) -> None:
        # american_football has no SXBET sport id, so the SXBET venue (and its
        # entries in the probe/devig venue lists) must drop out of the bin.
        result = pack({"american_football": 800}, capacity=2000)
        manifest = build_manifest(result.bins[0], load_template())

        venue_names = [venue["venue"] for venue in manifest["venues"]]
        assert venue_names == ["CLOUDBET", "POLYMARKET"]
        assert "SXBET" not in manifest["strategy"]["semantic_unmatched_quote_probe_venues"]
        assert "SXBET" not in manifest["strategy"]["devig_reference_venues"]

    def test_emitted_manifests_pass_end_to_end_validation(self, tmp_path: Path) -> None:
        result = pack(LIVE_WEIGHTS, capacity=2000)
        for path in emit_manifests(result.bins, tmp_path):
            summary = validate_manifest_file(path)
            assert summary["exec_clients"] == 0
            assert "live_pilot_manifest_in_validation_mode" in summary["lint_issues"]

    def test_validation_rejects_armed_manifest(self, tmp_path: Path) -> None:
        result = pack({"tennis": 1886}, capacity=2000)
        (path,) = emit_manifests(result.bins, tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["strategy"]["auto_execute"] = True
        payload["strategy"]["live_execution_armed"] = True
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        with pytest.raises(ManifestValidationError, match="auto_execute_armed"):
            validate_manifest_file(path)


class TestBudget:
    def test_starved_low_gap_grows_by_headroom_above_demand(self) -> None:
        payload = _budget_status_payload(
            "shard-basketball",
            counts={"CLOUDBET": 600},
            limits={"CLOUDBET": 600},
            exceeded={"CLOUDBET": 16},
            gaps={"CLOUDBET": 2},
            poll_stats=GENEROUS_POLL_STATS,
        )
        signals = extract_signals(payload)["CLOUDBET"]

        proposed, reason = propose(signals, current=600)

        # effective demand 600 + 16 - 2 = 614; * 1.10 = 675.4 -> 680.
        assert proposed == 680
        assert reason == "starved(+16) low-gap"

    def test_gap_heavy_shrinks_toward_effective_demand(self) -> None:
        payload = _budget_status_payload(
            "shard-basketball",
            counts={"CLOUDBET": 616},
            limits={"CLOUDBET": 600},
            exceeded={"CLOUDBET": 16},
            gaps={"CLOUDBET": 466},
            poll_stats=GENEROUS_POLL_STATS,
        )
        signals = extract_signals(payload)["CLOUDBET"]

        proposed, reason = propose(signals, current=600)

        # effective demand 616 + 16 - 466 = 166; * 1.10 = 182.6 -> 190.
        assert proposed == 190
        assert proposed < 600
        assert reason == "gap-heavy(466)"

    def test_no_common_fixture_everywhere_drops_to_floor(self) -> None:
        payload = _budget_status_payload(
            "shard-soccer",
            counts={"POLYMARKET": 479},
            limits={"POLYMARKET": 300},
            readiness=[
                {"venuePair": "CLOUDBET->POLYMARKET", "status": "no_common_fixture"},
                {"venuePair": "POLYMARKET->SXBET", "status": "no_common_fixture"},
                {"venuePair": "CLOUDBET->SXBET", "status": "cross_venue_candidates_observed"},
            ],
        )
        signals = extract_signals(payload)["POLYMARKET"]

        assert signals.no_common_fixture_everywhere
        proposed, reason = propose(signals, current=300)

        assert proposed == 75  # max(1, int(300 * 0.25))
        assert reason == "wasted: no_common_fixture"

    def test_one_healthy_pair_disables_the_no_common_fixture_floor(self) -> None:
        payload = _budget_status_payload(
            "shard-soccer",
            counts={"POLYMARKET": 290},
            limits={"POLYMARKET": 300},
            readiness=[
                {"venuePair": "CLOUDBET->POLYMARKET", "status": "no_common_fixture"},
                {"venuePair": "POLYMARKET->SXBET", "status": "cross_venue_candidates_observed"},
            ],
        )
        signals = extract_signals(payload)["POLYMARKET"]

        assert not signals.no_common_fixture_everywhere
        proposed, reason = propose(signals, current=300)

        assert proposed == 320  # 290 * 1.10 = 319 -> 320
        assert reason == "steady"

    def test_at_cap_clean_venue_grows_modestly(self) -> None:
        payload = _budget_status_payload(
            "shard-tennis",
            counts={"SXBET": 450},
            limits={"SXBET": 450},
        )
        signals = extract_signals(payload)["SXBET"]

        # Stream venue: no poll-capacity bound.
        assert signals.capacity_bound is None
        proposed, reason = propose(signals, current=450)

        assert proposed == 500  # 450 * 1.10 = 495 -> 500
        assert reason == "at-cap"

    def test_capacity_bound_caps_polled_venue_growth(self) -> None:
        payload = _budget_status_payload(
            "shard-tennis",
            counts={"CLOUDBET": 600},
            limits={"CLOUDBET": 600},
            exceeded={"CLOUDBET": 200},
            poll_stats={
                "CLOUDBET": {
                    "request_count": 1000,
                    "cycle_elapsed_secs": 25.0,
                    "poll_target_cycle_secs": 15.0,
                    "subscribed_instrument_count": 625,
                },
            },
        )
        signals = extract_signals(payload)["CLOUDBET"]

        # 40 req/s * 15s target * 625/1000 instruments-per-request = 375.
        assert signals.capacity_bound == 375
        proposed, reason = propose(signals, current=600)

        assert proposed == 375  # 880 wanted, capped at poll capacity
        assert reason == "starved(+200) low-gap capped(375)"

    def test_capacity_bound_requires_complete_poll_stats(self) -> None:
        assert capacity_bound({}) is None
        assert capacity_bound({"request_count": 100, "cycle_elapsed_secs": 0.0}) is None
        assert (
            capacity_bound(
                {
                    "request_count": 1200,
                    "cycle_elapsed_secs": 20.0,
                    "poll_target_cycle_secs": 30.0,
                    "subscribed_instrument_count": 600,
                },
            )
            == 900
        )


class TestBudgetCli:
    NODE_ID = "cloudbet-sxbet-polymarket-tennis"

    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        manifests_dir = tmp_path / "manifests"
        manifests_dir.mkdir()
        manifest_path = manifests_dir / f"{self.NODE_ID}.json"
        manifest_path.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"))
        nodes_root = tmp_path / "nodes"
        node_dir = nodes_root / self.NODE_ID
        node_dir.mkdir(parents=True)
        # One node reproducing the live cases: CLOUDBET starved (low gap),
        # SXBET pinned at cap, POLYMARKET sharing zero fixtures with anyone.
        (node_dir / "status.json").write_text(
            json.dumps(
                _budget_status_payload(
                    self.NODE_ID,
                    counts={"CLOUDBET": 600, "SXBET": 450, "POLYMARKET": 479},
                    limits={"CLOUDBET": 600, "SXBET": 450, "POLYMARKET": 300},
                    exceeded={"CLOUDBET": 16},
                    gaps={"CLOUDBET": 2},
                    readiness=[
                        {"venuePair": "CLOUDBET->POLYMARKET", "status": "no_common_fixture"},
                        {"venuePair": "POLYMARKET->SXBET", "status": "no_common_fixture"},
                        {
                            "venuePair": "CLOUDBET->SXBET",
                            "status": "cross_venue_candidates_observed",
                        },
                    ],
                    poll_stats=GENEROUS_POLL_STATS,
                ),
            ),
            encoding="utf-8",
        )
        return nodes_root, manifests_dir, manifest_path

    def test_plan_budgets_orders_deterministically(self, tmp_path: Path) -> None:
        nodes_root, manifests_dir, _ = self._setup(tmp_path)

        first = plan_budgets(nodes_root, manifests_dir)
        second = plan_budgets(nodes_root, manifests_dir)

        assert first == second
        assert [(p.node, p.venue) for p in first] == [
            (self.NODE_ID, "CLOUDBET"),
            (self.NODE_ID, "POLYMARKET"),
            (self.NODE_ID, "SXBET"),
        ]
        assert [(p.current, p.proposed) for p in first] == [(600, 680), (300, 75), (450, 500)]

    def test_dry_run_prints_table_and_leaves_manifests_untouched(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nodes_root, manifests_dir, manifest_path = self._setup(tmp_path)
        before = manifest_path.read_bytes()

        exit_code = plan.main(
            [
                "budget",
                "--nodes-root",
                str(nodes_root),
                "--manifests",
                str(manifests_dir),
            ],
        )
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "starved(+16) low-gap" in out
        assert "wasted: no_common_fixture" in out
        assert "at-cap" in out
        assert "Dry-run only" in out
        assert manifest_path.read_bytes() == before

    def test_apply_rewrites_manifest_and_revalidates(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nodes_root, manifests_dir, manifest_path = self._setup(tmp_path)

        exit_code = plan.main(
            [
                "budget",
                "--nodes-root",
                str(nodes_root),
                "--manifests",
                str(manifests_dir),
                "--apply",
            ],
        )
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "Rewritten manifests (validated: load + build + lint)" in out
        rewritten = json.loads(manifest_path.read_text(encoding="utf-8"))
        limits = {
            venue["venue"]: venue["quote_subscription_limit"] for venue in rewritten["venues"]
        }
        assert limits == {"CLOUDBET": 680, "SXBET": 500, "POLYMARKET": 75}
        # Only the subscription budget is re-planned in this iteration.
        template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        for emitted, original in zip(rewritten["venues"], template["venues"], strict=True):
            assert emitted["instrument_load_limit"] == original["instrument_load_limit"]
            assert emitted["market_discovery_limit"] == original["market_discovery_limit"]
        summary = validate_manifest_file(manifest_path)
        assert summary["exec_clients"] == 0

    def test_apply_fails_nonzero_when_validation_rejects_manifest(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        nodes_root, manifests_dir, manifest_path = self._setup(tmp_path)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["strategy"]["auto_execute"] = True
        manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        exit_code = plan.main(
            [
                "budget",
                "--nodes-root",
                str(nodes_root),
                "--manifests",
                str(manifests_dir),
                "--apply",
            ],
        )
        out = capsys.readouterr().out

        assert exit_code == 1
        assert "Manifest validation failed" in out

    def test_no_matching_manifests_exits_with_error(self, tmp_path: Path) -> None:
        nodes_root = tmp_path / "nodes"
        nodes_root.mkdir()
        manifests_dir = tmp_path / "manifests"
        manifests_dir.mkdir()

        with pytest.raises(SystemExit, match="No budget proposals"):
            plan.main(
                ["budget", "--nodes-root", str(nodes_root), "--manifests", str(manifests_dir)],
            )


class TestPlanCli:
    def _deploy_dir(self, tmp_path: Path) -> Path:
        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()
        # Existing per-sport tennis shard: same sport-set as the tennis bin.
        (deploy_dir / "tennis-shard.json").write_text(TEMPLATE_PATH.read_text(encoding="utf-8"))
        # Stale shard whose sport-set matches no bin.
        stale = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        for venue in stale["venues"]:
            if "sport_keys" in venue:
                venue["sport_keys"] = ["cricket"]
            if "sport_ids" in venue:
                venue["sport_ids"] = [15]
        (deploy_dir / "stale-cricket.json").write_text(json.dumps(stale))
        return deploy_dir

    def test_dry_run_reports_bins_diff_and_deploy_commands(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        weights_path = tmp_path / "weights.json"
        weights_path.write_text(json.dumps(LIVE_WEIGHTS))
        deploy_dir = self._deploy_dir(tmp_path)

        exit_code = plan.main(
            [
                "dry-run",
                "--static-weights",
                str(weights_path),
                "--capacity",
                "2000",
                "--deploy-dir",
                str(deploy_dir),
            ],
        )
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "shard-tennis" in out
        assert "matches tennis-shard.json" in out
        assert "shard-soccer-baseball" in out
        assert "NEW" in out
        assert "stale-cricket.json" in out
        assert "candidate to retire" in out
        assert "dropped (zero weight, out of season): american_football, ice_hockey" in out
        assert "gh workflow run strategy-node-release.yml" in out
        assert "-f container_name=betting-arbitrage-node-shard-soccer-baseball" in out
        assert "-f deploy_enabled=true" in out

    def test_dry_run_warns_on_over_capacity_bin(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        weights_path = tmp_path / "weights.json"
        weights_path.write_text(json.dumps({"tennis": 2600}))

        plan.main(
            [
                "dry-run",
                "--static-weights",
                str(weights_path),
                "--deploy-dir",
                str(self._deploy_dir(tmp_path)),
            ],
        )
        out = capsys.readouterr().out

        assert "OVER-CAPACITY" in out
        assert "league-level split" in out

    def test_emit_writes_and_validates_manifests(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        weights_path = tmp_path / "weights.json"
        weights_path.write_text(json.dumps(LIVE_WEIGHTS))
        out_dir = tmp_path / "out"

        exit_code = plan.main(
            [
                "emit",
                "--static-weights",
                str(weights_path),
                "--capacity",
                "2000",
                "--deploy-dir",
                str(self._deploy_dir(tmp_path)),
                "--out",
                str(out_dir),
            ],
        )
        out = capsys.readouterr().out

        assert exit_code == 0
        assert sorted(path.name for path in out_dir.glob("*.json")) == [
            "shard-basketball.json",
            "shard-soccer-baseball.json",
            "shard-tennis.json",
        ]
        assert "Emitted manifests (validated: load + build + lint)" in out
        assert "exec_clients=0" in out

    def test_no_weights_exits_with_error(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="No weights collected"):
            plan.main(["dry-run", "--nodes-root", str(tmp_path)])
