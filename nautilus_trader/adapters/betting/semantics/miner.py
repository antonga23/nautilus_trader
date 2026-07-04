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
Offline candidate mining over persisted normalized selections.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
import hashlib
from itertools import combinations
import json
import re

from nautilus_trader.adapters.betting.semantics.classifier import RuleClassifier
from nautilus_trader.adapters.betting.semantics.coverage import CoverageEngine
from nautilus_trader.adapters.betting.semantics.promotion import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics.store import RuleStore
from nautilus_trader.adapters.betting.semantics.types import CoverageHyperedge
from nautilus_trader.adapters.betting.semantics.types import CoverageProof
from nautilus_trader.adapters.betting.semantics.types import MinedRule
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics.types import PayoffVector
from nautilus_trader.adapters.betting.semantics.types import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics.types import TemplateSupportStats


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Cross-venue records of the same fixture rarely share an exact event_key because venue
# cutoff timestamps differ in precision (date-only vs to-the-second). Buckets within this
# tolerance of each other describe the same fixture (mirrors FixtureIdentityResolver's
# start-time tolerance); farther apart (doubleheaders, rematches) they stay separate.
_CROSS_VENUE_START_TOLERANCE_SECS = 7_200


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _split_event_key_time(event_key: str) -> tuple[str, datetime | None, str]:
    """
    Split a composed event key into (family key, start time, precision).

    Precision is "exact" for full timestamps, "date" for date-only cutoffs (some venues
    publish only the fixture date), and "none" when no trailing time segment exists.

    """
    head, sep, tail = event_key.rpartition("|")
    if not sep or not head:
        return event_key, None, "none"
    raw = tail.strip()
    if _DATE_ONLY_RE.match(raw):
        return head, datetime.fromisoformat(raw).replace(tzinfo=UTC), "date"
    # Require an explicit time separator: Python 3.11+ parses bare "YYYYMMDD" (and
    # would misread a trailing event-id segment as a midnight timestamp), which would
    # silently change family membership.
    if "T" not in raw:
        return event_key, None, "none"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return event_key, None, "none"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return head, parsed, "exact"


@dataclass
class _TimeCluster:
    # Stable per-fixture bucket identity: family (venue-independent sport|home|away) +
    # cluster anchor. Used as the evidence key so event_count = distinct fixtures, not
    # distinct (venue x timestamp-precision) keys.
    key: str
    times: list[datetime]
    records: list[NormalizedSelectionRecord]

    def dates(self) -> set[date]:
        return {moment.astimezone(UTC).date() for moment in self.times}


def _cluster_exact_members(
    family_prefix: str,
    members: list[tuple[datetime | None, str, list[NormalizedSelectionRecord]]],
) -> list[_TimeCluster]:
    exact_members: list[tuple[datetime, list[NormalizedSelectionRecord]]] = sorted(
        (
            (start_time, bucket)
            for start_time, precision, bucket in members
            if precision == "exact" and start_time is not None
        ),
        key=lambda item: item[0],
    )
    clusters: list[_TimeCluster] = []
    cluster_anchor: datetime | None = None
    for start_time, bucket in exact_members:
        if (
            clusters
            and cluster_anchor is not None
            and (start_time - cluster_anchor).total_seconds() <= _CROSS_VENUE_START_TOLERANCE_SECS
        ):
            clusters[-1].times.append(start_time)
            clusters[-1].records.extend(bucket)
        else:
            # Anchor stays fixed at the cluster's first member (matches the Rust runtime
            # graph's clustering) so membership is not transitive/chained.
            cluster_anchor = start_time
            clusters.append(
                _TimeCluster(
                    key=f"{family_prefix}@{start_time.astimezone(UTC).isoformat()}",
                    times=[start_time],
                    records=list(bucket),
                ),
            )
    return clusters


