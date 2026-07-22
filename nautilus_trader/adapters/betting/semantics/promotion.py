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
from nautilus_trader.adapters.betting.semantics.types import PayoffVector
from nautilus_trader.adapters.betting.semantics.types import PromotionStatus
from nautilus_trader.adapters.betting.semantics.types import RelationshipType
from nautilus_trader.adapters.betting.semantics.types import RuleValidationStats
from nautilus_trader.adapters.betting.semantics.types import SafetyTier
from nautilus_trader.adapters.betting.semantics.types import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics.types import SettlementState
from nautilus_trader.adapters.betting.semantics.types import is_partial_compatible_lock
from nautilus_trader.adapters.betting.semantics.types import is_void_compatible_middle
from nautilus_trader.adapters.betting.semantics.types import (
    venue_scope_supports_half_grade_settlement,
)


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


def _payoff_vector(result_states: tuple[str, ...], settlement: tuple[str, ...]) -> PayoffVector:
    # The partial-lock proof only reads ``settlement`` (and the ``has_partial`` derived
    # from it), so a minimal vector carrying the mined per-state settlement is sufficient.
    return PayoffVector(
        sport="",
        market_type="",
        selection="",
        params=(),
        result_states=result_states,
        settlement=settlement,
    )


def _rule_is_partial_compatible_lock(rule: MinedRule) -> bool:
    return is_partial_compatible_lock(
        rule.relationship_type,
        _payoff_vector(rule.result_states, rule.settlement_a),
        _payoff_vector(rule.result_states, rule.settlement_b),
        rule.caveats,
    )


def _template_is_partial_compatible_lock(template: SemanticRuleTemplate) -> bool:
    return is_partial_compatible_lock(
        template.relationship_type,
        _payoff_vector(template.result_states, template.settlement_a),
        _payoff_vector(template.result_states, template.settlement_b),
        template.caveats,
    )


