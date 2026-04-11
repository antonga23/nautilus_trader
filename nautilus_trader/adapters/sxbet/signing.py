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
SX.bet EIP712 signing utilities for order submission.
"""

import secrets
import time
from decimal import ROUND_DOWN
from decimal import ROUND_HALF_UP
from decimal import Decimal
from typing import Any


def generate_salt() -> int:
    """
    Generate a random salt for order uniqueness.
    """
    return secrets.randbits(256)


def get_expiry(hours: int = 24) -> int:
    """
    Get expiry timestamp (default 24 hours from now).
    """
    return int(time.time()) + (hours * 3600)


def decimal_odds_to_percentage(decimal_odds: float) -> int:
    """
    Convert decimal odds to SX.bet percentage odds format.

    SX.bet uses percentage odds = implied probability * 10000

    Parameters
    ----------
    decimal_odds : float
        Decimal odds (e.g., 2.0 for even money).

    Returns
    -------
    int
        Percentage odds in SX.bet format.

    Examples
    --------
    >>> decimal_odds_to_percentage(2.0)  # 50% probability
    5000
    >>> decimal_odds_to_percentage(1.5)  # 66.67% probability
    6667

    """
    implied_probability = Decimal(1) / Decimal(str(decimal_odds))
    return int(
        (implied_probability * Decimal(10000)).to_integral_value(rounding=ROUND_HALF_UP),
    )


def percentage_to_decimal_odds(percentage_odds: int) -> float:
    """
    Convert SX.bet percentage odds to decimal format.

    Parameters
    ----------
    percentage_odds : int
        SX.bet percentage odds (probability * 10000).

    Returns
    -------
    float
        Decimal odds.

    """
    implied_probability = percentage_odds / 10000
    return 1 / implied_probability


def to_wei(amount: Decimal | str | int, decimals: int = 6) -> int:
    """
    Convert token amount to wei (smallest unit).

    Parameters
    ----------
    amount : Decimal | str | int
        Amount in tokens (e.g., USDC).
    decimals : int, default 6
        Token decimals (USDC = 6).

    Returns
    -------
    int
        Amount in wei.

    """
    amount_dec = Decimal(str(amount))
    multiplier = Decimal(10) ** decimals
    return int((amount_dec * multiplier).to_integral_value(rounding=ROUND_DOWN))


def from_wei(wei_amount: int | str, decimals: int = 6) -> float:
    """
    Convert wei to token amount.

    Parameters
    ----------
    wei_amount : int | str
        Amount in wei.
    decimals : int, default 6
        Token decimals.

    Returns
    -------
    float
        Amount in tokens.

    """
    return int(wei_amount) / (10**decimals)


def _normalize_uint256(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("bool is not a valid uint256 value")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise TypeError(f"Unsupported uint256 value type: {type(value).__name__}")


def build_order_typed_data(
    order: dict[str, Any],
    chain_id: int = 416,
) -> dict[str, Any]:
    """
    Build EIP712 typed data structure for order signing.

    Parameters
    ----------
    order : dict
        Order parameters.
    chain_id : int, default 416
        SX Network chain ID.

    Returns
    -------
    dict
        EIP712 typed data structure.

    """
    canonical_order = {
        **order,
        "totalBetSize": _normalize_uint256(order["totalBetSize"]),
        "percentageOdds": _normalize_uint256(order["percentageOdds"]),
        "salt": _normalize_uint256(order["salt"]),
    }

    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "Order": [
                {"name": "marketHash", "type": "bytes32"},
                {"name": "maker", "type": "address"},
                {"name": "totalBetSize", "type": "uint256"},
                {"name": "percentageOdds", "type": "uint256"},
                {"name": "expiry", "type": "uint256"},
                {"name": "baseToken", "type": "address"},
                {"name": "salt", "type": "uint256"},
                {"name": "isMakerBettingOutcomeOne", "type": "bool"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "SportX",
            "version": "1.0",
            "chainId": chain_id,
        },
        "message": canonical_order,
    }


def sign_order_hash(order_hash: bytes, private_key: str) -> str:
    """
    Sign an order hash with a private key.

    This is a placeholder - actual implementation requires eth-account.

    Parameters
    ----------
    order_hash : bytes
        The keccak256 hash of the order.
    private_key : str
        The Ethereum private key.

    Returns
    -------
    str
        The signature (v, r, s concatenated).

    Note
    ----
    Requires eth-account library: pip install eth-account

    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        message = encode_defunct(order_hash)
        signed = Account.sign_message(message, private_key=private_key)
        return signed.signature.hex()
    except ImportError:
        raise ImportError(
            "eth-account library required for signing. Install with: pip install eth-account",
        )


def sign_eip712_order(
    order: dict[str, Any],
    private_key: str,
    chain_id: int = 416,
) -> str:
    """
    Sign an order using EIP712 typed data signing.

    Parameters
    ----------
    order : dict
        The order parameters.
    private_key : str
        The Ethereum private key.
    chain_id : int, default 416
        SX Network chain ID.

    Returns
    -------
    str
        The EIP712 signature.

    Note
    ----
    Requires eth-account library: pip install eth-account

    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        typed_data = build_order_typed_data(order, chain_id)
        signed = Account.sign_message(
            encode_typed_data(
                domain_data=typed_data["domain"],
                message_types=typed_data["types"],
                message_data=typed_data["message"],
            ),
            private_key=private_key,
        )
        return signed.signature.hex()
    except ImportError:
        raise ImportError(
            "eth-account library required for EIP712 signing. "
            "Install with: pip install eth-account",
        )


def get_wallet_address(private_key: str) -> str:
    """
    Get wallet address from private key.

    Parameters
    ----------
    private_key : str
        The Ethereum private key.

    Returns
    -------
    str
        The wallet address.

    """
    try:
        from eth_account import Account

        return Account.from_key(private_key).address
    except ImportError:
        raise ImportError(
            "eth-account library required. Install with: pip install eth-account",
        )
