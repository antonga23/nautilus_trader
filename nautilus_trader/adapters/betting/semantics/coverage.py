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
Generalized semantic coverage proofs for betting markets.

The pairwise matcher answers whether two selections are directly related. This module
lifts normalized selections into predicates over an outcome universe, so the same
primitives can evaluate two-leg hedges, three-way full books, and larger baskets with
explicit gaps, overlaps, and settlement risks.

"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
import hashlib
from itertools import product
import json

from nautilus_trader.adapters.betting.semantics.payoffs import PayoffVectorBuilder
from nautilus_trader.adapters.betting.semantics.types import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics.types import CoverageBlockerReason
from nautilus_trader.adapters.betting.semantics.types import CoverageGap
from nautilus_trader.adapters.betting.semantics.types import CoverageHyperedge
from nautilus_trader.adapters.betting.semantics.types import CoverageProof
from nautilus_trader.adapters.betting.semantics.types import CoverageRisk
from nautilus_trader.adapters.betting.semantics.types import CoverageSet
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics.types import OutcomeUniverse
from nautilus_trader.adapters.betting.semantics.types import PayoffVector
from nautilus_trader.adapters.betting.semantics.types import RelationshipType
from nautilus_trader.adapters.betting.semantics.types import SafetyTier
from nautilus_trader.adapters.betting.semantics.types import SelectionPredicate
from nautilus_trader.adapters.betting.semantics.types import SettlementState


WIN = SettlementState.WIN.value
LOSE = SettlementState.LOSE.value
VOID = SettlementState.VOID.value
HALF_WIN = SettlementState.HALF_WIN.value
HALF_LOSE = SettlementState.HALF_LOSE.value
PARTIAL_WIN = SettlementState.PARTIAL_WIN.value
PARTIAL_LOSE = SettlementState.PARTIAL_LOSE.value
UNKNOWN = SettlementState.UNKNOWN.value

_PARTIAL_STATES = frozenset({HALF_WIN, HALF_LOSE, PARTIAL_WIN, PARTIAL_LOSE})
_BINARY_SELECTION_GROUPS = (
    frozenset({"OVER", "UNDER"}),
    frozenset({"YES", "NO"}),
    frozenset({"ODD", "EVEN"}),
    frozenset({"HOME", "AWAY"}),
)


@dataclass(frozen=True)
class CoverageMiningReport:
    proof_count: int
    hyperedge_count: int
    complete_count: int
    execution_safe_count: int
    same_venue_eligible_count: int
    blocker_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "proof_count": self.proof_count,
            "hyperedge_count": self.hyperedge_count,
            "complete_count": self.complete_count,
            "execution_safe_count": self.execution_safe_count,
            "same_venue_eligible_count": self.same_venue_eligible_count,
            "blocker_counts": dict(sorted(self.blocker_counts.items())),
        }


class OutcomeUniverseBuilder:
    """
    Creates canonical outcome universes from predicate result states.
    """

    @classmethod
    def from_predicates(cls, predicates: Iterable[SelectionPredicate]) -> OutcomeUniverse:
        predicate_list = list(predicates)
        sport = predicate_list[0].sport if predicate_list else "unknown"
        scope = predicate_list[0].scope if predicate_list else "global"
        state_ids = tuple(
            sorted({state for predicate in predicate_list for state in predicate.result_states}),
        )
        rule_flags = tuple(
            sorted(
                {flag for predicate in predicate_list for flag in predicate.provider_rule_flags},
            ),
        )
        return OutcomeUniverse.from_state_ids(
            sport=sport,
            scope=scope,
            state_ids=state_ids,
            provider_rule_flags=rule_flags,
        )


