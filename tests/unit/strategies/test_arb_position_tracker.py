# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the arbitrage position tracker (real-money two-leg betting P&L).
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module
"""
Adversarial, hand-computed proofs for ArbPositionTracker.

Every expected number below is derived by hand from the back-bet payoff identities for a
binary market with mutually exclusive outcomes A and B:

  BACK at odds ``p`` for stake ``s``:
    outcome_win_payoff  = s * (p - 1)   (selection wins)
    outcome_lose_payoff = -s            (selection loses)

  joint payoff if A wins  = legA.win  + legB.lose
  joint payoff if B wins  = legB.win  + legA.lose

"""

from decimal import Decimal

from nautilus_trader.adapters.betting.fx import PortfolioCurrencyPolicy
from nautilus_trader.core.nautilus_pyo3 import BetSide
from nautilus_trader.examples.strategies.arb_position_tracker import ArbPositionTracker
from nautilus_trader.examples.strategies.arb_position_tracker import bet_side_for_order_side


LEG_A = "O-A"
LEG_B = "O-B"
OUTCOME_A = "HOME-1.5"
OUTCOME_B = "AWAY+1.5"


def _tracker_with_pair(odds_a, stake_a, odds_b, stake_b):
    tracker = ArbPositionTracker()
    tracker.record_fill(LEG_A, OUTCOME_A, "BUY", odds_a, stake_a, sibling_id=LEG_B)
    tracker.record_fill(LEG_B, OUTCOME_B, "BUY", odds_b, stake_b, sibling_id=LEG_A)
    return tracker, tracker.pair_key(LEG_A, LEG_B)


def _currency_tracker(policy, currency_a, currency_b, odds_a, stake_a, odds_b, stake_b):
    tracker = ArbPositionTracker(policy=policy)
    tracker.record_fill(
        LEG_A,
        OUTCOME_A,
        "BUY",
        odds_a,
        stake_a,
        sibling_id=LEG_B,
        currency=currency_a,
    )
    tracker.record_fill(
        LEG_B,
        OUTCOME_B,
        "BUY",
        odds_b,
        stake_b,
        sibling_id=LEG_A,
        currency=currency_b,
    )
    return tracker, tracker.pair_key(LEG_A, LEG_B)


def test_bet_side_mapping():
    assert bet_side_for_order_side("BUY") == BetSide.BACK
    assert bet_side_for_order_side("SELL") == BetSide.LAY


def test_a_genuine_arb_guaranteed_plus_one_in_both_outcomes():
    # 2.1/2.1, stake 10 each. A wins: 10*1.1 - 10 = +1 ; B wins: 10*1.1 - 10 = +1.
    tracker, pair_id = _tracker_with_pair("2.1", "10", "2.1", "10")
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("1.0")
    assert pnls[OUTCOME_B] == Decimal("1.0")
    assert pair.guaranteed_pnl() == Decimal("1.0")
    assert pair.best_case_pnl() == Decimal("1.0")
    assert pair.is_fully_hedged is True
    # exposure = price*stake per BACK leg = 21 + 21
    assert pair.exposure == Decimal("42.0")


def test_b_negative_margin_minus_one_in_both_outcomes():
    # 1.9/1.9, stake 10 each. A wins: 10*0.9 - 10 = -1 ; B wins: -1.
    tracker, pair_id = _tracker_with_pair("1.9", "10", "1.9", "10")
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("-1.0")
    assert pnls[OUTCOME_B] == Decimal("-1.0")
    assert pair.guaranteed_pnl() == Decimal("-1.0")
    assert pair.best_case_pnl() == Decimal("-1.0")


