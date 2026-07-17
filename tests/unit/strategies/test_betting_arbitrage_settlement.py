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
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
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

    def __init__(
        self,
        *,
        fill_second_leg: bool = True,
        over_venue: str = "SXBET",
        under_venue: str = "SXBET",
        **config_kwargs: object,
    ) -> None:
        self.cache = TestComponentStubs.cache()
        self.msgbus = TestComponentStubs.msgbus()
        self.over_venue = over_venue
        self.under_venue = under_venue
        self.over_instrument = _betting_instrument(venue=over_venue, outcome="over")
        self.under_instrument = _betting_instrument(venue=under_venue, outcome="under")
        self.cache.add_instrument(self.over_instrument)
        self.cache.add_instrument(self.under_instrument)

        self.strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({over_venue, under_venue}),
                **config_kwargs,  # type: ignore[arg-type]
            ),
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
            venue=over_venue,
        )
        if fill_second_leg:
            tracker.record_fill(
                self.under_leg_id,
                self.under_instrument.outcome,
                "BUY",
                Decimal("2.10"),
                Decimal(5),
                sibling_id=self.over_leg_id,
                venue=under_venue,
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
    # Both legs are SX.bet: the +0.50 gross floor realizes net of the 4% winning-profit
    # commission on the winning leg -> 5.50*0.96 - 5.00 = 5.28 - 5.00 = 0.28.
    ensure(h.pair.realized_pnl == Decimal("0.28"))
    stats = h.tracker_stats()
    ensure(stats["realized_pnl"] == "0.2800")
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


def test_middle_push_realizes_zero_and_leaves_kill_switch_untripped():  # skipcq
    # (f) a middle's shared push state refunds both stakes: realized P&L is exactly zero
    # with no commission, and the daily-loss kill-switch counter must stay at zero (a
    # break-even is not a loss).
    h = _Harness(
        execute_void_compatible_middles=True,
        min_middle_profit_margin=Decimal("0.05"),
    )

    h.settle(h.over_leg_id, SettlementResult.VOID)
    h.settle(h.under_leg_id, SettlementResult.VOID)

    ensure(h.pair.settled is True)
    ensure(h.pair.void is True)
    ensure(h.pair.realized_pnl == Decimal(0))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))
    ensure(h.strategy._live_execution_kill_switch_active() is False)


def test_middle_decisive_win_realizes_margin_net_of_sxbet_commission():  # skipcq
    # (f) a decisive state wins the whole edge; the winning SX.bet leg pays the 4%
    # net-winnings commission and the losing leg pays no fee.
    h = _Harness(
        execute_void_compatible_middles=True,
        min_middle_profit_margin=Decimal("0.05"),
    )
    expected = h.pair.outcome_pnls()[h.over_instrument.outcome]

    h.settle(h.over_leg_id, SettlementResult.WON)

    ensure(h.pair.settled is True)
    ensure(h.pair.winning_outcome == h.over_instrument.outcome)
    ensure(h.pair.realized_pnl == expected)
    # 5.50 gross floor -> 5.50*0.96 net of the 4% winning commission, minus the 5.00
    # losing leg's stake = 0.28.
    ensure(h.pair.realized_pnl == Decimal("0.28"))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


def test_naked_single_leg_settles_from_its_own_grading():  # skipcq
    h = _Harness(fill_second_leg=False)
    win_payoff = h.pair.legs[h.over_leg_id].win_payoff

    h.settle(h.over_leg_id, SettlementResult.WON)

    ensure(h.pair.settled is True)
    # The SX.bet leg realizes its winning payoff net of the 4% winning-profit commission.
    ensure(h.pair.realized_pnl == win_payoff * (Decimal(1) - Decimal("0.04")))
    ensure(h.pair.realized_pnl == Decimal("5.28"))


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


def _cap_block_reasons(h: _Harness, stake: Decimal = Decimal(5)) -> list[str]:
    """
    Run the live-execution cap gate against the harness pair's own instruments.

    The daily-loss gate reads ``_live_execution_realized_loss``; both legs settle in USDC,
    so the stake conversions are 1:1 and the only cap under test is ``max_daily_loss``.

    """
    same_venue = h.over_venue == h.under_venue
    opportunity = ArbitrageOpportunity(
        instrument_a=h.over_instrument,
        instrument_b=h.under_instrument,
        probability_a=Decimal("0.45"),
        probability_b=Decimal("0.45"),
        total_probability=Decimal("0.90"),
        profit_margin=Decimal("0.11"),
        odds_a=Decimal("2.10"),
        odds_b=Decimal("2.10"),
        is_same_venue=same_venue,
        match_type="same_market" if same_venue else "cross_venue",
    )
    return h.strategy._live_execution_cap_block_reasons(opportunity, stake, stake)


