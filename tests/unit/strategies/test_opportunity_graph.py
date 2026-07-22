# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for opportunity graph engines.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0116, PYL-R0913, PYL-W0212
# pylint: disable=duplicate-code,missing-function-docstring,no-name-in-module,too-many-arguments,protected-access
"""
Parity and fast-path tests for the opportunity graph engines.
"""

from decimal import Decimal
import json
from pathlib import Path
import random
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.semantics import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics import CoverageEngine
from nautilus_trader.adapters.betting.semantics import CoverageHyperedge
from nautilus_trader.adapters.betting.semantics import CoverageProof
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import NormalizedSelection
from nautilus_trader.adapters.betting.semantics import PromotionStatus
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SelectionPredicateBuilder
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.data import TestDataStubs


_CURRENCY = Currency.from_str("USDT")


def ensure(condition: bool) -> None:  # skipcq
    """
    Raise an assertion error when a boolean expectation is not met.
    """
    if not condition:
        raise AssertionError


def _instrument(
    *,
    venue: str = "SXBET",
    event_id: str = "event-1",
    event_name: str = "Team A vs Team B",
    home_name: str = "Team A",
    away_name: str = "Team B",
    sport_name: str = "Soccer",
    market_name: str = "Total Goals",
    market_type: str = "total_goals",
    outcome: str = "over",
    params: str = "line=2.5",
    price: float = 2.4,
    start_time: str | None = "2026-03-13T18:00:00Z",
    handicap: float | None = None,
    info: dict | None = None,
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id=event_id,
        event_name=event_name,
        home_name=home_name,
        away_name=away_name,
        sport_name=sport_name,
        competition_name="Test League",
        market_name=market_name,
        market_type=market_type,
        outcome=outcome,
        side=SelectionSide.BACK,
        price=price,
        currency=_CURRENCY,
        params=params,
        start_time=start_time,
        handicap=handicap,
        info=info,
    )


def _graph(engine: str, instruments: list[CryptoBettingInstrument]) -> OpportunityGraph:  # skipcq
    try:
        graph = OpportunityGraph(MarketMatcher(), engine=engine)
    except ImportError:
        pytest.skip("Rust OpportunityGraphCore is unavailable")
    graph.build(instruments)
    return graph


def _semantic_rule_store(
    cache_dir: Path,
    source: CryptoBettingInstrument,
    target: CryptoBettingInstrument,
) -> RuleStore:
    store = RuleStore(FileRuleCache(cache_dir))
    rule = RuleClassifier().classify(source, target)
    if rule is None:
        raise AssertionError("Expected semantic rule")
    template = SemanticRuleTemplate.from_rule(
        rule,
        support=TemplateSupportStats(
            template_id=SemanticRuleTemplate.from_rule(rule).template_id,
            observed_count=10,
            event_count=3,
            provider_count=1,
            providers=("SXBET",),
            sports=("soccer",),
            confidence=1.0,
        ),
        provider_scope=("SXBET",),
        promotion_status=PromotionStatus.PROMOTED.value,
        safety_tier=SafetyTier.EXECUTION_SAFE.value,
    )
    store.save_promoted_template(template)
    return store


def _edge_snapshot(graph: OpportunityGraph) -> dict[str, tuple[str, str, str, bool]]:  # skipcq
    return {
        edge_id: (
            edge.hedge_type,
            edge.market_relationship_type,
            f"{edge.confidence:.2f}",
            edge.push_capable,
        )
        for edge_id, edge in graph.edges_by_id.items()
    }


def _quote(
    instrument: CryptoBettingInstrument,
    odds: Decimal,
    ts_event: int = 1_000,
) -> object:  # skipcq
    return TestDataStubs.quote_tick(
        instrument=instrument,
        bid_price=float(odds),
        ask_price=float(odds),
        ts_event=ts_event,
    )


def _seed_quotes(
    graph: OpportunityGraph,
    instruments: list[CryptoBettingInstrument],
    odds_by_outcome: dict[str, Decimal],
) -> None:
    for index, instrument in enumerate(instruments):
        odds = odds_by_outcome[instrument.outcome]
        graph.update_quote(
            _quote(instrument, odds, ts_event=10_000 + index),
            odds=odds,
            received_ns=20_000 + index,
        )


@pytest.mark.parametrize("engine", ["python", "rust"])
def test_builds_same_market_cross_venue_edges(engine: str) -> None:  # skipcq
    instruments = [
        _instrument(venue="SXBET", outcome="over"),
        _instrument(venue="SXBET", outcome="under"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="over"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="under"),
    ]

    graph = _graph(engine, instruments)

    ensure(graph.node_count == 4)
    ensure(graph.edge_count == 4)
    ensure(all(edge.hedge_type == "same_market" for edge in graph.edges_by_id.values()))
    ensure(graph.connected_edge_count(str(instruments[0].id)) == 2)


def test_rust_and_python_topology_are_identical_for_common_edges() -> None:  # skipcq
    instruments = [
        _instrument(venue="SXBET", outcome="over"),
        _instrument(venue="SXBET", outcome="under"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="over"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="under"),
        _instrument(
            venue="SXBET",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            params="",
            info={"is_two_way_market": True},
        ),
        _instrument(
            venue="SXBET",
            market_name="Double Chance",
            market_type="double_chance",
            outcome="away_draw",
            params="",
        ),
    ]

    python_graph = _graph("python", instruments)
    rust_graph = _graph("rust", instruments)

    ensure(_edge_snapshot(rust_graph) == _edge_snapshot(python_graph))


def test_incremental_add_and_duplicate_match_python_fallback() -> None:  # skipcq
    base = [_instrument(outcome="over")]
    under = _instrument(outcome="under")
    python_graph = _graph("python", base)
    rust_graph = _graph("rust", base)

    ensure(python_graph.add_instrument(under) is True)
    ensure(rust_graph.add_instrument(under) is True)
    ensure(python_graph.add_instrument(under) is False)
    ensure(rust_graph.add_instrument(under) is False)
    ensure(_edge_snapshot(rust_graph) == _edge_snapshot(python_graph))


