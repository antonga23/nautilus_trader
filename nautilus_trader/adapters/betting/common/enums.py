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
Enums for betting adapters.
"""

from enum import Enum
from functools import lru_cache


class SelectionSide(str, Enum):
    """
    Side of a betting selection (BACK = betting FOR, LAY = betting AGAINST).
    """

    BACK = "BACK"
    LAY = "LAY"
    UNDEFINED = "UNDEFINED"

    def __str__(self) -> str:
        return self.value


class BettingMode(str, Enum):
    """
    Mode of betting market (pre-match or live in-play).
    """

    PRE_MATCH = "PRE_MATCH"
    LIVE = "LIVE"
    BOTH = "BOTH"

    def __str__(self) -> str:
        return self.value


class MarketType(str, Enum):
    """
    Common betting market types across venues.

    These are normalized market types for cross-venue matching.

    """

    # Match result markets
    MATCH_ODDS = "match_odds"  # 1X2, Money Line
    DOUBLE_CHANCE = "double_chance"  # 1X, X2, 12
    DRAW_NO_BET = "draw_no_bet"

    # Handicap markets
    ASIAN_HANDICAP = "asian_handicap"
    EUROPEAN_HANDICAP = "european_handicap"
    THREE_WAY_HANDICAP = "three_way_handicap"

    # Total goals/points markets
    TOTAL_GOALS = "total_goals"  # Over/Under
    TEAM_TOTAL_GOALS = "team_total_goals"
    EXACT_TOTAL_GOALS = "exact_total_goals"

    # Both teams to score
    BOTH_TEAMS_TO_SCORE = "both_teams_to_score"

    # Correct score
    CORRECT_SCORE = "correct_score"

    # Half/period markets
    MATCH_ODDS_FIRST_HALF = "match_odds_period_first_half"
    MATCH_ODDS_SECOND_HALF = "match_odds_period_second_half"
    ASIAN_HANDICAP_FIRST_HALF = "asian_handicap_period_first_half"
    ASIAN_HANDICAP_SECOND_HALF = "asian_handicap_period_second_half"
    TOTAL_GOALS_FIRST_HALF = "total_goals_period_first_half"
    TOTAL_GOALS_SECOND_HALF = "total_goals_period_second_half"

    # Other
    WINNER = "winner"  # Outright winner
    OTHER = "other"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, value: str) -> "MarketType":
        """
        Parse market type from string, with fuzzy matching.

        Parameters
        ----------
        value : str
            The market type string to parse.

        Returns
        -------
        MarketType
            The parsed market type, or OTHER if not recognized.

        """
        return _market_type_from_string(value)


@lru_cache(maxsize=256)
def _market_type_from_string(value: str) -> "MarketType":
    value_lower = value.lower().replace(" ", "_").replace("-", "_")

    # Direct match
    for market_type in MarketType:
        if market_type.value == value_lower:
            return market_type

    # Fuzzy matching for common variations
    if "1x2" in value_lower or "money_line" in value_lower:
        return MarketType.MATCH_ODDS
    if "handicap" in value_lower:
        if "asian" in value_lower:
            return MarketType.ASIAN_HANDICAP
        if "european" in value_lower:
            return MarketType.EUROPEAN_HANDICAP
        return MarketType.ASIAN_HANDICAP  # Default to Asian
    if "over" in value_lower or "under" in value_lower or "total" in value_lower:
        return MarketType.TOTAL_GOALS
    if "both_teams" in value_lower or "btts" in value_lower:
        return MarketType.BOTH_TEAMS_TO_SCORE
    if "double_chance" in value_lower:
        return MarketType.DOUBLE_CHANCE

    return MarketType.OTHER


class BetStatus(str, Enum):
    """
    Status of a placed bet.
    """

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_MATCHED = "PARTIALLY_MATCHED"
    FULLY_MATCHED = "FULLY_MATCHED"
    SETTLED_WON = "SETTLED_WON"
    SETTLED_LOST = "SETTLED_LOST"
    SETTLED_VOID = "SETTLED_VOID"
    SETTLED_PUSH = "SETTLED_PUSH"  # Refund (e.g., handicap of 0 and draw)
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    def __str__(self) -> str:
        return self.value

    @property
    def is_terminal(self) -> bool:
        """
        Check if the bet is in a terminal state.
        """
        return self in (
            BetStatus.SETTLED_WON,
            BetStatus.SETTLED_LOST,
            BetStatus.SETTLED_VOID,
            BetStatus.SETTLED_PUSH,
            BetStatus.CANCELLED,
            BetStatus.REJECTED,
            BetStatus.EXPIRED,
        )


class Outcome(str, Enum):
    """
    Common outcomes for betting selections.
    """

    HOME = "home"
    AWAY = "away"
    DRAW = "draw"
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"
    HOME_DRAW = "home_draw"  # Double chance 1X
    AWAY_DRAW = "away_draw"  # Double chance X2
    HOME_AWAY = "home_away"  # Double chance 12
    OTHER = "other"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, value: str) -> "Outcome":
        """
        Parse outcome from string.
        """
        value_lower = value.lower()

        for outcome in cls:
            if outcome.value == value_lower:
                return outcome

        # Common aliases
        if value_lower in ("1", "team1", "team_1"):
            return cls.HOME
        if value_lower in ("2", "team2", "team_2"):
            return cls.AWAY
        if value_lower in ("x", "tie"):
            return cls.DRAW
        if value_lower in ("1x", "home_or_draw"):
            return cls.HOME_DRAW
        if value_lower in ("x2", "draw_or_away"):
            return cls.AWAY_DRAW
        if value_lower in ("12", "no_draw"):
            return cls.HOME_AWAY

        return cls.OTHER

    def opposite(self) -> "Outcome | None":
        """
        Get the opposite outcome for hedging.
        """
        opposites = {
            Outcome.HOME: Outcome.AWAY,
            Outcome.AWAY: Outcome.HOME,
            Outcome.OVER: Outcome.UNDER,
            Outcome.UNDER: Outcome.OVER,
            Outcome.YES: Outcome.NO,
            Outcome.NO: Outcome.YES,
        }
        return opposites.get(self)