def _join_date_members(
    family_prefix: str,
    clusters: list[_TimeCluster],
    members: list[tuple[datetime | None, str, list[NormalizedSelectionRecord]]],
) -> None:
    # Date-only cutoffs join the cluster on the same UTC date when that is
    # unambiguous; otherwise (0 or 2+ clusters on the date) they group by date.
    unmatched_dates: dict[date, _TimeCluster] = {}
    for start_time, precision, bucket in members:
        if precision != "date" or start_time is None:
            continue
        day = start_time.astimezone(UTC).date()
        matching = [cluster for cluster in clusters if day in cluster.dates()]
        if len(matching) == 1:
            matching[0].records.extend(bucket)
        elif day in unmatched_dates:
            unmatched_dates[day].records.extend(bucket)
        else:
            unmatched_dates[day] = _TimeCluster(
                key=f"{family_prefix}@date:{day.isoformat()}",
                times=[start_time],
                records=list(bucket),
            )
    clusters.extend(unmatched_dates.values())


def _family_buckets(
    family_prefix: str,
    members: list[tuple[datetime | None, str, list[NormalizedSelectionRecord]]],
) -> list[tuple[str, list[NormalizedSelectionRecord]]]:
    clusters = _cluster_exact_members(family_prefix, members)
    _join_date_members(family_prefix, clusters, members)
    timeless = [bucket for _, precision, bucket in members if precision == "none"]
    if timeless and len(clusters) == 1:
        # Unambiguous: one cluster in the family, so a record without any start time
        # can only mean that fixture.
        for bucket in timeless:
            clusters[0].records.extend(bucket)
        return [(cluster.key, cluster.records) for cluster in clusters]
    result = [(cluster.key, cluster.records) for cluster in clusters]
    # Timeless records that cannot be attributed to a unique fixture form their own
    # bucket keyed on the family alone.
    result += [(family_prefix, list(bucket)) for bucket in timeless]
    return result


def _tolerant_event_buckets(
    records: Iterable[NormalizedSelectionRecord],
) -> list[tuple[str, list[NormalizedSelectionRecord]]]:
    exact: dict[tuple[str, str, str], list[NormalizedSelectionRecord]] = defaultdict(list)
    for record in records:
        selection = record.selection
        exact[(selection.sport, selection.event_key, selection.scope)].append(record)

    families: dict[
        tuple[str, str, str],
        list[tuple[datetime | None, str, list[NormalizedSelectionRecord]]],
    ] = defaultdict(list)
    for (sport, event_key, scope), bucket in exact.items():
        family_key, start_time, precision = _split_event_key_time(event_key)
        families[(sport, family_key, scope)].append((start_time, precision, bucket))

    merged: list[tuple[str, list[NormalizedSelectionRecord]]] = []
    for (_sport, family_key, scope), members in families.items():
        # family_key already embeds the sport (event_key is sport|home|away|...) or is a
        # globally-unique raw event id; scope disambiguates period markets.
        family_prefix = f"{family_key}|{scope}"
        merged.extend(_family_buckets(family_prefix, members))
    return merged


@dataclass
class _TemplateAccumulator:
    rule: MinedRule
    observed_count: int
    event_keys: set[str]
    providers: set[str]
    sports: set[str]
    example_rule_ids: list[str]
    caveats: set[str]
    unknown_settlement_count: int
    confidence: float


@dataclass(frozen=True)
class _PreparedRecord:
    record: NormalizedSelectionRecord
    vector: PayoffVector
    result_states: tuple[str, ...]


