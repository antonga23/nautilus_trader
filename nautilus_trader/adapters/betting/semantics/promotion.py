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
# skipcq
"""
Promotion policy for mined semantic rules.
"""

from __future__ import annotations

from dataclasses import replace

from nautilus_trader.adapters.betting.semantics.store import RuleStore
from nautilus_trader.adapters.betting.semantics.types import MinedRule
from nautilus_trader.adapters.betting.semantics.types import PromotionStatus
from nautilus_trader.adapters.betting.semantics.types import RelationshipType
from nautilus_trader.adapters.betting.semantics.types import RuleValidationStats
from nautilus_trader.adapters.betting.semantics.types import SafetyTier
from nautilus_trader.adapters.betting.semantics.types import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics.types import SettlementState


def _deterministic_complementary_partition(template: SemanticRuleTemplate) -> bool:
    # An exhaustive WIN/LOSE partition over every result state is provably complementary
    # regardless of observation count, so execution safety is a topology guarantee rather
    # than a statistical one — provided support is deterministic and uncontradicted.
    settlement_a = template.settlement_a
    settlement_b = template.settlement_b
    if not settlement_a or len(settlement_a) != len(settlement_b):
        return False
    if len(settlement_a) != len(template.result_states):
        return False
    win_lose = {SettlementState.WIN.value, SettlementState.LOSE.value}
    if not all(
        {state_a, state_b} == win_lose
        for state_a, state_b in zip(settlement_a, settlement_b, strict=True)
    ):
        return False
    return template.support.deterministic and template.support.mismatch_rate <= 0.01