# --- FIX 1: realized loss feeds the daily-loss kill switch --------------------------------


def test_realized_loss_accumulates_and_trips_daily_loss_gate():  # skipcq
    h = _Harness(max_daily_loss=Decimal(5))
    ensure("max_daily_loss_exceeded" not in _cap_block_reasons(h))

    # Both backs lose their stake: lose_payoff = -5 each, so realized = -10 (USDC base).
    h.settle(h.over_leg_id, SettlementResult.LOST)
    h.settle(h.under_leg_id, SettlementResult.LOST)

    ensure(h.pair.realized_pnl == Decimal(-10))
    ensure(h.strategy._live_execution_realized_loss == Decimal(10))
    ensure(h.strategy.get_stats()["live_execution"]["realized_loss"] == "10")
    ensure("max_daily_loss_exceeded" in _cap_block_reasons(h))


def test_realized_win_does_not_increase_loss_counter():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.WON)  # +0.28 net of SX.bet 4% commission

    ensure(h.pair.realized_pnl == Decimal("0.28"))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


def test_void_pair_does_not_increase_loss_counter():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.VOID)
    h.settle(h.under_leg_id, SettlementResult.VOID)

    ensure(h.pair.void is True)
    ensure(h.pair.realized_pnl == Decimal(0))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


def test_realized_loss_not_double_counted_across_repeated_settlements():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.LOST)
    h.settle(h.under_leg_id, SettlementResult.LOST)
    ensure(h.strategy._live_execution_realized_loss == Decimal(10))

    # Re-serves of the sibling grading (and the venue re-publishing) must not re-add loss.
    h.settle(h.under_leg_id, SettlementResult.LOST)
    h.settle(h.over_leg_id, SettlementResult.LOST)

    ensure(h.strategy._live_execution_realized_loss == Decimal(10))
    ensure(h.tracker_stats()["pairs_settled"] == 1)


# --- FIX 2: cross-venue pairs disable the WON shortcut ------------------------------------


def test_cross_venue_won_leg_does_not_settle_pair_immediately():  # skipcq
    h = _Harness(over_venue="CLOUDBET", under_venue="SXBET")
    ensure(h.pair.is_cross_venue is True)

    h.settle(h.over_leg_id, SettlementResult.WON)

    ensure(h.pair.settled is False)  # waits for the independent sibling venue to grade
    ensure(h.tracker_stats()["pairs_settled"] == 0)


def test_cross_venue_won_then_void_refunds_sibling_rather_than_full_loss():  # skipcq
    h = _Harness(over_venue="CLOUDBET", under_venue="SXBET")

    h.settle(h.over_leg_id, SettlementResult.WON)
    h.settle(h.under_leg_id, SettlementResult.VOID)

    ensure(h.pair.settled is True)
    ensure(h.pair.void is False)
    # WON leg +5.50, VOID leg refunds its stake -> 0. NOT the shortcut's +5.50 + (-5.00).
    ensure(h.pair.realized_pnl == Decimal("5.50"))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


def test_cross_venue_both_graded_normally_realizes_arb_floor():  # skipcq
    h = _Harness(over_venue="CLOUDBET", under_venue="SXBET")

    h.settle(h.over_leg_id, SettlementResult.WON)
    h.settle(h.under_leg_id, SettlementResult.LOST)

    ensure(h.pair.settled is True)
    # WON +5.50, LOST -5.00 -> +0.50, the locked arbitrage floor.
    ensure(h.pair.realized_pnl == Decimal("0.50"))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


def test_cross_venue_both_lost_accumulates_loss_into_gate():  # skipcq
    h = _Harness(over_venue="CLOUDBET", under_venue="SXBET", max_daily_loss=Decimal(5))

    h.settle(h.over_leg_id, SettlementResult.LOST)
    h.settle(h.under_leg_id, SettlementResult.LOST)

    ensure(h.pair.realized_pnl == Decimal(-10))
    ensure(h.strategy._live_execution_realized_loss == Decimal(10))
    ensure("max_daily_loss_exceeded" in _cap_block_reasons(h))


