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
Fee and vig helpers for betting arbitrage pricing.
"""

from dataclasses import dataclass
from decimal import Decimal


DEFAULT_TAKER_FEE_RATES: dict[str, Decimal] = {
    # Polymarket sports markets use a 3% category fee-rate parameter in the
    # protocol formula: shares * feeRate * price * (1 - price).
    "POLYMARKET": Decimal("0.03"),
}


@dataclass(frozen=True)
class FeeAdjustedOdds:
    """
    Venue-cost adjusted pricing for one quoted betting selection.
    """

    raw_odds: Decimal
    effective_odds: Decimal
    raw_probability: Decimal
    effective_probability: Decimal
    taker_fee_rate: Decimal
    winning_profit_fee_rate: Decimal
    taker_cost_fraction: Decimal


def normalize_venue_fee_rates(
    values: dict[str, Decimal | float | int | str] | None,
    *,
    defaults: dict[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """
    Normalize a venue keyed fee-rate mapping.
    """
    normalized: dict[str, Decimal] = {
        str(venue).strip().upper(): _normalized_rate(rate)
        for venue, rate in (defaults or {}).items()
        if str(venue).strip()
    }
    for venue, rate in (values or {}).items():
        venue_key = str(venue).strip().upper()
        if not venue_key:
            continue
        normalized[venue_key] = _normalized_rate(rate)
    return dict(sorted(normalized.items()))


def fee_adjusted_odds(
    odds: Decimal | float | str,
    *,
    taker_fee_rate: Decimal | float | str = Decimal(0),
    winning_profit_fee_rate: Decimal | float | str = Decimal(0),
) -> FeeAdjustedOdds:
    """
    Convert raw decimal odds into fee-adjusted effective odds.

    ``taker_fee_rate`` uses the prediction-market formula where fee per share is
    ``rate * price * (1 - price)``. Expressed per unit stake this is
    ``rate * (1 - price)``. ``winning_profit_fee_rate`` models sportsbook-style
    commissions charged only on winning profit.
    """
    raw_odds = Decimal(str(odds))
    if raw_odds <= 1:
        msg = f"Decimal odds must be greater than 1, got {raw_odds}"
        raise ValueError(msg)

    taker_rate = _normalized_rate(taker_fee_rate)
    winning_rate = _normalized_rate(winning_profit_fee_rate)
    raw_probability = Decimal(1) / raw_odds
    taker_cost_fraction = taker_rate * (Decimal(1) - raw_probability)
    net_return_per_stake = Decimal(1) + ((raw_odds - Decimal(1)) * (Decimal(1) - winning_rate))
    effective_odds = net_return_per_stake / (Decimal(1) + taker_cost_fraction)
    effective_probability = Decimal(1) / effective_odds
    return FeeAdjustedOdds(
        raw_odds=raw_odds,
        effective_odds=effective_odds,
        raw_probability=raw_probability,
        effective_probability=effective_probability,
        taker_fee_rate=taker_rate,
        winning_profit_fee_rate=winning_rate,
        taker_cost_fraction=taker_cost_fraction,
    )


def _normalized_rate(value: Decimal | float | str) -> Decimal:
    rate = Decimal(str(value or 0))
    if rate < 0:
        msg = f"Fee rate must be non-negative, got {rate}"
        raise ValueError(msg)
    if rate >= 1:
        msg = f"Fee rate must be less than 1, got {rate}"
        raise ValueError(msg)
    return rate