class SelectionPredicateBuilder:
    """
    Converts normalized selections into coverage predicates.
    """

    @classmethod
    def from_record(cls, record: NormalizedSelectionRecord) -> SelectionPredicate:
        return cls.from_selection(
            record.selection,
            provider=record.provider,
            source_record_id=record.record_id,
        )

    @classmethod
    def from_selection(
        cls,
        selection: NormalizedSelection,
        *,
        provider: str | None = None,
        source_record_id: str = "",
    ) -> SelectionPredicate:
        bucket_predicate = cls._bucket_predicate(
            selection,
            provider=provider or selection.venue,
            source_record_id=source_record_id,
        )
        if bucket_predicate is not None:
            return bucket_predicate
        vector = PayoffVectorBuilder.build(selection)
        return cls.from_vector(
            selection,
            vector,
            provider=provider or selection.venue,
            source_record_id=source_record_id,
        )

    @classmethod
    def _bucket_predicate(
        cls,
        selection: NormalizedSelection,
        *,
        provider: str,
        source_record_id: str,
    ) -> SelectionPredicate | None:
        state_id, bucket_key, bucket_value = cls._bucket_state(selection)
        if not state_id:
            return None

        params = dict(selection.params)
        params.setdefault(bucket_key, bucket_value)
        if selection.market_family == CanonicalMarketType.OTHER.value:
            raw_bucket_market = selection.raw_market_type or selection.raw_market_name
            if raw_bucket_market:
                params.setdefault(
                    "bucket_market",
                    "".join(
                        char if char.isalnum() else "_" for char in raw_bucket_market.lower()
                    ).strip("_"),
                )
        payload = {
            "instrument_id": selection.instrument_id,
            "sport": selection.sport,
            "scope": selection.scope,
            "market_type": selection.market_type,
            "selection": selection.selection,
            "params": tuple(sorted(params.items())),
            "provider": provider,
            "source_record_id": source_record_id,
        }
        return SelectionPredicate(
            predicate_id=_stable_digest("coverage:predicate", payload),
            instrument_id=selection.instrument_id,
            sport=selection.sport,
            scope=selection.scope,
            market_type=selection.market_type,
            market_family=selection.market_family,
            selection=selection.selection,
            params=tuple(sorted((str(key), str(value)) for key, value in params.items())),
            result_states=(state_id,),
            win_states=(state_id,),
            lose_states=(),
            void_states=(),
            partial_states=(),
            unknown_states=(),
            provider=provider,
            event_key=selection.event_key,
            source_record_id=source_record_id,
            caveats=(),
            provider_rule_flags=selection.rules_flags,
        )

    @staticmethod
    def _bucket_state(selection: NormalizedSelection) -> tuple[str | None, str, str]:
        raw_market = selection.raw_market_name.lower()
        if selection.market_family == CanonicalMarketType.CORRECT_SCORE.value:
            if selection.selection.startswith("SCORE_"):
                return (
                    selection.selection,
                    "score",
                    selection.selection.removeprefix("SCORE_").replace("_", "-"),
                )
            if selection.selection.startswith("ANY_OTHER_"):
                return selection.selection, "bucket", selection.selection
        if "exact_goals" in raw_market:
            return selection.selection, "bucket", selection.selection
        if "halftime_fulltime_result" in raw_market or "halftime_fulltime" in raw_market:
            return selection.selection, "bucket", selection.selection
        if "highest_scoring_quarter" in raw_market:
            return selection.selection, "bucket", selection.selection
        if "highest_scoring_inning" in raw_market:
            return selection.selection, "bucket", selection.selection
        if "winning_margin" in raw_market or "margin" in raw_market:
            return selection.selection, "bucket", selection.selection
        if "set_score" in raw_market or "map_score" in raw_market or "exact_sets" in raw_market:
            return selection.selection, "bucket", selection.selection
        return None, "", ""

    @classmethod
    def from_vector(
        cls,
        selection: NormalizedSelection,
        vector: PayoffVector,
        *,
        provider: str,
        source_record_id: str = "",
    ) -> SelectionPredicate:
        win_states: list[str] = []
        lose_states: list[str] = []
        void_states: list[str] = []
        partial_states: list[str] = []
        unknown_states: list[str] = []
        caveats: set[str] = set()

        for state, settlement in zip(vector.result_states, vector.settlement, strict=True):
            if settlement == WIN:
                win_states.append(state)
            elif settlement == LOSE:
                lose_states.append(state)
            elif settlement == VOID:
                void_states.append(state)
                caveats.add(CoverageBlockerReason.VOID_SETTLEMENT.value)
            elif settlement in _PARTIAL_STATES:
                partial_states.append(state)
                caveats.add(CoverageBlockerReason.PARTIAL_SETTLEMENT.value)
            else:
                unknown_states.append(state)
                caveats.add(CoverageBlockerReason.UNKNOWN_SETTLEMENT.value)

        resolution_policy = dict(selection.resolution_policy)
        if resolution_policy.get("tie_or_unknown") in {"50_50", "unknown"}:
            caveats.add(CoverageBlockerReason.AMBIGUOUS_RESOLUTION.value)

        payload = {
            "instrument_id": selection.instrument_id,
            "sport": selection.sport,
            "scope": selection.scope,
            "market_type": selection.market_type,
            "selection": selection.selection,
            "params": selection.params,
            "result_states": vector.result_states,
            "provider": provider,
            "source_record_id": source_record_id,
        }
        return SelectionPredicate(
            predicate_id=_stable_digest("coverage:predicate", payload),
            instrument_id=selection.instrument_id,
            sport=selection.sport,
            scope=selection.scope,
            market_type=selection.market_type,
            market_family=selection.market_family,
            selection=selection.selection,
            params=selection.params,
            result_states=vector.result_states,
            win_states=tuple(win_states),
            lose_states=tuple(lose_states),
            void_states=tuple(void_states),
            partial_states=tuple(partial_states),
            unknown_states=tuple(unknown_states),
            provider=provider,
            event_key=selection.event_key,
            source_record_id=source_record_id,
            caveats=tuple(sorted(caveats)),
            provider_rule_flags=selection.rules_flags,
        )


