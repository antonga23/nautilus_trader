# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for opportunity graph engines.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.data import TestDataStubs


_CURRENCY = Currency.from_str("USDT")


def _instrument(
    *,
    venue: str = "SXBET",
    event_id: str = "event-1",
    event_name: str = "Team A vs Team B",
    home_name: str = "Team A",
    away_name: str = "Team B",
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
        sport_name="Soccer",
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


def _graph(engine: str, instruments: list[CryptoBettingInstrument]) -> OpportunityGraph:
    try:
        graph = OpportunityGraph(MarketMatcher(), engine=engine)
    except ImportError:
        pytest.skip("Rust OpportunityGraphCore is unavailable")
    graph.build(instruments)
    return graph


def _edge_snapshot(graph: OpportunityGraph) -> dict[str, tuple[str, str, str, bool]]:
    return {
        edge_id: (
            edge.hedge_type,
            edge.market_relationship_type,
            f"{edge.confidence:.2f}",
            edge.push_capable,
        )
        for edge_id, edge in graph.edges_by_id.items()
    }


def _quote(instrument: CryptoBettingInstrument, odds: Decimal, ts_event: int = 1_000) -> object:
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
def test_builds_same_market_cross_venue_edges(engine: str) -> None:
    instruments = [
        _instrument(venue="SXBET", outcome="over"),
        _instrument(venue="SXBET", outcome="under"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="over"),
        _instrument(venue="BLACKBET", event_id="event-2", outcome="under"),
    ]

    graph = _graph(engine, instruments)

    assert graph.node_count == 4
    assert graph.edge_count == 4
    assert all(edge.hedge_type == "same_market" for edge in graph.edges_by_id.values())
    assert graph.connected_edge_count(str(instruments[0].id)) == 2


def test_rust_and_python_topology_are_identical_for_common_edges() -> None:
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

    assert _edge_snapshot(rust_graph) == _edge_snapshot(python_graph)


def test_incremental_add_and_duplicate_match_python_fallback() -> None:
    base = [_instrument(outcome="over")]
    under = _instrument(outcome="under")
    python_graph = _graph("python", base)
    rust_graph = _graph("rust", base)

    assert python_graph.add_instrument(under) is True
    assert rust_graph.add_instrument(under) is True
    assert python_graph.add_instrument(under) is False
    assert rust_graph.add_instrument(under) is False
    assert _edge_snapshot(rust_graph) == _edge_snapshot(python_graph)


def test_update_quote_and_evaluate_matches_python_candidates() -> None:
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

    assert python_state == rust_state
    rust_snapshot = sorted(
        (candidate.edge.edge_id, candidate.opportunity.profit_margin)
        for candidate in rust_candidates
    )
    python_snapshot = sorted(
        (candidate.edge.edge_id, candidate.opportunity.profit_margin)
        for candidate in python_candidates
    )
    assert rust_snapshot == python_snapshot
    assert {candidate.updated_node_id for candidate in rust_candidates} == {str(instruments[0].id)}


def test_update_quote_and_scan_fast_returns_primitive_snapshots() -> None:
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

    assert result is not None
    quote_updated, snapshots = result
    assert quote_updated is True
    assert graph.quote_state_count == 2
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot[1] == str(instruments[1].id)
    assert snapshot[2] == str(instruments[0].id)
    assert snapshot[3] == "same_market"
    assert snapshot[4] == 1.0
    assert snapshot[5] == 2.55
    assert snapshot[6] == 2.4
    assert snapshot[7] > 0.0
    assert snapshot[8] == 10_001
    assert snapshot[9] == 10_000
    assert snapshot[10] == "same_market"
    assert snapshot[11] is False


def test_update_quote_and_scan_fast_is_rust_only() -> None:
    instrument = _instrument()
    graph = _graph("python", [instrument])

    assert (
        graph.update_quote_and_scan_fast(
            _quote(instrument, Decimal("2.40")),
            odds=Decimal("2.40"),
            received_ns=20_000,
            min_profit_margin=Decimal("0.01"),
            now_ns=30_000,
        )
        is None
    )


def test_rust_scan_filters_unprofitable_edges_before_decimal_validation() -> None:
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

    assert graph.connected_edge_count(str(instruments[0].id)) > 1
    assert candidates == []


def test_push_capable_edges_are_built_but_not_evaluated() -> None:
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

    assert graph.edge_count == 1
    assert next(iter(graph.edges_by_id.values())).push_capable is True
    assert candidates == []


def test_missing_start_time_ambiguity_matches_python_fallback() -> None:
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

    assert _graph("rust", ambiguous).edge_count == _graph("python", ambiguous).edge_count == 0
    assert _graph("rust", unambiguous).edge_count == _graph("python", unambiguous).edge_count == 1


def test_engine_validation_and_missing_node_paths() -> None:
    instrument = _instrument()
    quote = _quote(instrument, Decimal("2.00"))

    with pytest.raises(ValueError, match="Invalid opportunity graph engine"):
        OpportunityGraph(MarketMatcher(), engine="invalid")

    python_graph = _graph("python", [])
    rust_graph = _graph("rust", [])

    assert python_graph.quote_state_count == 0
    assert python_graph.update_quote(quote, odds=Decimal("2.00"), received_ns=1) is None
    assert python_graph.evaluate_updated_node(
        str(instrument.id),
        min_profit_margin=Decimal("0.01"),
        now_ns=1,
    ) == []
    assert python_graph.update_quote_and_evaluate(
        quote,
        odds=Decimal("2.00"),
        received_ns=1,
        min_profit_margin=Decimal("0.01"),
        now_ns=1,
    ) == (None, [])
    assert rust_graph.update_quote_and_evaluate(
        quote,
        odds=Decimal("2.00"),
        received_ns=1,
        min_profit_margin=Decimal("0.01"),
        now_ns=1,
    ) == (None, [])


def test_python_evaluation_skips_missing_unprofitable_and_push_edges() -> None:
    instruments = [_instrument(outcome="over"), _instrument(outcome="under")]
    graph = _graph("python", instruments)
    graph.update_quote(
        _quote(instruments[0], Decimal("1.80")),
        odds=Decimal("1.80"),
        received_ns=1,
    )

    assert graph.evaluate_updated_node(
        str(instruments[0].id),
        min_profit_margin=Decimal("0.01"),
        now_ns=1,
    ) == []

    graph.update_quote(
        _quote(instruments[1], Decimal("1.80")),
        odds=Decimal("1.80"),
        received_ns=2,
    )
    assert graph.evaluate_updated_node(
        str(instruments[0].id),
        min_profit_margin=Decimal("0.01"),
        now_ns=3,
    ) == []

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

    assert push_graph.evaluate_updated_node(
        str(push_instruments[0].id),
        min_profit_margin=Decimal("0.01"),
        now_ns=4,
    ) == []


def test_node_snapshot_helper_fallbacks() -> None:
    instrument = _instrument()
    node = OpportunityGraph._node_from_instrument(instrument)

    class MinimalInstrument:
        pass

    class BadStartInstrument:
        def parsed_start_time(self):
            return "not-a-datetime"

    payload = OpportunityGraph._node_payload_from_node(node, MinimalInstrument())

    assert payload["event_key_no_time"] == node.canonical_event_key
    assert payload["selection_key"] == node.outcome
    assert payload["start_time_ns"] is None
    assert OpportunityGraph._start_time_ns(BadStartInstrument()) is None
