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
USD-equivalent portfolio accounting helpers for live betting execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


USD_EQUIVALENT_CURRENCIES = frozenset({"USD", "USDC", "USDT"})
SANDBOX_CURRENCIES = frozenset({"PLAY_EUR"})


@dataclass(frozen=True)
class FxConversion:
    """
    Conservative conversion result for one stake into the portfolio base.
    """

    source_currency: str
    target_currency: str
    source_amount: Decimal
    converted_amount: Decimal | None
    rate: Decimal | None
    source: str | None
    age_secs: float | None
    haircut_bps: int
    blocker_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.converted_amount is not None and self.blocker_reason is None


@dataclass(frozen=True)
class FxMarketQuote:
    """
    One FX quote for conservative live portfolio conversion.
    """

    pair: str
    rate: Decimal
    source: str
    age_secs: float
    bid: Decimal | None = None
    ask: Decimal | None = None


@dataclass(frozen=True)
class PortfolioCurrencyPolicy:
    """
    Convert venue stakes into one conservative portfolio base currency.
    """

    base_currency: str = "USD"
    stablecoin_currencies: frozenset[str] = USD_EQUIVALENT_CURRENCIES
    stablecoin_haircut_bps: int = 10
    fx_quote_max_age_secs: float = 30.0
    static_fx_rates: dict[str, Decimal] | None = None
    fx_quotes: Mapping[str, FxMarketQuote | Any] | None = None
    static_fx_source: str = "configured_static_fx"
    sandbox_currencies: frozenset[str] = SANDBOX_CURRENCIES

    def convert(self, amount: Decimal, currency: str) -> FxConversion:
        """
        Convert a NOTIONAL stake into the portfolio base currency.

        The haircut inflates the converted amount, which over-states committed risk
        against caps. This is the conservative direction for notional, but the wrong
        direction for a payoff/profit (see ``convert_payoff``).

        """
        return self._convert(amount, currency, haircut_sign=1)

    def convert_payoff(self, amount: Decimal, currency: str) -> FxConversion:
        """
        Convert a PAYOFF or profit into the portfolio base currency.

        The haircut reduces the converted amount so the edge is never over-stated.
        Reusing the notional-calibrated ``convert`` for a payoff would inflate the
        realised return by the haircut and manufacture a phantom cross-currency edge,
        so payoff conversion carries the opposite haircut sign.

        """
        return self._convert(amount, currency, haircut_sign=-1)

    def _convert(self, amount: Decimal, currency: str, *, haircut_sign: int) -> FxConversion:
        source_currency = _currency_code(currency)
        target_currency = _currency_code(self.base_currency) or "USD"
        if not source_currency:
            return FxConversion(
                source_currency="",
                target_currency=target_currency,
                source_amount=amount,
                converted_amount=None,
                rate=None,
                source=None,
                age_secs=None,
                haircut_bps=0,
                blocker_reason="unknown_settlement_currency",
            )
        if source_currency in self.sandbox_currencies:
            return FxConversion(
                source_currency=source_currency,
                target_currency=target_currency,
                source_amount=amount,
                converted_amount=None,
                rate=None,
                source=None,
                age_secs=None,
                haircut_bps=0,
                blocker_reason="sandbox_currency_not_live_settlement",
            )
        if source_currency == target_currency:
            return FxConversion(
                source_currency=source_currency,
                target_currency=target_currency,
                source_amount=amount,
                converted_amount=amount,
                rate=Decimal(1),
                source="identity",
                age_secs=0.0,
                haircut_bps=0,
            )
        if (
            source_currency in self.stablecoin_currencies
            and target_currency in self.stablecoin_currencies
        ):
            haircut = Decimal(haircut_sign) * Decimal(self.stablecoin_haircut_bps) / Decimal(10_000)
            return FxConversion(
                source_currency=source_currency,
                target_currency=target_currency,
                source_amount=amount,
                converted_amount=amount * (Decimal(1) + haircut),
                rate=Decimal(1),
                source="stablecoin_parity",
                age_secs=0.0,
                haircut_bps=self.stablecoin_haircut_bps,
            )
        rate: Decimal | None
        quote = self._find_quote(source_currency, target_currency)
        if quote is not None:
            rate, quote_source, quote_age = quote
            if quote_age > self.fx_quote_max_age_secs:
                return FxConversion(
                    source_currency=source_currency,
                    target_currency=target_currency,
                    source_amount=amount,
                    converted_amount=None,
                    rate=rate,
                    source=quote_source,
                    age_secs=quote_age,
                    haircut_bps=0,
                    blocker_reason="stale_fx_rate",
                )
        else:
            rate = self._configured_rate(source_currency, target_currency)
            quote_source = self.static_fx_source
            quote_age = 0.0
        if rate is None:
            return FxConversion(
                source_currency=source_currency,
                target_currency=target_currency,
                source_amount=amount,
                converted_amount=None,
                rate=None,
                source=None,
                age_secs=None,
                haircut_bps=0,
                blocker_reason="missing_fx_rate",
            )
        haircut = Decimal(haircut_sign) * Decimal(self.stablecoin_haircut_bps) / Decimal(10_000)
        return FxConversion(
            source_currency=source_currency,
            target_currency=target_currency,
            source_amount=amount,
            converted_amount=amount * rate * (Decimal(1) + haircut),
            rate=rate,
            source=quote_source,
            age_secs=quote_age,
            haircut_bps=self.stablecoin_haircut_bps,
        )

    def _find_quote(
        self,
        source_currency: str,
        target_currency: str,
    ) -> tuple[Decimal, str, float] | None:
        quotes = self.fx_quotes or {}
        direct_key = f"{source_currency}/{target_currency}"
        inverse_key = f"{target_currency}/{source_currency}"
        if direct_key in quotes:
            quote = _coerce_quote(direct_key, quotes[direct_key])
            rate = quote.ask or quote.rate
            if rate <= 0:
                return None
            return rate, quote.source, quote.age_secs
        if inverse_key in quotes:
            quote = _coerce_quote(inverse_key, quotes[inverse_key])
            inverse_rate = quote.bid or quote.rate
            if inverse_rate <= 0:
                return None
            return Decimal(1) / inverse_rate, quote.source, quote.age_secs
        return None

    def _configured_rate(self, source_currency: str, target_currency: str) -> Decimal | None:
        rates = self.static_fx_rates or {}
        direct_key = f"{source_currency}/{target_currency}"
        inverse_key = f"{target_currency}/{source_currency}"
        if direct_key in rates:
            direct = Decimal(str(rates[direct_key]))
            if direct > 0:
                return direct
        if inverse_key in rates:
            inverse = Decimal(str(rates[inverse_key]))
            if inverse > 0:
                return Decimal(1) / inverse
        return None


def _coerce_quote(pair: str, value: FxMarketQuote | Any) -> FxMarketQuote:
    if isinstance(value, FxMarketQuote):
        return value
    rate = Decimal(str(value.rate))
    source = str(getattr(value, "source", "fx_quote"))
    age_secs = float(getattr(value, "age_secs", 0.0))
    bid = getattr(value, "bid", None)
    ask = getattr(value, "ask", None)
    return FxMarketQuote(
        pair=pair,
        rate=rate,
        source=source,
        age_secs=age_secs,
        bid=Decimal(str(bid)) if bid is not None else None,
        ask=Decimal(str(ask)) if ask is not None else None,
    )


def _currency_code(value: object) -> str:
    code = getattr(value, "code", None)
    return str(code or value or "").strip().upper()
