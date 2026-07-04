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
from decimal import Decimal
from decimal import InvalidOperation

from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics.normalization import MarketNormalizer
from nautilus_trader.adapters.betting.semantics.payoffs import THREE_WAY_STATES
from nautilus_trader.adapters.betting.semantics.payoffs import PayoffVectorBuilder
from nautilus_trader.adapters.betting.semantics.polymarket_transform import (
    PolymarketSportsTransformer,
)
from nautilus_trader.adapters.betting.semantics.types import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics.types import MinedRule
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.adapters.betting.semantics.types import PayoffVector
from nautilus_trader.adapters.betting.semantics.types import RelationshipType
from nautilus_trader.adapters.betting.semantics.types import SettlementState


TWO_WAY_RESULT_STATES = ("HOME_WIN", "AWAY_WIN")
CROSS_FAMILY_PROJECTION_CAVEAT = "cross_family_partition_projection"
_HALF_POINT_LINE = Decimal("0.5")
_WIN_LOSE = frozenset({SettlementState.WIN.value, SettlementState.LOSE.value})
_NO_DRAW_SPORTS = PolymarketSportsTransformer.NO_DRAW_SPORTS
_CROSS_FAMILY_TWO_WAY_MARKETS = frozenset(
    {
        CanonicalMarketType.WINNER.value,
        CanonicalMarketType.POINT_SPREAD.value,
    },
)


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
        alternate_totals = self._alternate_totals_coverage_vectors(
            selection_a,
            selection_b,
        )
        projected_two_way: tuple[PayoffVector, PayoffVector] | None = None
        relationship_type: RelationshipType | None
        if alternate_totals is not None:
            vector_a, vector_b = alternate_totals
            relationship_type = RelationshipType.COMPLEMENTARY_COVERAGE
        else:
            projected_two_way = self._cross_family_two_way_vectors(
                selection_a,
                selection_b,
                vector_a,
                vector_b,
            )
            if projected_two_way is not None:
                vector_a, vector_b = projected_two_way
            relationship_type = self._relationship_type(
                selection_a,
                selection_b,
                vector_a,
                vector_b,
            )
        if relationship_type is None:
            return None

        confidence = self._confidence(relationship_type)
        caveats = self._caveats(selection_a, selection_b, vector_a, vector_b)
        if projected_two_way is not None:
            caveats = tuple(sorted({*caveats, CROSS_FAMILY_PROJECTION_CAVEAT}))
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
    def cross_family_partition_states(
        cls,
        selection: NormalizedSelection,
        vector: PayoffVector,
    ) -> tuple[str, ...] | None:
        projected = cls._two_way_partition_vector(selection, vector)
        if projected is None:
            return None
        return projected.result_states

    @classmethod
    def _cross_family_two_way_vectors(
        cls,
        selection_a: NormalizedSelection,
        selection_b: NormalizedSelection,
        vector_a: PayoffVector,
        vector_b: PayoffVector,
    ) -> tuple[PayoffVector, PayoffVector] | None:
        if vector_a.result_states == vector_b.result_states:
            return None
        if {selection_a.market_type, selection_b.market_type} != _CROSS_FAMILY_TWO_WAY_MARKETS:
            return None
        if selection_a.sport != selection_b.sport or selection_a.scope != selection_b.scope:
            return None
        projected_a = cls._two_way_partition_vector(selection_a, vector_a)
        if projected_a is None:
            return None
        projected_b = cls._two_way_partition_vector(selection_b, vector_b)
        if projected_b is None:
            return None
        return projected_a, projected_b

    @classmethod
    def _two_way_partition_vector(
        cls,
        selection: NormalizedSelection,
        vector: PayoffVector,
    ) -> PayoffVector | None:
        # Only partitions provably identical to the two-way fixture-winner partition
        # project: WINNER HOME/AWAY (already on it) and POINT_SPREAD at exactly +/-0.5
        # in a sport that cannot draw (DRAW is unreachable, so dropping it is exact).
        # Binary EVENT_TRUE/EVENT_FALSE states never project: the canonical fields
        # cannot prove which fixture outcome the event maps to (WINNER-family markets
        # like win-to-nil share the same YES/NO vector shape).
        if selection.sport not in _NO_DRAW_SPORTS:
            return None
        if selection.selection not in {"HOME", "AWAY"}:
            return None
        if any(state not in _WIN_LOSE for state in vector.settlement):
            return None
        if selection.market_type == CanonicalMarketType.WINNER.value:
            if vector.result_states != TWO_WAY_RESULT_STATES:
                return None
            return vector
        if selection.market_type != CanonicalMarketType.POINT_SPREAD.value:
            return None
        line = _decimal_param(selection, "line")
        if line is None or abs(line) != _HALF_POINT_LINE:
            return None
        if vector.result_states != THREE_WAY_STATES:
            return None
        settlement = (vector.settlement[0], vector.settlement[2])
        if set(settlement) != _WIN_LOSE:
            return None
        return PayoffVector(
            sport=vector.sport,
            market_type=vector.market_type,
            selection=vector.selection,
            params=vector.params,
            result_states=TWO_WAY_RESULT_STATES,
            settlement=settlement,
        )

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
    def _alternate_totals_coverage_vectors(
        selection_a: NormalizedSelection,
        selection_b: NormalizedSelection,
    ) -> tuple[PayoffVector, PayoffVector] | None:
        line_pair = _alternate_totals_line_pair(selection_a, selection_b)
        if line_pair is None:
            return None
        low_line, high_line, over_selection, under_selection = line_pair
        states = (
            f"TOTAL_BELOW_{_line_key(low_line)}",
            f"TOTAL_BETWEEN_{_line_key(low_line)}_{_line_key(high_line)}",
            f"TOTAL_ABOVE_{_line_key(high_line)}",
        )
        over_vector = PayoffVector(
            sport=over_selection.sport,
            market_type=over_selection.market_type,
            selection=over_selection.selection,
            params=over_selection.params,
            result_states=states,
            settlement=(
                SettlementState.LOSE.value,
                SettlementState.WIN.value,
                SettlementState.WIN.value,
            ),
        )
        under_vector = PayoffVector(
            sport=under_selection.sport,
            market_type=under_selection.market_type,
            selection=under_selection.selection,
            params=under_selection.params,
            result_states=states,
            settlement=(
                SettlementState.WIN.value,
                SettlementState.WIN.value,
                SettlementState.LOSE.value,
            ),
        )
        if selection_a.selection == "OVER":
            return over_vector, under_vector
        return under_vector, over_vector

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
        if RuleClassifier._has_state_where_both_win(vector_a, vector_b):
            caveats.add("overlapping_coverage")
        caveats.update(selection_a.rules_flags)
        caveats.update(selection_b.rules_flags)
        return tuple(sorted(caveats))

    @staticmethod
    def _has_state_where_both_win(vector_a: PayoffVector, vector_b: PayoffVector) -> bool:
        return any(
            state_a == SettlementState.WIN.value and state_b == SettlementState.WIN.value
            for state_a, state_b in zip(vector_a.settlement, vector_b.settlement, strict=True)
        )

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