class RulePromotionPolicy:
    """
    Encodes the tiered promotion gate between mined candidates and runtime rules.
    """

    def __init__(self, allowlisted_venue_scopes: set[tuple[str, ...]] | None = None) -> None:
        self._allowlisted_venue_scopes = allowlisted_venue_scopes or set()

    @staticmethod
    def _cross_venue_void_middle_executable(
        rule: MinedRule,
        *,
        tier: SafetyTier,
        venue_agnostic: bool,
        execution_scope_ok: bool,
    ) -> bool:
        return (
            tier == SafetyTier.VENUE_SAFE
            and is_void_compatible_middle(rule.relationship_type, rule.caveats)
            and not rule.has_partial
            and len(set(rule.venue_scope)) >= 2
            and (execution_scope_ok or not venue_agnostic)
        )

    @staticmethod
    def _cross_venue_void_middle_template_executable(
        template: SemanticRuleTemplate,
        *,
        tier: SafetyTier,
    ) -> bool:
        return (
            tier == SafetyTier.VENUE_SAFE
            and is_void_compatible_middle(template.relationship_type, template.caveats)
            and not template.has_partial
            and template.support.provider_count >= 2
        )

    @staticmethod
    def _partial_compatible_lock_executable(
        rule: MinedRule,
        *,
        tier: SafetyTier,
    ) -> bool:
        # A proven partial lock is executable only where every leg's venue half-grades the
        # settlement (see ``HALF_GRADE_SETTLEMENT_VENUES``); with SX.bet grading half-lines
        # as full WON/LOST this restricts partial locks to Cloudbet-legged pairs.
        return (
            tier == SafetyTier.VENUE_SAFE
            and _rule_is_partial_compatible_lock(rule)
            and venue_scope_supports_half_grade_settlement(rule.venue_scope)
        )

    @staticmethod
    def _partial_compatible_lock_template_executable(
        template: SemanticRuleTemplate,
        *,
        tier: SafetyTier,
    ) -> bool:
        return (
            tier == SafetyTier.VENUE_SAFE
            and _template_is_partial_compatible_lock(template)
            and venue_scope_supports_half_grade_settlement(
                template.provider_scope or template.support.providers,
            )
        )

    def classify_rule_tier(
        self,
        rule: MinedRule,
        stats: RuleValidationStats | None,
        *,
        allowlisted: bool = False,
        venue_agnostic: bool = False,
        allow_void_compatible_middles: bool = False,
        allow_partial_compatible_locks: bool = False,
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
        tier, extra_reasons = self._resolve_rule_execution_tier(
            rule,
            stats,
            tier=tier,
            venue_agnostic=venue_agnostic,
            execution_scope_ok=execution_scope_ok,
            allow_void_compatible_middles=allow_void_compatible_middles,
            allow_partial_compatible_locks=allow_partial_compatible_locks,
        )
        reasons.extend(extra_reasons)
        return tier, tuple(sorted(set(reasons)))

    def _resolve_rule_execution_tier(
        self,
        rule: MinedRule,
        stats: RuleValidationStats | None,
        *,
        tier: SafetyTier,
        venue_agnostic: bool,
        execution_scope_ok: bool,
        allow_void_compatible_middles: bool,
        allow_partial_compatible_locks: bool,
    ) -> tuple[SafetyTier, tuple[str, ...]]:
        if (
            tier != SafetyTier.AUDIT_ONLY
            and rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
            and not rule.has_void
            and not rule.has_partial
            and stats is not None
            and stats.promotable
            and (execution_scope_ok or not venue_agnostic)
        ):
            return SafetyTier.EXECUTION_SAFE, ("execution_safe_complementary_coverage",)
        if allow_void_compatible_middles and self._cross_venue_void_middle_executable(
            rule,
            tier=tier,
            venue_agnostic=venue_agnostic,
            execution_scope_ok=execution_scope_ok,
        ):
            # Cross-venue positive-EV middle: the structural no-both-lose guarantee makes it
            # executable across venues under the opt-in. A single-venue void middle stays
            # same-venue-eligible via the final branch, unchanged when the flag is off.
            return SafetyTier.EXECUTION_SAFE, ("execution_safe_void_compatible_middle",)
        if allow_partial_compatible_locks and self._partial_compatible_lock_executable(
            rule,
            tier=tier,
        ):
            # Proven partial lock (per-state combined payoff >= 0 through HALF/VOID grading)
            # on half-grade venues: elevated instead of being demoted for partial handling.
            return SafetyTier.EXECUTION_SAFE, ("execution_safe_partial_compatible_lock",)
        if (
            tier == SafetyTier.VENUE_SAFE
            and rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
            and (rule.has_void or rule.has_partial)
        ):
            return SafetyTier.COVERAGE_SAFE, ("coverage_requires_void_partial_risk_handling",)
        if tier == SafetyTier.VENUE_SAFE:
            return (
                SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE,
                ("same_venue_risk_engine_elevation_required",),
            )
        return tier, ()

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
        allow_void_compatible_middles: bool = False,
        allow_partial_compatible_locks: bool = False,
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

        tier, extra_reasons = self._resolve_template_execution_tier(
            template,
            tier=tier,
            allowlisted=allowlisted,
            venue_agnostic=venue_agnostic,
            allow_void_compatible_middles=allow_void_compatible_middles,
            allow_partial_compatible_locks=allow_partial_compatible_locks,
        )
        reasons.extend(extra_reasons)
        return tier, tuple(sorted(set(reasons)))

    def _resolve_template_execution_tier(
        self,
        template: SemanticRuleTemplate,
        *,
        tier: SafetyTier,
        allowlisted: bool,
        venue_agnostic: bool,
        allow_void_compatible_middles: bool,
        allow_partial_compatible_locks: bool,
    ) -> tuple[SafetyTier, tuple[str, ...]]:
        execution_reasons = self._template_execution_safe_reasons(
            template,
            allowlisted=allowlisted,
            venue_agnostic=venue_agnostic,
        )
        if execution_reasons:
            return SafetyTier.EXECUTION_SAFE, execution_reasons
        if allow_void_compatible_middles and self._cross_venue_void_middle_template_executable(
            template,
            tier=tier,
        ):
            return SafetyTier.EXECUTION_SAFE, ("execution_safe_void_compatible_middle",)
        if allow_partial_compatible_locks and self._partial_compatible_lock_template_executable(
            template,
            tier=tier,
        ):
            return SafetyTier.EXECUTION_SAFE, ("execution_safe_partial_compatible_lock",)
        if (
            tier == SafetyTier.VENUE_SAFE
            and template.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
            and (template.has_void or template.has_partial)
        ):
            return SafetyTier.COVERAGE_SAFE, ("coverage_requires_void_partial_risk_handling",)
        if tier == SafetyTier.VENUE_SAFE:
            return (
                SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE,
                ("same_venue_risk_engine_elevation_required",),
            )
        return tier, ()

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