def test_update_quote_and_evaluate_matches_python_candidates() -> None:  # skipcq
    instruments = [
        _instrument(outcome="over"),
        _instrument(outcome="under"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="under"),
    ]
    python_graph = _graph("python", instruments)
    rust_graph = _graph("rust", instruments)
    odds_by_outcome = {"over": Decimal("2.40"), "under": Decimal("2.55")}
    _seed_quotes(python_graph, instruments, odds_by_outcome)
    _seed_quotes(rust_graph, instruments, odds_by_outcome)

    quote = _quote(instruments[0], Decimal("2.40"), ts_event=99_000)
    python_state, python_candidates = python_graph.update_quote_and_evaluate(
        quote,
        odds=Decimal("2.40"),
        received_ns=100_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=100_000,
    )
    rust_state, rust_candidates = rust_graph.update_quote_and_evaluate(
        quote,
        odds=Decimal("2.40"),
        received_ns=100_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=100_000,
    )

    ensure(python_state == rust_state)
    rust_snapshot = sorted(
        (candidate.edge.edge_id, candidate.opportunity.profit_margin)
        for candidate in rust_candidates
    )
    python_snapshot = sorted(
        (candidate.edge.edge_id, candidate.opportunity.profit_margin)
        for candidate in python_candidates
    )
    ensure(rust_snapshot == python_snapshot)
    ensure({candidate.updated_node_id for candidate in rust_candidates} == {str(instruments[0].id)})


def test_update_quote_and_scan_fast_returns_primitive_snapshots() -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    graph = _graph("rust", instruments)
    graph.update_quote(
        _quote(instruments[0], Decimal("2.40"), ts_event=10_000),
        odds=Decimal("2.40"),
        received_ns=20_000,
    )

    result = graph.update_quote_and_scan_fast(
        _quote(instruments[1], Decimal("2.55"), ts_event=10_001),
        odds=Decimal("2.55"),
        received_ns=20_001,
        min_profit_margin=Decimal("0.01"),
        now_ns=30_000,
    )

    if result is None:
        raise AssertionError("Rust fast scan should return snapshots")
    quote_updated, snapshots = result
    ensure(quote_updated is True)
    ensure(graph.quote_state_count == 2)
    ensure(str(instruments[1].id) in graph.quotes_by_node_id)
    ensure(graph.quotes_by_node_id[str(instruments[1].id)].odds == Decimal("2.55"))
    ensure(len(snapshots) == 1)
    snapshot = snapshots[0]
    ensure(snapshot[1] == str(instruments[1].id))
    ensure(snapshot[2] == str(instruments[0].id))
    ensure(snapshot[3] == "same_market")
    ensure(snapshot[4] == 1.0)
    ensure(snapshot[5] == 2.55)
    ensure(snapshot[6] == 2.4)
    ensure(snapshot[7] > 0.0)
    ensure(snapshot[8] == 10_001)
    ensure(snapshot[9] == 10_000)
    ensure(snapshot[10] == "same_market")
    ensure(snapshot[11] is False)


def test_update_quote_and_scan_fast_returns_false_for_unknown_rust_node() -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    graph = _graph("rust", instruments)
    unknown = _instrument(event_id="missing", outcome="away")

    result = graph.update_quote_and_scan_fast(
        _quote(unknown, Decimal("2.40"), ts_event=99_000),
        odds=Decimal("2.40"),
        received_ns=100_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=100_000,
    )
    if result is None:
        raise AssertionError("Rust fast scan should return snapshots")
    ensure(result == (False, []))


def test_add_instrument_keeps_mirror_when_rust_already_has_node() -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    graph = _graph("rust", instruments)
    target = instruments[1]
    node_id = str(target.id)

    # Simulate a mirror/Rust desync: Rust still holds the node but the python
    # mirror lost it. Re-adding must keep the mirror entry (Rust returns False
    # because the node already exists), not pop it back out.
    graph.nodes_by_id.pop(node_id, None)
    graph.edge_ids_by_node_id.pop(node_id, None)
    ensure(node_id not in graph.nodes_by_id)

    added = graph.add_instrument(target)
    ensure(added is False)
    ensure(node_id in graph.nodes_by_id)

    result = graph.update_quote_and_scan_fast(
        _quote(target, Decimal("2.55"), ts_event=10_001),
        odds=Decimal("2.55"),
        received_ns=20_001,
        min_profit_margin=Decimal("0.01"),
        now_ns=30_000,
    )
    if result is None:
        raise AssertionError("Rust fast scan should return snapshots")
    quote_updated, _snapshots = result
    ensure(quote_updated is True)
    ensure(node_id in graph.quotes_by_node_id)
    ensure(graph.quote_state_count > 0)


def test_update_quote_and_scan_fast_is_rust_only() -> None:  # skipcq
    instrument = _instrument()
    graph = _graph("python", [instrument])

    ensure(
        graph.update_quote_and_scan_fast(
            _quote(instrument, Decimal("2.40")),
            odds=Decimal("2.40"),
            received_ns=20_000,
            min_profit_margin=Decimal("0.01"),
            now_ns=30_000,
        )
        is None,
    )


def test_python_engine_can_still_be_forced_when_matcher_has_rule_store() -> None:  # skipcq
    matcher = MarketMatcher()
    matcher._rule_store = cast(Any, object())

    graph = OpportunityGraph(matcher, engine="python")

    ensure(graph._rust_core is None)


def test_python_semantic_topology_reports_template_count(tmp_path: Path) -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    store = _semantic_rule_store(tmp_path / "python-rules", instruments[0], instruments[1])
    matcher = MarketMatcher(rule_store=store, allow_unpromoted_topology=False)
    graph = OpportunityGraph(matcher, engine="python")

    graph.build(instruments)

    ensure(graph.topology_source == "python")
    ensure(graph.semantic_template_count == 1)
    ensure(graph.edge_count == 1)


def test_semantic_rust_builds_topology_from_promoted_templates(tmp_path: Path) -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    store = _semantic_rule_store(tmp_path / "rules", instruments[0], instruments[1])
    matcher = MarketMatcher(rule_store=store, allow_unpromoted_topology=False)
    try:
        graph = OpportunityGraph(matcher, engine="semantic_rust")
    except ImportError:
        pytest.skip("Rust OpportunityGraphCore is unavailable")

    graph.build(instruments)

    ensure(graph.graph_engine == "rust")
    ensure(graph.topology_source == "rust_semantic")
    ensure(graph.semantic_template_count == 1)
    ensure(graph.edge_count == 1)
    ensure(next(iter(graph.edges_by_id.values())).execution_safe is True)