def test_same_venue_won_shortcut_unchanged():  # skipcq
    h = _Harness()  # both legs on SXBET
    ensure(h.pair.is_cross_venue is False)

    h.settle(h.over_leg_id, SettlementResult.WON)

    ensure(h.pair.settled is True)  # one WON event settles immediately, as before
    ensure(h.pair.winning_outcome == h.over_instrument.outcome)
    # Settlement path unchanged; the payoff is now net of the SX.bet 4% commission.
    ensure(h.pair.realized_pnl == Decimal("0.28"))


# --- B3: Asian half-line (HALF_WON / HALF_LOST) and PUSH per-leg settlement ---------------
#
#   Both harness legs BACK 2.10 stake 5: win_payoff = 5*1.10 = 5.50 ; lose_payoff = -5.00.
#     HALF_WON  = 5.50 / 2 = 2.75     HALF_LOST = -5.00 / 2 = -2.50     PUSH = 0
#   A HALF / PUSH grading breaks the single-winning-selection joint model even on one venue,
#   so the pair realizes from each leg's own per-leg payoff instead of the old log-only else.


def test_same_venue_half_won_void_realizes_per_leg_not_log_only():  # skipcq
    h = _Harness()  # both legs SXBET
    ensure(h.pair.is_cross_venue is False)

    h.settle(h.over_leg_id, SettlementResult.HALF_WON)
    ensure(h.pair.settled is False)  # HALF is not the WON shortcut; waits for the sibling

    h.settle(h.under_leg_id, SettlementResult.VOID)

    ensure(h.pair.settled is True)  # realized per-leg, NOT left open on a log-only branch
    ensure(h.pair.void is False)
    # HALF_WON 0.5*5.50 = 2.75 gross, net of the SX.bet 4% commission -> 2.75*0.96 = 2.64 ;
    # VOID refunds its stake -> 0.
    ensure(h.pair.realized_pnl == Decimal("2.64"))
    ensure(h.tracker_stats()["pairs_settled"] == 1)


def test_same_venue_half_won_and_half_lost_realizes_sum_of_halves():  # skipcq
    h = _Harness()  # both legs SXBET, one quarter-ball market

    h.settle(h.over_leg_id, SettlementResult.HALF_WON)
    h.settle(h.under_leg_id, SettlementResult.HALF_LOST)

    ensure(h.pair.settled is True)
    # HALF_WON 2.75 net of the SX.bet 4% commission -> 2.64 ; HALF_LOST -2.50 (no commission
    # on a loss) -> 2.64 - 2.50 = 0.14.
    ensure(h.pair.realized_pnl == Decimal("0.14"))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


def test_same_venue_all_push_realizes_zero_and_void_flag():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.PUSH)
    ensure(h.pair.settled is False)
    h.settle(h.under_leg_id, SettlementResult.PUSH)

    ensure(h.pair.settled is True)
    ensure(h.pair.void is True)  # every stake refunded, economically a void
    ensure(h.pair.realized_pnl == Decimal(0))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


def test_same_venue_half_lost_feeds_daily_loss_gate():  # skipcq
    h = _Harness(max_daily_loss=Decimal(2))

    # HALF_LOST -2.50 + PUSH 0 -> realized -2.50, a genuine loss into the daily-loss gate.
    h.settle(h.over_leg_id, SettlementResult.HALF_LOST)
    h.settle(h.under_leg_id, SettlementResult.PUSH)

    ensure(h.pair.realized_pnl == Decimal("-2.50"))
    ensure(h.strategy._live_execution_realized_loss == Decimal("2.50"))
    ensure("max_daily_loss_exceeded" in _cap_block_reasons(h))


def test_cross_venue_half_won_lost_realizes_per_leg():  # skipcq
    h = _Harness(over_venue="CLOUDBET", under_venue="SXBET")
    ensure(h.pair.is_cross_venue is True)

    h.settle(h.over_leg_id, SettlementResult.HALF_WON)
    ensure(h.pair.settled is False)  # independent sibling venue not graded yet

    h.settle(h.under_leg_id, SettlementResult.LOST)

    ensure(h.pair.settled is True)
    # CLOUDBET HALF_WON 0.5*5.50 = 2.75 (no commission) ; SX.bet LOST -5.00 -> -2.25.
    ensure(h.pair.realized_pnl == Decimal("-2.25"))
    ensure(h.strategy._live_execution_realized_loss == Decimal("2.25"))


