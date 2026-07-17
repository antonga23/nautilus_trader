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
Types for semantic betting market rules.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any


class CanonicalMarketType(str, Enum):
    MATCH_ODDS = "MATCH_ODDS"
    DOUBLE_CHANCE = "DOUBLE_CHANCE"
    DRAW_NO_BET = "DRAW_NO_BET"
    ASIAN_HANDICAP = "ASIAN_HANDICAP"
    POINT_SPREAD = "POINT_SPREAD"
    EUROPEAN_HANDICAP = "EUROPEAN_HANDICAP"
    TOTALS = "TOTALS"
    TEAM_TOTALS = "TEAM_TOTALS"
    BOTH_TEAMS_TO_SCORE = "BOTH_TEAMS_TO_SCORE"
    ODD_EVEN = "ODD_EVEN"
    WINNER = "WINNER"
    CORRECT_SCORE = "CORRECT_SCORE"
    OUTRIGHT = "OUTRIGHT"
    BINARY_OPTION = "BINARY_OPTION"
    OTHER = "OTHER"


class SettlementState(str, Enum):
    WIN = "WIN"
    LOSE = "LOSE"
    VOID = "VOID"
    HALF_WIN = "HALF_WIN"
    HALF_LOSE = "HALF_LOSE"
    PARTIAL_WIN = "PARTIAL_WIN"
    PARTIAL_LOSE = "PARTIAL_LOSE"
    UNKNOWN = "UNKNOWN"


class RelationshipType(str, Enum):
    EQUIVALENT_SELECTION = "EQUIVALENT_SELECTION"
    COMPLEMENTARY_COVERAGE = "COMPLEMENTARY_COVERAGE"
    VOID_COMPATIBLE_HEDGE = "VOID_COMPATIBLE_HEDGE"
    PARTIAL_SETTLEMENT_HEDGE = "PARTIAL_SETTLEMENT_HEDGE"
    DANGEROUS_NON_EQUIVALENT = "DANGEROUS_NON_EQUIVALENT"


# Relationships whose two legs form a complementary partition of the outcome
# space (no state where both legs lose), so the complementary-partition
# arb-margin formula ``1 / (1/odds_a + 1/odds_b) - 1`` is meaningful. Backing
# the same outcome on two books (EQUIVALENT_SELECTION) is a value / line-shopping
# signal, not a hedge, so it is deliberately excluded here; DANGEROUS_NON_EQUIVALENT
# is likewise not a guaranteed-coverage pair.
ARB_MARGIN_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        RelationshipType.COMPLEMENTARY_COVERAGE.value,
        RelationshipType.VOID_COMPATIBLE_HEDGE.value,
        RelationshipType.PARTIAL_SETTLEMENT_HEDGE.value,
    },
)


# Settlement-risk caveat/reason tokens marking a void/push outcome. The vocabulary is
# deliberately split across layers: ``coverage.py`` emits ``CoverageBlockerReason`` values
# (``void_settlement``) while ``classifier.py`` / runtime edges carry ``void_states_present``
# and ``push_states_present``. A middle's legs both push together on the void state, so
# these are the *expected* shape of a positive-EV middle, not a danger.
VOID_PUSH_SETTLEMENT_CAVEATS: frozenset[str] = frozenset(
    {
        "void_settlement",
        "void_states_present",
        "push_states_present",
    },
)

# Settlement risks that DISQUALIFY a middle: a leg that can settle UNKNOWN / PARTIAL /
# AMBIGUOUS (or an unresolved provider rule) is not a clean void-only pair. Spelled across
# both the coverage and classifier vocabularies so whichever token a live edge carries is
# caught.
NON_VOID_SETTLEMENT_RISK_CAVEATS: frozenset[str] = frozenset(
    {
        "unknown_settlement",
        "unknown_settlement_present",
        "partial_settlement",
        "partial_settlement_present",
        "partial_states_present",
        "ambiguous_resolution",
        "unresolved_provider_rule",
    },
)


def is_void_compatible_middle(
    relationship_type: str | None,
    caveats: Any,
) -> bool:
    """
    Whether a pair is a positive-EV middle: a ``VOID_COMPATIBLE_HEDGE`` (the structural
    no-both-lose guarantee) whose only settlement risks are VOID / PUSH.

    A non-void settlement risk (UNKNOWN / PARTIAL / AMBIGUOUS) disqualifies it regardless
    of any opt-in flag. This is the single structural predicate reused by the promotion
    tier, the runtime execution gate, and the approval-staging path.
    """
    if relationship_type != RelationshipType.VOID_COMPATIBLE_HEDGE.value:
        return False
    return not (set(caveats) & NON_VOID_SETTLEMENT_RISK_CAVEATS)


