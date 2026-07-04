# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
SX.bet venue risk policy.
"""

from decimal import Decimal

from nautilus_trader.adapters.betting.venue_risk import BettingVenueRiskPolicy
from nautilus_trader.adapters.betting.venue_risk import MaxExposureRule
from nautilus_trader.adapters.betting.venue_risk import OddsRequirementRule
from nautilus_trader.adapters.betting.venue_risk import RiskEvaluation
from nautilus_trader.adapters.betting.venue_risk import StakeLimitRule


class SXBetVenueRiskPolicy(BettingVenueRiskPolicy):
    """
    Venue risk policy for SX.bet.

    Implements SX.bet-specific risk rules:
    - Minimum bet size (5 USDC)
    - Maximum odds limits
    - Blockchain gas consideration

    """

    def __init__(
        self,
        min_stake_usdc: Decimal = Decimal("5.0"),
        max_stake_usdc: Decimal = Decimal(50000),
        min_odds: Decimal = Decimal("1.01"),
        max_odds: Decimal = Decimal("100.0"),
        max_exposure_usdc: Decimal = Decimal(100000),
    ):
        self._min_stake_usdc = min_stake_usdc
        self._max_stake_usdc = max_stake_usdc
        self._min_odds = min_odds
        self._max_odds = max_odds
        self._max_exposure_usdc = max_exposure_usdc
        super().__init__(venue_name="SXBET")

    def _initialize_rules(self) -> None:
        """
        Initialize SX.bet-specific risk rules.
        """
        # Stake limits
        self.add_rule(
            StakeLimitRule(
                max_stake=self._max_stake_usdc,
                currency="USDC",
                description=f"Maximum stake: {self._max_stake_usdc} USDC",
                is_critical=True,
            ),
        )

        # Odds requirements
        self.add_rule(
            OddsRequirementRule(
                min_odds=self._min_odds,
                max_odds=self._max_odds,
                description=f"Odds between {self._min_odds} and {self._max_odds}",
                is_critical=True,
            ),
        )

        # Exposure limits
        self.add_rule(
            MaxExposureRule(
                max_exposure=self._max_exposure_usdc,
                currency="USDC",
                description=f"Maximum exposure: {self._max_exposure_usdc} USDC",
                is_critical=True,
            ),
        )

    def evaluate_order(
        self,
        stake: Decimal,
        odds: Decimal,
        market_type: str,
        currency: str = "USDC",
        current_exposure: Decimal | None = None,
    ) -> RiskEvaluation:
        """
        Override to add minimum stake check.
        """
        # Check minimum stake
        violations = []
        warnings = []

        if stake < self._min_stake_usdc:
            violations.append(
                f"Stake {stake} {currency} below minimum {self._min_stake_usdc} USDC",
            )

        # Run base evaluation
        base_eval = super().evaluate_order(
            stake,
            odds,
            market_type,
            currency,
            current_exposure=current_exposure,
        )

        # Combine results
        violations.extend(base_eval.violations)
        warnings.extend(base_eval.warnings)

        return RiskEvaluation(
            approved=len(violations) == 0,
            violations=violations,
            warnings=warnings,
            platform_risk_required=base_eval.platform_risk_required,
            venue_policy=self.venue_name,
        )


# Backward-compatible alias while adapter call sites migrate away from the
# misleading RiskEngine name.
SXBetRiskEngine = SXBetVenueRiskPolicy