def test_c_void_refunds_both_stakes_pnl_zero():
    tracker, pair_id = _tracker_with_pair("2.1", "10", "2.1", "10")
    realized = tracker.settle(pair_id, void=True)

    assert realized == Decimal(0)
    pair = tracker.pair(pair_id)
    assert pair.settled is True
    assert pair.void is True
    assert pair.realized_pnl == Decimal(0)
    # settled pairs surface realized P&L, not open outcome scenarios
    assert pair.guaranteed_pnl() is None
    assert pair.summary()["outcome_pnls"] == {}


def test_d_one_leg_only_fill_outcome_dependent_no_guarantee():
    # Only leg A fills: BACK 2.1 stake 10. Selection wins => +11 ; loses => -10.
    tracker = ArbPositionTracker()
    tracker.record_fill(LEG_A, OUTCOME_A, "BUY", "2.1", "10", sibling_id=LEG_B)
    pair = tracker.pair_for_leg(LEG_A)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("11.0")
    # synthetic complement scenario = the selection loses
    assert pnls["__other__"] == Decimal(-10)
    assert pair.guaranteed_pnl() == Decimal(-10)  # no guarantee: worst case is a loss
    assert pair.best_case_pnl() == Decimal("11.0")
    assert pair.is_fully_hedged is False


def test_e_asymmetric_stakes_per_outcome_payoffs():
    # legA BACK 2.1 stake 10 ; legB BACK 2.1 stake 8.
    # A wins: legA.win 10*1.1=11 + legB.lose -8  = +3.0
    # B wins: legB.win 8*1.1=8.8 + legA.lose -10 = -1.2
    tracker, pair_id = _tracker_with_pair("2.1", "10", "2.1", "8")
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("3.0")
    assert pnls[OUTCOME_B] == Decimal("-1.2")
    assert pair.guaranteed_pnl() == Decimal("-1.2")
    assert pair.best_case_pnl() == Decimal("3.0")


def test_e_asymmetric_settlement_matches_worst_and_best():
    tracker, pair_id = _tracker_with_pair("2.1", "10", "2.1", "8")
    assert tracker.settle(pair_id, OUTCOME_B) == Decimal("-1.2")

    tracker2, pair_id2 = _tracker_with_pair("2.1", "10", "2.1", "8")
    assert tracker2.settle(pair_id2, OUTCOME_A) == Decimal("3.0")


def test_f_partial_fills_accumulate_per_leg():
    # legA fills 6 then 4 (both 2.1) => stake 10. legB fills 10 at 2.1.
    # Must reproduce the genuine-arb +1/+1 once fully accumulated.
    tracker = ArbPositionTracker()
    tracker.record_fill(LEG_A, OUTCOME_A, "BUY", "2.1", "6", sibling_id=LEG_B)
    tracker.record_fill(LEG_A, OUTCOME_A, "BUY", "2.1", "4", sibling_id=LEG_B)
    tracker.record_fill(LEG_B, OUTCOME_B, "BUY", "2.1", "10", sibling_id=LEG_A)
    pair = tracker.pair(tracker.pair_key(LEG_A, LEG_B))

    leg_a = pair.legs[LEG_A]
    assert len(leg_a.fills) == 2
    assert leg_a.stake == Decimal(10)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("1.0")
    assert pnls[OUTCOME_B] == Decimal("1.0")
    assert pair.guaranteed_pnl() == Decimal("1.0")


def test_partial_fills_at_different_prices_accumulate_exactly():
    # legA: 5 @ 2.0 then 5 @ 2.2 ; legB: 10 @ 2.1.
    # A wins: legA.win = 5*1.0 + 5*1.2 = 11 ; legB.lose = -10  => +1.0
    # B wins: legB.win = 10*1.1 = 11 ; legA.lose = -(5+5) = -10 => +1.0
    tracker = ArbPositionTracker()
    tracker.record_fill(LEG_A, OUTCOME_A, "BUY", "2.0", "5", sibling_id=LEG_B)
    tracker.record_fill(LEG_A, OUTCOME_A, "BUY", "2.2", "5", sibling_id=LEG_B)
    tracker.record_fill(LEG_B, OUTCOME_B, "BUY", "2.1", "10", sibling_id=LEG_A)
    pair = tracker.pair(tracker.pair_key(LEG_A, LEG_B))

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("1.0")
    assert pnls[OUTCOME_B] == Decimal("1.0")


