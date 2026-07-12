# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for realizing arbitrage P&L from bet settlement records.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module,protected-access
"""
Proofs for the settlement leg of the fill -> position -> settlement -> realized P&L
loop.

Execution clients publish one ``BetSettlement`` per graded order on the message bus; the
strategy maps each record onto its tracked arbitrage pair and settles the pair exactly
once: a WON leg fixes the winning outcome immediately (legs back mutually exclusive
outcomes, so at most one can win), anything else waits for every leg to grade.

"""

from decimal import Decimal

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.settlement import BET_SETTLEMENTS_TOPIC
from nautilus_trader.adapters.betting.common.settlement import BetSettlement
from nautilus_trader.adapters.betting.common.settlement import SettlementResult
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


def ensure(condition: bool) -> None:  # skipcq
    if not condition:
        raise AssertionError


def _betting_instrument(*, venue: str, outcome: str) -> CryptoBettingInstrument:
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
        currency=Currency.from_str("USDC"),
        params="",
        trading_status="ACTIVE",
    )


class _Harness:
    """
    A registered strategy with one tracked SX.bet arbitrage pair (both legs filled).
    """

    def __init__(self, *, fill_second_leg: bool = True) -> None:
        self.cache = TestComponentStubs.cache()
        self.msgbus = TestComponentStubs.msgbus()
        self.over_instrument = _betting_instrument(venue="SXBET", outcome="over")
        self.under_instrument = _betting_instrument(venue="SXBET", outcome="under")
        self.cache.add_instrument(self.over_instrument)
        self.cache.add_instrument(self.under_instrument)

        self.strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"SXBET"})),
        )
        self.strategy.register(
            trader_id=TraderId("TESTER-SETTLE"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=self.msgbus,
            cache=self.cache,
            clock=TestComponentStubs.clock(),
        )

        self.over_leg_id = "O-OVER-1"
        self.under_leg_id = "O-UNDER-1"
        self.strategy._arb_leg_siblings[self.over_leg_id] = self.under_leg_id
        self.strategy._arb_leg_siblings[self.under_leg_id] = self.over_leg_id
        tracker = self.strategy._arb_position_tracker
        # Backing 5 @ 2.10 on both mutually exclusive outcomes locks in +0.50 either way.
        tracker.record_fill(
            self.over_leg_id,
            self.over_instrument.outcome,
            "BUY",
            Decimal("2.10"),
            Decimal(5),
            sibling_id=self.under_leg_id,
        )
        if fill_second_leg:
            tracker.record_fill(
                self.under_leg_id,
                self.under_instrument.outcome,
                "BUY",
                Decimal("2.10"),
                Decimal(5),
                sibling_id=self.over_leg_id,
            )
        self.pair = tracker.pair_for_leg(self.over_leg_id)

    def settle(self, leg_id: str, result: SettlementResult) -> None:
        self.strategy._on_bet_settlement(
            BetSettlement(
                venue="SXBET",
                client_order_id=leg_id,
                instrument_id=None,
                result=result,
                settle_value=None,
                ts_event=0,
            ),
        )

    def tracker_stats(self) -> dict:
        return self.strategy.get_stats()["arb_position_tracker"]


def test_won_leg_settles_pair_immediately_with_pre_settlement_payoff():  # skipcq
    h = _Harness()
    expected = h.pair.outcome_pnls()[h.over_instrument.outcome]

    h.settle(h.over_leg_id, SettlementResult.WON)

    ensure(h.pair.settled is True)
    ensure(h.pair.winning_outcome == h.over_instrument.outcome)
    ensure(h.pair.realized_pnl == expected)
    ensure(h.pair.realized_pnl == Decimal("0.50"))
    stats = h.tracker_stats()
    ensure(stats["realized_pnl"] == "0.50")
    ensure(stats["pairs_settled"] == 1)
    ensure(stats["pairs_open"] == 0)


def test_settlements_are_idempotent_per_pair():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.WON)
    realized = h.pair.realized_pnl
    # The sibling's own grading and venue re-serves must not re-settle the pair.
    h.settle(h.under_leg_id, SettlementResult.LOST)
    h.settle(h.over_leg_id, SettlementResult.WON)

    ensure(h.pair.realized_pnl == realized)
    stats = h.tracker_stats()
    ensure(stats["pairs_settled"] == 1)
    ensure(stats["settlements_received"] == 3)


