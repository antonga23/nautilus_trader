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
SX.bet common utilities and types.
"""

from enum import Enum
from typing import TypedDict


class SXBetOrderStatus(str, Enum):
    """
    SX.bet order status values.
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    def __str__(self) -> str:
        return self.value


class SXBetMarketStatus(str, Enum):
    """
    SX.bet market status values.
    """

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"

    def __str__(self) -> str:
        return self.value


class SXBetMarketType(int, Enum):
    """
    SX.bet market type values.
    """

    MONEY_LINE = 0
    SPREAD = 1
    TOTAL = 2
    DRAW_NO_BET = 3
    BOTH_TO_SCORE = 4
    CORRECT_SCORE = 5

    def to_normalized(self) -> str:
        """
        Convert to normalized betting market type.
        """
        mapping = {
            0: "match_odds",
            1: "asian_handicap",
            2: "total_goals",
            3: "draw_no_bet",
            4: "both_teams_to_score",
            5: "correct_score",
        }
        return mapping.get(self.value, "other")


class EIP712Order(TypedDict):
    """
    EIP712 order structure for SX.bet.
    """

    marketHash: str
    maker: str
    totalBetSize: int
    percentageOdds: int
    expiry: int
    baseToken: str
    salt: int
    isMakerBettingOutcomeOne: bool


class EIP712Domain(TypedDict):
    """
    EIP712 domain for SX.bet signing.
    """

    name: str
    version: str
    chainId: int