class CoverageEngine:
    """
    Evaluates coverage sets and discovers event-scoped coverage baskets.
    """

    def evaluate(
        self,
        predicates: Iterable[SelectionPredicate],
        *,
        universe: OutcomeUniverse | None = None,
        relationship_type: str = RelationshipType.COMPLEMENTARY_COVERAGE.value,
    ) -> CoverageProof:
        predicate_list = tuple(predicates)
        if not predicate_list:
            universe = universe or OutcomeUniverse.from_state_ids(
                sport="unknown",
                scope="global",
                state_ids=(),
            )
            coverage_set = CoverageSet.create(
                sport=universe.sport,
                scope=universe.scope,
                event_key="",
                provider_scope=(),
                predicate_ids=(),
                market_families=(),
                relationship_type=relationship_type,
            )
            return _coverage_proof(
                universe=universe,
                coverage_set=coverage_set,
                predicates=(),
                complete=False,
                win_covered_states=(),
                overlapping_win_states=(),
                gaps=(CoverageGap("", CoverageBlockerReason.INCOMPLETE_COVERAGE.value),),
                risks=(),
                relationship_type=relationship_type,
            )

        universe = universe or OutcomeUniverseBuilder.from_predicates(predicate_list)
        event_key = _shared_value(predicate.event_key for predicate in predicate_list)
        coverage_set = CoverageSet.create(
            sport=universe.sport,
            scope=universe.scope,
            event_key=event_key,
            provider_scope=tuple(sorted({predicate.provider for predicate in predicate_list})),
            predicate_ids=tuple(predicate.predicate_id for predicate in predicate_list),
            market_families=tuple(
                sorted({predicate.market_family for predicate in predicate_list}),
            ),
            relationship_type=relationship_type,
        )

        win_map: dict[str, list[str]] = defaultdict(list)
        for predicate in predicate_list:
            for state in predicate.win_states:
                win_map[state].append(predicate.predicate_id)

        gaps = tuple(
            CoverageGap(
                state_id=state_id,
                reason=CoverageBlockerReason.INCOMPLETE_COVERAGE.value,
                detail="No selection wins on this outcome state.",
            )
            for state_id in universe.state_ids
            if not win_map.get(state_id)
        )
        overlapping = tuple(
            sorted(state_id for state_id, winners in win_map.items() if len(winners) > 1),
        )
        risks = list(self._settlement_risks(predicate_list))
        scope_values = {predicate.scope for predicate in predicate_list}
        if len(scope_values) > 1:
            risks.append(
                CoverageRisk(
                    reason=CoverageBlockerReason.SCOPE_MISMATCH.value,
                    detail="Selections span incompatible period/scope values.",
                    severity="audit",
                ),
            )
        risks.extend(
            CoverageRisk(
                reason=CoverageBlockerReason.OVERLAPPING_COVERAGE.value,
                state_id=state_id,
                detail="More than one selection wins on this outcome state.",
                severity="risk",
            )
            for state_id in overlapping
        )

        return _coverage_proof(
            universe=universe,
            coverage_set=coverage_set,
            predicates=predicate_list,
            complete=not gaps,
            win_covered_states=tuple(sorted(win_map)),
            overlapping_win_states=overlapping,
            gaps=gaps,
            risks=tuple(risks),
            relationship_type=relationship_type,
        )

    def discover_event_coverage(
        self,
        records: Iterable[NormalizedSelectionRecord],
    ) -> tuple[list[CoverageProof], list[CoverageHyperedge]]:
        predicates = [SelectionPredicateBuilder.from_record(record) for record in records]
        groups: dict[
            tuple[str, str, str, str, tuple[tuple[str, str], ...]],
            list[SelectionPredicate],
        ] = defaultdict(list)
        for predicate in predicates:
            key = (
                predicate.sport,
                predicate.scope,
                predicate.market_family,
                predicate.market_type,
                _coverage_group_params(predicate),
            )
            groups[key].append(predicate)

        proofs: dict[str, CoverageProof] = {}
        hyperedges: dict[str, CoverageHyperedge] = {}
        for group in groups.values():
            for candidate in _coverage_candidates(group):
                proof = self.evaluate(candidate)
                proofs[proof.proof_id] = proof
                if len(candidate) > 2:
                    hyperedge = CoverageHyperedge.from_proof(proof)
                    hyperedges[hyperedge.hyperedge_id] = hyperedge
        return list(proofs.values()), list(hyperedges.values())

    @staticmethod
    def summarize(
        proofs: Iterable[CoverageProof],
        hyperedges: Iterable[CoverageHyperedge],
    ) -> CoverageMiningReport:
        proof_list = list(proofs)
        hyperedge_list = list(hyperedges)
        blocker_counts: CounterDict = defaultdict(int)
        for proof in proof_list:
            for reason in proof.blocker_reasons:
                blocker_counts[reason] += 1
        return CoverageMiningReport(
            proof_count=len(proof_list),
            hyperedge_count=len(hyperedge_list),
            complete_count=sum(1 for proof in proof_list if proof.complete),
            execution_safe_count=sum(1 for proof in proof_list if proof.execution_safe),
            same_venue_eligible_count=sum(
                1 for proof in proof_list if proof.same_venue_execution_eligible
            ),
            blocker_counts=dict(blocker_counts),
        )

    @staticmethod
    def _settlement_risks(predicates: tuple[SelectionPredicate, ...]) -> tuple[CoverageRisk, ...]:
        risks: list[CoverageRisk] = []
        for predicate in predicates:
            for state_id in predicate.void_states:
                risks.append(
                    CoverageRisk(
                        reason=CoverageBlockerReason.VOID_SETTLEMENT.value,
                        state_id=state_id,
                        detail=f"{predicate.instrument_id} voids or pushes.",
                        severity="risk",
                    ),
                )
            for state_id in predicate.partial_states:
                risks.append(
                    CoverageRisk(
                        reason=CoverageBlockerReason.PARTIAL_SETTLEMENT.value,
                        state_id=state_id,
                        detail=f"{predicate.instrument_id} partially settles.",
                        severity="risk",
                    ),
                )
            for state_id in predicate.unknown_states:
                risks.append(
                    CoverageRisk(
                        reason=CoverageBlockerReason.UNKNOWN_SETTLEMENT.value,
                        state_id=state_id,
                        detail=f"{predicate.instrument_id} has unknown settlement.",
                        severity="audit",
                    ),
                )
            for caveat in predicate.caveats:
                if caveat in {
                    CoverageBlockerReason.VOID_SETTLEMENT.value,
                    CoverageBlockerReason.PARTIAL_SETTLEMENT.value,
                    CoverageBlockerReason.UNKNOWN_SETTLEMENT.value,
                }:
                    continue
                risks.append(
                    CoverageRisk(
                        reason=caveat,
                        detail=f"{predicate.instrument_id} carries provider caveat {caveat}.",
                        severity="audit",
                    ),
                )
        return tuple(risks)


