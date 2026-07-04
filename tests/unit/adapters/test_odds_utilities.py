# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for betting adapter odds utilities.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

import pytest

from nautilus_trader.adapters.betting.common.fees import fee_adjusted_basket_margin
from nautilus_trader.adapters.betting.common.fees import fee_adjusted_odds
from nautilus_trader.adapters.betting.common.fees import normalize_venue_fee_rates
from nautilus_trader.adapters.betting.common.odds import calculate_arbitrage_stakes
from nautilus_trader.adapters.betting.common.odds import devig_probabilities
from nautilus_trader.adapters.betting.common.odds import decimal_to_american
from nautilus_trader.adapters.betting.common.odds import decimal_to_fractional
from nautilus_trader.adapters.betting.common.odds import decimal_to_probability
from nautilus_trader.adapters.betting.common.odds import is_arbitrage_opportunity


FAVORITE_MONEYLINE = -200
SMALL_UNDERDOG_MONEYLINE = 150
LARGE_UNDERDOG_MONEYLINE = 200


class TestOddsConversion:
    """
    Test odds conversion utilities.
    """

    def test_decimal_to_probability(self):
        """
        Test converting decimal odds to probability.
        """
        assert decimal_to_probability(Decimal("2.0")) == Decimal("0.5")
        assert float(decimal_to_probability(Decimal("3.0"))) == pytest.approx(
            0.333333,
            rel=1e-5,
        )
        assert float(decimal_to_probability(Decimal("1.5"))) == pytest.approx(
            0.666667,
            rel=1e-5,
        )

    def test_decimal_to_american(self):
        """
        Test converting decimal to American odds.
        """
        # Favorites (< 2.0)
        assert decimal_to_american(Decimal("1.5")) == FAVORITE_MONEYLINE
        assert decimal_to_american(Decimal("1.91")) == pytest.approx(-110, rel=1e-1)

        # Underdogs (> 2.0)
        assert decimal_to_american(Decimal("2.5")) == SMALL_UNDERDOG_MONEYLINE
        assert decimal_to_american(Decimal("3.0")) == LARGE_UNDERDOG_MONEYLINE

    def test_decimal_to_fractional(self):
        """
        Test converting decimal to fractional odds.
        """
        assert decimal_to_fractional(Decimal("2.0")) == (1, 1)
        assert decimal_to_fractional(Decimal("3.0")) == (2, 1)
        assert decimal_to_fractional(Decimal("1.5")) == (1, 2)


