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
Historical validation for mined semantic rules using persisted provider evidence.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
import json

import msgspec

from nautilus_trader.adapters.betting.semantics.normalization import MarketNormalizer
from nautilus_trader.adapters.betting.semantics.store import RuleStore
from nautilus_trader.adapters.betting.semantics.types import MinedRule
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.adapters.betting.semantics.types import RuleValidationStats
from nautilus_trader.adapters.betting.semantics.types import SettlementState
from nautilus_trader.adapters.cloudbet.client.schema import BetResult
from nautilus_trader.adapters.cloudbet.client.schema import GetBetResponse
from nautilus_trader.adapters.cloudbet.client.schema import GetBetsResponse


@dataclass(frozen=True)
class ValidationObservation:
    provider: str
    event_key: str
    signature: tuple[str, str, str, str, tuple[tuple[str, str], ...]]
    settlement: str


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class HistoricalRuleValidator:
    """
    Validates candidate rules against persisted provider snapshots.
    """

    def __init__(
        self,
        store: RuleStore,
        normalizer: MarketNormalizer | None = None,
    ) -> None:
        self._store = store
        self._normalizer = normalizer or MarketNormalizer()

    def validate_store(
        self,
        *,
        provider: str | None = None,
        manifest_id: str | None = None,
        persist: bool = True,
    ) -> list[RuleValidationStats]:
        snapshots = self._load_snapshots(provider=provider, manifest_id=manifest_id)
        observations = self._collect_observations(snapshots)
        stats_list: list[RuleValidationStats] = []

        for rule_id in self._store.list_candidate_ids():
            rule = self._store.load_candidate(rule_id)
            if rule is None:
                continue
            stats = self._validate_rule(rule, observations)
            if stats is None:
                continue
            stats_list.append(stats)
            if persist:
                self._store.save_validation(stats)

        return stats_list

    def _load_snapshots(
        self,
        *,
        provider: str | None,
        manifest_id: str | None,
    ) -> list:
        allowed_snapshot_ids: set[str] | None = None
        if manifest_id is not None:
            manifest = self._store.load_manifest(manifest_id)
            if manifest is None:
                return []
            allowed_snapshot_ids = set(manifest.source_refs)

        snapshots = []
        for snapshot_id in self._store.list_snapshot_ids():
            if allowed_snapshot_ids is not None and snapshot_id not in allowed_snapshot_ids:
                continue
            snapshot = self._store.load_snapshot(snapshot_id)
            if snapshot is None:
                continue
            if provider is not None and snapshot.provider != provider:
                continue
            snapshots.append(snapshot)
        return snapshots

    def _collect_observations(
        self,
        snapshots: Iterable,
    ) -> dict[str, dict[tuple[str, str, str, str, tuple[tuple[str, str], ...]], set[str]]]:
        by_event: dict[
            str,
            dict[tuple[str, str, str, str, tuple[tuple[str, str], ...]], set[str]],
        ] = defaultdict(lambda: defaultdict(set))
        for snapshot in snapshots:
            if snapshot.provider == "CLOUDBET" and snapshot.endpoint.startswith("/pub/v4/bets"):
                for observation in self._cloudbet_observations(snapshot.payload):
                    by_event[observation.event_key][observation.signature].add(observation.settlement)
        return by_event

    def _cloudbet_observations(self, payload: bytes) -> list[ValidationObservation]:
        try:
            response = msgspec.json.decode(payload, type=GetBetsResponse)
        except (msgspec.DecodeError, msgspec.ValidationError):
            try:
                data = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return []
            if not isinstance(data, dict) or "items" not in data:
                return []
            if "has_next" in data and "hasNext" not in data:
                data["hasNext"] = data.pop("has_next")
            if "hasNext" not in data:
                return []
            try:
                response = msgspec.json.decode(msgspec.json.encode(data), type=GetBetsResponse)
            except (msgspec.DecodeError, msgspec.ValidationError):
                return []

        observations: list[ValidationObservation] = []
        for item in response.items:
            selection = self._normalizer.normalize(self._cloudbet_selection_snapshot(item))
            observations.append(
                ValidationObservation(
                    provider="CLOUDBET",
                    event_key=selection.event_key,
                    signature=self._selection_signature(selection),
                    settlement=self._cloudbet_settlement(item),
                ),
            )
        return observations

    def _validate_rule(
        self,
        rule: MinedRule,
        observations: dict[str, dict[tuple[str, str, str, str, tuple[tuple[str, str], ...]], set[str]]],
    ) -> RuleValidationStats | None:
        signature_a = self._rule_signature(
            sport=rule.sport,
            scope=rule.scope,
            market_type=rule.market_a,
            selection=rule.selection_a,
            params=rule.params_a,
        )
        signature_b = self._rule_signature(
            sport=rule.sport,
            scope=rule.scope,
            market_type=rule.market_b,
            selection=rule.selection_b,
            params=rule.params_b,
        )
        allowed_pairs = set(zip(rule.settlement_a, rule.settlement_b, strict=True))

        sample_count = 0
        match_count = 0
        mismatch_count = 0

        for event_signatures in observations.values():
            observed_a = event_signatures.get(signature_a)
            observed_b = event_signatures.get(signature_b)
            if not observed_a or not observed_b:
                continue

            sample_count += 1
            observed_pairs = {(left, right) for left in observed_a for right in observed_b}
            if observed_pairs and observed_pairs.issubset(allowed_pairs):
                match_count += 1
            else:
                mismatch_count += 1

        if sample_count == 0:
            return None

        return RuleValidationStats(
            rule_id=rule.rule_id,
            venue_id="|".join(rule.venue_scope) if rule.venue_scope else "CORPUS",
            sport=rule.sport,
            sample_count=sample_count,
            match_count=match_count,
            mismatch_count=mismatch_count,
            confidence=match_count / sample_count,
            last_validated_at=_utc_now(),
        )

    @staticmethod
    def _cloudbet_selection_snapshot(item: GetBetResponse) -> dict[str, str]:
        selection = item.selection or (item.selections[0] if item.selections else None)
        market_url = selection.market_url if selection is not None else item.market_url
        market_name = (
            selection.market_name
            if selection is not None and selection.market_name
            else market_url.partition("/")[0]
        )
        outcome_name = (
            selection.outcome_name
            if selection is not None and selection.outcome_name
            else market_url.partition("/")[2].partition("?")[0]
        )
        params = market_url.partition("?")[2]
        sport_key = market_url.partition(".")[0]
        return {
            "provider": "CLOUDBET",
            "event_id": item.event_id,
            "market_name": market_name,
            "market_type": market_name,
            "market_url": market_url,
            "outcome": outcome_name,
            "params": params,
            "sport_key": sport_key,
        }

    @staticmethod
    def _cloudbet_settlement(item: GetBetResponse) -> str:
        selection = item.selection or (item.selections[0] if item.selections else None)
        result = selection.result if selection is not None and selection.result is not None else item.result
        mapping = {
            BetResult.WIN: SettlementState.WIN.value,
            BetResult.LOSS: SettlementState.LOSE.value,
            BetResult.PUSH: SettlementState.VOID.value,
            BetResult.HALF_WIN: SettlementState.HALF_WIN.value,
            BetResult.HALF_LOSS: SettlementState.HALF_LOSE.value,
            BetResult.PARTIAL: SettlementState.PARTIAL_WIN.value,
            BetResult.CASHED_OUT: SettlementState.UNKNOWN.value,
            BetResult.PENDING: SettlementState.UNKNOWN.value,
        }
        return mapping.get(result, SettlementState.UNKNOWN.value)

    @staticmethod
    def _rule_signature(
        *,
        sport: str,
        scope: str,
        market_type: str,
        selection: str,
        params: tuple[tuple[str, str], ...],
    ) -> tuple[str, str, str, str, tuple[tuple[str, str], ...]]:
        return (sport, scope, market_type, selection, params)

    def _selection_signature(
        self,
        selection: NormalizedSelection,
    ) -> tuple[str, str, str, str, tuple[tuple[str, str], ...]]:
        return self._rule_signature(
            sport=selection.sport,
            scope=selection.scope,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
        )
