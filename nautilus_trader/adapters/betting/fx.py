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

from dataclasses import dataclass
from decimal import Decimal


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
class PortfolioCurrencyPolicy:
    """
    Convert venue stakes into one conservative portfolio base currency.
    """

    base_currency: str = "USD"
    stablecoin_currencies: frozenset[str] = USD_EQUIVALENT_CURRENCIES
    stablecoin_haircut_bps: int = 10
    fx_quote_max_age_secs: float = 30.0
    static_fx_rates: dict[str, Decimal] | None = None
    static_fx_source: str = "configured_static_fx"
    sandbox_currencies: frozenset[str] = SANDBOX_CURRENCIES

    def convert(self, amount: Decimal, currency: str) -> FxConversion:
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
            haircut = Decimal(self.stablecoin_haircut_bps) / Decimal(10_000)
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
        rate = self._configured_rate(source_currency, target_currency)
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
        haircut = Decimal(self.stablecoin_haircut_bps) / Decimal(10_000)
        return FxConversion(
            source_currency=source_currency,
            target_currency=target_currency,
            source_amount=amount,
            converted_amount=amount * rate * (Decimal(1) + haircut),
            rate=rate,
            source=self.static_fx_source,
            age_secs=0.0,
            haircut_bps=self.stablecoin_haircut_bps,
        )

    def _configured_rate(self, source_currency: str, target_currency: str) -> Decimal | None:
        rates = self.static_fx_rates or {}
        direct_key = f"{source_currency}/{target_currency}"
        inverse_key = f"{target_currency}/{source_currency}"
        if direct_key in rates:
            return Decimal(str(rates[direct_key]))
        if inverse_key in rates:
            inverse = Decimal(str(rates[inverse_key]))
            if inverse > 0:
                return Decimal(1) / inverse
        return None


def _currency_code(value: object) -> str:
    code = getattr(value, "code", None)
    return str(code or value or "").strip().upper()