class TestArbitrageCalculations:
    """
    Test arbitrage calculation functions.
    """

    @staticmethod
    def test_calculate_arbitrage_stakes_basic():
        """
        Test basic arbitrage stake calculation.
        """
        stake_a, stake_b, profit = calculate_arbitrage_stakes(
            odds_a=Decimal("2.1"),
            odds_b=Decimal("2.0"),
            total_stake=Decimal(1000),
        )

        # Stakes should sum to total
        assert stake_a + stake_b == Decimal(1000)

        # Both outcomes should yield same return
        return_a = stake_a * Decimal("2.1")
        return_b = stake_b * Decimal("2.0")
        assert pytest.approx(float(return_a), rel=1e-2) == pytest.approx(float(return_b), rel=1e-2)

        # Profit should be positive for arbitrage
        assert profit > 0

    @staticmethod
    def test_is_arbitrage_opportunity():
        """
        Test arbitrage opportunity detection.
        """
        # Valid arbitrage
        odds_a = Decimal("2.1")
        odds_b = Decimal("2.05")
        is_arb, margin = is_arbitrage_opportunity(odds_a=odds_a, odds_b=odds_b)
        assert is_arb is True
        total_prob = (Decimal(1) / odds_a) + (Decimal(1) / odds_b)
        expected_margin = (Decimal(1) / total_prob) - Decimal(1)
        assert pytest.approx(float(expected_margin), rel=1e-6) == float(margin)

        # No arbitrage
        is_arb, margin = is_arbitrage_opportunity(
            odds_a=Decimal("1.9"),
            odds_b=Decimal("1.9"),
        )
        assert is_arb is False
        assert margin == Decimal(0)

    @staticmethod
    def test_devig_probabilities_strips_three_way_overround():
        """
        Full-book diagnostics should expose no-vig probabilities separately.
        """
        market = devig_probabilities(
            (Decimal("2.60"), Decimal("3.20"), Decimal("2.70")),
            method="proportional",
        )

        assert market.method == "proportional"
        assert market.overround > Decimal(1)
        assert market.vig == market.overround - Decimal(1)
        assert abs(sum(market.no_vig_probabilities, Decimal(0)) - Decimal(1)) < Decimal("1e-12")
        assert market.no_vig_probabilities[0] < market.implied_probabilities[0]

    @staticmethod
    def test_devig_probabilities_supports_shin_reference_examples():
        """
        Shin devigging should support two-way and multi-way sportsbook books.
        """
        small_favorite = devig_probabilities((Decimal("1.75"), Decimal("2.20")), method="shin")
        heavy_favorite = devig_probabilities((Decimal("1.30"), Decimal("3.80")), method="shin")
        three_way = devig_probabilities(
            (Decimal("2.60"), Decimal("3.20"), Decimal("2.70")),
            method="shin",
        )

        assert small_favorite.method == "shin"
        assert small_favorite.convergence_status == "analytic"
        assert float(small_favorite.no_vig_probabilities[0]) == pytest.approx(0.55844, rel=1e-4)
        assert float(heavy_favorite.no_vig_probabilities[0]) == pytest.approx(0.75304, rel=1e-4)
        assert three_way.convergence_status == "converged"
        assert abs(sum(three_way.no_vig_probabilities, Decimal(0)) - Decimal(1)) < Decimal(
            "1e-12",
        )

    @staticmethod
    def test_devig_probabilities_auto_selects_balanced_shin_and_extreme_methods():
        balanced = devig_probabilities((Decimal("1.91"), Decimal("1.99")), method="auto")
        normal_three_way = devig_probabilities(
            (Decimal("2.60"), Decimal("3.20"), Decimal("2.70")),
            method="auto",
        )
        extreme = devig_probabilities((Decimal("1.08"), Decimal("11.00")), method="auto")

        assert balanced.method == "proportional"
        assert balanced.method_reason == "balanced_two_way"
        assert normal_three_way.method == "shin"
        assert normal_three_way.method_reason == "default_shin"
        assert extreme.method == "logarithmic"
        assert extreme.method_reason == "extreme_odds"
        assert abs(sum(extreme.no_vig_probabilities, Decimal(0)) - Decimal(1)) < Decimal("1e-12")

    @staticmethod
    def test_devig_probabilities_logarithmic_handles_extreme_tail():
        market = devig_probabilities((Decimal("1.08"), Decimal("11.00")), method="logarithmic")

        assert market.method == "logarithmic"
        assert market.convergence_status == "converged"
        assert market.no_vig_probabilities[0] < market.implied_probabilities[0]
        assert abs(sum(market.no_vig_probabilities, Decimal(0)) - Decimal(1)) < Decimal("1e-12")

    @staticmethod
    def test_devig_probabilities_rejects_incomplete_book():
        with pytest.raises(ValueError, match="At least two odds"):
            devig_probabilities((Decimal("2.0"),))

    @staticmethod
    def test_devig_probabilities_rejects_unknown_method():
        with pytest.raises(ValueError, match="Unsupported devig method"):
            devig_probabilities((Decimal("2.0"), Decimal("2.1")), method="bad")

    @staticmethod
    def test_calculate_arbitrage_stakes_equal_odds():
        """
        Test stake calculation with equal odds.
        """
        stake_a, stake_b, profit = calculate_arbitrage_stakes(
            odds_a=Decimal("2.0"),
            odds_b=Decimal("2.0"),
            total_stake=Decimal(1000),
        )

        # Equal odds should result in equal stakes
        assert stake_a == stake_b == Decimal(500)

        # No profit with equal odds
        assert profit == Decimal(0)

    @staticmethod
    def test_calculate_arbitrage_stakes_profit_matches_rounded_stakes():
        """
        Test the returned profit is computed from the executable rounded stakes.
        """
        odds_a = Decimal("2.1")
        odds_b = Decimal("2.0")
        total = Decimal(1000)

        stake_a, stake_b, profit = calculate_arbitrage_stakes(
            odds_a=odds_a,
            odds_b=odds_b,
            total_stake=total,
        )

        realized_profit = min(stake_a * odds_a, stake_b * odds_b) - total

        assert stake_a + stake_b == total
        assert profit == realized_profit.quantize(Decimal("0.01"))

    @staticmethod
    def test_calculate_arbitrage_stakes_uses_equal_return_split():
        """
        Test asymmetric odds still split stakes to the same realized return.
        """
        stake_a, stake_b, profit = calculate_arbitrage_stakes(
            odds_a=Decimal("4.0"),
            odds_b=Decimal("2.0"),
            total_stake=Decimal(120),
        )

        assert stake_a == Decimal("40.00")
        assert stake_b == Decimal("80.00")
        assert stake_a * Decimal("4.0") == stake_b * Decimal("2.0")
        assert profit == Decimal("40.00")


