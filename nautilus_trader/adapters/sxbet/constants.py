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

# API configuration (SX mainnet)
SXBET_API_BASE_URL = "https://api.sx.bet"
SXBET_WS_BASE_URL = "wss://api.sx.bet"

# Realtime streaming (Centrifugo bidirectional client protocol; replaces the
# Ably feed deprecated 2026-07-01). A JWT is obtained from the ``realtime_token``
# REST endpoint and passed in the Centrifugo connect frame.
SXBET_REALTIME_WS_URL = "wss://realtime.sx.bet/connection/websocket"

# Order-book update channel template. ``{market_hash}`` is a market hash such as
# ``order_book:market_0x1234...``.
SXBET_ORDER_BOOK_CHANNEL_TEMPLATE = "order_book:market_{market_hash}"

# API endpoints
SXBET_ENDPOINTS = {
    # Market data (no API key required)
    "sports": "/sports",
    "leagues": "/leagues",
    "active_leagues": "/leagues/active",
    "fixtures": "/fixture/active",
    "active_markets": "/markets/active",
    "market_lookup": "/markets/find",
    "best_odds": "/orders/odds/best",
    "realtime_token": "/user/realtime-token/api-key",
    # Order book
    "order_book": "/orders",
    # Trading (API key required)
    "place_order": "/orders/new",
    "fill_order": "/orders/fill/v2",
    "cancel_order": "/orders/cancel",
    "cancel_all_orders": "/orders/cancel/all",
    "user_orders": "/orders",
    "user_trades": "/trades",
}

# SX.bet sport IDs
#
# Keep this as a fallback only. The instrument provider refreshes the mapping
# from /sports + /leagues/active when the HTTP client exposes it.
SXBET_SPORT_IDS = {
    1: "basketball",
    2: "ice_hockey",
    3: "baseball",
    5: "soccer",
    6: "tennis",
    7: "mma",
    9: "esports",
    15: "cricket",
    20: "rugby_league",
    26: "australian_rules",
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
    "USDC": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
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
    "version": "6.0",
    "chainId": 4162,  # SX Network chain ID
}

# WebSocket channels
SXBET_WS_CHANNELS = {
    "markets": "markets",
    "orders": "orders",
    "trades": "trades",
    "scores": "scores",
}