def test_lost_leg_waits_for_sibling_grading_then_settles_once():  # skipcq
    h = _Harness()
    expected = h.pair.outcome_pnls()[h.under_instrument.outcome]

    h.settle(h.over_leg_id, SettlementResult.LOST)
    ensure(h.pair.settled is False)  # sibling not graded yet

    h.settle(h.under_leg_id, SettlementResult.WON)

    ensure(h.pair.settled is True)
    ensure(h.pair.winning_outcome == h.under_instrument.outcome)
    ensure(h.pair.realized_pnl == expected)
    ensure(h.tracker_stats()["pairs_settled"] == 1)


def test_all_legs_lost_realizes_complement_scenario():  # skipcq
    h = _Harness()
    lose_payoffs = sum(
        (leg.lose_payoff for leg in h.pair.filled_legs),
        Decimal(0),
    )

    h.settle(h.over_leg_id, SettlementResult.LOST)
    h.settle(h.under_leg_id, SettlementResult.LOST)

    ensure(h.pair.settled is True)
    ensure(h.pair.realized_pnl == lose_payoffs)
    ensure(h.pair.realized_pnl == Decimal(-10))


def test_void_legs_realize_zero():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.VOID)
    ensure(h.pair.settled is False)
    h.settle(h.under_leg_id, SettlementResult.VOID)

    ensure(h.pair.settled is True)
    ensure(h.pair.void is True)
    ensure(h.pair.realized_pnl == Decimal(0))
    ensure(h.tracker_stats()["realized_pnl"] == "0")


def test_naked_single_leg_settles_from_its_own_grading():  # skipcq
    h = _Harness(fill_second_leg=False)
    win_payoff = h.pair.legs[h.over_leg_id].win_payoff

    h.settle(h.over_leg_id, SettlementResult.WON)

    ensure(h.pair.settled is True)
    ensure(h.pair.realized_pnl == win_payoff)


def test_naked_single_leg_lost_realizes_lose_payoff():  # skipcq
    h = _Harness(fill_second_leg=False)
    lose_payoff = h.pair.legs[h.over_leg_id].lose_payoff

    h.settle(h.over_leg_id, SettlementResult.LOST)

    ensure(h.pair.settled is True)
    ensure(h.pair.realized_pnl == lose_payoff)
    ensure(h.pair.realized_pnl == Decimal(-5))


def test_untracked_leg_is_counted_and_ignored():  # skipcq
    h = _Harness()

    h.settle("O-UNKNOWN-1", SettlementResult.WON)

    ensure(h.pair.settled is False)
    stats = h.tracker_stats()
    ensure(stats["settlements_unmatched"] == 1)
    ensure(stats["pairs_settled"] == 0)


def test_mixed_lost_void_gradings_leave_pair_open():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.LOST)
    h.settle(h.under_leg_id, SettlementResult.VOID)

    ensure(h.pair.settled is False)
    ensure(h.tracker_stats()["pairs_settled"] == 0)


def test_on_start_subscribes_settlement_topic():  # skipcq
    h = _Harness()
    expected = h.pair.outcome_pnls()[h.over_instrument.outcome]
    h.strategy.on_start()

    h.msgbus.publish(
        topic=BET_SETTLEMENTS_TOPIC,
        msg=BetSettlement(
            venue="SXBET",
            client_order_id=h.over_leg_id,
            instrument_id=str(h.over_instrument.id),
            result=SettlementResult.WON,
            settle_value=None,
            ts_event=0,
        ),
    )

    ensure(h.pair.settled is True)
    ensure(h.pair.realized_pnl == expected)

    h.strategy.on_stop()
    h.msgbus.publish(
        topic=BET_SETTLEMENTS_TOPIC,
        msg=BetSettlement(
            venue="SXBET",
            client_order_id=h.under_leg_id,
            instrument_id=None,
            result=SettlementResult.LOST,
            settle_value=None,
            ts_event=0,
        ),
    )
    ensure(h.tracker_stats()["settlements_received"] == 1)