def _coverage_candidates(
    predicates: list[SelectionPredicate],
) -> Iterable[tuple[SelectionPredicate, ...]]:
    by_selection: dict[str, list[SelectionPredicate]] = defaultdict(list)
    for predicate in predicates:
        by_selection[predicate.selection].append(predicate)
    selection_set = frozenset(by_selection)
    market_family = predicates[0].market_family if predicates else ""

    for binary_set in _BINARY_SELECTION_GROUPS:
        if binary_set.issubset(selection_set):
            yield from _selection_combinations(by_selection, sorted(binary_set))

    if market_family in {
        CanonicalMarketType.MATCH_ODDS.value,
        CanonicalMarketType.WINNER.value,
        "MATCH_ODDS",
        "WINNER",
    }:
        three_way = ("HOME", "DRAW", "AWAY")
        if all(selection in by_selection for selection in three_way):
            yield from _selection_combinations(by_selection, list(three_way))

    range_predicates = [
        predicate for predicate in predicates if _range_param(predicate) is not None
    ]
    if len(range_predicates) >= 2:
        yield tuple(sorted(range_predicates, key=_range_sort_key))
    elif _bucket_market_group(predicates):
        yield tuple(sorted(predicates, key=lambda predicate: predicate.selection))


def _selection_combinations(
    by_selection: dict[str, list[SelectionPredicate]],
    selections: list[str],
) -> Iterable[tuple[SelectionPredicate, ...]]:
    # Each selection label may be quoted by more than one provider; yield the cross-product
    # across providers so cross-venue baskets (e.g. Cloudbet OVER + Polymarket UNDER) are
    # kept as distinct candidates instead of collapsing to a single arbitrary venue's leg.
    yield from product(*(by_selection[selection] for selection in selections))