def has_only_void_push_settlement_risk(caveats: Any) -> bool:
    """
    Whether a settlement-risk set is void/push-only — at least one VOID/PUSH risk and no
    UNKNOWN / PARTIAL / AMBIGUOUS risk.

    Used by the coverage tier, where a two-leg book is keyed on ``COMPLEMENTARY_COVERAGE``
    rather than the pairwise ``VOID_COMPATIBLE_HEDGE`` relationship, so eligibility turns
    on the risk shape alone.

    """
    reasons = set(caveats)
    return bool(reasons & VOID_PUSH_SETTLEMENT_CAVEATS) and not (
        reasons & NON_VOID_SETTLEMENT_RISK_CAVEATS
    )


class PromotionStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


class SafetyTier(str, Enum):
    AUDIT_ONLY = "AUDIT_ONLY"
    TOPOLOGY_SAFE = "TOPOLOGY_SAFE"
    COVERAGE_SAFE = "COVERAGE_SAFE"
    VENUE_SAFE = "VENUE_SAFE"
    EXECUTION_SAFE = "EXECUTION_SAFE"
    EXECUTION_SAFE_SAME_VENUE_ELIGIBLE = "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE"


class CoverageBlockerReason(str, Enum):
    POSITIVE = "positive"
    NEGATIVE_MARGIN = "negative_margin"
    BELOW_THRESHOLD = "below_threshold"
    STALE = "stale"
    CROSS_CYCLE = "cross_cycle"
    FETCH_LATENCY = "fetch_latency"
    LIQUIDITY = "liquidity"
    TOPOLOGY_ONLY = "topology_only"
    EQUIVALENT_SELECTION = "equivalent_selection"
    VOID_SETTLEMENT = "void_settlement"
    PARTIAL_SETTLEMENT = "partial_settlement"
    SAME_VENUE_POLICY = "same_venue_policy"
    SAME_MARKET_PARAMS_MISMATCH = "same_market_params_mismatch"
    PROVIDER_SCOPE_MISMATCH = "provider_scope_mismatch"
    FIXTURE_IDENTITY_MISMATCH = "fixture_identity_mismatch"
    NO_COMMON_FIXTURE = "no_common_fixture"
    SCOPE_MISMATCH = "scope_mismatch"
    UNSUPPORTED_MARKET_FAMILY = "unsupported_market_family"
    UNKNOWN_SETTLEMENT = "unknown_settlement"
    AMBIGUOUS_RESOLUTION = "ambiguous_resolution"
    NO_SEMANTIC_EDGE = "no_semantic_edge"
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    OVERLAPPING_COVERAGE = "overlapping_coverage"
    PROVIDER_RULE_CAVEAT = "provider_rule_caveat"


@dataclass(frozen=True)
class NormalizedSelection:
    venue: str
    instrument_id: str
    sport: str
    event_key: str
    period: str
    scope: str
    market_type: str
    market_family: str
    selection: str
    params: tuple[tuple[str, str], ...]
    raw_market_name: str
    raw_market_type: str
    raw_outcome: str
    outcome_key: str
    rules_flags: tuple[str, ...] = ()
    resolution_policy: tuple[tuple[str, str], ...] = ()
    source_ref: str = ""

    def param(self, name: str) -> str | None:
        for key, value in self.params:
            if key == name:
                return value
        return None


@dataclass(frozen=True)
class PayoffVector:
    sport: str
    market_type: str
    selection: str
    params: tuple[tuple[str, str], ...]
    result_states: tuple[str, ...]
    settlement: tuple[str, ...]

    @property
    def has_void(self) -> bool:
        return SettlementState.VOID.value in self.settlement

    @property
    def has_partial(self) -> bool:
        partial_states = {
            SettlementState.HALF_WIN.value,
            SettlementState.HALF_LOSE.value,
            SettlementState.PARTIAL_WIN.value,
            SettlementState.PARTIAL_LOSE.value,
        }
        return any(state in partial_states for state in self.settlement)

    @property
    def has_unknown(self) -> bool:
        return SettlementState.UNKNOWN.value in self.settlement


