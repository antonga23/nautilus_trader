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
from collections.abc import Mapping


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
    maker_rebate_rate: Decimal
    winning_profit_fee_rate: Decimal
    taker_cost_fraction: Decimal
    maker_rebate_fraction: Decimal


@dataclass(frozen=True)
class FeeAdjustedBasket:
    """
    Fee and incentive adjusted economics for a coverage basket or hyperedge.
    """

    raw_total_probability: Decimal
    effective_total_probability: Decimal
    raw_profit_margin: Decimal
    effective_profit_margin: Decimal
    basket_rebate_rate: Decimal
    basket_boost_rate: Decimal
    incentive_margin_delta: Decimal


def normalize_venue_fee_rates(
    values: Mapping[str, Decimal | float | int | str] | None,
    *,
    defaults: Mapping[str, Decimal] | None = None,
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
    maker_rebate_rate: Decimal | float | str = Decimal(0),
    winning_profit_fee_rate: Decimal | float | str = Decimal(0),
) -> FeeAdjustedOdds:
    """
    Convert raw decimal odds into fee-adjusted effective odds.

    ``taker_fee_rate`` uses the prediction-market formula where fee per share is
    ``rate * price * (1 - price)``. Expressed per unit stake this is
    ``rate * (1 - price)``. ``maker_rebate_rate`` mirrors that fee curve as a
    stake-cost reduction for passive fills. ``winning_profit_fee_rate`` models
    sportsbook-style commissions charged only on winning profit.

    """
    raw_odds = Decimal(str(odds))
    if raw_odds <= 1:
        msg = f"Decimal odds must be greater than 1, got {raw_odds}"
        raise ValueError(msg)

    taker_rate = _normalized_rate(taker_fee_rate)
    maker_rebate_rate = _normalized_rate(maker_rebate_rate)
    winning_rate = _normalized_rate(winning_profit_fee_rate)
    raw_probability = Decimal(1) / raw_odds
    taker_cost_fraction = taker_rate * (Decimal(1) - raw_probability)
    maker_rebate_fraction = maker_rebate_rate * (Decimal(1) - raw_probability)
    stake_cost_multiplier = Decimal(1) + taker_cost_fraction - maker_rebate_fraction
    if stake_cost_multiplier <= 0:
        msg = (
            "Maker rebate cannot fully offset or exceed stake cost, got "
            f"stake_cost_multiplier={stake_cost_multiplier}"
        )
        raise ValueError(msg)
    net_return_per_stake = Decimal(1) + ((raw_odds - Decimal(1)) * (Decimal(1) - winning_rate))
    effective_odds = net_return_per_stake / stake_cost_multiplier
    effective_probability = Decimal(1) / effective_odds
    return FeeAdjustedOdds(
        raw_odds=raw_odds,
        effective_odds=effective_odds,
        raw_probability=raw_probability,
        effective_probability=effective_probability,
        taker_fee_rate=taker_rate,
        maker_rebate_rate=maker_rebate_rate,
        winning_profit_fee_rate=winning_rate,
        taker_cost_fraction=taker_cost_fraction,
        maker_rebate_fraction=maker_rebate_fraction,
    )


def fee_adjusted_basket_margin(
    probabilities: list[Decimal] | tuple[Decimal, ...],
    *,
    raw_probabilities: list[Decimal] | tuple[Decimal, ...] | None = None,
    basket_rebate_rate: Decimal | float | str = Decimal(0),
    basket_boost_rate: Decimal | float | str = Decimal(0),
) -> FeeAdjustedBasket:
    """
    Apply basket-level rebates or reward boosts to a coverage set.

    ``probabilities`` should already include per-leg taker/maker/winning-profit
    adjustments. ``basket_rebate_rate`` models stake-cost rebates such as parlay
    cashback or temporary venue rewards. ``basket_boost_rate`` models a return
    boost applied to the covered basket. Both are kept explicit so runtime
    diagnostics can distinguish fee drag from promotional edge.

    """
    if not probabilities:
        msg = "At least one probability is required"
        raise ValueError(msg)
    effective_leg_total = sum((Decimal(str(value)) for value in probabilities), Decimal(0))
    raw_total = sum(
        (Decimal(str(value)) for value in (raw_probabilities or probabilities)),
        Decimal(0),
    )
    if effective_leg_total <= 0 or raw_total <= 0:
        msg = "Basket probabilities must be positive"
        raise ValueError(msg)

    rebate_rate = _normalized_rate(basket_rebate_rate)
    boost_rate = _normalized_rate(basket_boost_rate)
    effective_total_probability = (
        effective_leg_total * (Decimal(1) - rebate_rate) / (Decimal(1) + boost_rate)
    )
    if effective_total_probability <= 0:
        msg = "Basket fee adjustment produced a non-positive total probability"
        raise ValueError(msg)
    raw_profit_margin = (Decimal(1) / raw_total) - Decimal(1)
    effective_profit_margin = (Decimal(1) / effective_total_probability) - Decimal(1)
    return FeeAdjustedBasket(
        raw_total_probability=raw_total,
        effective_total_probability=effective_total_probability,
        raw_profit_margin=raw_profit_margin,
        effective_profit_margin=effective_profit_margin,
        basket_rebate_rate=rebate_rate,
        basket_boost_rate=boost_rate,
        incentive_margin_delta=effective_profit_margin
        - ((Decimal(1) / effective_leg_total) - Decimal(1)),
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