def _decimal_param(selection: NormalizedSelection, name: str) -> Decimal | None:
    raw = selection.param(name)
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _alternate_totals_line_pair(
    selection_a: NormalizedSelection,
    selection_b: NormalizedSelection,
) -> tuple[Decimal, Decimal, NormalizedSelection, NormalizedSelection] | None:
    if not _same_totals_market_scope(selection_a, selection_b):
        return None
    if {selection_a.selection, selection_b.selection} != {"OVER", "UNDER"}:
        return None
    line_a = _decimal_param(selection_a, "line")
    line_b = _decimal_param(selection_b, "line")
    if line_a is None or line_b is None or line_a == line_b:
        return None
    if not _is_half_point_line(line_a) or not _is_half_point_line(line_b):
        return None
    return _ordered_alternate_total_pair(selection_a, selection_b, line_a, line_b)


def _same_totals_market_scope(
    selection_a: NormalizedSelection,
    selection_b: NormalizedSelection,
) -> bool:
    return (
        selection_a.market_type == CanonicalMarketType.TOTALS.value
        and selection_b.market_type == CanonicalMarketType.TOTALS.value
        and selection_a.scope == selection_b.scope
        and selection_a.sport == selection_b.sport
    )


def _ordered_alternate_total_pair(
    selection_a: NormalizedSelection,
    selection_b: NormalizedSelection,
    line_a: Decimal,
    line_b: Decimal,
) -> tuple[Decimal, Decimal, NormalizedSelection, NormalizedSelection] | None:
    low_line = min(line_a, line_b)
    high_line = max(line_a, line_b)
    over_selection = selection_a if selection_a.selection == "OVER" else selection_b
    under_selection = selection_a if selection_a.selection == "UNDER" else selection_b
    if _decimal_param(over_selection, "line") != low_line:
        return None
    if _decimal_param(under_selection, "line") != high_line:
        return None
    return low_line, high_line, over_selection, under_selection


def _is_half_point_line(line: Decimal) -> bool:
    return abs(line % Decimal(1)) == Decimal("0.5")


def _line_key(line: Decimal) -> str:
    if line == 0:
        return "0"
    return format(line.normalize(), "f").replace("-", "minus_").replace(".", "_")