def test_semantic_rust_ignores_scope_only_period_params(tmp_path: Path) -> None:  # skipcq
    template_instruments = [
        _instrument(outcome="over", params="line=2.5&period=ft"),
        _instrument(outcome="under", params="line=2.5&period=ft"),
    ]
    live_instruments = [
        _instrument(outcome="over", params="line=2.5"),
        _instrument(outcome="under", params="line=2.5"),
    ]
    store = _semantic_rule_store(
        tmp_path / "period-rules",
        template_instruments[0],
        template_instruments[1],
    )
    matcher = MarketMatcher(rule_store=store, allow_unpromoted_topology=False)
    try:
        graph = OpportunityGraph(matcher, engine="semantic_rust")
    except ImportError:
        pytest.skip("Rust OpportunityGraphCore is unavailable")

    payloads = graph._semantic_template_payloads()
    pattern_a = cast(dict[str, object], payloads[0]["pattern_a"])
    ensure("period" not in str(pattern_a["params_key"]))

    graph.build(live_instruments)

    ensure(graph.topology_source == "rust_semantic")
    ensure(graph.edge_count == 1)


def test_semantic_rust_does_not_fall_back_to_public_matcher_without_templates() -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    try:
        graph = OpportunityGraph(MarketMatcher(), engine="semantic_rust")
    except ImportError:
        pytest.skip("Rust OpportunityGraphCore is unavailable")

    graph.build(instruments)

    ensure(graph.graph_engine == "rust")
    ensure(graph.topology_source == "rust_semantic")
    ensure(graph.semantic_template_count == 0)
    ensure(graph.edge_count == 0)


def test_venue_agnostic_polymarket_template_builds_cross_venue_edge(
    tmp_path: Path,
) -> None:  # skipcq
    polymarket_home = _instrument(
        venue="POLYMARKET",
        sport_name="Basketball",
        market_name="Basketball Winner",
        market_type="basketball.winner",
        outcome="home",
        params="",
        info={"resolution_policy": {"tie_or_unknown": "lose"}},
    )
    polymarket_away = _instrument(
        venue="POLYMARKET",
        sport_name="Basketball",
        market_name="Basketball Winner",
        market_type="basketball.winner",
        outcome="away",
        params="",
        info={"resolution_policy": {"tie_or_unknown": "lose"}},
    )
    cloudbet_away = _instrument(
        venue="CLOUDBET",
        sport_name="Basketball",
        market_name="Moneyline",
        market_type="basketball.moneyline",
        outcome="away",
        params="",
    )
    store = RuleStore(FileRuleCache(tmp_path / "polymarket-rules"))
    rule = RuleClassifier().classify(polymarket_home, polymarket_away)
    if rule is None:
        raise AssertionError("Expected portable Polymarket winner rule")
    template = SemanticRuleTemplate.from_rule(
        rule,
        support=TemplateSupportStats(
            template_id=SemanticRuleTemplate.from_rule(rule).template_id,
            observed_count=10,
            event_count=10,
            provider_count=1,
            providers=("POLYMARKET",),
            sports=("basketball",),
            confidence=1.0,
        ),
        provider_scope=("POLYMARKET",),
        venue_agnostic=True,
        promotion_status=PromotionStatus.PROMOTED.value,
        safety_tier=SafetyTier.EXECUTION_SAFE.value,
    )
    store.save_promoted_template(template)
    matcher = MarketMatcher(rule_store=store, allow_unpromoted_topology=False)
    graph = OpportunityGraph(matcher, engine="python")

    graph.build([polymarket_home, cloudbet_away])

    ensure(graph.edge_count == 1)
    edge = next(iter(graph.edges_by_id.values()))
    ensure(edge.template_id == template.template_id)
    ensure(edge.execution_safe is True)


def test_python_graph_coverage_summary_reports_store_tiers_and_samples() -> None:
    class FakeRuleStore:
        def list_promoted_template_ids(self):
            return []

        def list_coverage_proof_ids(self):
            return ["proof-exec", "proof-topology"]

        def load_coverage_proof(self, proof_id):
            return {
                "proof-exec": SimpleNamespace(
                    proof_id="proof-exec",
                    universe=SimpleNamespace(sport="soccer", scope="match"),
                    coverage_set=SimpleNamespace(provider_scope=("SXBET",)),
                    predicates=(
                        SimpleNamespace(instrument_id="home.SXBET"),
                        SimpleNamespace(instrument_id="draw.SXBET"),
                        SimpleNamespace(instrument_id="away.SXBET"),
                    ),
                    complete=True,
                    win_covered_states=("HOME_WIN", "DRAW", "AWAY_WIN"),
                    overlapping_win_states=(),
                    gaps=(),
                    risks=(),
                    safety_tier="EXECUTION_SAFE",
                    execution_safe=True,
                    same_venue_execution_eligible=False,
                    relationship_type="COMPLEMENTARY_COVERAGE",
                    blocker_reasons=(),
                ),
                "proof-topology": SimpleNamespace(
                    proof_id="proof-topology",
                    universe=SimpleNamespace(sport="soccer", scope="match"),
                    coverage_set=SimpleNamespace(provider_scope=("SXBET",)),
                    predicates=(
                        SimpleNamespace(instrument_id="dnb_home.SXBET"),
                        SimpleNamespace(instrument_id="ah0_home.SXBET"),
                    ),
                    complete=False,
                    win_covered_states=("HOME_WIN",),
                    overlapping_win_states=("HOME_WIN",),
                    gaps=(SimpleNamespace(reason="incomplete_coverage"),),
                    risks=(SimpleNamespace(reason="equivalent_selection"),),
                    safety_tier="TOPOLOGY_SAFE",
                    execution_safe=False,
                    same_venue_execution_eligible=True,
                    relationship_type="EQUIVALENT_SELECTION",
                    blocker_reasons=("equivalent_selection",),
                ),
            }.get(proof_id)

        def list_coverage_hyperedge_ids(self):
            return ["hyperedge-exec"]

        def load_coverage_hyperedge(self, hyperedge_id):
            return {
                "hyperedge-exec": SimpleNamespace(
                    hyperedge_id="hyperedge-exec",
                    coverage_proof_id="proof-exec",
                    instrument_ids=("home.SXBET", "draw.SXBET", "away.SXBET"),
                    provider_scope=("SXBET",),
                    relationship_type="COMPLEMENTARY_COVERAGE",
                    safety_tier="EXECUTION_SAFE",
                    execution_safe=True,
                    caveats=(),
                ),
            }.get(hyperedge_id)

    graph = OpportunityGraph(
        MarketMatcher(rule_store=cast(RuleStore, FakeRuleStore()), allow_unpromoted_topology=False),
        engine="python",
    )

    graph.build([_instrument()])

    summary = graph.semantic_coverage_summary()
    ensure(summary["coverageProofCount"] == 2)
    ensure(summary["coverageHyperedgeCount"] == 1)
    ensure(summary["executionSafeCoverageProofCount"] == 1)
    ensure(summary["executionSafeCoverageHyperedgeCount"] == 1)
    ensure(summary["sameVenueEligibleCoverageProofCount"] == 1)
    ensure(summary["proofSafetyTierCounts"] == {"EXECUTION_SAFE": 1, "TOPOLOGY_SAFE": 1})
    ensure(summary["hyperedgeSafetyTierCounts"] == {"EXECUTION_SAFE": 1})
    ensure(
        summary["proofRelationshipTypeCounts"]
        == {"COMPLEMENTARY_COVERAGE": 1, "EQUIVALENT_SELECTION": 1},
    )
    ensure(summary["proofBlockerReasonCounts"] == {"equivalent_selection": 1})
    ensure(summary["proofGapReasonCounts"] == {"incomplete_coverage": 1})
    ensure(summary["proofRiskReasonCounts"] == {"equivalent_selection": 1})
    ensure(summary["sampleProofIds"] == ["proof-exec", "proof-topology"])
    sample_proofs = cast(list[dict[str, object]], summary["sampleProofs"])
    sample_hyperedges = cast(list[dict[str, object]], summary["sampleHyperedges"])
    ensure(
        sample_proofs[0]["instrument_ids"] == ["home.SXBET", "draw.SXBET", "away.SXBET"],
    )
    ensure(sample_hyperedges[0]["hyperedge_id"] == "hyperedge-exec")


