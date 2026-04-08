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
SX.bet adapter configuration classes.
"""

from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import LiveExecClientConfig


class SXBetInstrumentProviderConfig(InstrumentProviderConfig, frozen=True):
    """
    Configuration for ``SXBetInstrumentProvider`` instances.

    Parameters
    ----------
    api_key : str, optional
        The SX.bet API key (not required for market data).
    api_url : str, optional
        The SX.bet API base URL.
    load_all : bool, default False
        If all venue instruments should be loaded on start.
    sport_ids : FrozenSet[int], optional
        Filter by sport IDs.
    league_ids : FrozenSet[int], optional
        Filter by league IDs.
    live_only : bool, default False
        If True, only load live markets.
    log_warnings : bool, default True
        If parser warnings should be logged.

    """

    api_key: str | None = None
    api_url: str | None = None
    load_all: bool = False
    sport_ids: frozenset[int] | None = None
    league_ids: frozenset[int] | None = None
    live_only: bool = False
    log_warnings: bool = True


class SXBetDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for ``SXBetDataClient`` instances.

    Parameters
    ----------
    api_key : str, optional
        The SX.bet API key (not required for market data but needed for WebSocket).
    api_url : str, optional
        The SX.bet API base URL.
    ws_url : str, optional
        The SX.bet WebSocket URL.
    instrument_provider : SXBetInstrumentProviderConfig
        The instrument provider configuration.
    sport_ids : FrozenSet[int], optional
        Sports to subscribe to.
    reconnect_on_disconnect : bool, default True
        If WebSocket should reconnect on disconnect.
    max_reconnect_attempts : int, default 5
        Maximum WebSocket reconnection attempts.

    """

    api_key: str | None = None
    api_url: str | None = None
    ws_url: str | None = None
    instrument_provider: SXBetInstrumentProviderConfig | None = None  # type: ignore[assignment]
    sport_ids: frozenset[int] | None = None
    reconnect_on_disconnect: bool = True
    max_reconnect_attempts: int = 5


class SXBetExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for ``SXBetExecutionClient`` instances.

    Parameters
    ----------
    api_key : str
        The SX.bet API key for authentication.
    private_key : str
        The Ethereum private key for signing orders (EIP712).
    wallet_address : str
        The Ethereum wallet address.
    api_url : str, optional
        The SX.bet API base URL.
    ws_url : str, optional
        The SX.bet WebSocket URL for order updates.
    instrument_provider : SXBetInstrumentProviderConfig
        The instrument provider configuration.
    max_retry_attempts : int, default 3
        Maximum retry attempts for failed order submissions.
    base_currency : str, default "USDC"
        The base currency for trading. Only ``"USDC"`` is currently supported
        by the execution client.

    """

    api_key: str = ""
    private_key: str = ""
    wallet_address: str = ""
    api_url: str | None = None
    ws_url: str | None = None
    instrument_provider: SXBetInstrumentProviderConfig | None = None  # type: ignore[assignment]
    max_retry_attempts: int = 3
    base_currency: str = "USDC"
