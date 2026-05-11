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
Rule classification from payoff vectors.
"""

from __future__ import annotations

import hashlib
import json

from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics.normalization import MarketNormalizer
from nautilus_trader.adapters.betting.semantics.payoffs import PayoffVectorBuilder
from nautilus_trader.adapters.betting.semantics.types import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics.types import MinedRule
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.adapters.betting.semantics.types import PayoffVector
from nautilus_trader.adapters.betting.semantics.types import RelationshipType
from nautilus_trader.adapters.betting.semantics.types import SettlementState


class RuleClassifier:
    """
    Classifies relationships between two betting selections by settlement semantics.
    """

    def __init__(
        self,
        normalizer: MarketNormalizer | None = None,
        vector_builder: PayoffVectorBuilder | None = None,
    ) -> None:
        self._normalizer = normalizer or MarketNormalizer()
        self._vector_builder = vector_builder or PayoffVectorBuilder()

    def classify(
        self,
        a: CryptoBettingInstrument | NormalizedSelection | object,
        b: CryptoBettingInstrument | NormalizedSelection | object,
    ) -> MinedRule | None:
        selection_a = self._coerce_selection(a)
        selection_b = self._coerce_selection(b)
        vector_a = self.build_payoff_vector(selection_a)
        vector_b = self.build_payoff_vector(selection_b)
        return self.classify_precomputed(selection_a, selection_b, vector_a, vector_b)

    def build_payoff_vector(
        self,
        item: CryptoBettingInstrument | NormalizedSelection | object,
    ) -> PayoffVector:
        selection = self._coerce_selection(item)
        return self._vector_builder.build(selection)

    def classify_precomputed(
        self,
        selection_a: NormalizedSelection,
        selection_b: NormalizedSelection,
        vector_a: PayoffVector,
        vector_b: PayoffVector,
    ) -> MinedRule | None:
        relationship_type = self._relationship_type(selection_a, selection_b, vector_a, vector_b)
        if relationship_type is None:
            return None

        confidence = self._confidence(relationship_type)
        caveats = self._caveats(selection_a, selection_b, vector_a, vector_b)
        rule_id = self._rule_id(selection_a, selection_b, vector_a, vector_b, relationship_type)
        return MinedRule(
            rule_id=rule_id,
            relationship_type=relationship_type.value,
            sport=selection_a.sport if selection_a.sport == selection_b.sport else "mixed",
            venue_scope=tuple(sorted({selection_a.venue, selection_b.venue})),
            scope=selection_a.scope if selection_a.scope == selection_b.scope else "mixed",
            market_a=selection_a.market_type,
            selection_a=selection_a.selection,
            params_a=selection_a.params,
            market_b=selection_b.market_type,
            selection_b=selection_b.selection,
            params_b=selection_b.params,
            result_states=vector_a.result_states
            if vector_a.result_states == vector_b.result_states
            else tuple(sorted(set(vector_a.result_states + vector_b.result_states))),
            settlement_a=vector_a.settlement,
            settlement_b=vector_b.settlement,
            confidence=confidence,
            caveats=caveats,
        )

    def _coerce_selection(
        self,
        item: CryptoBettingInstrument | NormalizedSelection | object,
    ) -> NormalizedSelection:
        if isinstance(item, NormalizedSelection):
            return item
        return self._normalizer.normalize(item)

    @classmethod
    def _relationship_type(
        cls,
        selection_a: NormalizedSelection,
        selection_b: NormalizedSelection,
        vector_a: PayoffVector,
        vector_b: PayoffVector,
    ) -> RelationshipType | None:
        if cls._dangerous_handicap_pair(selection_a, selection_b):
            return RelationshipType.DANGEROUS_NON_EQUIVALENT

        if vector_a.result_states != vector_b.result_states:
            return None

        if vector_a.has_unknown or vector_b.has_unknown:
            return None

        if vector_a.settlement == vector_b.settlement:
            return RelationshipType.EQUIVALENT_SELECTION

        if vector_a.has_partial or vector_b.has_partial:
            if cls._has_no_state_where_both_lose(vector_a, vector_b):
                return RelationshipType.PARTIAL_SETTLEMENT_HEDGE
            return None

        if vector_a.has_void or vector_b.has_void:
            if cls._has_no_state_where_both_lose(vector_a, vector_b):
                return RelationshipType.VOID_COMPATIBLE_HEDGE
            return None

        if cls._is_complementary_coverage(vector_a, vector_b):
            return RelationshipType.COMPLEMENTARY_COVERAGE

        return None

    @staticmethod
    def _dangerous_handicap_pair(
        selection_a: NormalizedSelection,
        selection_b: NormalizedSelection,
    ) -> bool:
        types = {selection_a.market_type, selection_b.market_type}
        if types != {
            CanonicalMarketType.ASIAN_HANDICAP.value,
            CanonicalMarketType.EUROPEAN_HANDICAP.value,
        }:
            return False
        return selection_a.selection == selection_b.selection and selection_a.param(
            "line",
        ) == selection_b.param("line")

    @staticmethod
    def _is_complementary_coverage(vector_a: PayoffVector, vector_b: PayoffVector) -> bool:
        return all(
            {state_a, state_b} == {SettlementState.WIN.value, SettlementState.LOSE.value}
            for state_a, state_b in zip(vector_a.settlement, vector_b.settlement, strict=True)
        )

    @staticmethod
    def _has_no_state_where_both_lose(vector_a: PayoffVector, vector_b: PayoffVector) -> bool:
        return all(
            state_a != SettlementState.LOSE.value or state_b != SettlementState.LOSE.value
            for state_a, state_b in zip(vector_a.settlement, vector_b.settlement, strict=True)
        )

    @staticmethod
    def _confidence(relationship_type: RelationshipType) -> float:
        if relationship_type in {
            RelationshipType.EQUIVALENT_SELECTION,
            RelationshipType.COMPLEMENTARY_COVERAGE,
            RelationshipType.DANGEROUS_NON_EQUIVALENT,
        }:
            return 1.0
        if relationship_type == RelationshipType.PARTIAL_SETTLEMENT_HEDGE:
            return 0.80
        return 0.75

    @staticmethod
    def _caveats(
        selection_a: NormalizedSelection,
        selection_b: NormalizedSelection,
        vector_a: PayoffVector,
        vector_b: PayoffVector,
    ) -> tuple[str, ...]:
        caveats: set[str] = {"validate_venue_rules", "price_correlation_not_proof"}
        if selection_a.sport == "soccer" or selection_b.sport == "soccer":
            caveats.add("validate_90_minute_rule_per_venue")
        if selection_a.scope != "full_time" or selection_b.scope != "full_time":
            caveats.add("non_full_time_period")
        if CanonicalMarketType.BINARY_OPTION.value in (
            selection_a.market_family,
            selection_b.market_family,
        ):
            caveats.add("binary_resolution_policy_required")
        if vector_a.has_void or vector_b.has_void:
            caveats.add("void_states_present")
        if vector_a.has_partial or vector_b.has_partial:
            caveats.add("partial_settlement_present")
        if vector_a.has_unknown or vector_b.has_unknown:
            caveats.add("unknown_settlement_present")
        caveats.update(selection_a.rules_flags)
        caveats.update(selection_b.rules_flags)
        return tuple(sorted(caveats))

    @staticmethod
    def _rule_id(
        selection_a: NormalizedSelection,
        selection_b: NormalizedSelection,
        vector_a: PayoffVector,
        vector_b: PayoffVector,
        relationship_type: RelationshipType,
    ) -> str:
        sides = sorted(
            [
                {
                    "market_type": selection_a.market_type,
                    "selection": selection_a.selection,
                    "params": selection_a.params,
                    "settlement": vector_a.settlement,
                },
                {
                    "market_type": selection_b.market_type,
                    "selection": selection_b.selection,
                    "params": selection_b.params,
                    "settlement": vector_b.settlement,
                },
            ],
            key=lambda item: json.dumps(item, sort_keys=True),
        )
        payload = {
            "relationship_type": relationship_type.value,
            "sport": selection_a.sport if selection_a.sport == selection_b.sport else "mixed",
            "venue_scope": tuple(sorted({selection_a.venue, selection_b.venue})),
            "scope": selection_a.scope if selection_a.scope == selection_b.scope else "mixed",
            "result_states": vector_a.result_states
            if vector_a.result_states == vector_b.result_states
            else tuple(sorted(set(vector_a.result_states + vector_b.result_states))),
            "sides": sides,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]
