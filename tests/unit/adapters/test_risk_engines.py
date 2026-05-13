# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for risk engines.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

import pytest

from nautilus_trader.adapters.betting.risk_engine import BettingVenueRiskPolicy as ShimBettingVenueRiskPolicy
from nautilus_trader.adapters.betting.venue_risk import BettingVenueRiskPolicy
from nautilus_trader.adapters.sxbet.risk_engine import SXBetVenueRiskPolicy
from nautilus_trader.adapters.tenbet.risk_engine import TenBetVenueRiskPolicy


def test_betting_risk_engine_module_reexports_venue_policy():
    assert ShimBettingVenueRiskPolicy is BettingVenueRiskPolicy


class TestSXBetVenueRiskPolicy:
    """
    Test SX.bet venue risk policy.
    """

    @pytest.fixture
    def risk_engine(self):
        """
        Create SX.bet venue risk policy.
        """
        return SXBetVenueRiskPolicy()

    def test_minimum_stake_enforcement(self, risk_engine):
        """
        Test minimum stake requirement.
        """
        # Below minimum
        eval_result = risk_engine.evaluate_order(
            stake=Decimal("3.0"),  # Below 5 USDC minimum
            odds=Decimal("2.0"),
            market_type="match_odds",
            currency="USDC",
        )
        assert eval_result.approved is False

        # Above minimum
        eval_result = risk_engine.evaluate_order(
            stake=Decimal("10.0"),
            odds=Decimal("2.0"),
            market_type="match_odds",
            currency="USDC",
        )
        assert eval_result.approved is True
        assert eval_result.requires_platform_risk_engine is True
        assert eval_result.venue_policy == "SXBET"

    def test_odds_limits(self, risk_engine):
        """
        Test odds limits.
        """
        # Odds too high
        eval_result = risk_engine.evaluate_order(
            stake=Decimal("10.0"),
            odds=Decimal("150.0"),  # Exceeds 100 max
            market_type="match_odds",
            currency="USDC",
        )
        assert eval_result.approved is False


class TestTenBetVenueRiskPolicy:
    """
    Test 10bet venue risk policy.
    """

    @pytest.fixture
    def risk_engine(self):
        """
        Create 10bet risk engine with bonus.
        """
        return TenBetVenueRiskPolicy(bonus_amount=Decimal(1000))

    def test_rollover_tracking(self, risk_engine):
        """
        Test rollover requirement tracking.
        """
        # Bet that qualifies for rollover
        eval_result = risk_engine.evaluate_order(
            stake=Decimal(500),
            odds=Decimal("1.80"),  # Above 1.60 minimum
            market_type="match_odds",
            currency="ZAR",
        )

        # Should have warnings about rollover
        assert len(eval_result.warnings) > 0

        # Update rollover
        risk_engine.update_rollover(
            stake=Decimal(500),
            odds=Decimal("1.80"),
            market_type="match_odds",
        )

        # Check progress
        progress = risk_engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(500)
        assert progress["rollover_required"] == Decimal(5000)  # 1000 * 5x

    def test_market_exclusion(self, risk_engine):
        """
        Test market exclusions from rollover.
        """
        # Over/Under market (excluded)
        eval_result = risk_engine.evaluate_order(
            stake=Decimal(500),
            odds=Decimal("2.0"),
            market_type="total_goals",  # Excluded
            currency="ZAR",
        )

        # Should warn about exclusion
        warnings_text = " ".join(eval_result.warnings)
        assert "excluded" in warnings_text.lower()
