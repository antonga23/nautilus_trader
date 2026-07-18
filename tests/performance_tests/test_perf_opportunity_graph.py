# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Performance tests for the betting opportunity graph.
# -------------------------------------------------------------------------------------------------
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-R0801, PYL-R0913, PYL-W0212
# pylint: disable=missing-module-docstring,missing-function-docstring,no-name-in-module,protected-access,duplicate-code,too-many-arguments
"""
Performance tests for the betting opportunity graph.
"""

from decimal import Decimal
from typing import Any
from typing import cast

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.data import TestDataStubs


_CURRENCY = Currency.from_str("ZAR")
_VENUES = (Venue("SXBET"), Venue("BLACKBET"))


def _instrument(
    *,
    event_idx: int,
    venue: Venue,
    outcome: str,
    market_name: str = "Total Goals",
    market_type: str = "total_goals",
    params: str = "line=2.5",
) -> CryptoBettingInstrument:
    instrument_kwargs = {
        "venue": venue,
        "event_id": f"event-{event_idx}-{venue.value}",
        "event_name": f"Team {event_idx} A vs Team {event_idx} B",
        "home_name": f"Team {event_idx} A",
        "away_name": f"Team {event_idx} B",
        "sport_name": "Soccer",
        "competition_name": "Benchmark League",
        "market_name": market_name,
        "market_type": market_type,
        "outcome": outcome,
        "side": SelectionSide.BACK,
        "price": 2.4 if outcome == "over" else 2.55,
        "currency": _CURRENCY,
        "params": params,
        "start_time": "2026-03-13T18:00:00Z",
    }
    return CryptoBettingInstrument(**instrument_kwargs)


def _instruments(event_count: int, *, paired: bool = True) -> list[CryptoBettingInstrument]:
    outcomes = ("over", "under") if paired else ("over",)
    return [
        _instrument(event_idx=event_idx, venue=venue, outcome=outcome)
        for event_idx in range(event_count)
        for venue in _VENUES
        for outcome in outcomes
    ]


def _single_event_instruments(venue_count: int) -> list[CryptoBettingInstrument]:
    return [
        _instrument(
            event_idx=0,
            venue=Venue(f"BENCH{venue_idx}"),
            outcome=outcome,
        )
        for venue_idx in range(venue_count)
        for outcome in ("over", "under")
    ]


def _odds(instrument: CryptoBettingInstrument) -> Decimal:
    return Decimal("2.40") if instrument.outcome == "over" else Decimal("2.55")


def _graph(engine: str) -> OpportunityGraph:
    try:
        graph = OpportunityGraph(MarketMatcher(), engine=engine)
    except ImportError:
        pytest.skip("Rust OpportunityGraphCore is unavailable")
    return graph


@pytest.mark.parametrize("event_count", [25, 100, 250])
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_opportunity_graph_build(benchmark, event_count: int, engine: str) -> None:
    instruments = _instruments(event_count)

    def run() -> OpportunityGraph:
        graph = _graph(engine)
        graph.build(instruments)
        return graph

    graph = benchmark.pedantic(run, rounds=1, iterations=1)
    assert graph.edge_count == event_count * 4


@pytest.mark.parametrize("engine", ["python", "rust"])
def test_opportunity_graph_add_instrument(benchmark, engine: str) -> None:
    base_instruments = _instruments(100)
    new_instrument = _instrument(event_idx=100, venue=Venue("SXBET"), outcome="over")

    def run(graph: OpportunityGraph, instrument: CryptoBettingInstrument) -> bool:
        return graph.add_instrument(instrument)

    def setup():
        graph = _graph(engine)
        graph.build(base_instruments)
        return (graph, new_instrument), {}

    assert benchmark.pedantic(run, setup=setup, rounds=10, iterations=1) is True


@pytest.mark.parametrize("profitable", [True, False], ids=["candidate-heavy", "no-candidate"])
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_opportunity_graph_update_and_evaluate(benchmark, profitable: bool, engine: str) -> None:
    instruments = _single_event_instruments(24)
    graph = _graph(engine)
    graph.build(instruments)
    odds_by_outcome = (
        {"over": Decimal("2.40"), "under": Decimal("2.55")}
        if profitable
        else {"over": Decimal("1.80"), "under": Decimal("1.80")}
    )
    ticks = [
        TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=float(odds_by_outcome[instrument.outcome]),
            ask_price=float(odds_by_outcome[instrument.outcome]),
            ts_event=10_000_000_000 + index,
        )
        for index, instrument in enumerate(instruments)
    ]
    for instrument, tick in zip(instruments, ticks, strict=True):
        graph.update_quote(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=11_000_000_000,
        )

    tick = ticks[-1]
    instrument = instruments[-1]

    def run():
        return graph.update_quote_and_evaluate(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=12_000_000_000,
            min_profit_margin=Decimal("0.01"),
            now_ns=12_000_000_000,
        )

    _, candidates = benchmark(run)
    if profitable:
        assert candidates
    else:
        assert not candidates