def test_python_graph_coverage_summary_samples_runtime_relevant_hyperedges() -> None:  # skipcq
    def _proof(proof_id: str, *, provider: str, event_key: str) -> SimpleNamespace:
        return SimpleNamespace(
            proof_id=proof_id,
            universe=SimpleNamespace(sport="soccer", scope="full_time"),
            coverage_set=SimpleNamespace(provider_scope=(provider,)),
            predicates=(
                SimpleNamespace(
                    predicate_id=f"{proof_id}:home",
                    instrument_id=event_key,
                    provider=provider,
                    event_key=event_key,
                    sport="soccer",
                    scope="full_time",
                    market_type="MATCH_ODDS",
                    market_family="MATCH_ODDS",
                    selection="HOME",
                    params=(),
                    result_states=("HOME_WIN", "DRAW", "AWAY_WIN"),
                    win_states=("HOME_WIN",),
                    void_states=(),
                    partial_states=(),
                    unknown_states=(),
                    provider_rule_flags=(),
                    caveats=(),
                ),
            ),
            complete=True,
            win_covered_states=("HOME_WIN",),
            overlapping_win_states=(),
            gaps=(),
            risks=(),
            safety_tier="EXECUTION_SAFE",
            execution_safe=True,
            same_venue_execution_eligible=False,
            relationship_type="COMPLEMENTARY_COVERAGE",
            blocker_reasons=(),
        )

    class FakeRuleStore:
        def list_promoted_template_ids(self):
            return []

        def load_promoted_template(self, template_id):
            return None

        def list_coverage_proof_ids(self):
            return [f"proof-cloudbet-{index}" for index in range(12)] + ["proof-sxbet"]

        def load_coverage_proof(self, proof_id):
            if proof_id == "proof-sxbet":
                return _proof(
                    proof_id,
                    provider="SXBET",
                    event_key="soccer|team_a|team_b|2026-03-13T18:00:00Z",
                )
            index = proof_id.rsplit("-", maxsplit=1)[-1]
            return _proof(
                proof_id,
                provider="CLOUDBET",
                event_key=f"soccer|other_home_{index}|other_away_{index}|2026-03-13T18:00:00Z",
            )

        def list_coverage_hyperedge_ids(self):
            return [f"hyperedge-cloudbet-{index}" for index in range(12)] + ["hyperedge-sxbet"]

        def load_coverage_hyperedge(self, hyperedge_id):
            if hyperedge_id == "hyperedge-sxbet":
                return SimpleNamespace(
                    hyperedge_id=hyperedge_id,
                    coverage_proof_id="proof-sxbet",
                    instrument_ids=("semantic-sxbet-home",),
                    provider_scope=("SXBET",),
                    relationship_type="COMPLEMENTARY_COVERAGE",
                    safety_tier="EXECUTION_SAFE",
                    execution_safe=True,
                    caveats=(),
                )
            index = hyperedge_id.rsplit("-", maxsplit=1)[-1]
            return SimpleNamespace(
                hyperedge_id=hyperedge_id,
                coverage_proof_id=f"proof-cloudbet-{index}",
                instrument_ids=(f"semantic-cloudbet-{index}",),
                provider_scope=("CLOUDBET",),
                relationship_type="COMPLEMENTARY_COVERAGE",
                safety_tier="EXECUTION_SAFE",
                execution_safe=True,
                caveats=(),
            )

    graph = OpportunityGraph(
        MarketMatcher(rule_store=cast(RuleStore, FakeRuleStore()), allow_unpromoted_topology=False),
        engine="python",
    )

    graph.build([_instrument()])

    summary = graph.semantic_coverage_summary()
    sample_hyperedges = cast(list[dict[str, object]], summary["sampleHyperedges"])
    ensure(summary["coverageHyperedgeCount"] == 13)
    ensure(sample_hyperedges[0]["hyperedge_id"] == "hyperedge-sxbet")


def test_sync_keeps_rust_semantic_edges_without_python_rediscovery() -> None:  # skipcq
    matcher = MarketMatcher(allow_unpromoted_topology=False)
    instruments = [
        _instrument(outcome="over", params="line=3.5"),
        _instrument(outcome="under", params="line=4.5"),
    ]
    graph = OpportunityGraph(matcher, engine="python")
    for instrument in instruments:
        node = graph._node_from_instrument(instrument)
        graph.nodes_by_id[node.node_id] = node
        graph.edge_ids_by_node_id[node.node_id] = set()
    edge_id = graph._edge_id(str(instruments[0].id), str(instruments[1].id))
    metadata = json.dumps(
        {
            "template_id": "template:rust-semantic",
            "relationship_type": "COMPLEMENTARY_COVERAGE",
            "promotion_status": "PROMOTED",
            "safety_tier": "EXECUTION_SAFE",
            "market_relationship_type": "same_market",
            "same_venue_execution_eligible": False,
            "partial_settlement": False,
            "caveats": [],
        },
    )

    class FakeRustCore:
        def edge_snapshots(self):
            return [
                (
                    edge_id,
                    str(instruments[0].id),
                    str(instruments[1].id),
                    "same_market",
                    1.0,
                    True,
                    metadata,
                    False,
                    True,
                    None,
                    None,
                    None,
                ),
            ]

    graph._rust_core = cast(Any, FakeRustCore())

    ensure(matcher._semantic_hedge_candidate(instruments[0], instruments[1]) is None)

    graph._sync_edges_from_rust()

    ensure(graph.edge_count == 1)
    edge = next(iter(graph.edges_by_id.values()))
    ensure(edge.template_id == "template:rust-semantic")
    ensure(edge.relationship_type == "COMPLEMENTARY_COVERAGE")
    ensure(edge.safety_tier == "EXECUTION_SAFE")
    ensure(edge.execution_safe is True)


