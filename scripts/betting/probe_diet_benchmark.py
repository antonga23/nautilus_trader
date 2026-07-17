#!/usr/bin/env python3
"""
Benchmark the runtime-probe every-cycle cost before and after the probe diet.

Before the diet, every RuntimeProbeStatusWriter heartbeat (5s target) copied the
whole opportunity graph (``_snapshot_probe_graph_state``) and ran the O(edges)/
O(nodes) passes over it (``_probe_edge_profitability``, ``_venue_pair_coverage``,
``_resolution_horizon_payload``). On a live 25,328-node / 20,624-edge multivenue
node one cycle took ~145s, so status.json refreshed every ~2.5min and the GIL
contention starved the asyncio venue quote pollers. After the diet those passes
run once per ``semantic_diagnostics_interval_secs`` (default 90s) via
``_collect_probe_heavy_sections`` and are carried forward in between, while the
every-cycle path reads only O(1) strategy stats.

This script builds a synthetic graph-shaped fixture at that scale (plain objects
matching the shapes the probe reads; no venue APIs) and measures:

- ``old_every_cycle``: the pass sequence the pre-diet code ran every heartbeat
  (semantic/coverage-devig diagnostics excluded -- those were already throttled)
- ``new_every_cycle``: ``_collect_runtime_probe_payload`` with a primed throttle
- ``heavy_refresh``: the throttled ``_collect_probe_heavy_sections`` pass

Run: .venv/bin/python scripts/betting/probe_diet_benchmark.py

"""

from __future__ import annotations

import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nautilus_trader.adapters.cloudbet.client.schema import SelectionSide  # noqa: E402
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE  # noqa: E402
from nautilus_trader.live.strategy_nodes.betting_arbitrage import runner as runner_mod  # noqa: E402
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument  # noqa: E402
from nautilus_trader.model.objects import Currency  # noqa: E402


# Live multivenue node scale (status.json, 2026-07): 25,328 nodes / 20,624 edges.
N_NODES = 25_328
N_EDGES = 20_624
QUOTED_NODE_FRACTION = 0.2
REPEATS = 5
MIN_PROFIT_MARGIN = Decimal("0.02")
HEARTBEAT_TARGET_SECS = 5.0

USD = Currency.from_str("USD")
SPORTS = ("soccer", "basketball", "baseball")


def _build_instrument(i: int) -> CryptoBettingInstrument:
    event_id = i // 6
    market_type = ("MATCH_ODDS", "TOTALS", "WINNER")[i % 3]
    return CryptoBettingInstrument(
        venue=CLOUDBET_VENUE,
        event_id=event_id,
        event_name=f"Home {event_id} vs Away {event_id}",
        home_name=f"Home {event_id}",
        away_name=f"Away {event_id}",
        sport_name=SPORTS[i % len(SPORTS)],
        competition_name=f"League {event_id % 40}",
        market_name=market_type,
        market_type=market_type,
        market_id=str(market_type),
        home_id=str(event_id),
        away_id=str(event_id),
        sport_id=SPORTS[i % len(SPORTS)],
        competition_id=str(event_id % 40),
        outcome=("HOME", "AWAY", "DRAW", "OVER", "UNDER")[i % 5],
        side=SelectionSide.BACK,
        price=1.9,
        currency=USD,
        params=f"line-{i}",
        live=False,
        enabled=True,
    )


