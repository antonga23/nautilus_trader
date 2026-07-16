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
FX-aware cross-currency arbitrage sizing, payoff conversion, and fee/FX composition.

Hand-computed adversarial cases for the phantom cross-currency edge class: a pair whose
single-currency arbitrage math shows a positive edge that FX conversion erases or inverts.

"""

from decimal import Decimal

import pytest

from nautilus_trader.adapters.betting.common.fees import fx_adjusted_effective_odds
from nautilus_trader.adapters.betting.common.odds import calculate_cross_currency_arbitrage_stakes
from nautilus_trader.adapters.betting.fx import FxMarketQuote
from nautilus_trader.adapters.betting.fx import PortfolioCurrencyPolicy


def _policy(haircut_bps: int = 10) -> PortfolioCurrencyPolicy:
    return PortfolioCurrencyPolicy(
        base_currency="USD",
        stablecoin_haircut_bps=haircut_bps,
        static_fx_rates={"EUR/USD": Decimal("1.10")},
    )


class TestPayoffHaircutSign:
    def test_notional_conversion_inflates_by_haircut(self):
        # convert() is calibrated for NOTIONAL: the haircut over-states committed risk.
        conversion = _policy().convert(Decimal(100), "EUR")
        assert conversion.converted_amount == Decimal("110.11000")

    def test_payoff_conversion_reduces_by_haircut(self):
        # convert_payoff() carries the opposite haircut sign so the edge is never inflated.
        conversion = _policy().convert_payoff(Decimal(100), "EUR")
        assert conversion.converted_amount == Decimal("109.89000")
        # Conservative: the converted payoff is strictly below the un-haircut FX value and
        # strictly below the notional conversion, never above either.
        assert conversion.converted_amount < Decimal(100) * Decimal("1.10")
        assert conversion.converted_amount < _policy().convert(Decimal(100), "EUR").converted_amount

    def test_stablecoin_payoff_conversion_reduces(self):
        conversion = _policy().convert_payoff(Decimal(100), "USDC")
        assert conversion.converted_amount == Decimal("99.900")
        assert conversion.converted_amount < Decimal(100)

    def test_identity_payoff_conversion_is_lossless(self):
        conversion = _policy().convert_payoff(Decimal(100), "USD")
        assert conversion.converted_amount == Decimal(100)
        assert conversion.rate == Decimal(1)


class TestFxAdjustedEffectiveOdds:
    def test_cross_currency_round_trip_reduces_odds(self):
        # EUR leg: notional * (1.10 * 1.001), payoff * (1.10 * 0.999).
        base_odds = fx_adjusted_effective_odds(
            Decimal("2.20"),
            payoff_factor=Decimal("1.09890"),
            notional_factor=Decimal("1.10110"),
        )
        # payoff/notional = 0.999/1.001 < 1 -> the round-trip FX cost shaves the odds.
        assert base_odds < Decimal("2.20")
        assert base_odds == Decimal("2.20") * Decimal("1.09890") / Decimal("1.10110")

    def test_identity_factors_leave_odds_unchanged(self):
        base_odds = fx_adjusted_effective_odds(
            Decimal("2.20"),
            payoff_factor=Decimal(1),
            notional_factor=Decimal(1),
        )
        assert base_odds == Decimal("2.20")

    def test_non_positive_factor_rejected(self):
        with pytest.raises(ValueError):
            fx_adjusted_effective_odds(
                Decimal("2.20"),
                payoff_factor=Decimal(0),
                notional_factor=Decimal(1),
            )


class TestCrossCurrencyStakeSolver:
    def test_genuine_cross_currency_arb_equalizes_post_fx_payoffs(self):
        # CB leg EUR @ 2.20, SX leg USDC @ 2.10, EUR/USD = 1.10, 10 bps haircut, $1000 base.
        result = calculate_cross_currency_arbitrage_stakes(
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.10"),
            total_stake=Decimal(1000),
            policy=_policy(),
            currency_a="EUR",
            currency_b="USDC",
        )
        assert result.is_available
        assert result.blocker_reason is None
        assert result.base_currency == "USD"
        # Stakes are denominated in each leg's own settlement currency.
        assert result.stake_a == Decimal("443.53")
        assert result.stake_b == Decimal("511.12")
        # Post-FX payoffs are equalized in USD and clear a positive guaranteed profit.
        assert result.base_payoff == Decimal("1072.27")
        assert result.base_notional == Decimal("1000.00")
        assert result.guaranteed_profit == Decimal("72.27")

    def test_missing_fx_rate_blocks_pair_never_raw_sums(self):
        policy = PortfolioCurrencyPolicy(base_currency="USD")  # no EUR/USD rate configured
        result = calculate_cross_currency_arbitrage_stakes(
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.10"),
            total_stake=Decimal(1000),
            policy=policy,
            currency_a="EUR",
            currency_b="USDC",
        )
        assert result.is_available is False
        assert result.blocker_reason == "missing_fx_rate"
        # Fail safe: no stakes, and notional is NOT a raw 1:1 sum of the two legs.
        assert result.stake_a == Decimal(0)
        assert result.stake_b == Decimal(0)
        assert result.base_notional == Decimal(0)
        assert result.guaranteed_profit == Decimal(0)

    def test_non_positive_direct_static_rate_blocks_pair(self):
        # A misconfigured zero/negative DIRECT-key static rate must be rejected the same
        # way a missing rate is (the direct-key path lacked the >0 guard the inverse path had).
        for bad_rate in (Decimal(0), Decimal("-1.10")):
            policy = PortfolioCurrencyPolicy(
                base_currency="USD",
                static_fx_rates={"EUR/USD": bad_rate},
            )
            result = calculate_cross_currency_arbitrage_stakes(
                odds_a=Decimal("2.20"),
                odds_b=Decimal("2.10"),
                total_stake=Decimal(1000),
                policy=policy,
                currency_a="EUR",
                currency_b="USDC",
            )
            assert result.is_available is False, bad_rate
            assert result.blocker_reason == "missing_fx_rate", bad_rate
            assert result.stake_a == Decimal(0)
            assert result.stake_b == Decimal(0)
            assert result.base_notional == Decimal(0)

    def test_stablecoin_vs_base_equalizes_post_fx_payoffs(self):
        # USDC leg vs USD leg with USD base: only the USDC leg carries a haircut, so the
        # solver skews the stakes to equalize the two USD payoffs rather than splitting 50/50.
        result = calculate_cross_currency_arbitrage_stakes(
            odds_a=Decimal("2.10"),
            odds_b=Decimal("2.10"),
            total_stake=Decimal(1000),
            policy=_policy(),
            currency_a="USDC",
            currency_b="USD",
        )
        assert result.is_available
        assert result.stake_a == Decimal("500.00")
        assert result.stake_b == Decimal("499.50")
        assert result.base_payoff == Decimal("1048.95")
        assert result.base_notional == Decimal("1000.00")
        assert result.guaranteed_profit == Decimal("48.95")


class TestLiveFxQuotes:
    def test_fresh_live_quote_takes_precedence_over_static_rate(self):
        policy = PortfolioCurrencyPolicy(
            base_currency="USD",
            static_fx_rates={"EUR/USD": Decimal("1.00")},
            fx_quotes={
                "EUR/USD": FxMarketQuote(
                    pair="EUR/USD",
                    rate=Decimal("1.10"),
                    source="hyperliquid",
                    age_secs=1.0,
                ),
            },
        )

        conversion = policy.convert(Decimal(100), "EUR")

        # 100 * 1.10 live * 1.001 notional haircut — NOT the static 1.00 rate.
        assert conversion.converted_amount == Decimal("110.11")
        assert conversion.rate == Decimal("1.10")
        assert conversion.source == "hyperliquid"

    def test_stale_live_quote_blocks_instead_of_falling_back_to_static(self):
        # A present-but-stale live quote must fail closed, not silently revert to the
        # configured static rate the live feed exists to supersede.
        policy = PortfolioCurrencyPolicy(
            base_currency="USD",
            fx_quote_max_age_secs=30.0,
            static_fx_rates={"EUR/USD": Decimal("1.10")},
            fx_quotes={
                "EUR/USD": FxMarketQuote(
                    pair="EUR/USD",
                    rate=Decimal("1.10"),
                    source="hyperliquid",
                    age_secs=31.0,
                ),
            },
        )

        conversion = policy.convert(Decimal(100), "EUR")

        assert conversion.converted_amount is None
        assert conversion.blocker_reason == "stale_fx_rate"
