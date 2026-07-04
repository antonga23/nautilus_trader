# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet venue risk policy.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

from nautilus_trader.adapters.betting.venue_risk import BettingVenueRiskPolicy
from nautilus_trader.adapters.betting.venue_risk import OddsRequirementRule
from nautilus_trader.adapters.betting.venue_risk import RiskEvaluation
from nautilus_trader.adapters.betting.venue_risk import RolloverRule
from nautilus_trader.adapters.betting.venue_risk import StakeLimitRule


class EasybetVenueRiskPolicy(BettingVenueRiskPolicy):
    """
    Risk engine for Easybet venue.

    Implements Easybet-specific promotional terms (similar to 10bet):
    - 8x rollover requirement
    - Minimum odds of 1.50
    - Maximum stake of R2000
    - Excluded markets from rollover

    """

    def __init__(
        self,
        max_stake_zar: Decimal = Decimal(2000),
        rollover_multiplier: Decimal = Decimal(8),
        min_rollover_odds: Decimal = Decimal("1.50"),
        bonus_amount: Decimal = Decimal(0),
    ):
        self._max_stake_zar = max_stake_zar
        self._rollover_multiplier = rollover_multiplier
        self._min_rollover_odds = min_rollover_odds
        self._bonus_amount = bonus_amount
        self._rollover_completed = Decimal(0)
        super().__init__(venue_name="EASYBET")

    def _initialize_rules(self) -> None:
        """
        Initialize Easybet-specific risk rules.
        """
        # Stake limits
        self.add_rule(
            StakeLimitRule(
                max_stake=self._max_stake_zar,
                currency="ZAR",
                description=f"Maximum stake: R{self._max_stake_zar}",
                is_critical=True,
            ),
        )

        # Rollover requirements
        self.add_rule(
            RolloverRule(
                multiplier=self._rollover_multiplier,
                min_odds=self._min_rollover_odds,
                description=(
                    f"{self._rollover_multiplier}x rollover with "
                    f"minimum odds {self._min_rollover_odds}"
                ),
                is_critical=False,
            ),
        )

        # Odds requirements
        self.add_rule(
            OddsRequirementRule(
                min_odds=self._min_rollover_odds,
                description=f"Minimum odds for rollover: {self._min_rollover_odds}",
                is_critical=False,
            ),
        )

    def evaluate_order(
        self,
        stake: Decimal,
        odds: Decimal,
        market_type: str,
        currency: str = "ZAR",
        current_exposure: Decimal | None = None,
    ) -> RiskEvaluation:
        """
        Override to add rollover-specific checks.
        """
        base_eval = super().evaluate_order(
            stake,
            odds,
            market_type,
            currency,
            current_exposure=current_exposure,
        )
        violations = list(base_eval.violations)
        warnings = list(base_eval.warnings)

        # Rollover tracking
        if self._bonus_amount > 0:
            rollover_required = self._bonus_amount * self._rollover_multiplier
            rollover_remaining = rollover_required - self._rollover_completed

            if rollover_remaining > 0:
                excluded_markets = {"total_goals", "over_under", "handicap"}
                if market_type in excluded_markets:
                    warnings.append(
                        f"Market '{market_type}' excluded from rollover. "
                        f"Remaining: R{rollover_remaining}",
                    )
                elif odds < self._min_rollover_odds:
                    warnings.append(
                        f"Odds {odds} below minimum {self._min_rollover_odds}. "
                        f"Not counted. Remaining: R{rollover_remaining}",
                    )
                else:
                    warnings.append(
                        f"Counts toward rollover. Remaining: R{rollover_remaining}",
                    )

        return RiskEvaluation(
            approved=len(violations) == 0,
            violations=violations,
            warnings=warnings,
        )

    def update_rollover(self, stake: Decimal, odds: Decimal, market_type: str) -> None:
        """
        Update rollover progress after bet placement.
        """
        excluded_markets = {"total_goals", "over_under", "handicap"}
        if market_type not in excluded_markets and odds >= self._min_rollover_odds:
            self._rollover_completed += stake

    def get_rollover_progress(self) -> dict:
        """
        Get rollover progress information.
        """
        rollover_required = self._bonus_amount * self._rollover_multiplier
        rollover_remaining = max(Decimal(0), rollover_required - self._rollover_completed)

        return {
            "bonus_amount": self._bonus_amount,
            "rollover_multiplier": self._rollover_multiplier,
            "rollover_required": rollover_required,
            "rollover_completed": self._rollover_completed,
            "rollover_remaining": rollover_remaining,
            "rollover_percentage": (
                (self._rollover_completed / rollover_required * 100)
                if rollover_required > 0
                else Decimal(100)
            ),
        }


# Backward-compatible alias while adapter call sites migrate away from the
# misleading RiskEngine name.
EasybetRiskEngine = EasybetVenueRiskPolicy
