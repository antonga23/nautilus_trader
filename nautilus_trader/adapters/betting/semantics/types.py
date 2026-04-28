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


class PromotionStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


class SafetyTier(str, Enum):
    AUDIT_ONLY = "AUDIT_ONLY"
    TOPOLOGY_SAFE = "TOPOLOGY_SAFE"
    VENUE_SAFE = "VENUE_SAFE"
    EXECUTION_SAFE = "EXECUTION_SAFE"
    EXECUTION_SAFE_SAME_VENUE_ELIGIBLE = "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE"


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
        return (
            self.sample_count >= 25
            and self.mismatch_rate <= 0.01
            and self.confidence >= 0.99
        )


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
        return cls(pattern_id=_stable_digest("pattern", payload), **payload)

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
        return cls(pattern_id=_stable_digest("pattern", payload), **payload)


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
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
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