def test_settlement_on_untraded_outcome_settles_every_leg_at_loss():
    # Neither leg backed outcome C: both legs lose their stakes => -(10) - (8) = -18.
    tracker, pair_id = _tracker_with_pair("2.1", "10", "2.1", "8")
    assert tracker.settle(pair_id, "SOME-VOIDED-THIRD-OUTCOME") == Decimal(-18)


def test_pair_key_is_order_independent():
    assert ArbPositionTracker.pair_key("z", "a") == ArbPositionTracker.pair_key("a", "z")


def test_summary_aggregates_open_and_realized():
    tracker = ArbPositionTracker()
    # open genuine arb: guaranteed +1
    tracker.record_fill("A1", OUTCOME_A, "BUY", "2.1", "10", sibling_id="B1")
    tracker.record_fill("B1", OUTCOME_B, "BUY", "2.1", "10", sibling_id="A1")
    # settled negative-margin pair: realized -1
    tracker.record_fill("A2", OUTCOME_A, "BUY", "1.9", "10", sibling_id="B2")
    tracker.record_fill("B2", OUTCOME_B, "BUY", "1.9", "10", sibling_id="A2")
    tracker.settle(tracker.pair_key("A2", "B2"), OUTCOME_A)

    summary = tracker.summary()
    assert summary["pairs_tracked"] == 2
    assert summary["pairs_open"] == 1
    assert summary["open_guaranteed_pnl"] == Decimal("1.0")
    assert summary["realized_pnl"] == Decimal("-1.0")
    assert summary["open_exposure"] == Decimal("42.0")


def test_sell_side_leg_maps_to_lay_payoffs():
    # A LAY leg on outcome A: outcome_win_payoff = -liability, outcome_lose_payoff = profit.
    # LAY 2.0 stake 10 -> liability 10*(2-1)=10 ; A wins => -10 ; A loses => +10.
    tracker = ArbPositionTracker()
    tracker.record_fill(LEG_A, OUTCOME_A, "SELL", "2.0", "10")
    pair = tracker.pair_for_leg(LEG_A)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal(-10)
    assert pnls["__other__"] == Decimal(10)


# --------------------------------------------------------------------------------------
# Cross-currency normalisation (M2 PR5).
#
# The pre-PR tracker raw-summed per-leg Decimals with no currency awareness: for a pair
# whose legs settle in different currencies that sum was a garbage number and the floor a
# false guarantee. Each leg payoff is now converted into the base currency through the
# haircut-reducing ``PortfolioCurrencyPolicy.convert_payoff`` before combining. All numbers
# below are hand-computed from that identity with a 10 bps payoff haircut (factor 0.999).
# --------------------------------------------------------------------------------------

# EUR is priced at 1.25 USD; USDC is a USD stablecoin (parity, 10 bps haircut).
_CROSS_POLICY = PortfolioCurrencyPolicy(
    base_currency="USD",
    static_fx_rates={"EUR/USD": Decimal("1.25")},
    stablecoin_haircut_bps=10,
)


def test_a_same_currency_pair_is_identity_no_regression():
    # Both legs settle in USDC with a live policy present. A same-currency pair carries no
    # cross-currency risk, so the floor must reduce EXACTLY to the pre-PR raw sum: the
    # genuine 2.1/2.1 stake-10 arb still locks +1.0 in both outcomes, no haircut applied.
    tracker, pair_id = _currency_tracker(
        _CROSS_POLICY,
        "USDC",
        "USDC",
        "2.1",
        "10",
        "2.1",
        "10",
    )
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("1.0")
    assert pnls[OUTCOME_B] == Decimal("1.0")
    assert pair.guaranteed_pnl() == Decimal("1.0")
    assert pair.best_case_pnl() == Decimal("1.0")
    assert pair.is_cross_currency is False
    assert pair.floor_currency_risk is False
    # Identical to the no-policy, no-currency baseline (test_a_genuine_arb...).
    baseline, baseline_id = _tracker_with_pair("2.1", "10", "2.1", "10")
    assert pair.guaranteed_pnl() == baseline.pair(baseline_id).guaranteed_pnl()