class TestFeeAdjustedOdds:
    def test_prediction_market_taker_fee_reduces_effective_odds(self):
        adjusted = fee_adjusted_odds(Decimal("2.02"), taker_fee_rate=Decimal("0.03"))

        assert adjusted.raw_odds == Decimal("2.02")
        assert adjusted.effective_odds < adjusted.raw_odds
        assert adjusted.effective_probability > adjusted.raw_probability
        # p = 1/2.02 < 0.5 so min(p, 1 - p) = p and the per-stake protocol fee
        # rate * min(p, 1 - p) / p collapses to the raw rate.
        assert adjusted.taker_cost_fraction == Decimal("0.03")

    def test_polymarket_taker_fee_matches_protocol_formula_by_side(self):  # skipcq
        # Favorite: price p = 0.8 (odds 1.25), min(p, 1 - p) = 1 - p = 0.2.
        # Protocol per-stake fee = rate * (1 - p) / p = 0.03 * 0.2 / 0.8 = 0.0075.
        favorite = fee_adjusted_odds(Decimal("1.25"), taker_fee_rate=Decimal("0.03"))
        assert favorite.raw_probability == Decimal("0.8")
        assert favorite.taker_cost_fraction == Decimal("0.0075")

        # Underdog: price p = 0.2 (odds 5), min(p, 1 - p) = p = 0.2.
        # Protocol per-stake fee = rate * p / p = rate = 0.03.
        underdog = fee_adjusted_odds(Decimal(5), taker_fee_rate=Decimal("0.03"))
        assert underdog.raw_probability == Decimal("0.2")
        assert underdog.taker_cost_fraction == Decimal("0.03")

    def test_winning_profit_fee_reduces_net_return(self):  # skipcq
        adjusted = fee_adjusted_odds(Decimal("3.00"), winning_profit_fee_rate=Decimal("0.10"))

        assert adjusted.effective_odds == Decimal("2.80")
        assert adjusted.effective_probability == Decimal(1) / Decimal("2.80")

    def test_prediction_market_maker_rebate_increases_effective_odds(self):
        adjusted = fee_adjusted_odds(Decimal("2.02"), maker_rebate_rate=Decimal("0.01"))

        assert adjusted.effective_odds > adjusted.raw_odds
        assert adjusted.effective_probability < adjusted.raw_probability
        assert adjusted.maker_rebate_fraction == Decimal("0.01") * (
            Decimal(1) - (Decimal(1) / Decimal("2.02"))
        )

    def test_basket_rebate_and_boost_improve_coverage_margin(self):
        basket = fee_adjusted_basket_margin(
            (Decimal("0.49"), Decimal("0.49")),
            basket_rebate_rate=Decimal("0.01"),
            basket_boost_rate=Decimal("0.02"),
        )

        assert basket.raw_profit_margin == (Decimal(1) / Decimal("0.98")) - Decimal(1)
        assert basket.effective_profit_margin > basket.raw_profit_margin
        assert basket.incentive_margin_delta > Decimal(0)

    def test_normalize_venue_fee_rates_uppercases_and_validates(self):
        assert normalize_venue_fee_rates({" polymarket ": "0.03"}) == {
            "POLYMARKET": Decimal("0.03"),
        }

        with pytest.raises(ValueError, match="less than 1"):
            normalize_venue_fee_rates({"SXBET": "1.0"})