@dataclass(frozen=True)
class RuleValidationStats:
    rule_id: str
    venue_id: str
    sport: str
    sample_count: int = 0
    match_count: int = 0
    mismatch_count: int = 0
    confidence: float = 0.0
    last_validated_at: str | None = None

    @property
    def mismatch_rate(self) -> float:
        if self.sample_count <= 0:
            return 1.0
        return self.mismatch_count / self.sample_count

    @property
    def promotable(self) -> bool:
        return self.sample_count >= 25 and self.mismatch_rate <= 0.01 and self.confidence >= 0.99


@dataclass(frozen=True)
class CorpusSnapshot:
    snapshot_id: str
    provider: str
    endpoint: str
    fetched_at: str
    payload: bytes
    source_ref: str = ""
    content_type: str = "application/json"


@dataclass(frozen=True)
class NormalizedSelectionRecord:
    record_id: str
    provider: str
    selection: NormalizedSelection
    manifest_id: str | None = None


@dataclass(frozen=True)
class SelectionPattern:
    pattern_id: str
    sport: str
    scope: str
    market_type: str
    market_family: str
    selection: str
    params: tuple[tuple[str, str], ...]
    result_states: tuple[str, ...]
    settlement: tuple[str, ...]
    rules_flags: tuple[str, ...] = ()
    resolution_policy: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_selection(
        cls,
        selection: NormalizedSelection,
        vector: PayoffVector,
    ) -> SelectionPattern:
        payload = {
            "sport": selection.sport,
            "scope": selection.scope,
            "market_type": selection.market_type,
            "market_family": selection.market_family,
            "selection": selection.selection,
            "params": selection.params,
            "result_states": vector.result_states,
            "settlement": vector.settlement,
            "rules_flags": selection.rules_flags,
            "resolution_policy": selection.resolution_policy,
        }
        return cls(
            pattern_id=_stable_digest("pattern", payload),
            sport=selection.sport,
            scope=selection.scope,
            market_type=selection.market_type,
            market_family=selection.market_family,
            selection=selection.selection,
            params=selection.params,
            result_states=vector.result_states,
            settlement=vector.settlement,
            rules_flags=selection.rules_flags,
            resolution_policy=selection.resolution_policy,
        )

    @classmethod
    def from_rule_side(
        cls,
        *,
        sport: str,
        scope: str,
        market_type: str,
        selection: str,
        params: tuple[tuple[str, str], ...],
        result_states: tuple[str, ...],
        settlement: tuple[str, ...],
        caveats: tuple[str, ...] = (),
    ) -> SelectionPattern:
        payload = {
            "sport": sport,
            "scope": scope,
            "market_type": market_type,
            "market_family": market_type,
            "selection": selection,
            "params": params,
            "result_states": result_states,
            "settlement": settlement,
            "rules_flags": tuple(sorted(flag for flag in caveats if flag.startswith("includes_"))),
            "resolution_policy": (),
        }
        return cls(
            pattern_id=_stable_digest("pattern", payload),
            sport=sport,
            scope=scope,
            market_type=market_type,
            market_family=market_type,
            selection=selection,
            params=params,
            result_states=result_states,
            settlement=settlement,
            rules_flags=tuple(
                sorted(flag for flag in caveats if flag.startswith("includes_")),
            ),
            resolution_policy=(),
        )


@dataclass(frozen=True)
class TemplateSupportStats:
    template_id: str
    observed_count: int = 0
    event_count: int = 0
    provider_count: int = 0
    providers: tuple[str, ...] = ()
    sports: tuple[str, ...] = ()
    example_rule_ids: tuple[str, ...] = ()
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    deterministic: bool = True
    unknown_settlement_count: int = 0
    mismatch_count: int = 0
    confidence: float = 1.0

    @property
    def mismatch_rate(self) -> float:
        if self.observed_count <= 0:
            return 1.0
        return self.mismatch_count / self.observed_count

    @property
    def catalog_promotable(self) -> bool:
        return (
            self.deterministic
            and self.unknown_settlement_count == 0
            and self.observed_count >= 10
            and self.event_count >= 3
            and self.mismatch_rate <= 0.01
            and self.confidence >= 0.99
        )

    @property
    def topology_safe(self) -> bool:
        return self.deterministic and self.unknown_settlement_count == 0

    @property
    def venue_safe(self) -> bool:
        return (
            self.topology_safe
            and self.observed_count >= 3
            and self.event_count >= 2
            and self.mismatch_rate <= 0.01
            and self.confidence >= 0.95
        )