def _coverage_group_params(predicate: SelectionPredicate) -> tuple[tuple[str, str], ...]:
    ignored = {"selection", "outcome", "side", "participant", "range", "bucket", "score", "margin"}
    return tuple((key, value) for key, value in predicate.params if key not in ignored)


def _bucket_market_group(predicates: list[SelectionPredicate]) -> bool:
    if len(predicates) < 2:
        return False
    families = {predicate.market_family for predicate in predicates}
    if CanonicalMarketType.CORRECT_SCORE.value in families:
        return True
    return all(_range_param(predicate) is not None for predicate in predicates)


def _blocker_reasons(
    gaps: tuple[CoverageGap, ...],
    risks: tuple[CoverageRisk, ...],
) -> tuple[str, ...]:
    reasons = {gap.reason for gap in gaps}
    reasons.update(risk.reason for risk in risks)
    if not reasons:
        reasons.add(CoverageBlockerReason.POSITIVE.value)
    return tuple(sorted(reasons))


def _coverage_safety_tier(
    *,
    complete: bool,
    risks: tuple[CoverageRisk, ...],
    provider_scope: tuple[str, ...],
    blocker_reasons: tuple[str, ...],
) -> SafetyTier:
    if not complete:
        return SafetyTier.AUDIT_ONLY
    if any(
        reason
        in {
            CoverageBlockerReason.UNKNOWN_SETTLEMENT.value,
            CoverageBlockerReason.AMBIGUOUS_RESOLUTION.value,
        }
        for reason in blocker_reasons
    ):
        return SafetyTier.AUDIT_ONLY
    if risks:
        return SafetyTier.COVERAGE_SAFE
    if len(provider_scope) == 1:
        return SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
    return SafetyTier.EXECUTION_SAFE


