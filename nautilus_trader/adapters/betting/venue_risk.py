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
Venue-specific betting order preflight.

Nautilus Trader owns the platform `RiskEngine`. Betting adapters add venue rule
checks on top of that platform layer, but they do not replace or subclass the
engine. Execution clients use these policies as a pre-submit filter before
orders continue through the platform risk engine.

"""

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from enum import Enum


class RiskRuleType(str, Enum):
    """
    Types of venue risk rules.
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
    Represents a venue preflight rule.

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
    Result of venue preflight evaluation.
    """

    approved: bool
    violations: list[str]
    warnings: list[str]
    platform_risk_required: bool = True
    venue_policy: str | None = None

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    @property
    def requires_platform_risk_engine(self) -> bool:
        """
        Whether this venue preflight must still pass Nautilus Trader risk checks.
        """
        return self.platform_risk_required


class BettingVenueRiskPolicy(ABC):
    """
    Venue-specific betting risk preflight policy.

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
        raise NotImplementedError

    def add_rule(self, rule: RiskRule) -> None:
        self._rules.append(rule)

    def evaluate_order(
        self,
        stake: Decimal,
        odds: Decimal,
        market_type: str,
        currency: str = "USD",
        current_exposure: Decimal | None = None,
    ) -> RiskEvaluation:
        violations: list[str] = []
        warnings: list[str] = []

        for rule in self._rules:
            if isinstance(rule, StakeLimitRule):
                self._evaluate_stake_limit(rule, stake, currency, violations, warnings)
            elif isinstance(rule, OddsRequirementRule):
                self._evaluate_odds_requirement(rule, odds, violations, warnings)
            elif isinstance(rule, MaxExposureRule):
                self._evaluate_max_exposure(
                    rule,
                    stake,
                    currency,
                    violations,
                    warnings,
                    current_exposure=current_exposure,
                )

        approved = len(violations) == 0
        return RiskEvaluation(
            approved=approved,
            violations=violations,
            warnings=warnings,
            platform_risk_required=True,
            venue_policy=self.venue_name,
        )

    @staticmethod
    def _evaluate_stake_limit(
        rule: StakeLimitRule,
        stake: Decimal,
        currency: str,
        violations: list[str],
        warnings: list[str],
    ) -> None:
        if currency != rule.currency:
            msg = (
                f"Cannot compare stake {stake} {currency} against limit "
                f"{rule.max_stake} {rule.currency}: currency mismatch with no FX conversion"
            )
            if rule.is_critical:
                violations.append(msg)
            else:
                warnings.append(msg)
            return

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
        current_exposure: Decimal | None = None,
    ) -> None:
        if currency != rule.currency:
            msg = (
                f"Cannot compare exposure in {currency} against limit "
                f"{rule.max_exposure} {rule.currency}: currency mismatch with no FX conversion"
            )
            if rule.is_critical:
                violations.append(msg)
            else:
                warnings.append(msg)
            return

        open_exposure = self._current_exposure if current_exposure is None else current_exposure
        potential_exposure = open_exposure + stake
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
        self._current_exposure += amount

    def reset_exposure(self) -> None:
        self._current_exposure = Decimal(0)

    def get_rules(self) -> list[RiskRule]:
        return self._rules.copy()


__all__ = [
    "BettingVenueRiskPolicy",
    "MaxExposureRule",
    "OddsRequirementRule",
    "RiskEvaluation",
    "RiskRule",
    "RiskRuleType",
    "RolloverRule",
    "StakeLimitRule",
]