class RulePromotionPolicy:
    """
    Encodes the tiered promotion gate between mined candidates and runtime rules.
    """

    def __init__(self, allowlisted_venue_scopes: set[tuple[str, ...]] | None = None) -> None:
        self._allowlisted_venue_scopes = allowlisted_venue_scopes or set()

    def classify_rule_tier(
        self,
        rule: MinedRule,
        stats: RuleValidationStats | None,
        *,
        allowlisted: bool = False,
        venue_agnostic: bool = False,
    ) -> tuple[SafetyTier, tuple[str, ...]]:
        reasons: list[str] = []
        if rule.relationship_type == RelationshipType.DANGEROUS_NON_EQUIVALENT.value:
            return SafetyTier.AUDIT_ONLY, ("dangerous_non_equivalent",)
        if "price_correlation_only" in rule.caveats:
            return SafetyTier.AUDIT_ONLY, ("price_correlation_only",)
        if rule.has_unknown or "unknown_settlement_present" in rule.caveats:
            return SafetyTier.AUDIT_ONLY, ("unknown_settlement_present",)

        reasons.append("deterministic_payoff_semantics")
        tier = SafetyTier.TOPOLOGY_SAFE

        if (
            stats is not None
            and stats.sample_count >= 3
            and stats.mismatch_rate <= 0.01
            and stats.confidence >= 0.95
        ):
            tier = SafetyTier.VENUE_SAFE
            reasons.append("provider_scoped_support")
            if rule.has_void:
                reasons.append("void_states_present")
            if rule.has_partial:
                reasons.append("partial_settlement_present")

        execution_scope_ok = (
            allowlisted or tuple(sorted(rule.venue_scope)) in self._allowlisted_venue_scopes
        )
        if (
            tier != SafetyTier.AUDIT_ONLY
            and rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
            and not rule.has_void
            and not rule.has_partial
            and stats is not None
            and stats.promotable
            and (execution_scope_ok or not venue_agnostic)
        ):
            tier = SafetyTier.EXECUTION_SAFE
            reasons.append("execution_safe_complementary_coverage")
        elif (
            tier == SafetyTier.VENUE_SAFE
            and rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
            and (rule.has_void or rule.has_partial)
        ):
            tier = SafetyTier.COVERAGE_SAFE
            reasons.append("coverage_requires_void_partial_risk_handling")
        elif tier == SafetyTier.VENUE_SAFE:
            tier = SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
            reasons.append("same_venue_risk_engine_elevation_required")

        return tier, tuple(sorted(set(reasons)))

    def can_promote(
        self,
        rule: MinedRule,
        stats: RuleValidationStats | None,
        *,
        allowlisted: bool = False,
        venue_agnostic: bool = False,
    ) -> bool:
        tier, _ = self.classify_rule_tier(
            rule,
            stats,
            allowlisted=allowlisted,
            venue_agnostic=venue_agnostic,
        )
        return tier != SafetyTier.AUDIT_ONLY

    def promote(
        self,
        store: RuleStore,
        rule: MinedRule,
        stats: RuleValidationStats | None,
        *,
        allowlisted: bool = False,
        venue_agnostic: bool = False,
    ) -> MinedRule | None:
        tier, reasons = self.classify_rule_tier(
            rule,
            stats,
            allowlisted=allowlisted,
            venue_agnostic=venue_agnostic,
        )
        if tier == SafetyTier.AUDIT_ONLY:
            return None
        promoted = replace(
            rule,
            promotion_status=PromotionStatus.PROMOTED.value,
            safety_tier=tier.value,
            eligibility_reasons=reasons,
            validation=stats,
        )
        store.save_promoted(promoted)
        return store.load_promoted(rule.rule_id)

    def _template_execution_safe_reasons(
        self,
        template: SemanticRuleTemplate,
        *,
        allowlisted: bool,
        venue_agnostic: bool,
    ) -> tuple[str, ...]:
        if (
            template.relationship_type != RelationshipType.COMPLEMENTARY_COVERAGE.value
            or template.has_void
            or template.has_partial
            or template.has_unknown
        ):
            return ()
        execution_scope_ok = (
            allowlisted or template.provider_scope in self._allowlisted_venue_scopes
        )
        multi_provider_ok = template.support.provider_count >= 2 or execution_scope_ok
        if venue_agnostic and not multi_provider_ok:
            return ()
        if template.support.catalog_promotable:
            return ("execution_safe_complementary_coverage",)
        # Only cross-venue templates get the topology bypass: cross-venue mining is too
        # starved to ever meet catalog_promotable thresholds (#224), while same-venue
        # templates must still earn promotion through settled observations.
        if template.support.provider_count >= 2 and _deterministic_complementary_partition(
            template,
        ):
            return (
                "deterministic_complementary_partition",
                "execution_safe_complementary_coverage",
            )
        return ()

    def classify_template_tier(
        self,
        template: SemanticRuleTemplate,
        *,
        allowlisted: bool = False,
        venue_agnostic: bool = False,
    ) -> tuple[SafetyTier, tuple[str, ...]]:
        reasons: list[str] = []
        if template.relationship_type == RelationshipType.DANGEROUS_NON_EQUIVALENT.value:
            return SafetyTier.AUDIT_ONLY, ("dangerous_non_equivalent",)
        if "price_correlation_only" in template.caveats:
            return SafetyTier.AUDIT_ONLY, ("price_correlation_only",)
        if template.has_unknown or "unknown_settlement_present" in template.caveats:
            return SafetyTier.AUDIT_ONLY, ("unknown_settlement_present",)

        tier = SafetyTier.TOPOLOGY_SAFE
        reasons.append("deterministic_payoff_semantics")

        if template.support.venue_safe:
            tier = SafetyTier.VENUE_SAFE
            reasons.append("provider_scoped_support")
            if template.has_void:
                reasons.append("void_states_present")
            if template.has_partial:
                reasons.append("partial_settlement_present")

        execution_reasons = self._template_execution_safe_reasons(
            template,
            allowlisted=allowlisted,
            venue_agnostic=venue_agnostic,
        )
        if execution_reasons:
            tier = SafetyTier.EXECUTION_SAFE
            reasons.extend(execution_reasons)
        elif (
            tier == SafetyTier.VENUE_SAFE
            and template.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
            and (template.has_void or template.has_partial)
        ):
            tier = SafetyTier.COVERAGE_SAFE
            reasons.append("coverage_requires_void_partial_risk_handling")
        elif tier == SafetyTier.VENUE_SAFE:
            tier = SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
            reasons.append("same_venue_risk_engine_elevation_required")

        return tier, tuple(sorted(set(reasons)))

    def can_promote_template(
        self,
        template: SemanticRuleTemplate,
        *,
        allowlisted: bool = False,
        venue_agnostic: bool = False,
    ) -> bool:
        tier, _ = self.classify_template_tier(
            template,
            allowlisted=allowlisted,
            venue_agnostic=venue_agnostic,
        )
        return tier != SafetyTier.AUDIT_ONLY

    def promote_template(
        self,
        store: RuleStore,
        template: SemanticRuleTemplate,
        *,
        allowlisted: bool = False,
        venue_agnostic: bool = False,
    ) -> SemanticRuleTemplate | None:
        tier, reasons = self.classify_template_tier(
            template,
            allowlisted=allowlisted,
            venue_agnostic=venue_agnostic,
        )
        if tier == SafetyTier.AUDIT_ONLY:
            return None
        promoted = replace(
            template,
            provider_scope=template.support.providers,
            venue_agnostic=venue_agnostic or template.support.provider_count >= 2 or allowlisted,
            promotion_status=PromotionStatus.PROMOTED.value,
            safety_tier=tier.value,
            eligibility_reasons=reasons,
        )
        store.save_promoted_template(promoted)
        return store.load_promoted_template(template.template_id)