def test_b_cross_currency_floor_normalised_to_base_and_conservative():
    # legA BACK EUR 2.5 stake 8 ; legB BACK USDC 2.5 stake 10 ; base USD, EUR/USD = 1.25.
    # 8 EUR of stake == 10 USD, so the base-currency legs are balanced.
    # Sign-aware conversion: a won payoff shrinks under the reducing haircut (x0.999); a
    # lost payoff is inflated under the raising haircut (x1.001) so the floor is a true
    # lower bound.
    # A wins: legA.win 8*1.5=12 EUR -> 12*1.25*0.999 = 14.985 ; legB.lose -10 USDC
    #         -> -(10*1.001) = -10.01  => +4.975
    # B wins: legB.win 10*1.5=15 USDC -> 15*0.999 = 14.985 ; legA.lose -8 EUR
    #         -> -(8*1.25*1.001) = -10.01  => +4.975
    tracker, pair_id = _currency_tracker(
        _CROSS_POLICY,
        "EUR",
        "USDC",
        "2.5",
        "8",
        "2.5",
        "10",
    )
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("4.975")
    assert pnls[OUTCOME_B] == Decimal("4.975")
    assert pair.guaranteed_pnl() == Decimal("4.975")
    assert pair.best_case_pnl() == Decimal("4.975")
    assert pair.is_cross_currency is True
    assert pair.floor_currency_risk is False
    # Conservative: the sign-aware haircut strictly reduces the +5.0 zero-haircut floor,
    # and the loss-inflation makes it strictly lower than the old reducing-only 4.995.
    assert pair.guaranteed_pnl() < Decimal("4.995")


def test_c_non_convertible_leg_marks_floor_unavailable_not_raw_sum():
    # legA settles in GBP with no configured rate (not a stablecoin) ; legB in USDC.
    # The base-currency payoff cannot be formed, so every outcome is unavailable and the
    # pair must be flagged currency-risk-bearing -- NEVER a raw sum of GBP and USDC.
    tracker, pair_id = _currency_tracker(
        _CROSS_POLICY,
        "GBP",
        "USDC",
        "2.1",
        "10",
        "2.1",
        "10",
    )
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] is None
    assert pnls[OUTCOME_B] is None
    assert pair.guaranteed_pnl() is None
    assert pair.best_case_pnl() is None
    assert pair.floor_currency_risk is True
    assert pair.is_cross_currency is True
    assert tracker.summary()["open_currency_risk_pairs"] == 1


def test_d_cross_currency_void_refunds_both_stakes_pnl_zero():
    tracker, pair_id = _currency_tracker(
        _CROSS_POLICY,
        "EUR",
        "USDC",
        "2.5",
        "8",
        "2.5",
        "10",
    )
    realized = tracker.settle(pair_id, void=True)

    assert realized == Decimal(0)
    pair = tracker.pair(pair_id)
    assert pair.void is True
    assert pair.realized_pnl == Decimal(0)


