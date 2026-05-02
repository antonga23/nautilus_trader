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
BetDex/Monaco adapter constants.
"""

from nautilus_trader.model.identifiers import Venue


BETDEX_VENUE = Venue("BETDEX")
BETDEX_SANDBOX_API_BASE_URL = "https://sandbox.api.monacoprotocol.xyz"
BETDEX_PRODUCTION_API_BASE_URL = "https://production.api.monacoprotocol.xyz"
BETDEX_SANDBOX_STREAM_URL = "wss://sandbox.stream.api.monacoprotocol.xyz"
BETDEX_PRODUCTION_STREAM_URL = "wss://production.stream.api.monacoprotocol.xyz"

BETDEX_AGGREGATED_VENUES = frozenset({"SXBET", "POLYMARKET"})

BETDEX_ENDPOINTS = {
    "sessions": "/sessions",
    "sessions_refresh": "/sessions/refresh",
    "events": "/events",
    "markets": "/markets",
    "market_prices": "/market-prices",
    "wallets": "/wallets",
    "orders": "/orders",
    "order_cancel": "/orders/{order_id}/cancel",
    "faucet": "/faucet",
}

BETDEX_DEFAULT_CURRENCY = "USDC"
