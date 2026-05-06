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
Completion verification for semantic rule mining coverage.
"""

from __future__ import annotations

from collections import Counter
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any

from nautilus_trader.adapters.betting.semantics.store import RuleStore
from nautilus_trader.adapters.betting.semantics.types import RelationshipType
from nautilus_trader.adapters.betting.semantics.types import SafetyTier
from nautilus_trader.adapters.betting.semantics.types import SemanticRuleTemplate


DEFAULT_REQUIRED_PROVIDERS = ("CLOUDBET", "SXBET", "POLYMARKET")
SEMANTIC_TARGET_SPORTS = (
    "soccer",
    "basketball",
    "tennis",
    "american_football",
    "ice_hockey",
    "baseball",
)
DEFAULT_TARGET_SPORTS = SEMANTIC_TARGET_SPORTS
DEFAULT_MIN_CANDIDATES = 10
DEFAULT_TARGET_CANDIDATES = 20


@dataclass(frozen=True)
class ProviderCompletion:
    provider: str
    manifest_count: int
    selection_count: int
    event_candidate_count: int
    coverage_proof_count: int
    coverage_hyperedge_count: int
    semantic_candidate_count: int
    template_candidate_count: int
    promoted_template_count: int
    execution_safe_template_count: int
    sports: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class SportCompletion:
    sport: str
    selection_count: int
    event_candidate_count: int
    coverage_proof_count: int
    coverage_hyperedge_count: int
    semantic_candidate_count: int
    template_candidate_count: int
    provider_count: int
    providers: tuple[str, ...]
    min_candidates: int
    target_candidates: int
    blockers: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.blockers

    @property
    def target_reached(self) -> bool:
        return self.semantic_candidate_count >= self.target_candidates


@dataclass(frozen=True)
class SemanticMiningCompletionReport:
    passed: bool
    required_providers: tuple[str, ...]
    target_sports: tuple[str, ...]
    min_candidates: int
    target_candidates: int
    total_normalized_selections: int
    total_event_candidates: int
    total_coverage_proofs: int
    total_coverage_hyperedges: int
    total_semantic_candidates: int
    total_template_candidates: int
    total_promoted_templates: int
    total_execution_safe_templates: int
    safety_tier_counts: tuple[tuple[str, int], ...]
    providers: tuple[ProviderCompletion, ...]
    sports: tuple[SportCompletion, ...]
    promotion_blockers: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_completion_report(  # noqa: C901
    store: RuleStore,
    *,
    required_providers: tuple[str, ...] = DEFAULT_REQUIRED_PROVIDERS,
    target_sports: tuple[str, ...] = DEFAULT_TARGET_SPORTS,
    min_candidates: int = DEFAULT_MIN_CANDIDATES,
    target_candidates: int = DEFAULT_TARGET_CANDIDATES,
) -> SemanticMiningCompletionReport:
    required = tuple(provider.upper() for provider in required_providers)
    sports = tuple(_normalize_sport(sport) for sport in target_sports)

    manifests = [
        manifest
        for manifest_id in store.list_manifest_ids()
        if (manifest := store.load_manifest(manifest_id)) is not None
    ]
    normalized_records = [
        record
        for record_id in store.list_normalized_ids()
        if (record := store.load_normalized_selection(record_id)) is not None
    ]
    candidate_rules = [
        rule
        for rule_id in store.list_candidate_ids()
        if (rule := store.load_candidate(rule_id)) is not None
    ]
    template_candidates = [
        template
        for template_id in store.list_template_candidate_ids()
        if (template := store.load_template_candidate(template_id)) is not None
    ]
    promoted_templates = [
        template
        for template_id in store.list_promoted_template_ids()
        if (template := store.load_promoted_template(template_id)) is not None
    ]
    coverage_proofs = [
        proof
        for proof_id in store.list_coverage_proof_ids()
        if (proof := store.load_coverage_proof(proof_id)) is not None
    ]
    coverage_hyperedges = [
        hyperedge
        for hyperedge_id in store.list_coverage_hyperedge_ids()
        if (hyperedge := store.load_coverage_hyperedge(hyperedge_id)) is not None
    ]
    proof_by_id = {proof.proof_id: proof for proof in coverage_proofs}
    safety_tier_counts = Counter(template.safety_tier for template in template_candidates)

    manifests_by_provider = Counter(manifest.provider.upper() for manifest in manifests)
    selections_by_provider = Counter(record.provider.upper() for record in normalized_records)
    selections_by_sport = Counter(
        _normalize_sport(record.selection.sport) for record in normalized_records
    )
    sports_by_provider: dict[str, set[str]] = defaultdict(set)
    for record in normalized_records:
        sports_by_provider[record.provider.upper()].add(_normalize_sport(record.selection.sport))

    event_candidates_by_provider: Counter[str] = Counter()
    event_candidates_by_sport: Counter[str] = Counter()
    coverage_proofs_by_provider: Counter[str] = Counter()
    coverage_proofs_by_sport: Counter[str] = Counter()
    coverage_hyperedges_by_provider: Counter[str] = Counter()
    coverage_hyperedges_by_sport: Counter[str] = Counter()
    providers_by_sport: dict[str, set[str]] = defaultdict(set)
    template_candidates_by_provider: Counter[str] = Counter()
    template_candidates_by_sport: Counter[str] = Counter()
    if template_candidates:
        for template in template_candidates:
            sport = _normalize_sport(template.sport)
            event_candidates_by_sport[sport] += template.support.observed_count
            template_candidates_by_sport[sport] += 1
            for provider in template.support.providers:
                normalized_provider = provider.upper()
                event_candidates_by_provider[normalized_provider] += template.support.observed_count
                template_candidates_by_provider[normalized_provider] += 1
                providers_by_sport[sport].add(normalized_provider)
    else:
        for rule in candidate_rules:
            sport = _normalize_sport(rule.sport)
            event_candidates_by_sport[sport] += 1
            for provider in rule.venue_scope:
                normalized_provider = provider.upper()
                event_candidates_by_provider[normalized_provider] += 1
                providers_by_sport[sport].add(normalized_provider)

    for proof in coverage_proofs:
        sport = _normalize_sport(proof.universe.sport)
        coverage_proofs_by_sport[sport] += 1
        for provider in proof.coverage_set.provider_scope:
            normalized_provider = provider.upper()
            coverage_proofs_by_provider[normalized_provider] += 1
            providers_by_sport[sport].add(normalized_provider)

    for hyperedge in coverage_hyperedges:
        proof = proof_by_id.get(hyperedge.coverage_proof_id)
        sport = _normalize_sport(proof.universe.sport) if proof is not None else "unknown"
        coverage_hyperedges_by_sport[sport] += 1
        for provider in hyperedge.provider_scope:
            normalized_provider = provider.upper()
            coverage_hyperedges_by_provider[normalized_provider] += 1

    promoted_by_provider: Counter[str] = Counter()
    execution_safe_by_provider: Counter[str] = Counter()
    for template in promoted_templates:
        for provider in template.support.providers:
            normalized_provider = provider.upper()
            promoted_by_provider[normalized_provider] += 1
            if template.execution_safe:
                execution_safe_by_provider[normalized_provider] += 1

    provider_reports = tuple(
        _provider_report(
            provider=provider,
            manifest_count=manifests_by_provider[provider],
            selection_count=selections_by_provider[provider],
            event_candidate_count=event_candidates_by_provider[provider],
            coverage_proof_count=coverage_proofs_by_provider[provider],
            coverage_hyperedge_count=coverage_hyperedges_by_provider[provider],
            template_candidate_count=template_candidates_by_provider[provider],
            promoted_template_count=promoted_by_provider[provider],
            execution_safe_template_count=execution_safe_by_provider[provider],
            sports=tuple(sorted(sports_by_provider[provider])),
        )
        for provider in required
    )

    sport_reports = tuple(
        _sport_report(
            sport=sport,
            selection_count=selections_by_sport[sport],
            event_candidate_count=event_candidates_by_sport[sport],
            coverage_proof_count=coverage_proofs_by_sport[sport],
            coverage_hyperedge_count=coverage_hyperedges_by_sport[sport],
            template_candidate_count=template_candidates_by_sport[sport],
            providers=tuple(sorted(providers_by_sport[sport])),
            min_candidates=min_candidates,
            target_candidates=target_candidates,
        )
        for sport in sports
    )

    blockers = _promotion_blockers(template_candidates)
    passed = all(provider.passed for provider in provider_reports) and all(
        sport.passed for sport in sport_reports
    )
    return SemanticMiningCompletionReport(
        passed=passed,
        required_providers=required,
        target_sports=sports,
        min_candidates=min_candidates,
        target_candidates=target_candidates,
        total_normalized_selections=len(normalized_records),
        total_event_candidates=sum(event_candidates_by_sport.values())
        if template_candidates
        else len(candidate_rules),
        total_coverage_proofs=len(coverage_proofs),
        total_coverage_hyperedges=len(coverage_hyperedges),
        total_semantic_candidates=(
            (sum(event_candidates_by_sport.values()) if template_candidates else len(candidate_rules))
            + len(coverage_proofs)
        ),
        total_template_candidates=len(template_candidates),
        total_promoted_templates=len(promoted_templates),
        total_execution_safe_templates=sum(
            1 for template in promoted_templates if template.execution_safe
        ),
        safety_tier_counts=tuple(sorted(safety_tier_counts.items())),
        providers=provider_reports,
        sports=sport_reports,
        promotion_blockers=tuple(sorted(blockers.items())),
    )


def _provider_report(
    *,
    provider: str,
    manifest_count: int,
    selection_count: int,
    event_candidate_count: int,
    coverage_proof_count: int,
    coverage_hyperedge_count: int,
    template_candidate_count: int,
    promoted_template_count: int,
    execution_safe_template_count: int,
    sports: tuple[str, ...],
) -> ProviderCompletion:
    semantic_candidate_count = event_candidate_count + coverage_proof_count
    blockers: list[str] = []
    if manifest_count == 0:
        blockers.append("missing_manifest")
    if selection_count == 0:
        blockers.append("no_normalized_selections")
    if semantic_candidate_count == 0:
        blockers.append("no_semantic_candidates")
    return ProviderCompletion(
        provider=provider,
        manifest_count=manifest_count,
        selection_count=selection_count,
        event_candidate_count=event_candidate_count,
        coverage_proof_count=coverage_proof_count,
        coverage_hyperedge_count=coverage_hyperedge_count,
        semantic_candidate_count=semantic_candidate_count,
        template_candidate_count=template_candidate_count,
        promoted_template_count=promoted_template_count,
        execution_safe_template_count=execution_safe_template_count,
        sports=sports,
        blockers=tuple(blockers),
    )


def _sport_report(
    *,
    sport: str,
    selection_count: int,
    event_candidate_count: int,
    coverage_proof_count: int,
    coverage_hyperedge_count: int,
    template_candidate_count: int,
    providers: tuple[str, ...],
    min_candidates: int,
    target_candidates: int,
) -> SportCompletion:
    semantic_candidate_count = event_candidate_count + coverage_proof_count
    blockers: list[str] = []
    if selection_count == 0:
        blockers.append("no_normalized_selections")
    if semantic_candidate_count == 0:
        blockers.append("no_semantic_candidates")
    elif semantic_candidate_count < min_candidates:
        blockers.append("below_min_candidate_count")
    return SportCompletion(
        sport=sport,
        selection_count=selection_count,
        event_candidate_count=event_candidate_count,
        coverage_proof_count=coverage_proof_count,
        coverage_hyperedge_count=coverage_hyperedge_count,
        semantic_candidate_count=semantic_candidate_count,
        template_candidate_count=template_candidate_count,
        provider_count=len(providers),
        providers=providers,
        min_candidates=min_candidates,
        target_candidates=target_candidates,
        blockers=tuple(blockers),
    )


def _promotion_blockers(templates: list[SemanticRuleTemplate]) -> Counter[str]:
    blockers: Counter[str] = Counter()
    for template in templates:
        if template.relationship_type == RelationshipType.DANGEROUS_NON_EQUIVALENT.value:
            blockers["dangerous_non_equivalent"] += 1
        if template.safety_tier == SafetyTier.AUDIT_ONLY.value:
            blockers["audit_only"] += 1
        if template.has_unknown:
            blockers["unknown_settlement"] += 1
        if template.has_void:
            blockers["void_settlement"] += 1
        if template.has_partial:
            blockers["partial_settlement"] += 1
        if template.support.observed_count < 10:
            blockers["observed_count_below_10"] += 1
        if template.support.event_count < 3:
            blockers["event_count_below_3"] += 1
        if template.support.provider_count < 2:
            blockers["single_provider_support"] += 1
    return blockers


def _normalize_sport(sport: str) -> str:
    normalized = sport.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "soccer/football": "soccer",
        "soccer_football": "soccer",
        "football": "american_football",
        "american_football": "american_football",
        "hockey": "ice_hockey",
    }
    return aliases.get(normalized, normalized)