def test_e_cross_currency_settlement_realizes_base_currency_pnl():
    # Settling on OUTCOME_A must realize the pre-settlement base-currency payoff (+4.975),
    # matching the sign-aware floor scenario for that outcome exactly.
    tracker, pair_id = _currency_tracker(
        _CROSS_POLICY,
        "EUR",
        "USDC",
        "2.5",
        "8",
        "2.5",
        "10",
    )
    pre_settlement = tracker.pair(pair_id).outcome_pnls()[OUTCOME_A]
    realized = tracker.settle(pair_id, OUTCOME_A)

    assert realized == Decimal("4.975")
    assert realized == pre_settlement
    assert tracker.pair(pair_id).realized_pnl == Decimal("4.975")

    # And the losing outcome realizes the symmetric base-currency payoff.
    tracker_b, pair_id_b = _currency_tracker(
        _CROSS_POLICY,
        "EUR",
        "USDC",
        "2.5",
        "8",
        "2.5",
        "10",
    )
    assert tracker_b.settle(pair_id_b, OUTCOME_B) == Decimal("4.975")


def test_f_sign_aware_floor_never_flips_a_losing_pair_positive():
    # Regression: a base-currency leg (identity, factor 1.0) paired with a foreign losing
    # leg must NOT report a positive locked floor. legA BACK USD 2.0 stake 124.9 ; legB
    # BACK EUR 3.0 stake 100 ; base USD, EUR/USD = 1.25.
    #   OUTCOME_A wins: legA.win 124.9*1.0 = 124.9 USD (identity, no haircut)
    #                   legB.lose -100 EUR -> loss inflated: -(100*1.25*1.001) = -125.125
    #                   => 124.9 - 125.125 = -0.225   (a genuine loss, not a false lock)
    #   OUTCOME_B wins: legB.win 100*2.0 = 200 EUR -> 200*1.25*0.999 = 249.75
    #                   legA.lose -124.9 USD (identity) = -124.9  => +124.85
    # The old reducing-only conversion shrank the -125 EUR loss to -124.875 and reported a
    # phantom +0.025 floor with floor_currency_risk False. The true exact-rate worst case
    # is 124.9 - 125 = -0.1 (a loss); loss-inflation makes the reported floor -0.225.
    tracker, pair_id = _currency_tracker(
        _CROSS_POLICY,
        "USD",
        "EUR",
        "2.0",
        "124.9",
        "3.0",
        "100",
    )
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_A] == Decimal("-0.225")
    assert pnls[OUTCOME_B] == Decimal("124.85")
    guaranteed = pair.guaranteed_pnl()
    # The floor is a real loss, not a false positive lock.
    assert guaranteed == Decimal("-0.225")
    assert guaranteed <= Decimal("-0.1")
    assert guaranteed < Decimal(0)
    assert pair.is_cross_currency is True
    # Both legs are convertible, so this is a genuine computed floor, not currency risk.
    assert pair.floor_currency_risk is False


def test_f_sign_aware_floor_conservative_when_other_outcome_is_the_loss():
    # Symmetric probe: mirror the pair so the LOSS falls in the other outcome. legA BACK
    # EUR 3.0 stake 100 ; legB BACK USD 2.0 stake 124.9 ; base USD, EUR/USD = 1.25.
    #   OUTCOME_B wins: legB.win 124.9*1.0 = 124.9 USD (identity)
    #                   legA.lose -100 EUR -> -(100*1.25*1.001) = -125.125  => -0.225
    #   OUTCOME_A wins: legA.win 100*2.0 = 200 EUR -> 200*1.25*0.999 = 249.75
    #                   legB.lose -124.9 USD (identity) = -124.9  => +124.85
    tracker, pair_id = _currency_tracker(
        _CROSS_POLICY,
        "EUR",
        "USD",
        "3.0",
        "100",
        "2.0",
        "124.9",
    )
    pair = tracker.pair(pair_id)

    pnls = pair.outcome_pnls()
    assert pnls[OUTCOME_B] == Decimal("-0.225")
    assert pnls[OUTCOME_A] == Decimal("124.85")
    guaranteed = pair.guaranteed_pnl()
    assert guaranteed == Decimal("-0.225")
    assert guaranteed <= Decimal("-0.1")
    assert pair.floor_currency_risk is False