@dataclass(frozen=True)
class SemanticRuleTemplate:
    template_id: str
    relationship_type: str
    sport: str
    scope: str
    pattern_a: SelectionPattern
    pattern_b: SelectionPattern
    result_states: tuple[str, ...]
    settlement_a: tuple[str, ...]
    settlement_b: tuple[str, ...]
    confidence: float
    caveats: tuple[str, ...]
    support: TemplateSupportStats
    provider_scope: tuple[str, ...] = ()
    venue_agnostic: bool = False
    promotion_status: str = PromotionStatus.CANDIDATE.value
    safety_tier: str = SafetyTier.AUDIT_ONLY.value
    eligibility_reasons: tuple[str, ...] = ()

    @property
    def has_void(self) -> bool:
        return SettlementState.VOID.value in self.settlement_a + self.settlement_b

    @property
    def has_partial(self) -> bool:
        partial_states = {
            SettlementState.HALF_WIN.value,
            SettlementState.HALF_LOSE.value,
            SettlementState.PARTIAL_WIN.value,
            SettlementState.PARTIAL_LOSE.value,
        }
        return any(state in partial_states for state in self.settlement_a + self.settlement_b)

    @property
    def has_unknown(self) -> bool:
        return SettlementState.UNKNOWN.value in self.settlement_a + self.settlement_b

    @property
    def execution_safe(self) -> bool:
        return self.safety_tier == SafetyTier.EXECUTION_SAFE.value

    @property
    def same_venue_execution_eligible(self) -> bool:
        return self.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value

    def applies_to_venues(self, venues: tuple[str, ...]) -> bool:
        if self.venue_agnostic:
            return True
        if not self.provider_scope:
            return False
        return set(venues).issubset(set(self.provider_scope))

    @classmethod
    def from_rule(
        cls,
        rule: MinedRule,
        *,
        support: TemplateSupportStats | None = None,
        provider_scope: tuple[str, ...] | None = None,
        venue_agnostic: bool = False,
        promotion_status: str = PromotionStatus.CANDIDATE.value,
        safety_tier: str = SafetyTier.AUDIT_ONLY.value,
        eligibility_reasons: tuple[str, ...] = (),
    ) -> SemanticRuleTemplate:
        pattern_a = SelectionPattern.from_rule_side(
            sport=rule.sport,
            scope=rule.scope,
            market_type=rule.market_a,
            selection=rule.selection_a,
            params=rule.params_a,
            result_states=rule.result_states,
            settlement=rule.settlement_a,
            caveats=rule.caveats,
        )
        pattern_b = SelectionPattern.from_rule_side(
            sport=rule.sport,
            scope=rule.scope,
            market_type=rule.market_b,
            selection=rule.selection_b,
            params=rule.params_b,
            result_states=rule.result_states,
            settlement=rule.settlement_b,
            caveats=rule.caveats,
        )
        template_id = cls.template_id_for(
            relationship_type=rule.relationship_type,
            sport=rule.sport,
            scope=rule.scope,
            pattern_a=pattern_a,
            pattern_b=pattern_b,
            result_states=rule.result_states,
        )
        resolved_support = support or TemplateSupportStats(
            template_id=template_id,
            observed_count=1,
            event_count=1 if rule.evidence_event_key else 0,
            provider_count=len(set(rule.venue_scope)),
            providers=tuple(sorted(rule.venue_scope)),
            sports=(rule.sport,),
            example_rule_ids=(rule.rule_id,),
            deterministic=not rule.has_unknown,
            unknown_settlement_count=1 if rule.has_unknown else 0,
            confidence=rule.confidence,
        )
        return cls(
            template_id=template_id,
            relationship_type=rule.relationship_type,
            sport=rule.sport,
            scope=rule.scope,
            pattern_a=pattern_a,
            pattern_b=pattern_b,
            result_states=rule.result_states,
            settlement_a=rule.settlement_a,
            settlement_b=rule.settlement_b,
            confidence=rule.confidence,
            caveats=rule.caveats,
            support=resolved_support,
            provider_scope=tuple(sorted(provider_scope or rule.venue_scope)),
            venue_agnostic=venue_agnostic,
            promotion_status=promotion_status,
            safety_tier=safety_tier,
            eligibility_reasons=eligibility_reasons,
        )

    @staticmethod
    def template_id_for(
        *,
        relationship_type: str,
        sport: str,
        scope: str,
        pattern_a: SelectionPattern,
        pattern_b: SelectionPattern,
        result_states: tuple[str, ...],
    ) -> str:
        sides = sorted(
            [
                {
                    "market_type": pattern_a.market_type,
                    "selection": pattern_a.selection,
                    "params": pattern_a.params,
                    "settlement": pattern_a.settlement,
                    "rules_flags": pattern_a.rules_flags,
                    "resolution_policy": pattern_a.resolution_policy,
                },
                {
                    "market_type": pattern_b.market_type,
                    "selection": pattern_b.selection,
                    "params": pattern_b.params,
                    "settlement": pattern_b.settlement,
                    "rules_flags": pattern_b.rules_flags,
                    "resolution_policy": pattern_b.resolution_policy,
                },
            ],
            key=lambda item: json.dumps(item, sort_keys=True),
        )
        payload = {
            "relationship_type": relationship_type,
            "sport": sport,
            "scope": scope,
            "result_states": result_states,
            "sides": sides,
        }
        return _stable_digest("template", payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> SemanticRuleTemplate:
        payload = json.loads(raw.decode("utf-8"))
        payload["pattern_a"] = _selection_pattern_from_payload(payload["pattern_a"])
        payload["pattern_b"] = _selection_pattern_from_payload(payload["pattern_b"])
        payload["support"] = _template_support_from_payload(payload["support"])
        for key in (
            "result_states",
            "settlement_a",
            "settlement_b",
            "caveats",
            "provider_scope",
            "eligibility_reasons",
        ):
            payload[key] = tuple(payload.get(key, ()))
        payload.setdefault("venue_agnostic", False)
        payload.setdefault("promotion_status", PromotionStatus.CANDIDATE.value)
        payload.setdefault("safety_tier", SafetyTier.AUDIT_ONLY.value)
        return cls(**payload)


@dataclass(frozen=True)
class OutcomeState:
    state_id: str
    label: str = ""
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OutcomeUniverse:
    universe_id: str
    sport: str
    scope: str
    states: tuple[OutcomeState, ...]
    provider_rule_flags: tuple[str, ...] = ()

    @classmethod
    def from_state_ids(
        cls,
        *,
        sport: str,
        scope: str,
        state_ids: tuple[str, ...],
        provider_rule_flags: tuple[str, ...] = (),
    ) -> OutcomeUniverse:
        states = tuple(OutcomeState(state_id=state_id, label=state_id) for state_id in state_ids)
        payload = {
            "sport": sport,
            "scope": scope,
            "state_ids": state_ids,
            "provider_rule_flags": provider_rule_flags,
        }
        return cls(
            universe_id=_stable_digest("coverage:universe", payload),
            sport=sport,
            scope=scope,
            states=states,
            provider_rule_flags=provider_rule_flags,
        )

    @property
    def state_ids(self) -> tuple[str, ...]:
        return tuple(state.state_id for state in self.states)


@dataclass(frozen=True)
class SelectionPredicate:
    predicate_id: str
    instrument_id: str
    sport: str
    scope: str
    market_type: str
    market_family: str
    selection: str
    params: tuple[tuple[str, str], ...]
    result_states: tuple[str, ...]
    win_states: tuple[str, ...]
    lose_states: tuple[str, ...]
    void_states: tuple[str, ...] = ()
    push_states: tuple[str, ...] = ()
    partial_states: tuple[str, ...] = ()
    unknown_states: tuple[str, ...] = ()
    provider: str = ""
    event_key: str = ""
    source_record_id: str = ""
    caveats: tuple[str, ...] = ()
    provider_rule_flags: tuple[str, ...] = ()

    @property
    def has_void_or_push(self) -> bool:
        return bool(self.void_states or self.push_states)

    @property
    def has_partial(self) -> bool:
        return bool(self.partial_states)

    @property
    def has_unknown(self) -> bool:
        return bool(self.unknown_states)


@dataclass(frozen=True)
class CoverageSet:
    coverage_set_id: str
    sport: str
    scope: str
    event_key: str
    provider_scope: tuple[str, ...]
    predicate_ids: tuple[str, ...]
    market_families: tuple[str, ...]
    relationship_type: str = RelationshipType.COMPLEMENTARY_COVERAGE.value

    @classmethod
    def create(
        cls,
        *,
        sport: str,
        scope: str,
        event_key: str,
        provider_scope: tuple[str, ...],
        predicate_ids: tuple[str, ...],
        market_families: tuple[str, ...],
        relationship_type: str = RelationshipType.COMPLEMENTARY_COVERAGE.value,
    ) -> CoverageSet:
        payload = {
            "sport": sport,
            "scope": scope,
            "event_key": event_key,
            "provider_scope": provider_scope,
            "predicate_ids": predicate_ids,
            "market_families": market_families,
            "relationship_type": relationship_type,
        }
        return cls(
            coverage_set_id=_stable_digest("coverage:set", payload),
            sport=sport,
            scope=scope,
            event_key=event_key,
            provider_scope=provider_scope,
            predicate_ids=predicate_ids,
            market_families=market_families,
            relationship_type=relationship_type,
        )


@dataclass(frozen=True)
class CoverageGap:
    state_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class CoverageRisk:
    reason: str
    state_id: str = ""
    detail: str = ""
    severity: str = "audit"


@dataclass(frozen=True)
class CoverageProof:
    proof_id: str
    universe: OutcomeUniverse
    coverage_set: CoverageSet
    predicates: tuple[SelectionPredicate, ...]
    complete: bool
    win_covered_states: tuple[str, ...]
    overlapping_win_states: tuple[str, ...]
    gaps: tuple[CoverageGap, ...]
    risks: tuple[CoverageRisk, ...]
    blocker_reasons: tuple[str, ...]
    relationship_type: str
    safety_tier: str
    execution_safe: bool
    same_venue_execution_eligible: bool = False
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CoverageProof:
        payload = json.loads(raw.decode("utf-8"))
        payload["universe"] = _outcome_universe_from_payload(payload["universe"])
        payload["coverage_set"] = _coverage_set_from_payload(payload["coverage_set"])
        payload["predicates"] = tuple(
            _selection_predicate_from_payload(item) for item in payload.get("predicates", ())
        )
        payload["gaps"] = tuple(CoverageGap(**item) for item in payload.get("gaps", ()))
        payload["risks"] = tuple(CoverageRisk(**item) for item in payload.get("risks", ()))
        for key in ("win_covered_states", "overlapping_win_states", "blocker_reasons"):
            payload[key] = tuple(payload.get(key, ()))
        payload.setdefault("same_venue_execution_eligible", False)
        payload.setdefault("confidence", 1.0)
        return cls(**payload)


@dataclass(frozen=True)
class CoverageHyperedge:
    hyperedge_id: str
    coverage_proof_id: str
    instrument_ids: tuple[str, ...]
    provider_scope: tuple[str, ...]
    relationship_type: str
    safety_tier: str
    execution_safe: bool
    caveats: tuple[str, ...] = ()

    @classmethod
    def from_proof(cls, proof: CoverageProof) -> CoverageHyperedge:
        instrument_ids = tuple(predicate.instrument_id for predicate in proof.predicates)
        payload = {
            "coverage_proof_id": proof.proof_id,
            "instrument_ids": instrument_ids,
            "provider_scope": proof.coverage_set.provider_scope,
            "relationship_type": proof.relationship_type,
            "safety_tier": proof.safety_tier,
        }
        return cls(
            hyperedge_id=_stable_digest("coverage:hyperedge", payload),
            coverage_proof_id=proof.proof_id,
            instrument_ids=instrument_ids,
            provider_scope=proof.coverage_set.provider_scope,
            relationship_type=proof.relationship_type,
            safety_tier=proof.safety_tier,
            execution_safe=proof.execution_safe,
            caveats=tuple(sorted(risk.reason for risk in proof.risks)),
        )

    def to_json_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CoverageHyperedge:
        payload = json.loads(raw.decode("utf-8"))
        for key in ("instrument_ids", "provider_scope", "caveats"):
            payload[key] = tuple(payload.get(key, ()))
        return cls(**payload)


@dataclass(frozen=True)
class RuleCorpusManifest:
    manifest_id: str
    provider: str
    fetched_at: str
    endpoint_version: str
    sport_count: int
    event_count: int
    selection_count: int
    market_taxonomy_hash: str
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class MinedRule:
    rule_id: str
    relationship_type: str
    sport: str
    market_a: str
    selection_a: str
    params_a: tuple[tuple[str, str], ...]
    market_b: str
    selection_b: str
    params_b: tuple[tuple[str, str], ...]
    result_states: tuple[str, ...]
    settlement_a: tuple[str, ...]
    settlement_b: tuple[str, ...]
    confidence: float
    caveats: tuple[str, ...]
    venue_scope: tuple[str, ...] = ()
    scope: str = "global"
    promotion_status: str = PromotionStatus.CANDIDATE.value
    safety_tier: str = SafetyTier.AUDIT_ONLY.value
    eligibility_reasons: tuple[str, ...] = ()
    validation: RuleValidationStats | None = None
    template_id: str | None = None
    evidence_event_key: str | None = None
    evidence_record_ids: tuple[str, ...] = ()

    @property
    def has_void(self) -> bool:
        return SettlementState.VOID.value in self.settlement_a + self.settlement_b

    @property
    def has_partial(self) -> bool:
        partial_states = {
            SettlementState.HALF_WIN.value,
            SettlementState.HALF_LOSE.value,
            SettlementState.PARTIAL_WIN.value,
            SettlementState.PARTIAL_LOSE.value,
        }
        return any(state in partial_states for state in self.settlement_a + self.settlement_b)

    @property
    def has_unknown(self) -> bool:
        return SettlementState.UNKNOWN.value in self.settlement_a + self.settlement_b

    @property
    def execution_safe(self) -> bool:
        return self.safety_tier == SafetyTier.EXECUTION_SAFE.value

    @property
    def same_venue_execution_eligible(self) -> bool:
        return self.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.validation is not None:
            data["validation"] = asdict(self.validation)
        return data

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> MinedRule:
        payload = json.loads(raw.decode("utf-8"))
        validation_payload = payload.get("validation")
        validation = (
            RuleValidationStats(**validation_payload)
            if isinstance(validation_payload, dict)
            else None
        )
        payload["validation"] = validation
        payload.setdefault("venue_scope", ())
        payload.setdefault("scope", "global")
        payload.setdefault("safety_tier", SafetyTier.AUDIT_ONLY.value)
        payload.setdefault("template_id", None)
        payload.setdefault("evidence_event_key", None)
        payload.setdefault("evidence_record_ids", ())
        payload.setdefault("eligibility_reasons", ())
        for key in (
            "venue_scope",
            "params_a",
            "params_b",
            "result_states",
            "settlement_a",
            "settlement_b",
            "caveats",
            "evidence_record_ids",
            "eligibility_reasons",
        ):
            value = payload.get(key, ())
            payload[key] = tuple(tuple(item) if isinstance(item, list) else item for item in value)
        return cls(**payload)


def _stable_digest(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8",
    )
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:24]}"