def _build_graph() -> SimpleNamespace:
    nodes: dict[str, SimpleNamespace] = {}
    for i in range(N_NODES):
        instrument = _build_instrument(i)
        node_id = f"node-{i}"
        nodes[node_id] = SimpleNamespace(
            node_id=node_id,
            instrument=instrument,
            venue=str(instrument.id.venue),
            canonical_event_key=f"event-{i // 6}",
            market_name=instrument.market_name,
            market_type=instrument.market_type,
            outcome=instrument.outcome,
        )
    node_ids = list(nodes)
    edges: dict[str, SimpleNamespace] = {}
    edge_ids_by_node_id: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for i in range(N_EDGES):
        source = node_ids[i % N_NODES]
        target = node_ids[(i * 7 + 3) % N_NODES]
        edge_id = f"edge-{i}"
        edges[edge_id] = SimpleNamespace(
            edge_id=edge_id,
            source_node_id=source,
            target_node_id=target,
            rule_id=f"rule-{i % 50}",
            template_id=f"template-{i % 50}",
            relationship_type="COMPLEMENTARY_COVERAGE",
            safety_tier="EXECUTION_SAFE",
            execution_safe=(i % 5 == 0),
            same_venue_execution_eligible=(i % 5 != 0 and i % 3 == 0),
        )
        edge_ids_by_node_id[source].add(edge_id)
        edge_ids_by_node_id[target].add(edge_id)
    quoted = int(N_NODES * QUOTED_NODE_FRACTION)
    quotes = {
        node_ids[i]: SimpleNamespace(
            odds=Decimal("2.10"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000),
        )
        for i in range(quoted)
    }
    return SimpleNamespace(
        nodes_by_id=nodes,
        edges_by_id=edges,
        quotes_by_node_id=quotes,
        edge_ids_by_node_id=edge_ids_by_node_id,
    )


def _build_strategy(graph: SimpleNamespace) -> SimpleNamespace:
    stats = {
        "subscribed_instruments": N_NODES,
        "opportunity_graph_nodes": len(graph.nodes_by_id),
        "opportunity_graph_edges": len(graph.edges_by_id),
        "opportunity_graph_quote_states": len(graph.quotes_by_node_id),
        "opportunity_graph_connected_nodes": sum(
            1 for ids in graph.edge_ids_by_node_id.values() if ids
        ),
        "opportunity_graph_rust_enabled": False,
        "opportunity_graph_topology_source": "python",
        "opportunity_graph_semantic_template_count": 0,
        "opportunity_graph_coverage_proof_count": 0,
        "opportunity_graph_coverage_hyperedge_count": 0,
        "opportunity_graph_coverage_summary": {},
        "venue_taker_fee_rates": {},
        "venue_maker_rebate_rates": {},
        "venue_winning_profit_fee_rates": {},
        "venue_basket_rebate_rates": {},
        "venue_basket_boost_rates": {},
        "devig_enabled": False,
        "devig_method": "auto",
        "devig_reference_venues": [],
        "value_diagnostics_enabled": False,
        "value_execution_enabled": False,
        "min_value_edge": "0",
        "live_execution": {},
        "execution_approvals": {},
        "fx_policy": {},
        "provider_quote_poll_stats": {},
        "max_resolution_horizon_hours": 48,
    }
    freshness = SimpleNamespace(
        profile="pre_match",
        max_quote_age_secs=30.0,
        max_pair_skew_secs=5.0,
        max_fetch_latency_secs=10.0,
    )
    return SimpleNamespace(
        opportunity_graph=graph,
        market_matcher=SimpleNamespace(check_arbitrage=lambda *_a, **_k: None),
        live_quote_age_slo_secs=5.0,
        _quote_subscribed_instrument_ids=frozenset(),
        _config=SimpleNamespace(
            execution_venue_mode="all",
            enabled_venues=frozenset({"CLOUDBET"}),
            semantic_quote_subscription_limit_by_venue={},
        ),
        get_stats=lambda: dict(stats),
        matcher_suspect_reason=lambda _a, _b: (False, "none"),
        semantic_fixture_suspect_reason=lambda _a, _b: (False, "none"),
        quote_age_secs=lambda _observed_ns, _quote: 0.1,
        _quote_pair_skew_secs=lambda _quote_a, _quote_b: 0.0,
        quote_fetch_latency_secs=lambda _quote: 0.1,
        quote_available_size=lambda _quote: Decimal(100),
        quote_freshness_thresholds=lambda _instrument_a, _instrument_b: freshness,
        fee_adjusted_opportunity=lambda opportunity: opportunity,
    )


