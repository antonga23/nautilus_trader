# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SX.bet EIP712 signing payload construction.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

import pytest
from eth_account.messages import encode_typed_data

from nautilus_trader.adapters.sxbet.constants import SXBET_EIP712_DOMAIN
from nautilus_trader.adapters.sxbet.signing import build_order_typed_data
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage
from nautilus_trader.adapters.sxbet.signing import percentage_to_decimal_odds
from nautilus_trader.adapters.sxbet.signing import to_wei


ORDER_TOTAL_BET_SIZE = 1000
ORDER_PERCENTAGE_ODDS = 50_000_000_000_000_000_000
DECIMAL_SAFE_WEI = 290000
PERCENTAGE_ODDS_ROUNDED = 66_666_666_666_666_666_667


def test_build_order_typed_data_canonicalizes_uint256_fields():
    typed_data = build_order_typed_data(
        order={
            "marketHash": "0x" + "12" * 32,
            "maker": "0x" + "34" * 20,
            "totalBetSize": str(ORDER_TOTAL_BET_SIZE),
            "percentageOdds": str(ORDER_PERCENTAGE_ODDS),
            "expiry": 1_700_000_000,
            "baseToken": "0x" + "56" * 20,
            "salt": "0x" + "78" * 32,
            "isMakerBettingOutcomeOne": True,
        },
        chain_id=SXBET_EIP712_DOMAIN["chainId"],
    )

    signable_message = encode_typed_data(full_message=typed_data)

    assert typed_data["message"]["totalBetSize"] == ORDER_TOTAL_BET_SIZE
    assert typed_data["message"]["percentageOdds"] == ORDER_PERCENTAGE_ODDS
    assert typed_data["message"]["salt"] == int("0x" + "78" * 32, 16)
    assert typed_data["domain"] == SXBET_EIP712_DOMAIN
    assert signable_message.body


def test_to_wei_uses_decimal_safe_rounding():
    assert to_wei(Decimal("0.29"), decimals=6) == DECIMAL_SAFE_WEI


def test_decimal_odds_to_percentage_rounds_to_nearest_integer():
    assert decimal_odds_to_percentage(1.5) == PERCENTAGE_ODDS_ROUNDED


def test_percentage_to_decimal_odds_round_trips_scaled_values():
    assert percentage_to_decimal_odds(decimal_odds_to_percentage(2.75)) == pytest.approx(2.75)


@pytest.mark.parametrize("percentage_odds", [0, -1])
def test_percentage_to_decimal_odds_rejects_non_positive_values(percentage_odds):
    with pytest.raises(ValueError, match="percentage_odds must be positive"):
        percentage_to_decimal_odds(percentage_odds)