# --- B4: SX.bet winning-profit commission folded into realized P&L ------------------------
#
#   SX.bet charges 4% on the winning leg's net profit. The strategy config carries the
#   venue -> rate map (defaulting to 4% SX.bet) and threads it into the position tracker, so
#   a winning SX.bet leg realizes net of commission and that net figure feeds the daily-loss
#   kill switch. Harness legs BACK 2.10 stake 5: win_payoff = 5.50, commission 4% = 0.22.


def test_config_default_winning_commission_reduces_realized_win():  # skipcq
    h = _Harness()  # both legs SX.bet, default config -> 4% winning-profit commission

    h.settle(h.over_leg_id, SettlementResult.WON)

    # WON 5.50 net of 4% -> 5.28 ; sibling booked at its -5.00 lose payoff -> +0.28.
    ensure(h.pair.realized_pnl == Decimal("0.28"))


def test_config_override_winning_commission_rate_is_threaded_to_tracker():  # skipcq
    h = _Harness(venue_winning_profit_fee_rates={"SXBET": Decimal("0.02")})

    h.settle(h.over_leg_id, SettlementResult.WON)

    # Overridden to 2%: WON 5.50*0.98 = 5.39 ; sibling -5.00 -> +0.39.
    ensure(h.pair.realized_pnl == Decimal("0.39"))


def test_config_override_can_disable_winning_commission():  # skipcq
    h = _Harness(venue_winning_profit_fee_rates={"SXBET": Decimal(0)})

    h.settle(h.over_leg_id, SettlementResult.WON)

    # Rate 0 restores the pre-commission +0.50 gross floor.
    ensure(h.pair.realized_pnl == Decimal("0.50"))


def test_sxbet_commission_composes_with_half_and_feeds_kill_switch_net():  # skipcq
    # Both SX.bet, one quarter-ball market: HALF_WON 2.75 net 4% -> 2.64 ; HALF_LOST -2.50 ->
    # net +0.14. The winning half is commissioned, the losing half is not, and the daily-loss
    # kill switch consumes this net realized figure: a net win never raises the loss counter.
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.HALF_WON)
    h.settle(h.under_leg_id, SettlementResult.HALF_LOST)

    ensure(h.pair.realized_pnl == Decimal("0.14"))
    ensure(h.strategy._live_execution_realized_loss == Decimal(0))


# --- PR-B: per-pair / per-leg trades detail in get_stats (observability only) --------------
#
#   ``get_stats()["arb_position_tracker"]["pairs"]`` carries the flat trades detail the DB
#   shipper turns into arb_pairs / trade_legs rows. It is JSON-safe (Decimal -> str), capped
#   to arb_pairs_stats_cap pairs, and derived entirely from the same settlement the tests
#   above pin -- so every aggregate scalar and realized-P&L number is unchanged by its
#   presence. Harness legs BACK 2.10 stake 5 on SX.bet: win_payoff 5.50, 4% commission.


def test_get_stats_pairs_carry_half_won_commission_detail():  # skipcq
    h = _Harness()  # both legs SX.bet, one quarter-ball market

    h.settle(h.over_leg_id, SettlementResult.HALF_WON)
    h.settle(h.under_leg_id, SettlementResult.HALF_LOST)

    stats = h.tracker_stats()
    # Aggregates are unchanged by the added detail (same realized 0.14 pinned above).
    ensure(stats["realized_pnl"] == "0.1400")
    ensure(stats["pairs_settled"] == 1)
    ensure(stats["pairs_open"] == 0)
    ensure(stats["pairs_tracked"] == 1)

    pairs = stats["pairs"]
    ensure(len(pairs) == 1)
    pair = pairs[0]
    ensure(pair["pair_id"] == "O-OVER-1|O-UNDER-1")
    ensure(pair["realized_pnl"] == "0.1400")
    # Per-leg grading and realized native, JSON-safe: HALF_WON net 4% -> 2.64, HALF_LOST -2.5.
    ensure(pair["leg_results"] == {"O-OVER-1": "HALF_WON", "O-UNDER-1": "HALF_LOST"})
    ensure(
        pair["leg_realized_native"] == {"O-OVER-1": "2.6400", "O-UNDER-1": "-2.5"},
    )
    legs = {leg["client_order_id"]: leg for leg in pair["legs"]}
    over = legs["O-OVER-1"]
    ensure(over["stake"] == "5")
    ensure(over["exposure"] == "10.50")
    ensure(over["fills"] == 1)  # back-compat count retained
    ensure(over["fill_events"] == [{"ts": None, "px": "2.10", "qty": "5"}])