def _cross_venue_fake_rust_core(source_id: str, target_id: str, edge_id: str) -> Any:
    metadata = json.dumps(
        {
            "template_id": "template:rust-semantic",
            "relationship_type": "COMPLEMENTARY_COVERAGE",
            "promotion_status": "PROMOTED",
            "safety_tier": "TOPOLOGY_SAFE",
            "market_relationship_type": "same_market",
            "same_venue_execution_eligible": False,
            "partial_settlement": False,
            "caveats": ["cross_venue_topology_only"],
        },
    )

    class FakeRustCore:
        def edge_snapshots(self):
            return [
                (
                    edge_id,
                    source_id,
                    target_id,
                    "same_market",
                    1.0,
                    False,
                    metadata,
                    False,
                    False,
                    None,
                    None,
                    None,
                ),
            ]

    return FakeRustCore()


def test_sync_rematerializes_missing_endpoint_from_mirror_registry() -> None:  # skipcq
    instruments = [
        _instrument(venue="CLOUDBET", event_id="cb-1", outcome="over"),
        _instrument(venue="SXBET", event_id="sx-1", outcome="under"),
    ]
    graph = OpportunityGraph(MarketMatcher(allow_unpromoted_topology=False), engine="python")
    graph.build(instruments)
    source_id = str(instruments[0].id)
    target_id = str(instruments[1].id)
    edge_id = graph._edge_id(source_id, target_id)
    graph._rust_core = cast(Any, _cross_venue_fake_rust_core(source_id, target_id, edge_id))
    graph.nodes_by_id.pop(source_id)

    graph._sync_edges_from_rust()

    ensure(source_id in graph.nodes_by_id)
    ensure(target_id in graph.nodes_by_id)
    ensure(graph.edge_count == 1)
    edge = graph.edges_by_id[edge_id]
    ensure(edge.same_venue is False)
    ensure(edge_id in graph.edge_ids_by_node_id[source_id])
    ensure(edge_id in graph.edge_ids_by_node_id[target_id])
    ensure(graph.stats()["cross_venue_edges_dropped_missing_endpoint"] == 0)


def test_sync_counts_dropped_cross_venue_edge_when_endpoint_unrecoverable() -> None:  # skipcq
    instruments = [
        _instrument(venue="CLOUDBET", event_id="cb-1", outcome="over"),
        _instrument(venue="SXBET", event_id="sx-1", outcome="under"),
    ]
    graph = OpportunityGraph(MarketMatcher(allow_unpromoted_topology=False), engine="python")
    graph.build(instruments)
    source_id = str(instruments[0].id)
    target_id = str(instruments[1].id)
    edge_id = graph._edge_id(source_id, target_id)
    graph._rust_core = cast(Any, _cross_venue_fake_rust_core(source_id, target_id, edge_id))
    graph.nodes_by_id.pop(source_id)
    graph._mirrored_nodes_by_id.pop(source_id)

    graph._sync_edges_from_rust()

    ensure(graph.edge_count == 0)
    ensure(graph.stats()["cross_venue_edges_dropped_missing_endpoint"] == 1)
    ensure(graph.cross_venue_edges_dropped_missing_endpoint == 1)


def test_node_payload_fallbacks_cover_missing_helper_methods() -> None:  # skipcq
    template = _instrument()

    class BareInstrument:
        def __init__(self) -> None:
            self.id = template.id
            self.event_id = template.event_id
            self.event_name = template.event_name
            self.market_name = template.market_name
            self.market_type = template.market_type
            self.outcome = template.outcome
            self.params = template.params
            self.handicap = template.handicap

    bare = cast(Any, BareInstrument())
    node = OpportunityGraph._node_from_instrument(bare)
    payload = OpportunityGraph._node_payload_from_node(node, bare)

    ensure(node.canonical_event_key == str(template.id))
    ensure(node.canonical_outcome_key.endswith("|over"))
    ensure(payload["event_key_no_time"] == str(template.id))
    ensure(payload["selection_key"] == "over")
    ensure(payload["start_time_ns"] is None)


def test_node_payload_treats_date_only_start_as_imprecise() -> None:  # skipcq
    instrument = _instrument(start_time="2026-05-14")
    node = OpportunityGraph._node_from_instrument(instrument)
    payload = OpportunityGraph._node_payload_from_node(node, instrument)

    ensure(payload["start_time_ns"] is None)


def test_node_payload_ignores_non_iterable_alias_helper_return() -> None:  # skipcq
    template = _instrument()

    class MockLikeInstrument:
        def __init__(self) -> None:
            self.id = template.id
            self.event_id = template.event_id
            self.event_name = template.event_name
            self.market_name = template.market_name
            self.market_type = template.market_type
            self.outcome = template.outcome
            self.params = template.params
            self.handicap = template.handicap

        def event_key(self, *, include_start_time: bool = True) -> str:
            return "soccer:minnesota timberwolves:san antonio spurs"

        def event_alias_keys(self, *, include_start_time: bool = True) -> object:
            return object()

    instrument = cast(Any, MockLikeInstrument())
    node = OpportunityGraph._node_from_instrument(instrument)
    payload = OpportunityGraph._node_payload_from_node(node, instrument)

    ensure(payload["event_alias_keys"] == ("soccer:minnesota timberwolves:san antonio spurs",))


def test_rust_scan_filters_unprofitable_edges_before_decimal_validation() -> None:  # skipcq
    instruments = [
        _instrument(venue=f"VENUE{index}", event_id=f"event-{index}", outcome=outcome)
        for index in range(12)
        for outcome in ("over", "under")
    ]
    graph = _graph("rust", instruments)
    _seed_quotes(graph, instruments, {"over": Decimal("1.80"), "under": Decimal("1.80")})

    _, candidates = graph.update_quote_and_evaluate(
        _quote(instruments[0], Decimal("1.80"), ts_event=99_000),
        odds=Decimal("1.80"),
        received_ns=100_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=100_000,
    )

    ensure(graph.connected_edge_count(str(instruments[0].id)) > 1)
    ensure(not candidates)


