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
Venue-neutral bet settlement records.

Betting execution clients learn about grading (a bet resolving WON / LOST / VOID) from
venue-specific feeds, but the strategy realizes arbitrage P&L venue-agnostically. Each
execution client publishes a ``BetSettlement`` per graded order on the message bus topic
below; the strategy subscribes once and maps records back to tracked pairs.

"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# Message bus topic on which betting execution clients publish ``BetSettlement`` records.
BET_SETTLEMENTS_TOPIC = "betting.settlements"


class SettlementResult(str, Enum):
    """
    Final grading of a single bet from the bettor's perspective.

    ``HALF_WON`` / ``HALF_LOST`` are Asian half-line (quarter-ball handicap) gradings:
    half the stake settles at odds (win or loss) and the other half is refunded, so their
    realized P&L is exactly half the corresponding full ``WON`` / ``LOST`` payoff. ``PUSH``
    refunds the full stake (economically identical to ``VOID``); it is kept distinct from
    ``VOID`` so the venue's own PUSH grading is preserved end-to-end rather than collapsed.

    """

    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    HALF_WON = "HALF_WON"
    HALF_LOST = "HALF_LOST"
    PUSH = "PUSH"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class BetSettlement:
    """
    One graded order, exactly once.

    ``settle_value`` is the venue-reported settlement figure passed through untouched for
    diagnostics; realized P&L is computed from tracked fills, never from this field.

    """

    venue: str
    client_order_id: str
    instrument_id: str | None
    result: SettlementResult
    settle_value: float | None
    ts_event: int