@pytest.mark.parametrize("profitable", [True, False], ids=["candidate-heavy", "no-candidate"])
def test_opportunity_graph_fast_scan(benchmark, profitable: bool) -> None:
    instruments = _single_event_instruments(24)
    graph = _graph("rust")
    graph.build(instruments)
    odds_by_outcome = (
        {"over": Decimal("2.40"), "under": Decimal("2.55")}
        if profitable
        else {"over": Decimal("1.80"), "under": Decimal("1.80")}
    )
    ticks = [
        TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=float(odds_by_outcome[instrument.outcome]),
            ask_price=float(odds_by_outcome[instrument.outcome]),
            ts_event=10_000_000_000 + index,
        )
        for index, instrument in enumerate(instruments)
    ]
    for instrument, tick in zip(instruments, ticks, strict=True):
        graph.update_quote(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=11_000_000_000,
        )

    tick = ticks[-1]
    instrument = instruments[-1]

    def run():
        return graph.update_quote_and_scan_fast(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=12_000_000_000,
            min_profit_margin=Decimal("0.01"),
            now_ns=12_000_000_000,
        )

    result = benchmark(run)
    assert result is not None
    _, snapshots = result
    if profitable:
        assert snapshots
    else:
        assert not snapshots


def test_opportunity_graph_core_direct_candidate_heavy(benchmark) -> None:
    instruments = _single_event_instruments(24)
    graph = _graph("rust")
    graph.build(instruments)
    odds_by_outcome = {"over": Decimal("2.40"), "under": Decimal("2.55")}
    ticks = [
        TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=float(odds_by_outcome[instrument.outcome]),
            ask_price=float(odds_by_outcome[instrument.outcome]),
            ts_event=10_000_000_000 + index,
        )
        for index, instrument in enumerate(instruments)
    ]
    for instrument, tick in zip(instruments, ticks, strict=True):
        graph.update_quote(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=11_000_000_000,
        )

    tick = ticks[-1]
    instrument = instruments[-1]

    def run():
        return graph._rust_core.update_quote_and_scan_fast(
            str(tick.instrument_id),
            float(odds_by_outcome[instrument.outcome]),
            12_000_000_000,
            int(tick.ts_event),
            0.01,
            12_000_000_000,
        )

    assert benchmark(run)


@pytest.mark.parametrize("duplicate_heavy", [False, True], ids=["accepted", "duplicate-heavy"])
def test_betting_arbitrage_strategy_fast_scan(benchmark, duplicate_heavy: bool) -> None:
    instruments = _single_event_instruments(24)
    strategy = BettingArbitrageStrategy(
        config=BettingArbitrageConfig(
            min_profit_margin=Decimal("0.01"),
            enabled_venues=frozenset(instrument.id.venue.value for instrument in instruments),
            opportunity_log_manual_instructions=False,
        ),
    )
    strategy_any = cast(Any, strategy)
    strategy_any._handle_arbitrage_opportunity = lambda opportunity, diagnostics=None: None
    strategy_any._log_fast_arbitrage_snapshot = lambda *args, **kwargs: None
    strategy_any._log_arbitrage_summary = lambda *args, **kwargs: None
    graph = strategy._opportunity_graph
    graph.build(instruments)
    odds_by_outcome = {"over": Decimal("2.40"), "under": Decimal("2.55")}
    ticks = [
        TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=float(odds_by_outcome[instrument.outcome]),
            ask_price=float(odds_by_outcome[instrument.outcome]),
            ts_event=10_000_000_000 + index,
        )
        for index, instrument in enumerate(instruments)
    ]
    for instrument, tick in zip(instruments, ticks, strict=True):
        strategy._latest_quotes[str(instrument.id)] = tick
        graph.update_quote(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=11_000_000_000,
        )

    tick = ticks[-1]
    instrument = instruments[-1]
    result = graph.update_quote_and_scan_fast(
        tick,
        odds=odds_by_outcome[instrument.outcome],
        received_ns=12_000_000_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=12_000_000_000,
    )
    if result is None:
        pytest.skip("Rust OpportunityGraphCore is unavailable")
    assert result is not None
    _, initial_snapshots = result
    duplicate_pairs = {
        strategy._canonical_pair_id(
            graph.nodes_by_id[source_node_id].instrument,
            graph.nodes_by_id[target_node_id].instrument,
        )
        for _, source_node_id, target_node_id, *_ in initial_snapshots
    }

    def run() -> int:
        strategy._seen_opportunity_pairs = duplicate_pairs.copy() if duplicate_heavy else set()
        result = graph.update_quote_and_scan_fast(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=12_000_000_000,
            min_profit_margin=Decimal("0.01"),
            now_ns=12_000_000_000,
        )
        assert result is not None
        _, snapshots = result
        strategy._handle_fast_opportunity_snapshots(snapshots, 12_000_000_000)
        return len(snapshots)

    assert benchmark(run) == len(initial_snapshots)


