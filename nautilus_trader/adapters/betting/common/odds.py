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

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import gcd


DECIMAL_ODDS_EVEN_THRESHOLD = Decimal(2)
AMERICAN_ODDS_BASE = Decimal(100)


@dataclass(frozen=True)
class DeviggedMarket:
    """
    No-vig probability view of a complete market book.
    """

    implied_probabilities: tuple[Decimal, ...]
    no_vig_probabilities: tuple[Decimal, ...]
    overround: Decimal
    vig: Decimal
    method: str


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
    odds: Sequence[float | Decimal],
    *,
    method: str = "proportional",
) -> DeviggedMarket:
    """
    Strip market overround from a complete book of decimal odds.

    The proportional method divides each implied probability by the total book
    probability. It is deterministic, N-outcome safe, and cheap enough for runtime
    diagnostics on full books and coverage hyperedges. Execution still uses executable
    fee-adjusted odds; this is a no-vig reference view.

    """
    normalized_method = method.strip().lower()
    if normalized_method != "proportional":
        msg = f"Unsupported devig method: {method}"
        raise ValueError(msg)
    if len(odds) < 2:
        msg = "At least two odds values are required to devig a market"
        raise ValueError(msg)

    implied = tuple(decimal_to_probability(Decimal(str(value))) for value in odds)
    total_probability = sum(implied, Decimal(0))
    if total_probability <= 0:
        msg = "Market implied probability must be positive"
        raise ValueError(msg)
    no_vig = tuple(probability / total_probability for probability in implied)
    return DeviggedMarket(
        implied_probabilities=implied,
        no_vig_probabilities=no_vig,
        overround=total_probability,
        vig=total_probability - Decimal(1),
        method=normalized_method,
    )


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
