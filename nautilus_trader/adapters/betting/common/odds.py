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
Odds conversion utilities for betting adapters.

Provides conversions between different odds formats:
- Decimal (European): 2.50
- American/Moneyline: +150 or -200
- Fractional (UK): 3/2
- Implied Probability: 0.40 (40%)

"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import gcd
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from nautilus_trader.adapters.betting.fx import PortfolioCurrencyPolicy


DECIMAL_ODDS_EVEN_THRESHOLD = Decimal(2)
AMERICAN_ODDS_BASE = Decimal(100)


VALID_DEVIG_METHODS = frozenset({"auto", "proportional", "shin", "logarithmic"})
DEVIG_CONVERGENCE_THRESHOLD = Decimal("1e-12")
DEVIG_MAX_ITERATIONS = 1_000
DEVIG_EXTREME_LOW_ODDS = Decimal("1.10")
DEVIG_EXTREME_HIGH_ODDS = Decimal("10.0")
DEVIG_BALANCED_MIN_ODDS = Decimal("1.60")
DEVIG_BALANCED_MAX_ODDS = Decimal("2.50")
DEVIG_BALANCED_MAX_ABS_VIG = Decimal("0.05")


@dataclass(frozen=True)
class DeviggedBook:
    """
    No-vig probability view of a complete market book.
    """

    implied_probabilities: tuple[Decimal, ...]
    no_vig_probabilities: tuple[Decimal, ...]
    overround: Decimal
    vig: Decimal
    method: str
    method_reason: str = "configured"
    convergence_status: str = "not_required"
    iterations: int = 0
    delta: Decimal = Decimal(0)
    z: Decimal | None = None

    @property
    def fair_probabilities(self) -> tuple[Decimal, ...]:
        """
        Alias used by runtime diagnostics and value-edge code.
        """
        return self.no_vig_probabilities

    @property
    def converged(self) -> bool:
        """
        Whether iterative devigging converged or did not require optimisation.
        """
        return self.convergence_status in {"converged", "not_required", "analytic"}


DeviggedMarket = DeviggedBook


def decimal_to_probability(odds: float | Decimal) -> Decimal:
    """
    Convert decimal odds to implied probability.

    Parameters
    ----------
    odds : float | Decimal
        Decimal odds (e.g., 2.50 means 2.5x return).

    Returns
    -------
    Decimal
        Implied probability (0-1).

    Examples
    --------
    >>> decimal_to_probability(2.0)
    Decimal('0.5')
    >>> decimal_to_probability(4.0)
    Decimal('0.25')

    """
    if odds <= 0:
        raise ValueError(f"Odds must be positive, got {odds}")
    return Decimal(1) / Decimal(str(odds))


def probability_to_decimal(probability: float | Decimal) -> Decimal:
    """
    Convert implied probability to decimal odds.

    Parameters
    ----------
    probability : float | Decimal
        Implied probability (0-1).

    Returns
    -------
    Decimal
        Decimal odds.

    Examples
    --------
    >>> probability_to_decimal(0.5)
    Decimal('2')
    >>> probability_to_decimal(0.25)
    Decimal('4')

    """
    if not 0 < probability < 1:
        raise ValueError(f"Probability must be between 0 and 1, got {probability}")
    return Decimal(1) / Decimal(str(probability))


def devig_probabilities(
    odds: Sequence[float | Decimal | str],
    *,
    method: str = "auto",
) -> DeviggedBook:
    """
    Strip market overround from a complete book of decimal odds.

    Devigging is a no-vig reference view for complete books. It is intentionally
    separate from executable arbitrage proof, which still uses executable fee-adjusted
    odds.

    """
    normalized_method = method.strip().lower()
    if normalized_method not in VALID_DEVIG_METHODS:
        msg = f"Unsupported devig method: {method}"
        raise ValueError(msg)
    if len(odds) < 2:
        msg = "At least two odds values are required to devig a market"
        raise ValueError(msg)

    decimal_odds = tuple(Decimal(str(value)) for value in odds)
    if any(value <= 1 for value in decimal_odds):
        msg = f"Decimal odds must all be greater than 1, got {decimal_odds}"
        raise ValueError(msg)

    implied = tuple(decimal_to_probability(value) for value in decimal_odds)
    total_probability = sum(implied, Decimal(0))
    if total_probability <= 0:
        msg = "Market implied probability must be positive"
        raise ValueError(msg)

    selected_method, method_reason = _select_devig_method(
        decimal_odds,
        total_probability=total_probability,
        requested_method=normalized_method,
    )
    if selected_method == "proportional":
        no_vig, convergence_status, iterations, delta, z = (
            _proportional_devig(implied),
            "not_required",
            0,
            Decimal(0),
            None,
        )
    elif selected_method == "shin":
        try:
            no_vig, convergence_status, iterations, delta, z = _shin_devig(implied)
        except ArithmeticError:
            no_vig, convergence_status, iterations, delta, z = (
                _proportional_devig(implied),
                "fallback_proportional",
                0,
                Decimal(0),
                None,
            )
            selected_method = "proportional"
            method_reason = "shin_failed_fallback"
    else:
        no_vig, convergence_status, iterations, delta, z = _logarithmic_devig(implied)

    return DeviggedBook(
        implied_probabilities=implied,
        no_vig_probabilities=_normalize_probabilities(no_vig),
        overround=total_probability,
        vig=total_probability - Decimal(1),
        method=selected_method,
        method_reason=method_reason,
        convergence_status=convergence_status,
        iterations=iterations,
        delta=delta,
        z=z,
    )


