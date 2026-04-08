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
SX.bet constants.
"""

from nautilus_trader.model.identifiers import Venue


# Primary venue identifier
SXBET_VENUE = Venue("SXBET")

# API configuration (SX Network - Arbitrum chain)
SXBET_API_BASE_URL = "https://api.sx.bet/v3"
SXBET_WS_BASE_URL = "wss://api.sx.bet"

# API endpoints
SXBET_ENDPOINTS = {
    # Market data (no API key required)
    "sports": "/sports",
    "leagues": "/leagues",
    "fixtures": "/fixtures",
    "markets": "/markets",
    "market_by_id": "/markets/{market_hash}",
    "active_leagues": "/activeLeagues",
    "active_sports": "/activeSports",
    # Order book
    "order_book": "/orders/active",
    "market_orders": "/orders/active/by-market/{market_hash}",
    # Trading (API key required)
    "place_order": "/orders/new",
    "cancel_order": "/orders/cancel",
    "user_orders": "/orders/user",
    "user_trades": "/trades/user",
    # Account
    "balance": "/balance",
}

# SX.bet sport IDs
SXBET_SPORT_IDS = {
    1: "soccer",
    2: "basketball",
    3: "baseball",
    4: "ice_hockey",
    5: "american_football",
    6: "tennis",
    7: "mma",
    8: "boxing",
    9: "esports",
    10: "cricket",
    14: "golf",
    15: "rugby",
    17: "table_tennis",
}

# Market types
SXBET_MARKET_TYPES = {
    0: "money_line",  # Moneyline / Match winner
    1: "spread",  # Point spread / Handicap
    2: "total",  # Over/Under
    3: "draw_no_bet",  # Draw no bet
    4: "both_to_score",  # Both teams to score
    5: "correct_score",  # Correct score
}

# Token addresses (SX Network / Arbitrum)
SXBET_TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "SX": "0x9b85A81c3D4f0B012C2a8F3D2a53f1B56d5c5EF9",
}

# Rate limiting
SXBET_RATE_LIMIT_PER_SECOND = 20
SXBET_RATE_LIMIT_BURST = 50

# Order constants
SXBET_MIN_ORDER_SIZE_USDC = 5  # Minimum bet size in USDC
SXBET_MAX_ODDS = 100.0  # Maximum decimal odds

# EIP712 domain for signing
SXBET_EIP712_DOMAIN = {
    "name": "SportX",
    "version": "1.0",
    "chainId": 416,  # SX Network chain ID
}

# WebSocket channels
SXBET_WS_CHANNELS = {
    "markets": "markets",
    "orders": "orders",
    "trades": "trades",
    "scores": "scores",
}