# --- cross-venue tagging + per-leg realization ------------------------------------------
#
#   BACK 2.1 stake 10: outcome_win_payoff = 10*1.1 = 11 ; outcome_lose_payoff = -10.
#   A cross-venue pair grades each leg independently, so realized P&L is the SUM of each
#   leg's actual result -- WON contributes win, LOST contributes lose, VOID contributes 0
#   (stake refunded) -- NOT the single-market joint payoff that books the sibling lost.


def _venued_tracker(venue_a, venue_b):
    tracker = ArbPositionTracker()
    tracker.record_fill(LEG_A, OUTCOME_A, "BUY", "2.1", "10", sibling_id=LEG_B, venue=venue_a)
    tracker.record_fill(LEG_B, OUTCOME_B, "BUY", "2.1", "10", sibling_id=LEG_A, venue=venue_b)
    return tracker, tracker.pair_key(LEG_A, LEG_B)


def test_is_cross_venue_detects_distinct_venues():
    tracker, pair_id = _venued_tracker("CLOUDBET", "SXBET")
    assert tracker.pair(pair_id).is_cross_venue is True


def test_is_cross_venue_false_for_same_or_unknown_venue():
    same, same_id = _venued_tracker("SXBET", "SXBET")
    assert same.pair(same_id).is_cross_venue is False
    # Legs recorded without a venue (the pre-PR path) must never read as cross-venue.
    unknown, unknown_id = _tracker_with_pair("2.1", "10", "2.1", "10")
    assert unknown.pair(unknown_id).is_cross_venue is False


def test_settle_from_leg_results_won_and_lost_sums_actual_payoffs():
    tracker, pair_id = _venued_tracker("CLOUDBET", "SXBET")
    pair = tracker.pair(pair_id)

    realized = pair.settle_from_leg_results({LEG_A: "WON", LEG_B: "LOST"})

    assert realized == Decimal("1.0")  # 11 + (-10)
    assert pair.realized_pnl == Decimal("1.0")
    assert pair.settled is True
    assert pair.void is False


def test_settle_from_leg_results_void_leg_refunds_stake_not_full_loss():
    tracker, pair_id = _venued_tracker("CLOUDBET", "SXBET")
    pair = tracker.pair(pair_id)

    realized = pair.settle_from_leg_results({LEG_A: "WON", LEG_B: "VOID"})

    assert realized == Decimal("11.0")  # 11 + 0, NOT the joint 11 + (-10) = 1
    assert pair.void is False


def test_settle_from_leg_results_all_void_realizes_zero_and_void_flag():
    tracker, pair_id = _venued_tracker("CLOUDBET", "SXBET")
    pair = tracker.pair(pair_id)

    realized = pair.settle_from_leg_results({LEG_A: "VOID", LEG_B: "VOID"})

    assert realized == Decimal(0)
    assert pair.void is True


def test_settle_from_leg_results_both_lost_realizes_full_downside():
    tracker, pair_id = _venued_tracker("CLOUDBET", "SXBET")
    pair = tracker.pair(pair_id)

    realized = pair.settle_from_leg_results({LEG_A: "LOST", LEG_B: "LOST"})

    assert realized == Decimal(-20)  # -10 + -10


def test_settle_from_leg_results_cross_currency_void_refund_in_base():
    # EUR/USDC floor scenario: WON legA 8*1.5 = 12 EUR -> 12*1.25*0.999 = 14.985 base ;
    # VOID legB refunds its stake -> 0. Realized = 14.985 base USD, sibling not booked lost.
    tracker, pair_id = _currency_tracker(_CROSS_POLICY, "EUR", "USDC", "2.5", "8", "2.5", "10")
    pair = tracker.pair(pair_id)

    realized = pair.settle_from_leg_results({LEG_A: "WON", LEG_B: "VOID"})

    assert realized == Decimal("14.985")
    assert pair.void is False
