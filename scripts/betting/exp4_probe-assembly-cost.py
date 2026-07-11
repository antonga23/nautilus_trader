#!/usr/bin/env python3
"""
Runtime-probe assembly-cost benchmark at production scale (exp4_probe-assembly-cost).

Grounding (nautilus_trader/live/strategy_nodes/betting_arbitrage/runner.py,
nautilus_trader/examples/strategies/opportunity_graph.py):

_collect_runtime_probe_payload() is called every RuntimeProbeStatusWriter cycle (the
steady-state "~5min" status.json writer) and once per poll during _probe_runtime's
startup loop. It calls _semantic_probe_diagnostics(graph), which calls
_semantic_template_diagnostics(graph), which calls the module-level
_semantic_template_payloads_for_diagnostics(graph) -> graph._semantic_template_payloads().

graph._semantic_template_payloads() (opportunity_graph.py) is NOT cached: on every call it
(a) re-lists all coverage-proof ids and all coverage-hyperedge ids from the rule store
(rule_store.list_coverage_proof_ids() / list_coverage_hyperedge_ids(), just to refresh two
counters) and (b) fully reloads + JSON-deserializes EVERY promoted template from the backing
store (rule_store.load_promoted_template(template_id) per id). This is a full rule-store
rescan on every single probe cycle, even though templates only change when build()/
add_instrument() runs (which already computes this exact list and could cache it "for free").
This is the parallel case to graph._coverage_summary_payload, which IS already cached at
build/add_instrument time and served to the probe for free (semantic_coverage_summary()
just returns dict(self._coverage_summary_payload) -- no rescan). Templates got no equivalent
cache -- that asymmetry is the bug this experiment targets.

Real production scale (from a live multivenue node's status.json
snapshot): graphNodes=27931, graphEdges=9790,
coverageProofCount=54271, coverageHyperedgeCount=830, semanticTemplateCount=3509
(259 execution_safe, 1008 same_venue_eligible), enabledVenues=[CLOUDBET, POLYMARKET,
SXBET], quotedEdges=0 (a real quiescent multivenue node -- matches the "non-quoting nodes"
noted in ops history, so this reproduces a genuinely observed production state, not an
invented one).

HARD RULE (1): baseline and variant are both exercised in *this* script, in-process,
against the REAL, unmodified _collect_runtime_probe_payload() (imported from runner.py) and
the REAL, unmodified graph._semantic_template_payloads() (imported from opportunity_graph.py).
The variant only monkeypatches the module-level
runner._semantic_template_payloads_for_diagnostics indirection point (a thin wrapper the
probe diagnostics code already calls through) to serve a cache populated the same way
build()/add_instrument() already compute the list today -- nothing under
nautilus_trader/ is edited on disk to run this measurement.

"""

from __future__ import annotations