def _coverage_proof(
    *,
    universe: OutcomeUniverse,
    coverage_set: CoverageSet,
    predicates: tuple[SelectionPredicate, ...],
    complete: bool,
    win_covered_states: tuple[str, ...],
    overlapping_win_states: tuple[str, ...],
    gaps: tuple[CoverageGap, ...],
    risks: tuple[CoverageRisk, ...],
    relationship_type: str,
) -> CoverageProof:
    blocker_reasons = _blocker_reasons(gaps, risks)
    safety_tier = _coverage_safety_tier(
        complete=complete,
        risks=risks,
        provider_scope=coverage_set.provider_scope,
        blocker_reasons=blocker_reasons,
    )
    proof_payload = {
        "universe_id": universe.universe_id,
        "coverage_set_id": coverage_set.coverage_set_id,
        "predicate_ids": coverage_set.predicate_ids,
        "complete": complete,
        "blocker_reasons": blocker_reasons,
        "relationship_type": relationship_type,
    }
    return CoverageProof(
        proof_id=_stable_digest("coverage:proof", proof_payload),
        universe=universe,
        coverage_set=coverage_set,
        predicates=predicates,
        complete=complete,
        win_covered_states=win_covered_states,
        overlapping_win_states=overlapping_win_states,
        gaps=gaps,
        risks=risks,
        blocker_reasons=blocker_reasons,
        relationship_type=relationship_type,
        safety_tier=safety_tier.value,
        execution_safe=safety_tier == SafetyTier.EXECUTION_SAFE,
        same_venue_execution_eligible=(
            safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
        ),
    )


def _range_param(predicate: SelectionPredicate) -> str | None:
    for key, value in predicate.params:
        if key in {"range", "bucket", "margin", "score"}:
            return value
    return None


def _range_sort_key(predicate: SelectionPredicate) -> tuple[Decimal, Decimal]:
    raw = _range_param(predicate) or ""
    lower, upper = _parse_range(raw)
    return lower, upper


def _parse_range(raw: str) -> tuple[Decimal, Decimal]:
    normal = raw.lower().replace(" ", "")
    if normal.endswith("+"):
        try:
            return Decimal(normal[:-1]), Decimal(999999)
        except InvalidOperation:
            return Decimal(0), Decimal(999999)
    for separator in ("-", "to", ":"):
        if separator in normal:
            left, right = normal.split(separator, 1)
            try:
                return Decimal(left), Decimal(right)
            except InvalidOperation:
                return Decimal(0), Decimal(0)
    try:
        value = Decimal(normal)
    except InvalidOperation:
        value = Decimal(0)
    return value, value


def _shared_value(values: Iterable[str]) -> str:
    unique = {value for value in values if value}
    return unique.pop() if len(unique) == 1 else ""


def _stable_digest(prefix: str, payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8",
    )
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:24]}"


CounterDict = dict[str, int]
