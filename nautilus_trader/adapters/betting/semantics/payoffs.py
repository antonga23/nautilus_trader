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
Deterministic payoff vector construction for normalized betting selections.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR

from nautilus_trader.adapters.betting.semantics.types import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.adapters.betting.semantics.types import PayoffVector
from nautilus_trader.adapters.betting.semantics.types import SettlementState


WIN = SettlementState.WIN.value
LOSE = SettlementState.LOSE.value
VOID = SettlementState.VOID.value
UNKNOWN = SettlementState.UNKNOWN.value
HALF_WIN = SettlementState.HALF_WIN.value
HALF_LOSE = SettlementState.HALF_LOSE.value
PARTIAL_WIN = SettlementState.PARTIAL_WIN.value
PARTIAL_LOSE = SettlementState.PARTIAL_LOSE.value
THREE_WAY_STATES = ("HOME_WIN", "DRAW", "AWAY_WIN")


class SettlementPluginRegistry:
    """
    Dispatches payoff vector generation by canonical market family.
    """

    @classmethod
    def build(cls, selection: NormalizedSelection) -> PayoffVector:
        market_type = CanonicalMarketType(selection.market_type)
        builders = {
            CanonicalMarketType.MATCH_ODDS: cls._match_odds,
            CanonicalMarketType.WINNER: cls._winner,
            CanonicalMarketType.DOUBLE_CHANCE: cls._double_chance,
            CanonicalMarketType.DRAW_NO_BET: cls._draw_no_bet,
            CanonicalMarketType.ASIAN_HANDICAP: cls._handicap,
            CanonicalMarketType.POINT_SPREAD: cls._handicap,
            CanonicalMarketType.EUROPEAN_HANDICAP: cls._european_handicap,
            CanonicalMarketType.TOTALS: cls._totals,
            CanonicalMarketType.TEAM_TOTALS: cls._totals,
            CanonicalMarketType.BOTH_TEAMS_TO_SCORE: cls._yes_no,
            CanonicalMarketType.ODD_EVEN: cls._yes_no,
            CanonicalMarketType.BINARY_OPTION: cls._binary_option,
        }
        builder = builders.get(market_type)
        if builder is None:
            return cls._unknown(selection)
        return builder(selection)

    @classmethod
    def _match_odds(cls, selection: NormalizedSelection) -> PayoffVector:
        settlement_by_selection = {
            "HOME": (WIN, LOSE, LOSE),
            "DRAW": (LOSE, WIN, LOSE),
            "AWAY": (LOSE, LOSE, WIN),
        }
        return cls._vector(selection, THREE_WAY_STATES, settlement_by_selection)

    @classmethod
    def _winner(cls, selection: NormalizedSelection) -> PayoffVector:
        if selection.selection in {"HOME", "AWAY"}:
            settlement_by_selection = {
                "HOME": (WIN, LOSE),
                "AWAY": (LOSE, WIN),
            }
            return cls._vector(selection, ("HOME_WIN", "AWAY_WIN"), settlement_by_selection)
        if selection.selection in {"YES", "NO"}:
            settlement_by_selection = {
                "YES": (WIN, LOSE),
                "NO": (LOSE, WIN),
            }
            return cls._vector(selection, ("EVENT_TRUE", "EVENT_FALSE"), settlement_by_selection)
        return cls._unknown(selection)

    @classmethod
    def _double_chance(cls, selection: NormalizedSelection) -> PayoffVector:
        settlement_by_selection = {
            "HOME_DRAW": (WIN, WIN, LOSE),
            "AWAY_DRAW": (LOSE, WIN, WIN),
            "HOME_AWAY": (WIN, LOSE, WIN),
        }
        return cls._vector(selection, THREE_WAY_STATES, settlement_by_selection)

    @classmethod
    def _draw_no_bet(cls, selection: NormalizedSelection) -> PayoffVector:
        settlement_by_selection = {
            "HOME": (WIN, VOID, LOSE),
            "AWAY": (LOSE, VOID, WIN),
        }
        return cls._vector(selection, THREE_WAY_STATES, settlement_by_selection)

    @classmethod
    def _handicap(cls, selection: NormalizedSelection) -> PayoffVector:
        line = cls._line(selection)
        if line is None or selection.selection not in {"HOME", "AWAY"}:
            return cls._unknown(selection)

        if cls._is_quarter_line(line):
            lower, upper = cls._split_quarter_line(line)
            states = cls._handicap_states(lower, upper)
            lower_settlement = cls._handicap_settlement(selection.selection, lower, states)
            upper_settlement = cls._handicap_settlement(selection.selection, upper, states)
            settlement = tuple(
                cls._combine_split_settlement(left, right)
                for left, right in zip(lower_settlement, upper_settlement, strict=True)
            )
        else:
            states = cls._handicap_states(line)
            settlement = cls._handicap_settlement(selection.selection, line, states)

        return PayoffVector(
            sport=selection.sport,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
            result_states=states,
            settlement=settlement,
        )

    @classmethod
    def _european_handicap(cls, selection: NormalizedSelection) -> PayoffVector:
        line = cls._line(selection)
        if line is None or selection.selection not in {"HOME", "DRAW", "AWAY"}:
            return cls._unknown(selection)

        states = cls._handicap_states(line)
        settlement: list[str] = []
        for state in states:
            home_margin = cls._margin_for_state(state)
            adjusted = home_margin + line
            if selection.selection == "HOME":
                settlement.append(WIN if adjusted > 0 else LOSE)
            elif selection.selection == "DRAW":
                settlement.append(WIN if adjusted == 0 else LOSE)
            else:
                settlement.append(WIN if adjusted < 0 else LOSE)

        return PayoffVector(
            sport=selection.sport,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
            result_states=states,
            settlement=tuple(settlement),
        )

    @classmethod
    def _totals(cls, selection: NormalizedSelection) -> PayoffVector:
        line = cls._line(selection)
        if line is None or selection.selection not in {"OVER", "UNDER"}:
            return cls._unknown(selection)

        states: tuple[str, ...]
        settlement: tuple[str, ...]
        line_key = cls._line_key(line)
        if cls._is_quarter_line(line):
            lower, upper = cls._split_quarter_line(line)
            whole_component = lower if lower == lower.to_integral_value() else upper
            lower_is_whole = whole_component == lower
            states = (
                f"TOTAL_ABOVE_{line_key}",
                f"TOTAL_AT_SPLIT_{line_key}",
                f"TOTAL_BELOW_{line_key}",
            )
            if selection.selection == "OVER":
                settlement = (WIN, HALF_LOSE if lower_is_whole else HALF_WIN, LOSE)
            else:
                settlement = (LOSE, HALF_WIN if lower_is_whole else HALF_LOSE, WIN)
        elif line == line.to_integral_value():
            states = (
                f"TOTAL_OVER_{line_key}",
                f"TOTAL_EQUAL_{line_key}",
                f"TOTAL_UNDER_{line_key}",
            )
            if selection.selection == "OVER":
                settlement = (WIN, VOID, LOSE)
            else:
                settlement = (LOSE, VOID, WIN)
        else:
            states = (f"TOTAL_OVER_{line_key}", f"TOTAL_UNDER_{line_key}")
            if selection.selection == "OVER":
                settlement = (WIN, LOSE)
            else:
                settlement = (LOSE, WIN)

        return PayoffVector(
            sport=selection.sport,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
            result_states=states,
            settlement=settlement,
        )

    @classmethod
    def _yes_no(cls, selection: NormalizedSelection) -> PayoffVector:
        if selection.selection not in {"YES", "NO", "ODD", "EVEN"}:
            return cls._unknown(selection)

        if selection.market_type == CanonicalMarketType.ODD_EVEN.value:
            states = ("ODD", "EVEN")
            settlement = (WIN, LOSE) if selection.selection == "ODD" else (LOSE, WIN)
        else:
            states = ("YES", "NO")
            settlement = (WIN, LOSE) if selection.selection == "YES" else (LOSE, WIN)

        return PayoffVector(
            sport=selection.sport,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
            result_states=states,
            settlement=settlement,
        )

    @classmethod
    def _binary_option(cls, selection: NormalizedSelection) -> PayoffVector:
        resolution_policy = dict(selection.resolution_policy)
        if selection.selection not in {"YES", "NO"}:
            return cls._unknown(selection)
        if resolution_policy.get("tie_or_unknown") in {"50_50", "unknown"}:
            return cls._unknown(selection)
        settlement = (WIN, LOSE) if selection.selection == "YES" else (LOSE, WIN)
        return PayoffVector(
            sport=selection.sport,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
            result_states=("EVENT_TRUE", "EVENT_FALSE"),
            settlement=settlement,
        )

    @staticmethod
    def _vector(
        selection: NormalizedSelection,
        result_states: tuple[str, ...],
        settlement_by_selection: Mapping[str, tuple[str, ...]],
    ) -> PayoffVector:
        return PayoffVector(
            sport=selection.sport,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
            result_states=result_states,
            settlement=settlement_by_selection.get(
                selection.selection,
                tuple(UNKNOWN for _ in result_states),
            ),
        )

    @staticmethod
    def _unknown(selection: NormalizedSelection) -> PayoffVector:
        return PayoffVector(
            sport=selection.sport,
            market_type=selection.market_type,
            selection=selection.selection,
            params=selection.params,
            result_states=("UNKNOWN",),
            settlement=(UNKNOWN,),
        )

    @staticmethod
    def _line(selection: NormalizedSelection) -> Decimal | None:
        raw = selection.param("line") or selection.param("total") or selection.param("handicap")
        if raw is None:
            return None
        try:
            return Decimal(str(raw).split("|", 1)[0])
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _is_quarter_line(line: Decimal) -> bool:
        fractional = abs(line % Decimal(1))
        return fractional in {Decimal("0.25"), Decimal("0.75")}

    @staticmethod
    def _split_quarter_line(line: Decimal) -> tuple[Decimal, Decimal]:
        lower = (line * 2).to_integral_value(rounding=ROUND_FLOOR) / Decimal(2)
        upper = lower + Decimal("0.5")
        return lower, upper

    @staticmethod
    def _line_key(line: Decimal) -> str:
        if line == 0:
            return "0"
        return format(line.normalize(), "f").replace("-", "minus_").replace(".", "_")

    @classmethod
    def _handicap_states(
        cls,
        line: Decimal,
        second_line: Decimal | None = None,
    ) -> tuple[str, ...]:
        lines = [abs(line)]
        if second_line is not None:
            lines.append(abs(second_line))
        if all(item <= Decimal("0.5") for item in lines):
            return THREE_WAY_STATES
        max_abs = int(max(lines).to_integral_value(rounding=ROUND_CEILING)) + 2
        states = [f"HOME_BY_{max_abs}_PLUS"]
        states.extend(f"HOME_BY_{value}" for value in range(max_abs - 1, 0, -1))
        states.append("DRAW")
        states.extend(f"AWAY_BY_{value}" for value in range(1, max_abs))
        states.append(f"AWAY_BY_{max_abs}_PLUS")
        return tuple(states)

    @classmethod
    def _handicap_settlement(
        cls,
        selection: str,
        line: Decimal,
        states: tuple[str, ...],
    ) -> tuple[str, ...]:
        settlement: list[str] = []
        for state in states:
            home_margin = cls._margin_for_state(state)
            outcome_margin = home_margin if selection == "HOME" else -home_margin
            adjusted = outcome_margin + line
            if adjusted > 0:
                settlement.append(WIN)
            elif adjusted < 0:
                settlement.append(LOSE)
            else:
                settlement.append(VOID)
        return tuple(settlement)

    @staticmethod
    def _margin_for_state(state: str) -> Decimal:
        if state == "HOME_WIN":
            return Decimal(1)
        if state == "DRAW":
            return Decimal(0)
        if state == "AWAY_WIN":
            return Decimal(-1)
        if state.startswith("HOME_BY_"):
            raw = state.removeprefix("HOME_BY_").removesuffix("_PLUS")
            return Decimal(raw)
        raw = state.removeprefix("AWAY_BY_").removesuffix("_PLUS")
        return Decimal(f"-{raw}")

    @staticmethod
    def _combine_split_settlement(left: str, right: str) -> str:
        if left == right:
            return left
        pair = {left, right}
        if pair == {WIN, VOID}:
            return HALF_WIN
        if pair == {LOSE, VOID}:
            return HALF_LOSE
        if pair == {WIN, LOSE}:
            return PARTIAL_WIN
        if pair == {VOID, PARTIAL_WIN}:
            return PARTIAL_WIN
        if pair == {VOID, PARTIAL_LOSE}:
            return PARTIAL_LOSE
        return UNKNOWN


class PayoffVectorBuilder:
    """
    Builds settlement/payoff vectors from normalized selections.
    """

    @classmethod
    def build(cls, selection: NormalizedSelection) -> PayoffVector:
        return SettlementPluginRegistry.build(selection)