def _measure(fn, repeats: int) -> list[float]:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def main() -> int:
    print(f"building synthetic fixture: {N_NODES} nodes / {N_EDGES} edges...")
    graph = _build_graph()
    strategy = _build_strategy(graph)
    stats = strategy.get_stats()

    def run_old_every_cycle() -> None:
        # The pass sequence the pre-diet _collect_runtime_probe_payload ran on
        # EVERY heartbeat (semantic/coverage-devig excluded: already throttled).
        snapshot = runner_mod._snapshot_probe_graph_state(graph)
        assert snapshot is not None
        sum(1 for edge in snapshot["edges"] if edge.execution_safe)
        sum(1 for edge in snapshot["edges"] if edge.same_venue_execution_eligible)
        profitability = runner_mod._probe_edge_profitability(
            strategy,
            edges=snapshot["edges"],
            nodes=snapshot["nodes"],
            quotes=snapshot["quotes"],
            min_profit_margin=MIN_PROFIT_MARGIN,
        )
        venue_coverage = runner_mod._venue_pair_coverage(
            strategy,
            edges=snapshot["edges"],
            nodes=snapshot["nodes"],
            quotes=snapshot["quotes"],
            matched_node_ids=snapshot["matched_node_ids"],
            candidate_venue_pairs=profitability["venue_pairs"],
        )
        runner_mod._resolution_horizon_payload(
            stats,
            nodes=snapshot["nodes"],
            quotes=snapshot["quotes"],
            edges=snapshot["edges"],
        )
        runner_mod._runtime_latency_diagnostics(stats, profitability)
        runner_mod._probe_quote_observation_state(stats, venue_coverage)

    def run_heavy_refresh() -> None:
        runner_mod._collect_probe_heavy_sections(
            strategy,
            stats=stats,
            min_profit_margin=MIN_PROFIT_MARGIN,
        )

    throttle = runner_mod._RuntimeProbeDiagnosticsThrottle(10**9)

    def run_new_every_cycle() -> None:
        runner_mod._collect_runtime_probe_payload(
            strategy,
            min_profit_margin=MIN_PROFIT_MARGIN,
            elapsed_seconds=1.0,
            diagnostics=throttle,
        )

    run_new_every_cycle()  # prime the throttle so steady-state cycles are measured
    old_samples = _measure(run_old_every_cycle, REPEATS)
    heavy_samples = _measure(run_heavy_refresh, REPEATS)
    new_samples = _measure(run_new_every_cycle, REPEATS)

    old_median = statistics.median(old_samples)
    heavy_median = statistics.median(heavy_samples)
    new_median = statistics.median(new_samples)
    speedup = old_median / new_median if new_median > 0 else float("inf")

    result = {
        "fixture": {
            "nodes": N_NODES,
            "edges": N_EDGES,
            "quotedNodes": len(graph.quotes_by_node_id),
        },
        "repeats": REPEATS,
        "medianSeconds": {
            "oldEveryCycle": old_median,
            "newEveryCycle": new_median,
            "heavyRefresh": heavy_median,
        },
        "samplesSeconds": {
            "oldEveryCycle": old_samples,
            "newEveryCycle": new_samples,
            "heavyRefresh": heavy_samples,
        },
        "everyCycleSpeedup": speedup,
        "newEveryCycleWithinHeartbeat": new_median < HEARTBEAT_TARGET_SECS,
        "verdict": "PASS" if speedup >= 10.0 else "FAIL",
    }
    print(json.dumps(result, indent=2))
    print()
    print(f"old every-cycle: {old_median * 1000:.1f} ms")
    print(f"new every-cycle: {new_median * 1000:.3f} ms")
    print(
        f"heavy refresh (once per semantic_diagnostics_interval_secs): {heavy_median * 1000:.1f} ms",
    )
    print(f"every-cycle speedup: {speedup:.0f}x -> {result['verdict']}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
