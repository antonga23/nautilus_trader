# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Decimal money primitives for the bonus-EV double-entry ledger.

Every monetary quantity in the ledger is a :class:`decimal.Decimal`. Floats are
rejected at the boundary rather than tolerated: binary floating point cannot
represent a cent (or 1e-18 ETH) exactly, and a ledger that silently loses a cent
cannot be reconciled back to a bank statement afterwards.

Two precisions matter and they are deliberately different:

* *minor-unit precision* per currency (ZAR/USD cents, USDC 6dp, BTC 8dp) applies
  to amounts that will be settled against a real balance;
* *rate precision* (12dp) applies to FX and cost-basis rates, which are ratios
  rather than balances and need the extra digits so that
  ``qty * rate`` reproduces the recorded base amount.

Rounding is banker's rounding (``ROUND_HALF_EVEN``) everywhere so repeated
quantization of a large book does not drift upward.

"""

from __future__ import annotations

import decimal
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final


ZAR: Final = "ZAR"
USD: Final = "USD"
USDC: Final = "USDC"
USDT: Final = "USDT"
BTC: Final = "BTC"
ETH: Final = "ETH"

DEFAULT_BASE_CURRENCY: Final = ZAR

# Rates are ratios, not balances, so they carry more digits than any currency.
RATE_EXPONENT: Final = 12
ROUNDING: Final = decimal.ROUND_HALF_EVEN

# Wide enough that an 18dp ETH quantity times a 12dp rate never loses a digit to
# the default 28-digit context before it is quantized back down.
CALC_CONTEXT: Final = decimal.Context(prec=60, rounding=ROUNDING)

_CURRENCY_EXPONENTS: dict[str, int] = {
    ZAR: 2,
    USD: 2,
    USDC: 6,
    USDT: 6,
    BTC: 8,
    ETH: 18,
}

ONE: Final = Decimal(1)
ZERO: Final = Decimal(0)


class MoneyError(Exception):
    """
    Base class for every money-domain failure raised by this module.
    """


class UnknownCurrency(MoneyError):
    """
    Raised when a currency code has no registered minor-unit precision.
    """


class InvalidRate(MoneyError):
    """
    Raised when a conversion rate is not a finite positive number.
    """


def register_currency(code: str, exponent: int) -> None:
    """
    Register ``code`` with ``exponent`` minor-unit decimal places.

    >>> register_currency("XTS", 3)
    >>> exponent_for("XTS")
    3

    """
    if exponent < 0:
        raise MoneyError(f"currency exponent must be non-negative, was {exponent}")

    _CURRENCY_EXPONENTS[code] = exponent


def exponent_for(currency: str) -> int:
    """
    Return the minor-unit decimal places registered for ``currency``.
    """
    try:
        return _CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise UnknownCurrency(f"unknown currency {currency!r}") from None


def dec(value: str | int | Decimal) -> Decimal:
    """
    Coerce ``value`` to :class:`~decimal.Decimal`, rejecting floats.

    >>> dec("12.34")
    Decimal('12.34')
    >>> dec(7)
    Decimal('7')

    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        # bool is an int subclass; neither it nor float is ever a valid amount.
        raise MoneyError(f"money must be Decimal, str or int, got {type(value).__name__}")

    try:
        return Decimal(value)
    except decimal.InvalidOperation:
        raise MoneyError(f"cannot read {value!r} as a decimal amount") from None


def quantize(amount: Decimal, currency: str) -> Decimal:
    """
    Round ``amount`` to the minor-unit precision of ``currency``.

    >>> quantize(Decimal("1.005"), "ZAR")
    Decimal('1.00')
    >>> quantize(Decimal("1.015"), "ZAR")
    Decimal('1.02')

    """
    exponent = Decimal(1).scaleb(-exponent_for(currency))

    return amount.quantize(exponent, rounding=ROUNDING)


def quantize_rate(rate: Decimal) -> Decimal:
    """
    Round ``rate`` to the ledger's fixed rate precision.

    >>> quantize_rate(Decimal("18.1234567890123456"))
    Decimal('18.123456789012')

    """
    if not rate.is_finite() or rate < 0:
        raise InvalidRate(f"rate must be finite and non-negative, was {rate}")

    return rate.quantize(Decimal(1).scaleb(-RATE_EXPONENT), rounding=ROUNDING)


def apply_rate(
    amount: Decimal,
    rate: Decimal,
    base_currency: str = DEFAULT_BASE_CURRENCY,
) -> Decimal:
    """
    Convert ``amount`` at ``rate`` units of ``base_currency`` per native unit.

    >>> apply_rate(Decimal("10"), Decimal("18.5"))
    Decimal('185.00')

    """
    if not rate.is_finite() or rate < 0:
        raise InvalidRate(f"rate must be finite and non-negative, was {rate}")

    with decimal.localcontext(CALC_CONTEXT):
        product = amount * rate

    return quantize(product, base_currency)


def implied_rate(base_amount: Decimal, quantity: Decimal) -> Decimal:
    """
    Return the per-unit rate that ``base_amount`` over ``quantity`` implies.

    Used to record the rate actually applied when the base amount is derived from
    cost basis (a FIFO lot consumption) rather than from a quoted market rate.

    >>> implied_rate(Decimal("1850.00"), Decimal("100"))
    Decimal('18.500000000000')

    """
    if quantity == 0:
        raise InvalidRate("cannot imply a rate from a zero quantity")

    with decimal.localcontext(CALC_CONTEXT):
        return quantize_rate(base_amount / quantity)


def total(amounts: Iterable[Decimal]) -> Decimal:
    """
    Sum ``amounts`` exactly, without going through float.

    >>> total([Decimal("0.10"), Decimal("0.20"), Decimal("-0.30")])
    Decimal('0.00')

    """
    with decimal.localcontext(CALC_CONTEXT):
        return sum(amounts, ZERO)


@dataclass(frozen=True)
class FxQuote:
    """
    A rate snapshot taken at the moment a transaction was recorded.

    ``rate`` is expressed as units of ``base_ccy`` per one unit of ``quote_ccy``,
    so ``base_amount = amount * rate`` for an amount denominated in ``quote_ccy``.
    The snapshot is persisted alongside the postings it produced, because the rate
    a transaction actually used is not recoverable from a rate history afterwards.

    """

    ts_utc: datetime
    base_ccy: str
    quote_ccy: str
    rate: Decimal
    source: str
