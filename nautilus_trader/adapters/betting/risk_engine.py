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
Venue risk policies for betting adapters.

Nautilus Trader owns the platform risk engine. These classes model venue-specific
betting constraints which execution clients evaluate before a command reaches the
platform risk engine.

"""

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from enum import Enum


class RiskRuleType(str, Enum):
    """
    Types of risk rules.
    """

    ROLLOVER_REQUIREMENT = "rollover_requirement"
    STAKE_LIMIT = "stake_limit"
    ODDS_REQUIREMENT = "odds_requirement"
    MAX_EXPOSURE = "max_exposure"
    BONUS_WAGERING = "bonus_wagering"
    MARKET_RESTRICTION = "market_restriction"


@dataclass(kw_only=True)
class RiskRule:
    """
    Represents a risk management rule.

    Attributes
    ----------
    rule_type : RiskRuleType
        The type of rule.
    description : str
        Human-readable description.
    is_critical : bool
        If True, violation blocks order submission.

    """

    rule_type: RiskRuleType = field(init=False)
    description: str
    is_critical: bool = True


@dataclass
class RolloverRule(RiskRule):
    """
    Rollover requirement rule.

    Attributes
    ----------
    multiplier : Decimal
        Rollover multiplier (e.g., 5x).
    min_odds : Decimal
        Minimum odds required for rollover.

    """

    multiplier: Decimal
    min_odds: Decimal

    def __post_init__(self):
        self.rule_type = RiskRuleType.ROLLOVER_REQUIREMENT
        if not self.description:
            self.description = (
                f"{self.multiplier}x rollover requirement with minimum odds {self.min_odds}"
            )


@dataclass
class StakeLimitRule(RiskRule):
    """
    Stake limit rule.

    Attributes
    ----------
    max_stake : Decimal
        Maximum stake per bet.
    currency : str
        Currency code.

    """

    max_stake: Decimal
    currency: str

    def __post_init__(self):
        self.rule_type = RiskRuleType.STAKE_LIMIT
        if not self.description:
            self.description = f"Maximum stake: {self.max_stake} {self.currency}"


@dataclass
class OddsRequirementRule(RiskRule):
    """
    Odds requirement rule.

    Attributes
    ----------
    min_odds : Decimal
        Minimum acceptable odds.
    max_odds : Decimal, optional
        Maximum acceptable odds.

    """

    min_odds: Decimal
    max_odds: Decimal | None = None

    def __post_init__(self):
        self.rule_type = RiskRuleType.ODDS_REQUIREMENT
        if not self.description:
            if self.max_odds:
                self.description = f"Odds must be between {self.min_odds} and {self.max_odds}"
            else:
                self.description = f"Minimum odds: {self.min_odds}"


@dataclass
class MaxExposureRule(RiskRule):
    """
    Maximum exposure rule.

    Attributes
    ----------
    max_exposure : Decimal
        Maximum total exposure.
    currency : str
        Currency code.

    """

    max_exposure: Decimal
    currency: str

    def __post_init__(self):
        self.rule_type = RiskRuleType.MAX_EXPOSURE
        if not self.description:
            self.description = f"Maximum exposure: {self.max_exposure} {self.currency}"


@dataclass
class RiskEvaluation:
    """
    Result of risk evaluation.

    Attributes
    ----------
    approved : bool
        If True, order passes all risk checks.
    violations : list[str]
        List of rule violations.
    warnings : list[str]
        List of warnings (non-critical).

    """

    approved: bool
    violations: list[str]
    warnings: list[str]

    @property
    def has_violations(self) -> bool:
        """
        Check if there are any violations.
        """
        return len(self.violations) > 0

    @property
    def has_warnings(self) -> bool:
        """
        Check if there are any warnings.
        """
        return len(self.warnings) > 0


class BettingVenueRiskPolicy(ABC):
    """
    Venue-specific betting risk policy.

    Enforces venue-specific order constraints without replacing Nautilus Trader's
    platform risk engine.

    """

    def __init__(self, venue_name: str):
        self.venue_name = venue_name
        self._rules: list[RiskRule] = []
        self._current_exposure: Decimal = Decimal(0)
        self._initialize_rules()

    @abstractmethod
    def _initialize_rules(self) -> None:
        """
        Initialize venue-specific risk rules.
        """
        raise NotImplementedError

    def add_rule(self, rule: RiskRule) -> None:
        """
        Add a risk rule to the engine.
        """
        self._rules.append(rule)

    def evaluate_order(
        self,
        stake: Decimal,
        odds: Decimal,
        market_type: str,
        currency: str = "USD",
    ) -> RiskEvaluation:
        """
        Evaluate an order against risk rules.

        Parameters
        ----------
        stake : Decimal
            Stake amount.
        odds : Decimal
            Decimal odds.
        market_type : str
            Market type (e.g., "match_odds").
        currency : str, default "USD"
            Currency code.

        Returns
        -------
        RiskEvaluation
            Evaluation result.

        """
        violations: list[str] = []
        warnings: list[str] = []

        for rule in self._rules:
            if isinstance(rule, StakeLimitRule):
                self._evaluate_stake_limit(rule, stake, currency, violations, warnings)
            elif isinstance(rule, OddsRequirementRule):
                self._evaluate_odds_requirement(rule, odds, violations, warnings)
            elif isinstance(rule, MaxExposureRule):
                self._evaluate_max_exposure(rule, stake, currency, violations, warnings)

        approved = len(violations) == 0
        return RiskEvaluation(
            approved=approved,
            violations=violations,
            warnings=warnings,
        )

    @staticmethod
    def _evaluate_stake_limit(
        rule: StakeLimitRule,
        stake: Decimal,
        currency: str,
        violations: list[str],
        warnings: list[str],
    ) -> None:
        """
        Evaluate a stake limit rule against the given stake.
        """
        if stake > rule.max_stake:
            msg = f"Stake {stake} {currency} exceeds maximum {rule.max_stake} {rule.currency}"
            if rule.is_critical:
                violations.append(msg)
            else:
                warnings.append(msg)

    @staticmethod
    def _evaluate_odds_requirement(
        rule: OddsRequirementRule,
        odds: Decimal,
        violations: list[str],
        warnings: list[str],
    ) -> None:
        """
        Evaluate an odds requirement rule against the given odds.
        """
        if odds < rule.min_odds:
            msg = f"Odds {odds} below minimum {rule.min_odds}"
            if rule.is_critical:
                violations.append(msg)
            else:
                warnings.append(msg)

        if rule.max_odds and odds > rule.max_odds:
            msg = f"Odds {odds} above maximum {rule.max_odds}"
            if rule.is_critical:
                violations.append(msg)
            else:
                warnings.append(msg)

    def _evaluate_max_exposure(
        self,
        rule: MaxExposureRule,
        stake: Decimal,
        currency: str,
        violations: list[str],
        warnings: list[str],
    ) -> None:
        """
        Evaluate a max exposure rule against potential exposure.
        """
        potential_exposure = self._current_exposure + stake
        if potential_exposure > rule.max_exposure:
            msg = (
                f"Potential exposure {potential_exposure} {currency} "
                f"exceeds maximum {rule.max_exposure} {rule.currency}"
            )
            if rule.is_critical:
                violations.append(msg)
            else:
                warnings.append(msg)

    def update_exposure(self, amount: Decimal) -> None:
        """
        Update current exposure.
        """
        self._current_exposure += amount

    def reset_exposure(self) -> None:
        """
        Reset current exposure to zero.
        """
        self._current_exposure = Decimal(0)

    def get_rules(self) -> list[RiskRule]:
        """
        Get all registered risk rules.
        """
        return self._rules.copy()