def test_get_stats_pairs_cover_won_and_push_leg_results():  # skipcq
    h = _Harness()

    h.settle(h.over_leg_id, SettlementResult.WON)  # WON shortcut fixes the pair immediately

    pair = h.tracker_stats()["pairs"][0]
    # A same-venue WON settles via the joint path (no per-leg grading map), so leg_results is
    # empty there; the per-leg grading map is populated on the cross-venue per-leg path below.
    ensure(pair["winning_outcome"] == "over")
    ensure(pair["realized_pnl"] == "0.2800")

    # Cross-venue disables the WON shortcut, so a WON + PUSH pair realizes from each leg's own
    # grading -- the path that persists the per-leg map.
    push_h = _Harness(over_venue="CLOUDBET", under_venue="SXBET")
    push_h.settle(push_h.over_leg_id, SettlementResult.WON)
    push_h.settle(push_h.under_leg_id, SettlementResult.PUSH)
    push_pair = push_h.tracker_stats()["pairs"][0]
    ensure(push_pair["leg_results"] == {"O-OVER-1": "WON", "O-UNDER-1": "PUSH"})
    # CLOUDBET WON native 5.50 (no commission) ; SX.bet PUSH refunds its stake -> 0.
    ensure(push_pair["leg_realized_native"] == {"O-OVER-1": "5.50", "O-UNDER-1": "0"})


def test_get_stats_is_json_serializable():  # skipcq
    import json

    h = _Harness()
    h.settle(h.over_leg_id, SettlementResult.HALF_WON)
    h.settle(h.under_leg_id, SettlementResult.HALF_LOST)

    # The full stats payload -- including the nested per-pair trades detail -- round-trips
    # through json without a custom encoder.
    dumped = json.dumps(h.strategy.get_stats())
    reloaded = json.loads(dumped)
    ensure(reloaded["arb_position_tracker"]["pairs"][0]["realized_pnl"] == "0.1400")


def test_get_stats_pairs_capped_to_arb_pairs_stats_cap():  # skipcq
    # The cap bounds only the emitted list; tracking still counts every pair.
    h = _Harness(arb_pairs_stats_cap=2)
    tracker = h.strategy._arb_position_tracker
    for i in range(5):
        a, b = f"X{i}", f"Y{i}"
        tracker.record_fill(a, "over", "BUY", Decimal("2.1"), Decimal(5), sibling_id=b)
        tracker.record_fill(b, "under", "BUY", Decimal("2.1"), Decimal(5), sibling_id=a)

    stats = h.tracker_stats()
    ensure(stats["pairs_tracked"] == 6)  # the harness pair plus five, none evicted
    ensure(len(stats["pairs"]) == 2)  # only the two most-recently-active emitted


def test_get_stats_pairs_saturated_and_realistic_payload_sizes_bounded():  # skipcq
    # JSON-size guard for the DB shipper. Two bounds are pinned: a saturated worst case
    # (cap-200 pairs x 2 legs x 50 fill events each) under a sane hard ceiling, and a
    # realistic load (200 pairs x ~1 fill/leg) comfortably small. Fill-event dicts are used
    # for shipper clarity; the saturated ceiling is generous by design (~1-1.3 MB), while the
    # realistic path -- what a live node actually emits -- stays well under a few hundred KB.
    import json

    def _fill(size: int) -> str:
        h = _Harness(arb_pairs_stats_cap=200, arb_leg_fills_cap=50)
        tracker = h.strategy._arb_position_tracker
        for i in range(200):
            a, b = f"X{i}", f"Y{i}"
            for _ in range(size):
                tracker.record_fill(a, "over", "BUY", Decimal("2.1"), Decimal(5), sibling_id=b)
                tracker.record_fill(b, "under", "BUY", Decimal("2.1"), Decimal(5), sibling_id=a)
        return json.dumps(h.strategy.get_stats())

    saturated = _fill(50)  # 200 pairs, both legs at the 50-fill cap
    realistic = _fill(1)  # 200 pairs, one fill per leg

    ensure(len(saturated.encode()) < 2_000_000)  # generous hard ceiling for the worst case
    ensure(len(realistic.encode()) < 400_000)  # the load a live node actually ships