def test_push_capable_edges_are_built_but_not_evaluated() -> None:  # skipcq
    instruments = [
        _instrument(
            market_name="Draw No Bet",
            market_type="draw_no_bet",
            outcome="home",
            params="",
        ),
        _instrument(
            market_name="Draw No Bet",
            market_type="draw_no_bet",
            outcome="away",
            params="",
        ),
    ]
    graph = _graph("rust", instruments)
    _seed_quotes(graph, instruments, {"home": Decimal("2.40"), "away": Decimal("2.55")})

    _, candidates = graph.update_quote_and_evaluate(
        _quote(instruments[0], Decimal("2.40")),
        odds=Decimal("2.40"),
        received_ns=100_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=100_000,
    )

    ensure(graph.edge_count == 1)
    ensure(next(iter(graph.edges_by_id.values())).push_capable is True)
    ensure(not candidates)


def test_missing_start_time_ambiguity_matches_python_fallback() -> None:  # skipcq
    ambiguous = [
        _instrument(event_id="early", outcome="over", start_time="2026-03-13T10:00:00Z"),
        _instrument(event_id="late", outcome="under", start_time="2026-03-13T20:00:00Z"),
        _instrument(
            venue="BLACKBET",
            event_id="missing",
            outcome="under",
            start_time=None,
        ),
    ]
    unambiguous = [
        _instrument(event_id="early", outcome="over", start_time="2026-03-13T10:00:00Z"),
        _instrument(
            venue="BLACKBET",
            event_id="missing",
            outcome="under",
            start_time=None,
        ),
    ]

    ensure(_graph("rust", ambiguous).edge_count == _graph("python", ambiguous).edge_count == 0)
    ensure(_graph("rust", unambiguous).edge_count == _graph("python", unambiguous).edge_count == 1)


def test_engine_validation_and_missing_node_paths() -> None:  # skipcq
    instrument = _instrument()
    quote = _quote(instrument, Decimal("2.00"))

    with pytest.raises(ValueError, match="Invalid opportunity graph engine"):
        OpportunityGraph(MarketMatcher(), engine="invalid")

    python_graph = _graph("python", [])
    rust_graph = _graph("rust", [])

    ensure(python_graph.quote_state_count == 0)
    ensure(python_graph.update_quote(quote, odds=Decimal("2.00"), received_ns=1) is None)
    ensure(
        not python_graph.evaluate_updated_node(
            str(instrument.id),
            min_profit_margin=Decimal("0.01"),
            now_ns=1,
        ),
    )
    ensure(
        python_graph.update_quote_and_evaluate(
            quote,
            odds=Decimal("2.00"),
            received_ns=1,
            min_profit_margin=Decimal("0.01"),
            now_ns=1,
        )
        == (None, []),
    )
    ensure(
        rust_graph.update_quote_and_evaluate(
            quote,
            odds=Decimal("2.00"),
            received_ns=1,
            min_profit_margin=Decimal("0.01"),
            now_ns=1,
        )
        == (None, []),
    )


def test_python_evaluation_skips_missing_unprofitable_and_push_edges() -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    graph = _graph("python", instruments)
    graph.update_quote(
        _quote(instruments[0], Decimal("1.80")),
        odds=Decimal("1.80"),
        received_ns=1,
    )

    ensure(
        not graph.evaluate_updated_node(
            str(instruments[0].id),
            min_profit_margin=Decimal("0.01"),
            now_ns=1,
        ),
    )

    graph.update_quote(
        _quote(instruments[1], Decimal("1.80")),
        odds=Decimal("1.80"),
        received_ns=2,
    )
    ensure(
        not graph.evaluate_updated_node(
            str(instruments[0].id),
            min_profit_margin=Decimal("0.01"),
            now_ns=3,
        ),
    )

    push_instruments = [
        _instrument(
            market_name="Draw No Bet",
            market_type="draw_no_bet",
            outcome="home",
            params="",
        ),
        _instrument(
            market_name="Draw No Bet",
            market_type="draw_no_bet",
            outcome="away",
            params="",
        ),
    ]
    push_graph = _graph("python", push_instruments)
    _seed_quotes(push_graph, push_instruments, {"home": Decimal("2.40"), "away": Decimal("2.55")})

    ensure(
        not push_graph.evaluate_updated_node(
            str(push_instruments[0].id),
            min_profit_margin=Decimal("0.01"),
            now_ns=4,
        ),
    )


def test_semantic_template_payloads_cache_avoids_disk_reread(tmp_path: Path) -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    store = _semantic_rule_store(tmp_path / "rules", instruments[0], instruments[1])
    matcher = MarketMatcher(rule_store=store, allow_unpromoted_topology=False)
    graph = OpportunityGraph(matcher, engine="python")

    real_load = store.load_promoted_template
    load_calls = {"count": 0}

    def _counting_load(template_id: str) -> Any:
        load_calls["count"] += 1
        return real_load(template_id)

    store.load_promoted_template = _counting_load  # type: ignore[method-assign]

    first = graph._semantic_template_payloads()
    reads_after_first = load_calls["count"]
    ensure(reads_after_first >= 1)

    second = graph._semantic_template_payloads()
    # No promoted template re-read from disk while the store generation is unchanged.
    ensure(load_calls["count"] == reads_after_first)
    ensure(second is first)

    # A store write bumps the generation and invalidates the cache, forcing a reload.
    template_id = store.list_promoted_template_ids()[0]
    reloaded_template = real_load(template_id)
    assert reloaded_template is not None
    store.save_promoted_template(reloaded_template)
    third = graph._semantic_template_payloads()
    ensure(load_calls["count"] > reads_after_first)
    ensure(third is not first)


class _RecordingSemanticRustCore:  # skipcq
    def __init__(self) -> None:
        self.coverage_loads = 0

    def clear(self) -> None:
        pass

    def build_semantic(self, nodes, templates) -> None:
        pass

    def load_semantic_templates(self, templates):
        return len(list(templates))

    def load_semantic_coverage(self, proofs, hyperedges):
        self.coverage_loads += 1
        return (len(list(proofs)), len(list(hyperedges)))

    def add_instrument_semantic(self, node) -> bool:
        return True

    def edge_snapshots(self):
        return []

    def edge_snapshots_for_node(self, node_id):
        return []