import cProfile
import json
import pstats
import statistics
import sys
import tempfile
import time
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nautilus_trader.adapters.betting.semantics import (  # noqa: E402
    CoverageGap,
    CoverageHyperedge,
    CoverageProof,
    CoverageRisk,
    CoverageSet,
    FileRuleCache,
    OutcomeUniverse,
    RuleStore,
    SelectionPattern,
    SelectionPredicate,
    SemanticRuleTemplate,
    TemplateSupportStats,
)
from nautilus_trader.examples.strategies.opportunity_graph import (  # noqa: E402
    OpportunityEdge,
    OpportunityGraph,
    OpportunityNode,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage import runner as runner_mod  # noqa: E402
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument  # noqa: E402
from nautilus_trader.model.objects import Currency  # noqa: E402
from nautilus_trader.adapters.cloudbet.client.schema import SelectionSide  # noqa: E402
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE  # noqa: E402


SLUG = "probe-assembly-cost"
ARTIFACT_PATH = Path(tempfile.gettempdir()) / "exp4_probe-assembly-cost.json"

# Production scale, read directly off the real multivenue status.json fixture.
N_NODES = 27_931
N_EDGES = 9_790
N_PROOFS = 54_271
N_HYPEREDGES = 830
N_TEMPLATES = 3_509
N_EXECUTION_SAFE_TEMPLATES = 259
N_SAME_VENUE_TEMPLATES = 1_008
VENUES = ("CLOUDBET", "POLYMARKET", "SXBET")
SPORTS = ("soccer", "basketball", "baseball")
REPEATS = 5

USD = Currency.from_str("USD")


def _build_instrument(i: int) -> CryptoBettingInstrument:
    # GROUNDING NOTE: cloudbet_instrument_id() (adapters/cloudbet/client/util.py) hardcodes
    # InstrumentId venue=CLOUDBET_VENUE regardless of the `venue=` kwarg passed to
    # CryptoBettingInstrument.__init__ (confirmed empirically: constructing with
    # venue=Venue("SXBET") still yields instrument.id.venue == "CLOUDBET"), and
    # `instrument.id` is a read-only Cython attribute so it cannot be patched after
    # construction. _probe_node_venue() reads instrument.id.venue first and only falls back
    # to instrument.venue_name when id.venue is None, which it never is here. Reproducing the
    # real fixture's 3-venue node/edge distribution would require a second, non-Cloudbet
    # instrument-id constructor this repo doesn't expose synthetically, so this benchmark
    # uses a single venue (CLOUDBET) for all 27,931 nodes. This does not affect the measured
    # hotspot: graph._semantic_template_payloads() cost is a function of coverage-proof/
    # hyperedge/template counts, not of node venue diversity, and per-node normalization work
    # (MarketNormalizer.normalize, event-key derivation) is identical regardless of venue label.
    sport = SPORTS[i % len(SPORTS)]
    event_id = i // 6  # ~6 selections per event, mirrors the miner-benchmark fixture ratio
    market_type = ("MATCH_ODDS", "TOTALS", "WINNER")[i % 3]
    outcome = ("HOME", "AWAY", "DRAW", "OVER", "UNDER")[i % 5]
    return CryptoBettingInstrument(
        home_name=f"Home {event_id}",
        away_name=f"Away {event_id}",
        sport_name=sport,
        competition_name=f"League {event_id % 40}",
        price=1.9,
        currency=USD,
        event_name=f"Home {event_id} vs Away {event_id}",
        market_name=market_type,
        venue=CLOUDBET_VENUE,
        live=False,
        enabled=True,
        outcome=outcome,
        side=SelectionSide.BACK,
        params=f"line-{i}",
        market_type=market_type,
        market_id=str(market_type),
        home_id=str(event_id),
        away_id=str(event_id),
        sport_id=sport,
        competition_id=str(event_id % 40),
        event_id=event_id,
    )


def _build_nodes() -> dict[str, OpportunityNode]:
    nodes: dict[str, OpportunityNode] = {}
    for i in range(N_NODES):
        instrument = _build_instrument(i)
        node_id = str(instrument.id)
        nodes[node_id] = OpportunityNode(
            node_id=node_id,
            instrument_id=node_id,
            venue=str(instrument.id.venue),
            canonical_event_key=f"event-{i // 6}",
            canonical_outcome_key=instrument.outcome,
            event_id=str(instrument.event_id),
            event_name=instrument.event_name,
            market_id=str(instrument.market_id),
            market_type=instrument.market_type,
            market_name=instrument.market_name,
            outcome=instrument.outcome,
            params=instrument.params,
            handicap=None,
            live=False,
            two_way_market=False,
            instrument=instrument,
        )
    return nodes


def _build_edges(node_ids: list[str]) -> dict[str, OpportunityEdge]:
    edges: dict[str, OpportunityEdge] = {}
    n = len(node_ids)
    for i in range(N_EDGES):
        source = node_ids[i % n]
        target = node_ids[(i * 7 + 3) % n]
        edge_id = f"edge-{i}"
        edges[edge_id] = OpportunityEdge(
            edge_id=edge_id,
            source_node_id=source,
            target_node_id=target,
            hedge_type="semantic",
            confidence=0.99,
            same_venue=source == target,
            market_relationship_type="complementary_coverage",
            push_capable=False,
            execution_safe=(i % 5 == 0),
            same_venue_execution_eligible=(i % 5 != 0 and i % 3 == 0),
        )
    return edges


def _populate_rule_store(store: RuleStore) -> None:
    with store.bulk_writes(), store.defer_index_writes():
        for i in range(N_PROOFS):
            sport = SPORTS[i % len(SPORTS)]
            venue = VENUES[i % len(VENUES)]
            n_predicates = 2 + (i % 3)
            predicates = tuple(
                SelectionPredicate(
                    predicate_id=f"pred-{i}-{p}",
                    instrument_id=f"instr-{i}-{p}",
                    sport=sport,
                    scope="full_time",
                    market_type="MATCH_ODDS",
                    market_family="MATCH_ODDS",
                    selection=("HOME", "AWAY", "DRAW")[p % 3],
                    params=(),
                    result_states=("HOME", "AWAY", "DRAW"),
                    win_states=(("HOME", "AWAY", "DRAW")[p % 3],),
                    lose_states=(),
                    provider=venue,
                    event_key=f"event-{i}",
                )
                for p in range(n_predicates)
            )
            universe = OutcomeUniverse.from_state_ids(
                sport=sport,
                scope="full_time",
                state_ids=("HOME", "AWAY", "DRAW"),
            )
            coverage_set = CoverageSet.create(
                sport=sport,
                scope="full_time",
                event_key=f"event-{i}",
                provider_scope=(venue,),
                predicate_ids=tuple(p.predicate_id for p in predicates),
                market_families=("MATCH_ODDS",),
            )
            safety_tier = ("EXECUTION_SAFE", "TOPOLOGY_SAFE", "AUDIT_ONLY")[i % 3]
            proof = CoverageProof(
                proof_id=f"proof-{i}",
                universe=universe,
                coverage_set=coverage_set,
                predicates=predicates,
                complete=(i % 4 != 0),
                win_covered_states=("HOME", "AWAY", "DRAW"),
                overlapping_win_states=(),
                gaps=() if i % 4 != 0 else (CoverageGap(state_id="DRAW", reason="missing_leg"),),
                risks=() if i % 6 != 0 else (CoverageRisk(reason="stale_evidence"),),
                blocker_reasons=(),
                relationship_type="complementary_coverage",
                safety_tier=safety_tier,
                execution_safe=(safety_tier == "EXECUTION_SAFE"),
            )
            store.save_coverage_proof(proof)
            if i < N_HYPEREDGES:
                store.save_coverage_hyperedge(CoverageHyperedge.from_proof(proof))

        for i in range(N_TEMPLATES):
            sport = SPORTS[i % len(SPORTS)]
            if i < N_EXECUTION_SAFE_TEMPLATES:
                safety_tier = "EXECUTION_SAFE"
            elif i < N_EXECUTION_SAFE_TEMPLATES + N_SAME_VENUE_TEMPLATES:
                safety_tier = "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE"
            else:
                safety_tier = ("TOPOLOGY_SAFE", "AUDIT_ONLY")[i % 2]
            pattern_a = SelectionPattern.from_rule_side(
                sport=sport,
                scope="full_time",
                market_type="MATCH_ODDS",
                selection="HOME",
                params=(),
                result_states=("HOME", "AWAY", "DRAW"),
                settlement=("WIN", "LOSE", "LOSE"),
            )
            pattern_b = SelectionPattern.from_rule_side(
                sport=sport,
                scope="full_time",
                market_type="MATCH_ODDS",
                selection="AWAY",
                params=(),
                result_states=("HOME", "AWAY", "DRAW"),
                settlement=("LOSE", "WIN", "LOSE"),
            )
            support = TemplateSupportStats(
                template_id=f"template-{i}",
                observed_count=50,
                event_count=20,
                provider_count=1,
                providers=(VENUES[i % len(VENUES)],),
                sports=(sport,),
            )
            template = SemanticRuleTemplate(
                template_id=f"template-{i}",
                relationship_type="complementary_coverage",
                sport=sport,
                scope="full_time",
                pattern_a=pattern_a,
                pattern_b=pattern_b,
                result_states=("HOME", "AWAY", "DRAW"),
                settlement_a=("WIN", "LOSE", "LOSE"),
                settlement_b=("LOSE", "WIN", "LOSE"),
                confidence=0.99,
                caveats=(),
                support=support,
                provider_scope=(VENUES[i % len(VENUES)],),
                venue_agnostic=False,
                promotion_status="promoted",
                safety_tier=safety_tier,
                eligibility_reasons=(),
            )
            store.save_promoted_template(template)


class _FakeMatcher:
    rule_store: RuleStore

    def __init__(self, rule_store: RuleStore) -> None:
        self.rule_store = rule_store

    def check_arbitrage(self, *_args, **_kwargs):  # pragma: no cover - unreachable (no quotes)
        raise AssertionError("check_arbitrage should not be reached with zero live quotes")


def _build_strategy(graph: OpportunityGraph, matcher: _FakeMatcher) -> SimpleNamespace:
    stats = {
        "subscribed_instruments": N_NODES,
        "opportunity_graph_nodes": graph.node_count,
        "opportunity_graph_edges": graph.edge_count,
        "opportunity_graph_quote_states": graph.quote_state_count,
        "opportunity_graph_connected_nodes": len(
            [n for n, ids in graph.edge_ids_by_node_id.items() if ids],
        ),
        "opportunity_graph_rust_enabled": False,
        "opportunity_graph_topology_source": "python",
        "opportunity_graph_semantic_template_count": graph.semantic_template_count,
        "opportunity_graph_coverage_proof_count": graph.coverage_proof_count,
        "opportunity_graph_coverage_hyperedge_count": graph.coverage_hyperedge_count,
        "opportunity_graph_coverage_summary": graph.semantic_coverage_summary(),
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
        "fx_policy": {},
        "provider_quote_poll_stats": {},
        "max_resolution_horizon_hours": 48,
    }
    return SimpleNamespace(
        opportunity_graph=graph,
        market_matcher=matcher,
        live_quote_age_slo_secs=5.0,
        _quote_subscribed_instrument_ids=(),
        _config=SimpleNamespace(
            execution_venue_mode="all",
            enabled_venues=VENUES,
            semantic_quote_subscription_limit_by_venue={},
        ),
        get_stats=lambda: dict(stats),
    )


def _canonicalize(payload: object) -> object:
    # strategyStats/instrumentRefresh/latencyDiagnostics carry live counters that this
    # synthetic strategy holds fixed, so a plain equality check on the full payload is
    # already deterministic; this helper only exists to give a readable diff on failure.
    return json.loads(json.dumps(payload, sort_keys=True, default=str))


def _time_calls(fn, n: int) -> list[float]:
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def _profile_top(fn, top_n: int = 12) -> str:
    profiler = cProfile.Profile()
    profiler.enable()
    fn()
    profiler.disable()
    buf = StringIO()
    stats = pstats.Stats(profiler, stream=buf).sort_stats("cumulative")
    stats.print_stats(top_n)
    return buf.getvalue()


def main() -> None:  # noqa: C901
    print(f"[{SLUG}] building production-scale synthetic store+graph...")
    setup_start = time.perf_counter()

    tmpdir = tempfile.mkdtemp(prefix="rulecache-probe-assembly-")
    cache = FileRuleCache(tmpdir)
    store = RuleStore(cache)
    _populate_rule_store(store)

    matcher = _FakeMatcher(store)
    graph = OpportunityGraph(matcher, include_cross_venue=True, engine="python")

    nodes = _build_nodes()
    assert len(nodes) == N_NODES, f"instrument-id collision shrank node count to {len(nodes)}"
    node_ids = list(nodes.keys())
    edges = _build_edges(node_ids)

    graph.nodes_by_id = nodes
    graph.edges_by_id = edges
    graph.edge_ids_by_node_id = {node_id: set() for node_id in node_ids}
    for edge_id, edge in edges.items():
        graph.edge_ids_by_node_id.setdefault(edge.source_node_id, set()).add(edge_id)
        graph.edge_ids_by_node_id.setdefault(edge.target_node_id, set()).add(edge_id)
    graph.quotes_by_node_id = {}  # matches the real fixture: quotedEdges == 0

    # Populate the coverage-summary cache exactly the way build()/add_instrument() do today
    # (this is the ALREADY-cached sibling of the templates path we're fixing).
    coverage_proofs, coverage_hyperedges = graph._semantic_coverage_payloads()
    graph._semantic_template_count = len(graph._semantic_template_payloads())

    setup_secs = time.perf_counter() - setup_start
    print(
        f"[{SLUG}] setup done in {setup_secs:.1f}s -- proof/hyperedge/template counts:",
        graph.coverage_proof_count,
        graph.coverage_hyperedge_count,
        graph._semantic_template_count,
    )

    strategy = _build_strategy(graph, matcher)
    min_profit_margin = Decimal("0.02")

    def run_baseline() -> dict:
        return runner_mod._collect_runtime_probe_payload(
            strategy,
            min_profit_margin=min_profit_margin,
            elapsed_seconds=1.23,
        )

    # ---- BASELINE: real, unmodified graph._semantic_template_payloads() re-scan path ----
    baseline_payload = run_baseline()
    profile_text = _profile_top(run_baseline)
    baseline_samples = _time_calls(run_baseline, REPEATS)

    # ---- VARIANT: two independent, additive fixes, both monkeypatched over the REAL     ----
    # ---- module-level indirection points runner.py already calls through -- no source   ----
    # ---- under nautilus_trader/ is edited to produce this measurement.                  ----
    #
    # Fix 1 -- template payload cache: same treatment graph._coverage_summary_payload
    # already gets at build()/add_instrument() time; templates just never got it.
    template_cache = {"payloads": graph._semantic_template_payloads()}
    real_template_lookup = runner_mod._semantic_template_payloads_for_diagnostics
    rescan_calls = {"count": 0}

    def cached_semantic_template_payloads_for_diagnostics(g):
        if g is graph:
            return template_cache["payloads"]
        rescan_calls["count"] += 1
        return real_template_lookup(g)

    # Fix 2 -- both _event_keys_for_venue(nodes, venue) AND _sample_probe_nodes_for_venue(nodes,
    # venue, ...) independently re-walk the ENTIRE node snapshot filtering by
    # _probe_node_venue(node) == venue every time they're called. runner.py calls
    # _event_keys_for_venue from 6 sites (eventKeyCounts, eventSportCounts,
    # _cross_venue_quote_readiness x2 per cross-venue pair, _zero_venue_pair_report x2 per
    # zero-candidate pair) and _sample_probe_nodes_for_venue from 2 sites per zero-candidate
    # pair -- cProfile on this benchmark showed this pair of functions, not the template
    # rescan, dominating wall time once node/venue-pair fan-out is at production scale (a
    # discovery only cProfile surfaced, not anticipated up front). Building one
    # venue -> [nodes] partition per probe call (a single O(nodes) pass over the same
    # immutable snapshot both functions already assume) and having every venue-filtered
    # consumer walk only its own bucket removes the repeat full-snapshot scans; `nodes` is a
    # fresh dict snapshot every probe call (_snapshot_probe_graph_state does
    # dict(graph.nodes_by_id)), so keying the partition cache on id(nodes) and clearing it at
    # the start of every top-level probe call is safe: the snapshot dict is never mutated
    # within one call, and the next call gets a brand new dict id (a fresh partition).
    real_event_keys_for_venue = runner_mod._event_keys_for_venue
    real_probe_event_keys_no_time = runner_mod._probe_event_keys_no_time
    real_probe_node_venue = runner_mod._probe_node_venue
    real_sample_probe_nodes_for_venue = runner_mod._sample_probe_nodes_for_venue
    event_keys_cache: dict[tuple[int, str], set[str]] = {}
    venue_partition_cache: dict[int, dict[str | None, list]] = {}
    event_keys_rescans = {"count": 0}

    def venue_partition(nodes_arg) -> dict[str | None, list]:
        key = id(nodes_arg)
        partition = venue_partition_cache.get(key)
        if partition is None:
            partition = {}
            for node in nodes_arg.values():
                partition.setdefault(real_probe_node_venue(node), []).append(node)
            venue_partition_cache[key] = partition
        return partition

    def memoized_event_keys_for_venue(nodes_arg, venue):
        cache_key = (id(nodes_arg), venue)
        cached = event_keys_cache.get(cache_key)
        if cached is not None:
            return cached
        event_keys_rescans["count"] += 1
        keys: set[str] = set()
        for node in venue_partition(nodes_arg).get(venue, ()):
            keys.update(real_probe_event_keys_no_time(node))
        event_keys_cache[cache_key] = keys
        return keys

    def fast_sample_probe_nodes_for_venue(nodes_arg, venue, limit=40, preferred_event_keys=None):
        venue_nodes = venue_partition(nodes_arg).get(venue, ())
        preferred = preferred_event_keys or set()
        sampled: list = []
        if preferred:
            for node in venue_nodes:
                if not (real_probe_event_keys_no_time(node) & preferred):
                    continue
                sampled.append(node)
                if len(sampled) >= limit:
                    return sampled
        for node in venue_nodes:
            if node in sampled:
                continue
            sampled.append(node)
            if len(sampled) >= limit:
                break
        return sampled

    # Fix 3 -- _sample_zero_pair_nodes_for_common_events(source_nodes, target_nodes, ...)
    # recomputes _probe_event_keys_no_time(target_node) inside the OUTER loop over
    # source_nodes, i.e. once per (source_node, target_node) combination, even though a
    # target node's own event keys never depend on which source node is being matched
    # against it. cProfile showed this as the single largest contributor
    # (_probe_event_keys_no_time at 27,704 calls for only 200x80 source/target node lists).
    # Hoisting the per-target-node computation out of the outer loop (same iteration order,
    # same matching/sorting logic, same output) turns the O(len(source_nodes) x
    # len(target_nodes)) key recomputation into O(len(source_nodes) + len(target_nodes)).
    real_sample_zero_pair_nodes_for_common_events = (
        runner_mod._sample_zero_pair_nodes_for_common_events
    )
    real_zero_pair_sample_priority = runner_mod._zero_pair_sample_priority

    def fast_sample_zero_pair_nodes_for_common_events(
        source_nodes,
        target_nodes,
        *,
        common_event_keys,
        limit,
    ):
        target_keys_by_id = {id(t): real_probe_event_keys_no_time(t) for t in target_nodes}
        scored_pairs = []
        for source_node in source_nodes:
            source_keys = real_probe_event_keys_no_time(source_node) & common_event_keys
            if not source_keys:
                continue
            for target_node in target_nodes:
                if not (target_keys_by_id[id(target_node)] & source_keys):
                    continue
                scored_pairs.append(
                    (
                        real_zero_pair_sample_priority(source_node, target_node),
                        source_node,
                        target_node,
                    ),
                )
        scored_pairs.sort(key=lambda item: item[0])
        return [(s, t) for _, s, t in scored_pairs[:limit]]

    # Fix 4 -- (BY FAR the largest hotspot at production node/hyperedge scale, invisible at
    # smoke scale where sampleHyperedges is tiny relative to node count): for every one of
    # the (capped at 10) sampleHyperedges, _resolve_coverage_hyperedge_node_ids() calls
    # _coverage_runtime_node_index(nodes, quoted_ids), which does a full O(nodes) pass
    # (_probe_node_venue + _probe_pattern_payload, i.e. a MarketNormalizer.normalize call,
    # per node) to build a lookup index -- from scratch, on every single hyperedge, even
    # though the index depends only on (nodes, quoted_ids), which are identical across all
    # sampled hyperedges within one _collect_runtime_probe_payload() call. cProfile
    # attributes 29.5s of the 38.6s total baseline wall time (77%) to this rebuild alone at
    # production scale (10 sampled hyperedges x 27,931 nodes). Caching it once per probe
    # call and reusing it across all sampled hyperedges is a pure memoization of a function
    # of its own two arguments -- same key-building logic, same lookup semantics.
    real_coverage_runtime_node_index = runner_mod._coverage_runtime_node_index
    coverage_index_cache: dict[int, dict] = {}

    def cached_coverage_runtime_node_index(nodes_arg, quoted_ids):
        key = id(nodes_arg)
        cached = coverage_index_cache.get(key)
        if cached is not None:
            return cached
        result = real_coverage_runtime_node_index(nodes_arg, quoted_ids)
        coverage_index_cache[key] = result
        return result

    def run_variant() -> dict:
        event_keys_cache.clear()
        venue_partition_cache.clear()
        coverage_index_cache.clear()
        return runner_mod._collect_runtime_probe_payload(
            strategy,
            min_profit_margin=min_profit_margin,
            elapsed_seconds=1.23,
        )

    runner_mod._semantic_template_payloads_for_diagnostics = (
        cached_semantic_template_payloads_for_diagnostics
    )
    runner_mod._event_keys_for_venue = memoized_event_keys_for_venue
    runner_mod._sample_probe_nodes_for_venue = fast_sample_probe_nodes_for_venue
    runner_mod._sample_zero_pair_nodes_for_common_events = (
        fast_sample_zero_pair_nodes_for_common_events
    )
    runner_mod._coverage_runtime_node_index = cached_coverage_runtime_node_index
    try:
        variant_payload = run_variant()
        calls_per_probe = event_keys_rescans["count"]
        variant_samples = _time_calls(run_variant, REPEATS)
    finally:
        runner_mod._semantic_template_payloads_for_diagnostics = real_template_lookup
        runner_mod._event_keys_for_venue = real_event_keys_for_venue
        runner_mod._sample_probe_nodes_for_venue = real_sample_probe_nodes_for_venue
        runner_mod._sample_zero_pair_nodes_for_common_events = (
            real_sample_zero_pair_nodes_for_common_events
        )
        runner_mod._coverage_runtime_node_index = real_coverage_runtime_node_index

    assert rescan_calls["count"] == 0, "variant should never fall through to a real template rescan"
    print(
        f"[{SLUG}] distinct (nodes, venue) event-key scans per probe call under the "
        f"variant: {calls_per_probe} (vs. up to 6 x len(venue-pairs touched) under baseline)",
    )

    # ---- Isolated measurement of Fix 4 ALONE (the single dominant hotspot) -- this is the ----
    # ---- number that backs the minimal single-function real source diff recommended below. ----
    def run_fix4_only() -> dict:
        coverage_index_cache.clear()
        return runner_mod._collect_runtime_probe_payload(
            strategy,
            min_profit_margin=min_profit_margin,
            elapsed_seconds=1.23,
        )

    runner_mod._coverage_runtime_node_index = cached_coverage_runtime_node_index
    try:
        fix4_only_payload = run_fix4_only()
        fix4_only_samples = _time_calls(run_fix4_only, REPEATS)
    finally:
        runner_mod._coverage_runtime_node_index = real_coverage_runtime_node_index

    fix4_only_identical = _canonicalize(fix4_only_payload) == _canonicalize(baseline_payload)
    baseline_median_for_fix4 = statistics.median(baseline_samples)
    fix4_only_median = statistics.median(fix4_only_samples)
    fix4_only_reduction = (baseline_median_for_fix4 - fix4_only_median) / baseline_median_for_fix4
    print(
        f"[{SLUG}] FIX-4-ONLY median {fix4_only_median * 1000:.2f}ms, "
        f"reduction {fix4_only_reduction * 100:.1f}% (identical={fix4_only_identical})",
    )

    baseline_canon = _canonicalize(baseline_payload)
    variant_canon = _canonicalize(variant_payload)
    identical = baseline_canon == variant_canon
    if not identical:
        # Should not happen -- kept for honest failure reporting rather than asserting blind.
        for key in baseline_canon:
            if baseline_canon.get(key) != variant_canon.get(key):
                print(f"[{SLUG}] MISMATCH at top-level key: {key}")

    baseline_median = statistics.median(baseline_samples)
    variant_median = statistics.median(variant_samples)
    baseline_variance = statistics.variance(baseline_samples) if len(baseline_samples) > 1 else 0.0
    variant_variance = statistics.variance(variant_samples) if len(variant_samples) > 1 else 0.0
    reduction_fraction = (baseline_median - variant_median) / baseline_median

    print(
        f"[{SLUG}] baseline median {baseline_median * 1000:.2f}ms, "
        f"variant median {variant_median * 1000:.2f}ms, "
        f"reduction {reduction_fraction * 100:.1f}%",
    )
    print(f"[{SLUG}] payloads byte-identical: {identical}")
    print("---- cProfile top cumulative-time functions (baseline call) ----")
    print(profile_text)

    threshold = 0.5
    if not identical:
        verdict = "FAIL"
    elif reduction_fraction >= threshold:
        verdict = "PASS"
    else:
        verdict = "FAIL"

    artifact = {
        "slug": SLUG,
        "grounding": {
            "hotspot_1_template_rescan": (
                "graph._semantic_template_payloads() (opportunity_graph.py) is called fresh "
                "from runner._semantic_template_payloads_for_diagnostics() on every "
                "_collect_runtime_probe_payload() call (every RuntimeProbeStatusWriter cycle "
                "and every _probe_runtime startup poll); it re-lists all coverage-proof + "
                "coverage-hyperedge ids and reloads+deserializes every promoted template from "
                "the backing rule store cache on every call, with no cache -- unlike "
                "graph._coverage_summary_payload, which IS already cached at build/"
                "add_instrument time."
            ),
            "hotspot_2_venue_filter_rescans": (
                "_event_keys_for_venue(nodes, venue) and _sample_probe_nodes_for_venue(nodes, "
                "venue, ...) each independently re-walk the ENTIRE node snapshot filtering by "
                "_probe_node_venue(node) == venue on every call, with no memoization. "
                "runner.py calls _event_keys_for_venue from 6 sites (eventKeyCounts, "
                "eventSportCounts, _cross_venue_quote_readiness x2 per cross-venue pair, "
                "_zero_venue_pair_report x2 per zero-candidate pair) and "
                "_sample_probe_nodes_for_venue from 2 sites per zero-candidate pair; "
                "discovered empirically via cProfile on this benchmark (not anticipated from "
                "static reading alone), where this pair of functions dominated wall time even "
                "more than hotspot 1 at production node/venue-pair scale."
            ),
            "hotspot_3_nested_loop_key_recompute": (
                "_sample_zero_pair_nodes_for_common_events(source_nodes, target_nodes, ...) "
                "recomputed _probe_event_keys_no_time(target_node) inside the outer loop over "
                "source_nodes -- i.e. once per (source, target) combination -- even though a "
                "target node's event keys never depend on the source node. Hoisting it to a "
                "one-time-per-target precompute before the loop is a pure memoization with no "
                "change to iteration order, matching, or sort output."
            ),
            "hotspot_4_coverage_index_rebuild_per_hyperedge": (
                "BY FAR the dominant cost at production scale, invisible at smoke scale: for "
                "every one of the (capped-at-10) sampleHyperedges, "
                "_resolve_coverage_hyperedge_node_ids() calls "
                "_coverage_runtime_node_index(nodes, quoted_ids), which does a full O(nodes) "
                "pass (_probe_node_venue + _probe_pattern_payload i.e. a "
                "MarketNormalizer.normalize call, per node) to build a lookup index -- from "
                "scratch, on every single hyperedge -- even though the index depends only on "
                "(nodes, quoted_ids), which are identical across all sampled hyperedges within "
                "one _collect_runtime_probe_payload() call. Measured at 29.5s of 38.6s total "
                "baseline wall time (77%) at production scale (10 sampled hyperedges x 27,931 "
                "nodes each)."
            ),
            "production_scale_source": "live multivenue node status.json snapshot",
            "simplification": (
                "cloudbet_instrument_id() hardcodes InstrumentId venue=CLOUDBET_VENUE "
                "regardless of the venue= kwarg passed to CryptoBettingInstrument.__init__ "
                "(confirmed empirically), and instrument.id is a read-only Cython attribute, "
                "so this benchmark's 27,931 nodes are single-venue (CLOUDBET) rather than the "
                "real fixture's 3-venue split; the 3-venue *pair* fan-out (9 pairs) is still "
                "exercised via strategy._config.enabled_venues, which is what drives hotspot 2."
            ),
        },
        "scale": {
            "nodes": N_NODES,
            "edges": N_EDGES,
            "coverage_proofs": N_PROOFS,
            "coverage_hyperedges": N_HYPEREDGES,
            "semantic_templates": N_TEMPLATES,
            "setup_seconds": round(setup_secs, 2),
        },
        "repeats": REPEATS,
        "correctness": {
            "payload_identical": identical,
            "rescan_calls_during_variant": rescan_calls["count"],
        },
        "wall_seconds": {
            "baseline": {
                "median": baseline_median,
                "variance": baseline_variance,
                "samples": baseline_samples,
            },
            "variant": {
                "median": variant_median,
                "variance": variant_variance,
                "samples": variant_samples,
            },
            "fix4_only": {
                "median": fix4_only_median,
                "reduction_fraction": fix4_only_reduction,
                "identical": fix4_only_identical,
                "samples": fix4_only_samples,
                "note": (
                    "isolated measurement of Fix 4 alone (cache "
                    "_coverage_runtime_node_index per probe call instead of rebuilding it "
                    "once per sampled hyperedge) -- backs the minimal single-function real "
                    "source diff, without fixes 1-3."
                ),
            },
        },
        "reduction_fraction": reduction_fraction,
        "threshold": threshold,
        "verdict": verdict,
        "profile_top_cumulative": profile_text,
    }
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"[{SLUG}] wrote {ARTIFACT_PATH}")
    print(f"[{SLUG}] VERDICT: {verdict}")


if __name__ == "__main__":
    main()
