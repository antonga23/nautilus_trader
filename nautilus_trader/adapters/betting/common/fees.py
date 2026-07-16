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

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.adapters.betting.common.odds import devig_probabilities


DEFAULT_TAKER_FEE_RATES: dict[str, Decimal] = {
    # Polymarket sports markets use a 3% category fee-rate parameter in the
    # protocol formula: feeRate * shares * min(price, 1 - price).
    "POLYMARKET": Decimal("0.03"),
    # Cloudbet is a bookmaker: its cost is the margin embedded in the quoted odds,
    # which the arbitrage math already prices, so the explicit per-stake fee is zero.
    "CLOUDBET": Decimal(0),
    # SX.bet charges no per-stake taker fee; its commission applies to net winnings
    # (see DEFAULT_WINNING_PROFIT_FEE_RATES).
    "SXBET": Decimal(0),
}

DEFAULT_WINNING_PROFIT_FEE_RATES: dict[str, Decimal] = {
    # SX.bet taker commission: 4% of net winnings on the winning leg.
    "SXBET": Decimal("0.04"),
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


@dataclass(frozen=True)
class FeeAdjustedCoverageBasket:
    """
    Fee and incentive adjusted economics for an N-leg coverage proof.
    """

    legs: tuple[FeeAdjustedOdds, ...]
    basket: FeeAdjustedBasket
    no_vig_probabilities: tuple[Decimal, ...]
    overround: Decimal
    vig: Decimal
    devig_method: str = "auto"
    devig_method_reason: str = ""
    devig_convergence_status: str = ""

    @property
    def leg_count(self) -> int:
        """
        Number of selections in the adjusted coverage set.
        """
        return len(self.legs)


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

    ``taker_fee_rate`` uses the prediction-market (Polymarket) protocol formula
    where the fee per share is ``rate * min(price, 1 - price)``. With
    ``shares = stake / price`` this is ``rate * min(price, 1 - price) / price``
    per unit stake (``price`` here is ``raw_probability``). ``maker_rebate_rate``
    applies a ``rate * (1 - price)`` stake-cost reduction for passive fills.
    ``winning_profit_fee_rate`` models sportsbook-style commissions charged only
    on winning profit.

    """
    raw_odds = Decimal(str(odds))
    if raw_odds <= 1:
        msg = f"Decimal odds must be greater than 1, got {raw_odds}"
        raise ValueError(msg)

    taker_rate = _normalized_rate(taker_fee_rate)
    maker_rebate_rate = _normalized_rate(maker_rebate_rate)
    winning_rate = _normalized_rate(winning_profit_fee_rate)
    raw_probability = Decimal(1) / raw_odds
    taker_min_side = min(raw_probability, Decimal(1) - raw_probability)
    taker_cost_fraction = taker_rate * taker_min_side / raw_probability
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


def fx_adjusted_effective_odds(
    effective_odds: Decimal | float | str,
    *,
    payoff_factor: Decimal,
    notional_factor: Decimal,
) -> Decimal:
    """
    Fold the cost of a cross-currency conversion into fee-adjusted effective odds.

    ``effective_odds`` is the net-of-fee return per unit stake denominated in the
    leg's own currency. To express it per unit of base-currency notional, the stake
    outlay is converted with ``notional_factor`` (base per one unit of the leg
    currency, haircut-inflated) and the resulting payoff is converted back with
    ``payoff_factor`` (haircut-reduced). The ratio ``payoff_factor / notional_factor``
    is therefore the round-trip FX cost, which is strictly below 1 for a genuine
    cross-currency leg and exactly 1 for a same-currency (identity) conversion.

    """
    odds = Decimal(str(effective_odds))
    if notional_factor <= 0 or payoff_factor <= 0:
        msg = (
            "FX conversion factors must be positive, got "
            f"payoff_factor={payoff_factor}, notional_factor={notional_factor}"
        )
        raise ValueError(msg)
    return odds * payoff_factor / notional_factor


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


def fee_adjusted_coverage_basket(
    odds: Sequence[Decimal | float | str],
    *,
    taker_fee_rates: Sequence[Decimal | float | str] | None = None,
    maker_rebate_rates: Sequence[Decimal | float | str] | None = None,
    winning_profit_fee_rates: Sequence[Decimal | float | str] | None = None,
    basket_rebate_rate: Decimal | float | str = Decimal(0),
    basket_boost_rate: Decimal | float | str = Decimal(0),
    devig_method: str = "auto",
) -> FeeAdjustedCoverageBasket:
    """
    Apply per-leg fees and basket incentives to an arbitrary coverage proof.

    This is the N-leg equivalent of ``fee_adjusted_basket_margin``. It is used
    for full books and future hyperedge execution diagnostics where venue
    promotions can improve the economics of a complete coverage set without
    changing the semantic safety proof.

    """
    if not odds:
        msg = "At least one odds value is required"
        raise ValueError(msg)

    taker_rates = _normalized_sequence(taker_fee_rates, len(odds), "taker_fee_rates")
    maker_rates = _normalized_sequence(maker_rebate_rates, len(odds), "maker_rebate_rates")
    winning_rates = _normalized_sequence(
        winning_profit_fee_rates,
        len(odds),
        "winning_profit_fee_rates",
    )
    legs = tuple(
        fee_adjusted_odds(
            leg_odds,
            taker_fee_rate=taker_rate,
            maker_rebate_rate=maker_rate,
            winning_profit_fee_rate=winning_rate,
        )
        for leg_odds, taker_rate, maker_rate, winning_rate in zip(
            odds,
            taker_rates,
            maker_rates,
            winning_rates,
            strict=True,
        )
    )
    basket = fee_adjusted_basket_margin(
        tuple(leg.effective_probability for leg in legs),
        raw_probabilities=tuple(leg.raw_probability for leg in legs),
        basket_rebate_rate=basket_rebate_rate,
        basket_boost_rate=basket_boost_rate,
    )
    devigged = devig_probabilities(tuple(leg.raw_odds for leg in legs), method=devig_method)
    return FeeAdjustedCoverageBasket(
        legs=legs,
        basket=basket,
        no_vig_probabilities=devigged.no_vig_probabilities,
        overround=devigged.overround,
        vig=devigged.vig,
        devig_method=devigged.method,
        devig_method_reason=devigged.method_reason,
        devig_convergence_status=devigged.convergence_status,
    )


def _normalized_sequence(
    values: Sequence[Decimal | float | str] | None,
    expected_length: int,
    name: str,
) -> tuple[Decimal | float | str, ...]:
    if values is None:
        return tuple(Decimal(0) for _ in range(expected_length))
    if len(values) != expected_length:
        msg = f"{name} length must match odds length: {len(values)} != {expected_length}"
        raise ValueError(msg)
    return tuple(values)


def _normalized_rate(value: Decimal | float | str) -> Decimal:
    rate = Decimal(str(value or 0))
    if rate < 0:
        msg = f"Fee rate must be non-negative, got {rate}"
        raise ValueError(msg)
    if rate >= 1:
        msg = f"Fee rate must be less than 1, got {rate}"
        raise ValueError(msg)
    return rate