def _coverage_selection(instrument_id: str, selection: str) -> NormalizedSelection:  # skipcq
    return NormalizedSelection(
        venue="SXBET",
        instrument_id=instrument_id,
        sport="soccer",
        event_key="team-a-team-b-2026-03-13",
        period="full_time",
        scope="full_time",
        market_type=CanonicalMarketType.TOTALS.value,
        market_family=CanonicalMarketType.TOTALS.value,
        selection=selection,
        params=(("line", "2.5"),),
        raw_market_name="total_goals",
        raw_market_type="total_goals",
        raw_outcome=selection.lower(),
        outcome_key=selection.lower(),
    )


def _seed_coverage_proof(store: RuleStore) -> CoverageProof:  # skipcq
    over = SelectionPredicateBuilder.from_selection(
        _coverage_selection("over-25", "OVER"),
        provider="SXBET",
    )
    under = SelectionPredicateBuilder.from_selection(
        _coverage_selection("under-25", "UNDER"),
        provider="SXBET",
    )
    proof = CoverageEngine().evaluate((over, under))
    store.save_coverage_proof(proof)
    store.save_coverage_hyperedge(CoverageHyperedge.from_proof(proof))
    return proof


def _semantic_graph_with_recording_core(  # skipcq
    store: RuleStore,
) -> tuple[OpportunityGraph, _RecordingSemanticRustCore]:
    matcher = MarketMatcher(rule_store=store, allow_unpromoted_topology=False)
    graph = OpportunityGraph(matcher, engine="auto")
    core = _RecordingSemanticRustCore()
    graph._rust_core = cast(Any, core)
    return graph, core


def _event_instrument(event_idx: int, outcome: str) -> CryptoBettingInstrument:  # skipcq
    return _instrument(
        event_id=f"event-{event_idx}",
        event_name=f"Team {event_idx}A vs Team {event_idx}B",
        home_name=f"Team {event_idx}A",
        away_name=f"Team {event_idx}B",
        outcome=outcome,
    )


def test_semantic_coverage_cache_serves_adds_without_store_reread(tmp_path: Path) -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    store = _semantic_rule_store(tmp_path / "rules", instruments[0], instruments[1])
    proof = _seed_coverage_proof(store)
    graph, core = _semantic_graph_with_recording_core(store)

    calls = {"list_proofs": 0, "load_proofs": 0}
    real_list = store.list_coverage_proof_ids
    real_load = store.load_coverage_proof

    def _counting_list() -> list[str]:
        calls["list_proofs"] += 1
        return real_list()

    def _counting_load(proof_id: str) -> Any:
        calls["load_proofs"] += 1
        return real_load(proof_id)

    store.list_coverage_proof_ids = _counting_list  # type: ignore[method-assign]
    store.load_coverage_proof = _counting_load  # type: ignore[method-assign]

    graph.build(instruments)
    ensure(graph.topology_source == "rust_semantic")
    ensure(core.coverage_loads == 1)
    ensure(calls["load_proofs"] == 1)
    listed_after_build = calls["list_proofs"]

    ensure(graph.add_instrument(_event_instrument(2, "over")) is True)
    ensure(graph.add_instrument(_event_instrument(2, "under")) is True)

    # Unchanged store generation: proofs were listed/loaded once at build and the
    # incremental adds neither re-read the store nor re-marshal into Rust.
    ensure(calls["list_proofs"] == listed_after_build)
    ensure(calls["load_proofs"] == 1)
    ensure(core.coverage_loads == 1)

    # A store write bumps the shared generation and forces a fresh read + Rust reload.
    store.save_coverage_proof(proof)
    ensure(graph.add_instrument(_event_instrument(3, "over")) is True)
    ensure(calls["load_proofs"] == 2)
    ensure(core.coverage_loads == 2)


def test_full_rebuild_and_store_swap_force_rust_coverage_reload(tmp_path: Path) -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    cache_dir = tmp_path / "rules"
    store = _semantic_rule_store(cache_dir, instruments[0], instruments[1])
    _seed_coverage_proof(store)
    graph, core = _semantic_graph_with_recording_core(store)

    graph.build(instruments)
    ensure(core.coverage_loads == 1)

    # A rebuild clears the Rust core, so coverage must reload even though the store
    # generation is unchanged.
    graph.build(instruments)
    ensure(core.coverage_loads == 2)

    # The semantic cache reload path points the matcher at a fresh RuleStore over the
    # published cache dir; the new store identity defeats the generation guard.
    graph._matcher.set_rule_store(RuleStore(FileRuleCache(cache_dir)))
    graph.build(instruments)
    ensure(graph.topology_source == "rust_semantic")
    ensure(core.coverage_loads == 3)

    # Incremental adds against the reloaded store go back to skipping.
    ensure(graph.add_instrument(_event_instrument(2, "over")) is True)
    ensure(core.coverage_loads == 3)


def _edge_equivalence_key(edge: Any) -> tuple:  # skipcq
    # Endpoints are compared as a set: the incremental connect path always treats the
    # new node as the source while a full rebuild orders endpoints by bucket iteration,
    # and every downstream consumer (matcher, evaluation, candidate filters) is
    # direction-agnostic for these edges.
    return (
        frozenset((edge.source_node_id, edge.target_node_id)),
        edge.hedge_type,
        f"{edge.confidence:.9f}",
        edge.same_venue,
        edge.market_relationship_type,
        edge.push_capable,
        edge.execution_safe,
        edge.rule_id,
        edge.template_id,
        edge.relationship_type,
        tuple(sorted(edge.caveats)),
        edge.promotion_status,
        edge.safety_tier,
        edge.same_venue_execution_eligible,
        edge.void_capable,
        edge.partial_settlement,
    )


def _topology_snapshot(graph: OpportunityGraph) -> dict[str, Any]:  # skipcq
    return {
        "nodes": sorted(graph.nodes_by_id),
        "edges": {
            edge_id: _edge_equivalence_key(edge) for edge_id, edge in graph.edges_by_id.items()
        },
        "edge_ids_by_node_id": {
            node_id: sorted(edge_ids)
            for node_id, edge_ids in graph.edge_ids_by_node_id.items()
            if edge_ids
        },
    }