class RuleMiner:
    """
    Mines candidate semantic rules from persisted normalized selections.
    """

    def __init__(
        self,
        store: RuleStore,
        classifier: RuleClassifier | None = None,
    ) -> None:
        self._store = store
        self._classifier = classifier or RuleClassifier()
        self._promotion_policy = RulePromotionPolicy()
        self._coverage_engine = CoverageEngine()

    def load_records(
        self,
        *,
        provider: str | None = None,
        manifest_id: str | None = None,
    ) -> list[NormalizedSelectionRecord]:
        records: list[NormalizedSelectionRecord] = []
        for record_id in self._store.list_normalized_ids():
            record = self._store.load_normalized_selection(record_id)
            if record is None:
                continue
            if provider is not None and record.provider != provider:
                continue
            if manifest_id is not None and record.manifest_id != manifest_id:
                continue
            records.append(record)
        return records

    def mine_candidates(
        self,
        records: Iterable[NormalizedSelectionRecord],
        *,
        persist: bool = True,
    ) -> list[MinedRule]:
        event_rules = self.mine_event_candidates(records, persist=persist)
        discovered: dict[str, MinedRule] = {}
        for rule in event_rules:
            discovered[rule.rule_id] = rule
        return list(discovered.values())

    def mine_event_candidates(
        self,
        records: Iterable[NormalizedSelectionRecord],
        *,
        persist: bool = True,
    ) -> list[MinedRule]:
        """
        Mine event-scoped relationship evidence before template generalization.
        """
        discovered: list[MinedRule] = []
        for bucket_key, bucket in _tolerant_event_buckets(records):
            prepared = self._prepare_bucket(bucket)
            grouped_by_result_states: dict[tuple[str, ...], list[_PreparedRecord]] = defaultdict(
                list,
            )
            for item in prepared:
                grouped_by_result_states[item.result_states].append(item)
            for prepared_bucket in grouped_by_result_states.values():
                for left, right in combinations(prepared_bucket, 2):
                    rule = self._classifier.classify_precomputed(
                        left.record.selection,
                        right.record.selection,
                        left.vector,
                        right.vector,
                    )
                    if rule is None:
                        continue
                    rule = self._with_evidence(rule, bucket_key, left.record, right.record)
                    discovered.append(rule)

        if persist:
            self._store.save_candidates(discovered)

        return discovered

    def mine_store(
        self,
        *,
        provider: str | None = None,
        manifest_id: str | None = None,
        persist: bool = True,
    ) -> list[MinedRule]:
        records = self.load_records(provider=provider, manifest_id=manifest_id)
        return self.mine_candidates(records, persist=persist)

    def mine_templates(
        self,
        records: Iterable[NormalizedSelectionRecord],
        *,
        persist: bool = True,
        persist_event_candidates: bool = True,
    ) -> list[SemanticRuleTemplate]:
        event_rules = self.mine_event_candidates(records, persist=persist_event_candidates)
        return self.generalize(event_rules, persist=persist)

    def mine_templates_from_store(
        self,
        *,
        provider: str | None = None,
        manifest_id: str | None = None,
        persist: bool = True,
        persist_event_candidates: bool = True,
    ) -> list[SemanticRuleTemplate]:
        records = self.load_records(provider=provider, manifest_id=manifest_id)
        return self.mine_templates(
            records,
            persist=persist,
            persist_event_candidates=persist_event_candidates,
        )

    def mine_coverage(
        self,
        records: Iterable[NormalizedSelectionRecord],
        *,
        persist: bool = True,
    ) -> tuple[list[CoverageProof], list[CoverageHyperedge]]:
        """
        Mine generalized event coverage proofs and hyperedges from normalized records.
        """
        proof_by_id: dict[str, CoverageProof] = {}
        hyperedge_by_id: dict[str, CoverageHyperedge] = {}
        for _bucket_key, bucket in _tolerant_event_buckets(records):
            proofs, hyperedges = self._coverage_engine.discover_event_coverage(bucket)
            for proof in proofs:
                proof_by_id[proof.proof_id] = proof
                if persist:
                    self._store.save_coverage_proof(proof)
            for hyperedge in hyperedges:
                hyperedge_by_id[hyperedge.hyperedge_id] = hyperedge
                if persist:
                    self._store.save_coverage_hyperedge(hyperedge)
        return list(proof_by_id.values()), list(hyperedge_by_id.values())

    def mine_coverage_from_store(
        self,
        *,
        provider: str | None = None,
        manifest_id: str | None = None,
        persist: bool = True,
    ) -> tuple[list[CoverageProof], list[CoverageHyperedge]]:
        records = self.load_records(provider=provider, manifest_id=manifest_id)
        return self.mine_coverage(records, persist=persist)

    def _prepare_bucket(
        self,
        bucket: list[NormalizedSelectionRecord],
    ) -> list[_PreparedRecord]:
        prepared: list[_PreparedRecord] = []
        for record in bucket:
            vector = self._classifier.build_payoff_vector(record.selection)
            if vector.has_unknown:
                continue
            prepared.append(
                _PreparedRecord(
                    record=record,
                    vector=vector,
                    result_states=vector.result_states,
                ),
            )
        return prepared

    def generalize(
        self,
        candidates: Iterable[MinedRule],
        *,
        persist: bool = True,
    ) -> list[SemanticRuleTemplate]:
        """
        Generalize event-level candidates into reusable catalog-derived templates.
        """
        now = _utc_now()
        accumulators: dict[str, _TemplateAccumulator] = {}

        for rule in candidates:
            template = SemanticRuleTemplate.from_rule(rule)
            item = accumulators.setdefault(
                template.template_id,
                _TemplateAccumulator(
                    rule=rule,
                    observed_count=0,
                    event_keys=set(),
                    providers=set(),
                    sports=set(),
                    example_rule_ids=[],
                    caveats=set(),
                    unknown_settlement_count=0,
                    confidence=rule.confidence,
                ),
            )
            item.observed_count += 1
            if rule.evidence_event_key:
                item.event_keys.add(rule.evidence_event_key)
            item.providers.update(rule.venue_scope)
            item.sports.add(rule.sport)
            if rule.rule_id not in item.example_rule_ids and len(item.example_rule_ids) < 20:
                item.example_rule_ids.append(rule.rule_id)
            item.caveats.update(rule.caveats)
            if rule.has_unknown:
                item.unknown_settlement_count += 1
            item.confidence = min(item.confidence, rule.confidence)

        templates: list[SemanticRuleTemplate] = []
        for template_id, item in accumulators.items():
            support = TemplateSupportStats(
                template_id=template_id,
                observed_count=item.observed_count,
                event_count=len(item.event_keys),
                provider_count=len(item.providers),
                providers=tuple(sorted(item.providers)),
                sports=tuple(sorted(item.sports)),
                example_rule_ids=tuple(item.example_rule_ids),
                first_seen_at=now,
                last_seen_at=now,
                deterministic=item.unknown_settlement_count == 0,
                unknown_settlement_count=item.unknown_settlement_count,
                mismatch_count=0,
                confidence=item.confidence,
            )
            template = SemanticRuleTemplate.from_rule(
                item.rule,
                support=support,
                provider_scope=support.providers,
            )
            safety_tier, reasons = self._promotion_policy.classify_template_tier(template)
            template = replace(
                template,
                caveats=tuple(sorted(item.caveats)),
                confidence=support.confidence,
                safety_tier=safety_tier.value,
                eligibility_reasons=reasons,
            )
            templates.append(template)
            if persist:
                self._store.save_template_candidate(template)

        return templates

    @staticmethod
    def _with_evidence(
        rule: MinedRule,
        bucket_key: str,
        left: NormalizedSelectionRecord,
        right: NormalizedSelectionRecord,
    ) -> MinedRule:
        # The tolerant bucket key is the fixture identity (family + cluster anchor),
        # shared by all records of one physical fixture across venues and timestamp
        # precisions. Using it for every pair makes template event_count equal the
        # number of distinct fixtures — the diversity gate that guards EXECUTION_SAFE
        # promotion — rather than distinct (venue x timestamp) keys.
        template_id = SemanticRuleTemplate.from_rule(rule).template_id
        return replace(
            rule,
            rule_id=RuleMiner._event_candidate_rule_id(rule, bucket_key, left, right),
            template_id=template_id,
            evidence_event_key=bucket_key,
            evidence_record_ids=(left.record_id, right.record_id),
        )

    @staticmethod
    def _event_candidate_rule_id(
        rule: MinedRule,
        evidence_event_key: str | None,
        left: NormalizedSelectionRecord,
        right: NormalizedSelectionRecord,
    ) -> str:
        """
        Derive an event-observation ID without changing reusable template identity.

        The classifier's base rule ID intentionally describes the semantic relationship
        shape. Event candidates are evidence observations and must not overwrite each
        other when two fixtures expose the same reusable relationship.

        """
        payload = {
            "base_rule_id": rule.rule_id,
            "event_key": evidence_event_key,
            "record_ids": sorted((left.record_id, right.record_id)),
            "instrument_ids": sorted((left.selection.instrument_id, right.selection.instrument_id)),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:24]
        return f"{rule.rule_id}:event:{digest}"
