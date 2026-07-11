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
from typing import cast

from nautilus_trader.adapters.sxbet.constants import SXBET_EIP712_DOMAIN


def generate_salt() -> int:
    """
    Generate a random salt for order uniqueness.
    """
    return secrets.randbits(256)


SXBET_PERCENTAGE_ODDS_SCALE = Decimal("1e20")


def _default_chain_id() -> int:
    return cast(int, SXBET_EIP712_DOMAIN["chainId"])


def get_expiry(hours: int = 24) -> int:
    """
    Get expiry timestamp (default 24 hours from now).
    """
    return int(time.time()) + (hours * 3600)


def decimal_odds_to_percentage(decimal_odds: float) -> int:
    """
    Convert decimal odds to SX.bet percentage odds format.

    The live SX.bet REST API uses percentage odds encoded as implied probability on
    a ``10^20`` scale.

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
    50000000000000000000
    >>> decimal_odds_to_percentage(1.5)  # 66.67% probability
    66666666666666666667

    """
    implied_probability = Decimal(1) / Decimal(str(decimal_odds))
    return int(
        (implied_probability * SXBET_PERCENTAGE_ODDS_SCALE).to_integral_value(
            rounding=ROUND_HALF_UP,
        ),
    )


def percentage_to_decimal_odds(percentage_odds: int) -> float:
    """
    Convert SX.bet percentage odds to decimal format.

    Parameters
    ----------
    percentage_odds : int
        SX.bet percentage odds (implied probability scaled by ``10^20``).

    Returns
    -------
    float
        Decimal odds.

    """
    implied_probability = Decimal(str(percentage_odds)) / SXBET_PERCENTAGE_ODDS_SCALE
    if implied_probability <= 0:
        raise ValueError("percentage_odds must be positive")
    return float(Decimal(1) / implied_probability)


def taker_decimal_odds_from_maker_percentage(percentage_odds: int) -> float:
    """
    Convert a resting maker order's percentage odds to the decimal odds a taker
    receives.

    On SX.bet a taker matches a maker by backing the *opposite* outcome, so the
    taker's implied probability is the complement of the maker's:
    ``taker_implied = 1 - maker_percentage / 1e20`` and ``decimal = 1 / taker_implied``.
    Applying ``percentage_to_decimal_odds`` directly to a maker order (as if the
    maker's probability were the taker's) overstates the odds and manufactures a
    phantom overlay on every two-sided book.

    Returns ``0.0`` when the maker odds fall outside the open interval that yields
    valid taker odds (``> 1``), so callers can skip unusable orders.

    Parameters
    ----------
    percentage_odds : int
        Resting maker order percentage odds (implied probability scaled by ``10^20``).

    Returns
    -------
    float
        Decimal odds available to the taker, or ``0.0`` if out of range.

    """
    maker_implied = Decimal(str(percentage_odds)) / SXBET_PERCENTAGE_ODDS_SCALE
    taker_implied = Decimal(1) - maker_implied
    if taker_implied <= 0 or taker_implied >= 1:
        return 0.0
    return float(Decimal(1) / taker_implied)


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
    chain_id: int | None = None,
) -> dict[str, Any]:
    """
    Build EIP712 typed data structure for order signing.

    Parameters
    ----------
    order : dict
        Order parameters.
    chain_id : int, default 4162
        SX Network chain ID.

    Returns
    -------
    dict
        EIP712 typed data structure.

    """
    if chain_id is None:
        chain_id = _default_chain_id()

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
            "name": SXBET_EIP712_DOMAIN["name"],
            "version": SXBET_EIP712_DOMAIN["version"],
            "chainId": chain_id,
        },
        "message": canonical_order,
    }


def build_fill_order_typed_data(
    fill: dict[str, Any],
    chain_id: int | None = None,
) -> dict[str, Any]:
    """
    Build EIP712 typed data structure for SX.bet taker fill signing.
    """
    if chain_id is None:
        chain_id = _default_chain_id()

    canonical_fill = {
        **fill,
        "stakeWei": _normalize_uint256(fill["stakeWei"]),
        "desiredOdds": _normalize_uint256(fill["desiredOdds"]),
        "oddsSlippage": _normalize_uint256(fill["oddsSlippage"]),
        "fillSalt": _normalize_uint256(fill["fillSalt"]),
    }

    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "FillOrder": [
                {"name": "market", "type": "string"},
                {"name": "taker", "type": "address"},
                {"name": "baseToken", "type": "address"},
                {"name": "isTakerBettingOutcomeOne", "type": "bool"},
                {"name": "stakeWei", "type": "uint256"},
                {"name": "desiredOdds", "type": "uint256"},
                {"name": "oddsSlippage", "type": "uint256"},
                {"name": "fillSalt", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "primaryType": "FillOrder",
        "domain": {
            "name": SXBET_EIP712_DOMAIN["name"],
            "version": SXBET_EIP712_DOMAIN["version"],
            "chainId": chain_id,
        },
        "message": canonical_fill,
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
    chain_id: int | None = None,
) -> str:
    """
    Sign an order using EIP712 typed data signing.

    Parameters
    ----------
    order : dict
        The order parameters.
    private_key : str
        The Ethereum private key.
    chain_id : int, default 4162
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

        if chain_id is None:
            chain_id = _default_chain_id()

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


def sign_eip712_fill_order(
    fill: dict[str, Any],
    private_key: str,
    chain_id: int | None = None,
) -> str:
    """
    Sign a taker fill payload using EIP712 typed data signing.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        if chain_id is None:
            chain_id = _default_chain_id()

        typed_data = build_fill_order_typed_data(fill, chain_id)
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
