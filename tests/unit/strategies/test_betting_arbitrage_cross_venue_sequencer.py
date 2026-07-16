# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the event-gated cross-venue leg sequencer.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module,protected-access,too-many-arguments
"""
Adversarial proofs for the event-gated cross-venue leg sequencer.

``Strategy.submit_order`` is fire-and-forget (it enqueues a command on the msgbus and
returns; the real submit runs asynchronously in the exec client), so merely reordering
the two submit calls would NOT sequence the legs. The sequencer is an event-driven state
machine: for a cross-venue arb it submits only the un-cancelable CLOUDBET anchor first,
holds the second (SX) leg, and submits it from ``on_order_filled`` only once the anchor's
terminal fill arrives and the arb still holds on fresh quotes. An anchor terminal
non-fill aborts the sequence with zero second-leg exposure; a same-venue arb keeps the
existing simultaneous two-leg behaviour; and the feature is default OFF.

"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs


def ensure(condition: bool) -> None:  # skipcq
    if not condition:
        raise AssertionError


def _betting_instrument(
    *,
    venue: str,
    outcome: str,
    currency: str = "USDC",
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="Test League",
        market_name="total_goals",
        market_type="total_goals",
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str(currency),
        params="",
        trading_status="ACTIVE",
    )


class _Harness:
    """
    A registered, armed strategy wired so ``submit_order`` records and caches each
    order.
    """

    def __init__(
        self,
        *,
        sequential: bool,
        armed: bool = True,
        venue_a: str = "CLOUDBET",
        venue_b: str = "SXBET",
        is_same_venue: bool = False,
        anchor_venue: str | None = None,
        real_gates: bool = False,
    ) -> None:
        self.cache = TestComponentStubs.cache()
        self.instrument_a = _betting_instrument(venue=venue_a, outcome="over")
        self.instrument_b = _betting_instrument(venue=venue_b, outcome="under")
        self.cache.add_instrument(self.instrument_a)
        self.cache.add_instrument(self.instrument_b)

        self.strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({venue_a, venue_b}),
                auto_execute=True,
                live_execution_armed=armed,
                cross_venue_sequential_execution=sequential,
                cross_venue_anchor_venue=anchor_venue,
                unwind_filled_leg_enabled=True,
                max_total_stake=Decimal(25),
            ),
        )
        self.strategy.register(
            trader_id=TraderId("TESTER-SEQ"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=self.cache,
            clock=TestComponentStubs.clock(),
        )

        self.submitted: list = []

        def _submit(order):
            self.cache.add_order(order)
            self.submitted.append(order)

        self.strategy.submit_order = _submit
        if not real_gates:
            self.strategy._live_execution_block_reasons_for = Mock(  # type: ignore[method-assign]
                return_value=[],
            )
            self.strategy._live_execution_refresh_opportunity = Mock(  # type: ignore[method-assign]
                side_effect=lambda opp: (opp, []),
            )

        self.opportunity = ArbitrageOpportunity(
            instrument_a=self.instrument_a,
            instrument_b=self.instrument_b,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=is_same_venue,
            match_type="same_market" if is_same_venue else "cross_venue",
        )

    def execute(self) -> list[str]:
        return self.strategy._execute_arbitrage(self.opportunity)

    def submitted_venues(self) -> list[str]:
        return [str(order.instrument_id.venue).upper() for order in self.submitted]

    def fill_terminally(self, order) -> object:
        order.apply(TestEventStubs.order_submitted(order))
        order.apply(TestEventStubs.order_accepted(order))
        instrument = self.cache.instrument(order.instrument_id)
        fill = TestEventStubs.order_filled(
            order,
            instrument=instrument,
            last_qty=order.quantity,
            last_px=order.price,
        )
        order.apply(fill)
        self.cache.update_order(order)
        return fill

    def stats(self) -> dict:
        return self.strategy.get_stats()["live_execution"]


def _terminal_event(order, venue: str):
    return SimpleNamespace(
        client_order_id=order.client_order_id,
        instrument_id=SimpleNamespace(venue=Venue(venue)),
    )


def test_sequencer_submits_only_cloudbet_anchor_then_sx_after_terminal_fill():  # skipcq
    # (a) Only the CLOUDBET anchor is submitted initially; the SX leg is submitted ONLY
    # after the anchor's terminal OrderFilled. Ordering is asserted via the event
    # sequence: one submission before the fill, two after.
    h = _Harness(sequential=True)

    reasons = h.execute()

    ensure(reasons == [])
    ensure(len(h.submitted) == 1)
    ensure(h.submitted_venues() == ["CLOUDBET"])
    anchor = h.submitted[0]
    anchor_id = str(anchor.client_order_id)
    ensure(anchor_id in h.strategy._pending_cross_venue_sequences)
    ensure(h.stats()["cross_venue_sequences_opened"] == 1)
    ensure(h.stats()["cross_venue_sequences_completed"] == 0)

    fill = h.fill_terminally(anchor)
    h.strategy.on_order_filled(fill)

    ensure(len(h.submitted) == 2)
    ensure(h.submitted_venues() == ["CLOUDBET", "SXBET"])
    ensure(anchor_id not in h.strategy._pending_cross_venue_sequences)
    ensure(h.stats()["cross_venue_sequences_completed"] == 1)
    ensure(h.strategy._opportunities_executed == 1)


def test_sequencer_anchor_rejected_never_submits_second_leg():  # skipcq
    # (b) Anchor terminal non-fill (rejected): the SX leg is NEVER submitted (zero
    # second-leg orders) and no exposure remains.
    h = _Harness(sequential=True)

    h.execute()
    ensure(len(h.submitted) == 1)
    anchor = h.submitted[0]

    h.strategy.on_order_rejected(_terminal_event(anchor, "CLOUDBET"))

    ensure(len(h.submitted) == 1)  # no second leg
    ensure(str(anchor.client_order_id) not in h.strategy._pending_cross_venue_sequences)
    ensure(h.stats()["cross_venue_sequences_aborted"] == 1)
    ensure(h.stats()["cross_venue_sequences_completed"] == 0)
    ensure(h.strategy._opportunities_executed == 0)


def test_sequencer_second_leg_adverse_move_flattens_naked_anchor():  # skipcq
    # (c) The second-leg price moved adversely by the time the anchor filled: the SX leg
    # is NOT placed and the now-naked anchor is routed to the existing flatten path.
    h = _Harness(sequential=True)
    # First refresh (inside _execute_arbitrage) passes so the anchor is placed; the
    # second refresh (inside the commit on the anchor fill) reports an adverse move.
    h.strategy._live_execution_refresh_opportunity = Mock(
        side_effect=[
            (h.opportunity, []),
            (h.opportunity, ["final_below_min_profit_margin"]),
        ],
    )
    flatten = Mock()
    h.strategy._handle_naked_filled_leg = flatten

    h.execute()
    ensure(len(h.submitted) == 1)
    anchor = h.submitted[0]

    fill = h.fill_terminally(anchor)
    h.strategy.on_order_filled(fill)

    # SX leg never placed; only the anchor was ever submitted.
    ensure(h.submitted_venues() == ["CLOUDBET"])
    ensure(h.stats()["cross_venue_second_leg_blocked"] == 1)
    ensure(h.stats()["cross_venue_sequences_completed"] == 0)
    flatten.assert_called_once()
    flattened_order = flatten.call_args.args[0]
    ensure(str(flattened_order.client_order_id) == str(anchor.client_order_id))


def test_same_venue_arb_submits_both_legs_simultaneously_when_sequencer_on():  # skipcq
    # (d) A same-venue arb does not use the sequencer even when it is enabled: both legs
    # are submitted simultaneously, exactly as before.
    h = _Harness(sequential=True, venue_a="SXBET", venue_b="SXBET", is_same_venue=True)

    reasons = h.execute()

    ensure(reasons == [])
    ensure(len(h.submitted) == 2)
    ensure(len(h.strategy._pending_cross_venue_sequences) == 0)
    ensure(h.stats()["cross_venue_sequences_opened"] == 0)


def test_not_armed_submits_nothing_even_with_sequencer_on():  # skipcq
    # (e) Validation mode / not armed: the live gate stack blocks before any order is
    # constructed, so nothing is submitted and no sequence is opened.
    h = _Harness(sequential=True, armed=False, real_gates=True)

    reasons = h.execute()

    ensure(reasons != [])
    ensure("manifest_not_live_armed" in reasons)
    ensure(len(h.submitted) == 0)
    ensure(len(h.strategy._pending_cross_venue_sequences) == 0)
    ensure(h.stats()["cross_venue_sequences_opened"] == 0)


def test_sequencer_off_default_submits_both_legs_simultaneously():  # skipcq
    # (f) Sequencer OFF (default): a cross-venue arb submits both legs at once, identical
    # to the pre-existing behaviour.
    h = _Harness(sequential=False)

    reasons = h.execute()

    ensure(reasons == [])
    ensure(len(h.submitted) == 2)
    ensure(h.submitted_venues() == ["CLOUDBET", "SXBET"])
    ensure(len(h.strategy._pending_cross_venue_sequences) == 0)
    ensure(h.stats()["cross_venue_sequences_opened"] == 0)
    ensure(h.strategy._opportunities_executed == 1)


def test_anchor_selection_falls_back_to_configured_venue_without_cloudbet():  # skipcq
    # No CLOUDBET leg: the configured anchor venue is placed first and held-second is the
    # other venue.
    h = _Harness(
        sequential=True,
        venue_a="SXBET",
        venue_b="POLYMARKET",
        anchor_venue="POLYMARKET",
    )

    h.execute()

    ensure(len(h.submitted) == 1)
    ensure(h.submitted_venues() == ["POLYMARKET"])
    seq = next(iter(h.strategy._pending_cross_venue_sequences.values()))
    ensure(seq.second_venue == "SXBET")