@pytest.mark.parametrize("duplicate_heavy", [False, True], ids=["accepted", "duplicate-heavy"])
def test_betting_arbitrage_strategy_public_scan(benchmark, duplicate_heavy: bool) -> None:
    instruments = _single_event_instruments(24)
    strategy = BettingArbitrageStrategy(
        config=BettingArbitrageConfig(
            min_profit_margin=Decimal("0.01"),
            enabled_venues=frozenset(instrument.id.venue.value for instrument in instruments),
            opportunity_log_manual_instructions=False,
        ),
    )
    strategy_any = cast(Any, strategy)
    strategy_any._handle_arbitrage_opportunity = lambda opportunity, diagnostics=None: None
    strategy_any._log_arbitrage_summary = lambda *args, **kwargs: None
    graph = strategy._opportunity_graph
    graph.build(instruments)
    odds_by_outcome = {"over": Decimal("2.40"), "under": Decimal("2.55")}
    ticks = [
        TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=float(odds_by_outcome[instrument.outcome]),
            ask_price=float(odds_by_outcome[instrument.outcome]),
            ts_event=10_000_000_000 + index,
        )
        for index, instrument in enumerate(instruments)
    ]
    for instrument, tick in zip(instruments, ticks, strict=True):
        strategy._latest_quotes[str(instrument.id)] = tick
        graph.update_quote(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=11_000_000_000,
        )

    tick = ticks[-1]
    instrument = instruments[-1]
    _, initial_candidates = graph.update_quote_and_evaluate(
        tick,
        odds=odds_by_outcome[instrument.outcome],
        received_ns=12_000_000_000,
        min_profit_margin=Decimal("0.01"),
        now_ns=12_000_000_000,
    )
    duplicate_pairs = {
        strategy._canonical_pair_id(
            candidate.opportunity.instrument_a,
            candidate.opportunity.instrument_b,
        )
        for candidate in initial_candidates
    }

    def run() -> int:
        strategy._seen_opportunity_pairs = duplicate_pairs.copy() if duplicate_heavy else set()
        _, candidates = graph.update_quote_and_evaluate(
            tick,
            odds=odds_by_outcome[instrument.outcome],
            received_ns=12_000_000_000,
            min_profit_margin=Decimal("0.01"),
            now_ns=12_000_000_000,
        )
        for candidate in candidates:
            strategy._handle_opportunity_candidate(candidate, 12_000_000_000)
        return len(candidates)

    assert benchmark(run) == len(initial_candidates)


def test_opportunity_graph_incremental_add_remove_at_scale(benchmark) -> None:
    # 500 events x 2 venues x 2 outcomes = 2,000 instruments. The add path must stay
    # bucket-local (edge_snapshots_for_node), so this benchmark should not grow with
    # total edge count the way a full edge re-sync per add did.
    graph = _graph("rust")
    graph.build(_instruments(500))
    assert graph.node_count == 2_000
    new_instrument = _instrument(event_idx=500, venue=Venue("SXBET"), outcome="over")
    new_node_id = str(new_instrument.id)

    def run() -> None:
        assert graph.add_instrument(new_instrument) is True
        assert graph.remove_instrument(new_node_id) is True

    benchmark(run)
    assert graph.node_count == 2_000