def _assert_edge_index_symmetric(graph: OpportunityGraph) -> None:  # skipcq
    for edge_id, edge in graph.edges_by_id.items():
        ensure(edge_id in graph.edge_ids_by_node_id.get(edge.source_node_id, set()))
        ensure(edge_id in graph.edge_ids_by_node_id.get(edge.target_node_id, set()))
    for node_id, edge_ids in graph.edge_ids_by_node_id.items():
        for edge_id in edge_ids:
            edge = graph.edges_by_id[edge_id]
            ensure(node_id in (edge.source_node_id, edge.target_node_id))


def _instrument_pool() -> list[CryptoBettingInstrument]:  # skipcq
    # Every instrument carries a concrete start time on purpose: the missing-start-time
    # ambiguity fallback makes pair matching depend on the WHOLE bucket's history, so a
    # from-scratch rebuild of the same final set is not required to agree with the
    # incremental path there (that divergence predates removal and applies to
    # incremental adds already).
    return [
        _instrument(
            venue=venue,
            event_id=f"event-{event_idx}-{venue}",
            event_name=f"Team {event_idx}A vs Team {event_idx}B",
            home_name=f"Team {event_idx}A",
            away_name=f"Team {event_idx}B",
            outcome=outcome,
        )
        for event_idx in range(4)
        for venue in ("SXBET", "BLACKBET", "CLOUDBET")
        for outcome in ("over", "under")
    ]


@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("seed", [7, 21, 1337])
def test_incremental_add_remove_matches_full_build(engine: str, seed: int) -> None:  # skipcq
    rng = random.Random(seed)  # noqa: S311
    pool = {str(instrument.id): instrument for instrument in _instrument_pool()}
    incremental = _graph(engine, [])
    present: dict[str, CryptoBettingInstrument] = {}

    for _ in range(60):
        add = not present or (len(present) < len(pool) and rng.random() < 0.6)
        if add:
            node_id = rng.choice(sorted(set(pool) - set(present)))
            ensure(incremental.add_instrument(pool[node_id]) is True)
            present[node_id] = pool[node_id]
        else:
            node_id = rng.choice(sorted(present))
            ensure(incremental.remove_instrument(node_id) is True)
            del present[node_id]
        _assert_edge_index_symmetric(incremental)

    full = _graph(engine, list(present.values()))

    ensure(_topology_snapshot(incremental) == _topology_snapshot(full))


def test_incremental_semantic_rust_matches_full_build(tmp_path: Path) -> None:  # skipcq
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    store = _semantic_rule_store(tmp_path / "incremental-rules", instruments[0], instruments[1])

    def build_graph() -> OpportunityGraph:
        matcher = MarketMatcher(rule_store=store, allow_unpromoted_topology=False)
        try:
            graph = OpportunityGraph(matcher, engine="semantic_rust")
        except ImportError:
            pytest.skip("Rust OpportunityGraphCore is unavailable")
        return graph

    extra = _instrument(
        event_id="event-2",
        event_name="Team C vs Team D",
        home_name="Team C",
        away_name="Team D",
        outcome="over",
    )
    extra_under = _instrument(
        event_id="event-2",
        event_name="Team C vs Team D",
        home_name="Team C",
        away_name="Team D",
        outcome="under",
    )

    incremental = build_graph()
    incremental.build(instruments)
    ensure(incremental.add_instrument(extra) is True)
    ensure(incremental.add_instrument(extra_under) is True)
    ensure(incremental.remove_instrument(str(instruments[0].id)) is True)
    _assert_edge_index_symmetric(incremental)

    full = build_graph()
    full.build([instruments[1], extra, extra_under])

    ensure(incremental.topology_source == "rust_semantic")
    ensure(_topology_snapshot(incremental) == _topology_snapshot(full))


def test_remove_instrument_preserves_surviving_quote_state() -> None:  # skipcq
    instruments = [
        _instrument(venue="SXBET", outcome="over"),
        _instrument(venue="SXBET", outcome="under"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="over"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="under"),
    ]
    graph = _graph("rust", instruments)
    _seed_quotes(graph, instruments, {"over": Decimal("2.40"), "under": Decimal("2.55")})
    removed_id = str(instruments[0].id)
    survivor_id = str(instruments[1].id)

    ensure(graph.remove_instrument(removed_id) is True)

    ensure(removed_id not in graph.nodes_by_id)
    ensure(removed_id not in graph.quotes_by_node_id)
    ensure(removed_id not in graph.edge_ids_by_node_id)
    ensure(survivor_id in graph.quotes_by_node_id)
    ensure(graph.quote_state_count == 3)
    _assert_edge_index_symmetric(graph)
    # Removal is inert for unknown ids.
    ensure(graph.remove_instrument(removed_id) is False)

    # The survivor still evaluates against its remaining cross-venue counterpart.
    _, candidates = graph.update_quote_and_evaluate(
        _quote(instruments[1], Decimal("2.55"), ts_event=99_000),
        odds=Decimal("2.55"),
        received_ns=100_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=100_000,
    )
    ensure(len(candidates) == 1)


def test_add_instrument_edge_sync_is_bucket_local_not_all_edges() -> None:  # skipcq
    event_count = 500
    instruments = [
        _instrument(
            venue=venue,
            event_id=f"event-{event_idx}-{venue}",
            event_name=f"Team {event_idx}A vs Team {event_idx}B",
            home_name=f"Team {event_idx}A",
            away_name=f"Team {event_idx}B",
            outcome=outcome,
        )
        for event_idx in range(event_count)
        for venue in ("SXBET", "BLACKBET")
        for outcome in ("over", "under")
    ]
    graph = _graph("rust", instruments)
    ensure(graph.node_count == 2_000)
    ensure(graph.edge_count >= 1_500)

    matcher_calls = {"count": 0}
    real_matcher = graph._best_public_hedge_candidate

    def _counting_matcher(source: Any, target: Any) -> Any:
        matcher_calls["count"] += 1
        return real_matcher(source, target)

    graph._best_public_hedge_candidate = _counting_matcher  # type: ignore[method-assign]

    new_instrument = _instrument(
        venue="CLOUDBET",
        event_id="event-0-CLOUDBET",
        event_name="Team 0A vs Team 0B",
        home_name="Team 0A",
        away_name="Team 0B",
        outcome="under",
    )
    ensure(graph.add_instrument(new_instrument) is True)

    # The add must only re-mirror the new node's bucket-local edges, never re-run the
    # Python matcher across the whole edge set.
    ensure(0 < matcher_calls["count"] < 10)
    ensure(matcher_calls["count"] < graph.edge_count)
    stats = graph.stats()
    ensure(stats["edge_sync_delta_runs"] == 1)
    ensure(stats["edge_sync_full_runs"] == 1)
    _assert_edge_index_symmetric(graph)