def _selection_pattern_from_payload(payload: dict[str, Any]) -> SelectionPattern:
    for key in ("params", "result_states", "settlement", "rules_flags", "resolution_policy"):
        value = payload.get(key, ())
        payload[key] = tuple(tuple(item) if isinstance(item, list) else item for item in value)
    return SelectionPattern(**payload)


def _template_support_from_payload(payload: dict[str, Any]) -> TemplateSupportStats:
    for key in ("providers", "sports", "example_rule_ids"):
        payload[key] = tuple(payload.get(key, ()))
    return TemplateSupportStats(**payload)


def _outcome_state_from_payload(payload: dict[str, Any]) -> OutcomeState:
    payload["attributes"] = tuple(tuple(item) for item in payload.get("attributes", ()))
    return OutcomeState(**payload)


def _outcome_universe_from_payload(payload: dict[str, Any]) -> OutcomeUniverse:
    payload["states"] = tuple(_outcome_state_from_payload(item) for item in payload["states"])
    payload["provider_rule_flags"] = tuple(payload.get("provider_rule_flags", ()))
    return OutcomeUniverse(**payload)


def _selection_predicate_from_payload(payload: dict[str, Any]) -> SelectionPredicate:
    for key in (
        "params",
        "result_states",
        "win_states",
        "lose_states",
        "void_states",
        "push_states",
        "partial_states",
        "unknown_states",
        "caveats",
        "provider_rule_flags",
    ):
        value = payload.get(key, ())
        payload[key] = tuple(tuple(item) if isinstance(item, list) else item for item in value)
    return SelectionPredicate(**payload)


def _coverage_set_from_payload(payload: dict[str, Any]) -> CoverageSet:
    for key in ("provider_scope", "predicate_ids", "market_families"):
        payload[key] = tuple(payload.get(key, ()))
    return CoverageSet(**payload)