def _select_devig_method(
    decimal_odds: tuple[Decimal, ...],
    *,
    total_probability: Decimal,
    requested_method: str,
) -> tuple[str, str]:
    if requested_method != "auto":
        return requested_method, "configured"
    if total_probability <= Decimal(1):
        return "proportional", "underround_or_fair_book"
    if min(decimal_odds) <= DEVIG_EXTREME_LOW_ODDS or max(decimal_odds) >= DEVIG_EXTREME_HIGH_ODDS:
        return "logarithmic", "extreme_odds"
    if (
        len(decimal_odds) == 2
        and all(
            DEVIG_BALANCED_MIN_ODDS <= value <= DEVIG_BALANCED_MAX_ODDS for value in decimal_odds
        )
        and abs(total_probability - Decimal(1)) <= DEVIG_BALANCED_MAX_ABS_VIG
    ):
        return "proportional", "balanced_two_way"
    return "shin", "default_shin"


def _proportional_devig(implied: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    total_probability = sum(implied, Decimal(0))
    if total_probability <= 0:
        msg = "Market implied probability must be positive"
        raise ArithmeticError(msg)
    return tuple(probability / total_probability for probability in implied)


def _shin_devig(
    implied: tuple[Decimal, ...],
) -> tuple[tuple[Decimal, ...], str, int, Decimal, Decimal]:
    total_probability = sum(implied, Decimal(0))
    if total_probability <= 0:
        msg = "Market implied probability must be positive"
        raise ArithmeticError(msg)
    if total_probability <= Decimal(1):
        return _proportional_devig(implied), "not_required", 0, Decimal(0), Decimal(0)
    if len(implied) == 2:
        z = _shin_two_way_z(implied, total_probability)
        return (
            _shin_probabilities(implied, total_probability=total_probability, z=z),
            "analytic",
            0,
            Decimal(0),
            z,
        )

    z = Decimal(0)
    delta = Decimal("Infinity")
    iterations = 0
    while delta > DEVIG_CONVERGENCE_THRESHOLD and iterations < DEVIG_MAX_ITERATIONS:
        previous_z = z
        z = (
            sum(
                (
                    (z * z)
                    + (
                        Decimal(4)
                        * (Decimal(1) - z)
                        * probability
                        * probability
                        / total_probability
                    )
                ).sqrt()
                for probability in implied
            )
            - Decimal(2)
        ) / Decimal(len(implied) - 2)
        delta = abs(z - previous_z)
        iterations += 1
    convergence_status = "converged" if delta <= DEVIG_CONVERGENCE_THRESHOLD else "failed"
    if convergence_status == "failed":
        msg = f"Shin devig failed to converge after {iterations} iterations"
        raise ArithmeticError(msg)
    return (
        _shin_probabilities(implied, total_probability=total_probability, z=z),
        convergence_status,
        iterations,
        delta,
        z,
    )


def _shin_two_way_z(implied: tuple[Decimal, ...], total_probability: Decimal) -> Decimal:
    diff = implied[0] - implied[1]
    numerator = (total_probability - Decimal(1)) * ((diff * diff) - total_probability)
    denominator = total_probability * ((diff * diff) - Decimal(1))
    if denominator == 0:
        msg = "Shin two-way denominator is zero"
        raise ArithmeticError(msg)
    return numerator / denominator


def _shin_probabilities(
    implied: tuple[Decimal, ...],
    *,
    total_probability: Decimal,
    z: Decimal,
) -> tuple[Decimal, ...]:
    if z >= Decimal(1):
        msg = f"Shin insider parameter must be less than 1, got {z}"
        raise ArithmeticError(msg)
    return tuple(
        (
            (
                (z * z)
                + (Decimal(4) * (Decimal(1) - z) * probability * probability / total_probability)
            ).sqrt()
            - z
        )
        / (Decimal(2) * (Decimal(1) - z))
        for probability in implied
    )


def _logarithmic_devig(
    implied: tuple[Decimal, ...],
) -> tuple[tuple[Decimal, ...], str, int, Decimal, None]:
    float_probs = [float(probability) for probability in implied]
    if any(probability <= 0 or probability >= 1 for probability in float_probs):
        msg = f"Logarithmic devig requires probabilities in (0, 1), got {implied}"
        raise ArithmeticError(msg)
    low = 0.000001
    high = 100.0
    iterations = 0
    previous = 0.0
    while iterations < DEVIG_MAX_ITERATIONS:
        iterations += 1
        middle = (low + high) / 2
        total = sum(probability**middle for probability in float_probs)
        previous = middle
        if abs(total - 1.0) <= 1e-12:
            break
        if total > 1.0:
            low = middle
        else:
            high = middle
    power = (low + high) / 2
    no_vig = tuple(Decimal(str(probability**power)) for probability in float_probs)
    return no_vig, "converged", iterations, Decimal(str(abs(power - previous))), None


def _normalize_probabilities(probabilities: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    total = sum(probabilities, Decimal(0))
    if total <= 0:
        msg = "Devigged probabilities must sum to a positive value"
        raise ArithmeticError(msg)
    return tuple(probability / total for probability in probabilities)


def decimal_to_american(odds: float | Decimal) -> int:
    """
    Convert decimal odds to American/Moneyline odds.

    Parameters
    ----------
    odds : float | Decimal
        Decimal odds.

    Returns
    -------
    int
        American odds (+150 or -200).

    Examples
    --------
    >>> decimal_to_american(2.5)
    150
    >>> decimal_to_american(1.5)
    -200

    """
    odds = Decimal(str(odds))
    if odds <= 1:
        raise ValueError(f"Decimal odds must be greater than 1, got {odds}")

    if odds >= DECIMAL_ODDS_EVEN_THRESHOLD:
        # Positive (underdog): +100 * (decimal - 1)
        return int((odds - 1) * AMERICAN_ODDS_BASE)

    # Negative (favorite): -100 / (decimal - 1)
    return int(-AMERICAN_ODDS_BASE / (odds - 1))


def american_to_decimal(odds: int) -> Decimal:
    """
    Convert American/Moneyline odds to decimal odds.

    Parameters
    ----------
    odds : int
        American odds (+150 or -200).

    Returns
    -------
    Decimal
        Decimal odds.

    Examples
    --------
    >>> american_to_decimal(150)
    Decimal('2.5')
    >>> american_to_decimal(-200)
    Decimal('1.5')

    """
    if odds == 0:
        raise ValueError("American odds cannot be zero")

    if odds > 0:
        # Positive: 1 + (odds / 100)
        return Decimal(1) + Decimal(odds) / AMERICAN_ODDS_BASE

    # Negative: 1 + (100 / abs(odds))
    return Decimal(1) + AMERICAN_ODDS_BASE / Decimal(abs(odds))


def fractional_to_decimal(numerator: int, denominator: int) -> Decimal:
    """
    Convert fractional odds to decimal odds.

    Parameters
    ----------
    numerator : int
        Top number of fraction (e.g., 3 in 3/2).
    denominator : int
        Bottom number of fraction (e.g., 2 in 3/2).

    Returns
    -------
    Decimal
        Decimal odds.

    Examples
    --------
    >>> fractional_to_decimal(3, 2)
    Decimal('2.5')
    >>> fractional_to_decimal(1, 1)
    Decimal('2')

    """
    if denominator == 0:
        raise ValueError("Denominator cannot be zero")
    return Decimal(1) + Decimal(numerator) / Decimal(denominator)


def decimal_to_fractional(odds: float | Decimal) -> tuple[int, int]:
    """
    Convert decimal odds to fractional odds.

    Note: This provides an approximation as not all decimal values
    can be expressed as clean fractions.

    Parameters
    ----------
    odds : float | Decimal
        Decimal odds.

    Returns
    -------
    tuple[int, int]
        (numerator, denominator) of the fraction.

    Examples
    --------
    >>> decimal_to_fractional(2.5)
    (3, 2)
    >>> decimal_to_fractional(2.0)
    (1, 1)

    """
    odds = Decimal(str(odds))
    if odds <= 1:
        raise ValueError(f"Decimal odds must be greater than 1, got {odds}")

    # Convert to fraction of profit (decimal - 1)
    profit = float(odds - 1)

    # Common denominators in betting
    common_denoms = [1, 2, 4, 5, 8, 10, 20, 25, 50, 100]

    best_num, best_denom = 0, 1
    best_error = float("inf")

    for denom in common_denoms:
        num = round(profit * denom)
        if num > 0:
            error = abs(profit - num / denom)
            if error < best_error:
                best_error = error
                best_num = num
                best_denom = denom

    # Reduce fraction
    g = gcd(best_num, best_denom)
    return (best_num // g, best_denom // g)


def is_arbitrage_opportunity(
    odds_a: float | Decimal,
    odds_b: float | Decimal,
) -> tuple[bool, Decimal]:
    """
    Check if two mutually exclusive outcomes create an arbitrage opportunity.

    An arbitrage exists when the sum of implied probabilities < 1.

    Parameters
    ----------
    odds_a : float | Decimal
        Decimal odds for outcome A.
    odds_b : float | Decimal
        Decimal odds for outcome B (must be mutually exclusive with A).

    Returns
    -------
    tuple[bool, Decimal]
        (is_arbitrage, margin) where margin is ROI for the total stake.

    Examples
    --------
    >>> is_arbitrage_opportunity(2.1, 2.1)  # Sum of probs = 0.952 < 1
    (True, Decimal('0.050'))
    >>> is_arbitrage_opportunity(2.0, 2.0)  # Sum of probs = 1.0 (break even)
    (False, Decimal('0'))

    """
    prob_a = decimal_to_probability(odds_a)
    prob_b = decimal_to_probability(odds_b)

    total_prob = prob_a + prob_b

    if total_prob < 1:
        margin = (Decimal(1) / total_prob) - Decimal(1)
        return (True, margin)

    return (False, Decimal(0))


def calculate_arbitrage_stakes(
    odds_a: float | Decimal,
    odds_b: float | Decimal,
    total_stake: float | Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Calculate optimal stake split for arbitrage between two outcomes.

    Parameters
    ----------
    odds_a : float | Decimal
        Decimal odds for outcome A.
    odds_b : float | Decimal
        Decimal odds for outcome B.
    total_stake : float | Decimal
        Total amount to stake.

    Returns
    -------
    tuple[Decimal, Decimal, Decimal]
        (stake_a, stake_b, guaranteed_profit) for equal profit regardless of outcome.

    Examples
    --------
    >>> calculate_arbitrage_stakes(2.1, 2.1, 100)
    (Decimal('50'), Decimal('50'), Decimal('5'))

    """
    odds_a = Decimal(str(odds_a))
    odds_b = Decimal(str(odds_b))
    total = Decimal(str(total_stake))
    stake_quantum = Decimal("0.01")

    prob_a = decimal_to_probability(odds_a)
    prob_b = decimal_to_probability(odds_b)

    # Optimal split: stake proportional to the implied probability of the
    # same outcome so both sides return the same amount when either wins.
    # stake_a = total * prob_a / (prob_a + prob_b)
    total_prob = prob_a + prob_b

    stake_a = (total * prob_a / total_prob).quantize(stake_quantum)
    stake_b = (total - stake_a).quantize(stake_quantum)

    # Rounding can create a small imbalance between the two outcomes, so return
    # the worst-case profit for the executable stakes.
    guaranteed_return = min(stake_a * odds_a, stake_b * odds_b)
    guaranteed_profit = guaranteed_return - total

    return (
        stake_a,
        stake_b,
        guaranteed_profit.quantize(stake_quantum),
    )


@dataclass(frozen=True)
class CrossCurrencyArbitrageStakes:
    """
    Stake split for a two-leg arbitrage whose legs settle in different currencies.

    ``stake_a`` / ``stake_b`` are denominated in each leg's own settlement currency
    (the executable order sizes). All profit and exposure figures are expressed in the
    portfolio base currency after FX conversion, so a genuine cross-currency edge and a
    phantom one (positive single-currency margin that FX erases) are distinguishable.

    """

    stake_a: Decimal
    stake_b: Decimal
    guaranteed_profit: Decimal
    base_payoff: Decimal
    base_notional: Decimal
    base_currency: str
    is_available: bool
    blocker_reason: str | None = None


def calculate_cross_currency_arbitrage_stakes(
    odds_a: float | Decimal,
    odds_b: float | Decimal,
    total_stake: float | Decimal,
    *,
    policy: PortfolioCurrencyPolicy,
    currency_a: str,
    currency_b: str,
) -> CrossCurrencyArbitrageStakes:
    """
    Size a two-leg arbitrage so the post-FX payoffs are equalized in base currency.

    Unlike :func:`calculate_arbitrage_stakes`, which balances returns within a single
    currency, this solver converts each leg's payoff into the portfolio base currency
    before balancing. Leg notionals use the haircut-inflated conversion (conservative
    against risk caps); leg payoffs use the haircut-reduced conversion (conservative
    against the edge). ``total_stake`` is the base-currency notional budget.

    If any required conversion is unavailable (missing or stale FX rate, sandbox or
    unknown currency), the pair is reported as unavailable and MUST NOT be executed;
    the caller never falls back to a raw same-currency or 1:1 split.

    """
    odds_a = Decimal(str(odds_a))
    odds_b = Decimal(str(odds_b))
    total = Decimal(str(total_stake))
    stake_quantum = Decimal("0.01")

    notional_a = policy.convert(Decimal(1), currency_a)
    notional_b = policy.convert(Decimal(1), currency_b)
    payoff_a = policy.convert_payoff(Decimal(1), currency_a)
    payoff_b = policy.convert_payoff(Decimal(1), currency_b)
    conversions = (notional_a, notional_b, payoff_a, payoff_b)
    base_currency = notional_a.target_currency
    blocker = next((c.blocker_reason for c in conversions if c.blocker_reason), None)
    if blocker is not None or any(c.converted_amount is None for c in conversions):
        return CrossCurrencyArbitrageStakes(
            stake_a=Decimal(0),
            stake_b=Decimal(0),
            guaranteed_profit=Decimal(0),
            base_payoff=Decimal(0),
            base_notional=Decimal(0),
            base_currency=base_currency,
            is_available=False,
            blocker_reason=blocker or "missing_fx_rate",
        )

    n_a = notional_a.converted_amount
    n_b = notional_b.converted_amount
    p_a = payoff_a.converted_amount
    p_b = payoff_b.converted_amount
    if n_a is None or n_b is None or p_a is None or p_b is None:  # pragma: no cover
        return CrossCurrencyArbitrageStakes(
            stake_a=Decimal(0),
            stake_b=Decimal(0),
            guaranteed_profit=Decimal(0),
            base_payoff=Decimal(0),
            base_notional=Decimal(0),
            base_currency=base_currency,
            is_available=False,
            blocker_reason="missing_fx_rate",
        )

    # Equalize base payoffs:   stake_a * odds_a * p_a == stake_b * odds_b * p_b
    # Spend the base budget:   stake_a * n_a + stake_b * n_b == total
    payoff_ratio = (odds_a * p_a) / (odds_b * p_b)
    stake_a = (total / (n_a + payoff_ratio * n_b)).quantize(stake_quantum)
    stake_b = (stake_a * payoff_ratio).quantize(stake_quantum)

    base_payoff = min(stake_a * odds_a * p_a, stake_b * odds_b * p_b).quantize(stake_quantum)
    base_notional = (stake_a * n_a + stake_b * n_b).quantize(stake_quantum)
    guaranteed_profit = (base_payoff - base_notional).quantize(stake_quantum)

    return CrossCurrencyArbitrageStakes(
        stake_a=stake_a,
        stake_b=stake_b,
        guaranteed_profit=guaranteed_profit,
        base_payoff=base_payoff,
        base_notional=base_notional,
        base_currency=base_currency,
        is_available=True,
    )


def format_odds(
    odds: float | Decimal,
    odds_format: str = "decimal",
) -> str:
    """
    Format odds to string in specified format.

    Parameters
    ----------
    odds : float | Decimal
        Decimal odds value.
    odds_format : str
        Output format: "decimal", "american", "fractional", "probability".

    Returns
    -------
    str
        Formatted odds string.

    """
    odds = Decimal(str(odds))

    if odds_format == "decimal":
        return f"{float(odds):.2f}"
    if odds_format == "american":
        american = decimal_to_american(odds)
        return f"+{american}" if american > 0 else str(american)
    if odds_format == "fractional":
        num, denom = decimal_to_fractional(odds)
        return f"{num}/{denom}"
    if odds_format == "probability":
        prob = decimal_to_probability(odds)
        return f"{float(prob) * 100:.1f}%"

    raise ValueError(f"Unknown format: {odds_format}")
