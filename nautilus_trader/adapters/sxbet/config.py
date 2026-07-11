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
from nautilus_trader.config import PositiveFloat
from nautilus_trader.config import PositiveInt


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
    instrument_load_limit : PositiveInt, optional
        Maximum number of instruments to create from loaded markets.
    market_discovery_limit : PositiveInt, optional
        Maximum number of markets to discover before liquidity selection.
    prefer_liquid_markets : bool, default False
        If True, probe order books and prefer markets with two-sided active orders.
    liquidity_probe_limit : PositiveInt, optional
        Maximum markets to probe while searching for liquid markets.
    min_two_sided_markets : PositiveInt, default 1
        Minimum desired count of markets with active orders on both outcomes.
    max_resolution_horizon_hours : PositiveFloat, optional
        Prefer markets whose fixture starts within this many hours when applying
        discovery and liquidity-selection budgets.
    api_key_pool : tuple[str, ...], optional
        SX.bet API keys for realtime/WebSocket-capable surfaces.
    log_warnings : bool, default True
        If parser warnings should be logged.

    """

    api_key: str | None = None
    api_url: str | None = None
    load_all: bool = False
    sport_ids: frozenset[int] | None = None
    league_ids: frozenset[int] | None = None
    live_only: bool = False
    instrument_load_limit: PositiveInt | None = None
    market_discovery_limit: PositiveInt | None = None
    prefer_liquid_markets: bool = False
    liquidity_probe_limit: PositiveInt = 100
    min_two_sided_markets: PositiveInt = 1
    max_resolution_horizon_hours: PositiveFloat | None = None
    api_key_pool: tuple[str, ...] | None = None
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
    auto_subscribe_quote_ticks : bool, default False
        If loaded instruments should be subscribed for quote polling after connect.
    quote_subscription_limit : PositiveInt, optional
        Maximum loaded instruments to subscribe when auto-subscription is enabled.
    order_book_poll_interval_secs : PositiveFloat, default 3.0
        Order book polling interval in seconds.
    order_book_poll_summary_interval_secs : PositiveFloat, default 30.0
        Minimum interval between order book polling summary log lines.
    order_book_concurrency : PositiveInt, default 4
        Maximum concurrent order-book REST requests per poll cycle.
    order_book_poll_mode : str, default "order_book"
        Quote polling mode. ``"order_book"`` fetches each market order book and
        preserves depth diagnostics. ``"best_odds_batch"`` fetches top-of-book
        odds in market batches for live low-latency pilots.
    order_book_best_odds_batch_size : PositiveInt, default 30
        Number of market hashes per SX.bet best-odds batch request.
    order_book_min_concurrency : PositiveInt, default 1
        Lower bound for adaptive order-book polling concurrency.
    order_book_max_concurrency : PositiveInt, optional
        Upper bound for adaptive order-book polling concurrency.
    order_book_target_cycle_secs : PositiveFloat, optional
        Target poll-cycle duration for live latency diagnostics and adaptive fanout.
    order_book_adaptive_concurrency : bool, default False
        If true, adjust polling concurrency toward the target cycle duration.
    fetch_timeout_secs : PositiveFloat, optional
        Independent timeout for each poll-cycle fetch; a fetch exceeding it is
        recorded as a failure. Defaults to ``order_book_poll_interval_secs``.
    cycle_deadline_secs : PositiveFloat, optional
        Hard deadline for a full poll cycle; still-pending fetches are cancelled
        and recorded as failures. Defaults to ``2 * order_book_poll_interval_secs``.
    api_key_pool : tuple[str, ...], optional
        SX.bet API keys for realtime/WebSocket-capable surfaces.

    """

    api_key: str | None = None
    api_url: str | None = None
    ws_url: str | None = None
    instrument_provider: SXBetInstrumentProviderConfig | None = None  # type: ignore[assignment]
    sport_ids: frozenset[int] | None = None
    reconnect_on_disconnect: bool = True
    max_reconnect_attempts: int = 5
    auto_subscribe_quote_ticks: bool = False
    quote_subscription_limit: PositiveInt | None = None
    order_book_poll_interval_secs: PositiveFloat = 3.0
    order_book_poll_summary_interval_secs: PositiveFloat = 30.0
    order_book_concurrency: PositiveInt = 4
    order_book_poll_mode: str = "order_book"
    order_book_best_odds_batch_size: PositiveInt = 30
    order_book_min_concurrency: PositiveInt = 1
    order_book_max_concurrency: PositiveInt | None = None
    order_book_target_cycle_secs: PositiveFloat | None = None
    order_book_adaptive_concurrency: bool = False
    fetch_timeout_secs: PositiveFloat | None = None
    cycle_deadline_secs: PositiveFloat | None = None
    api_key_pool: tuple[str, ...] | None = None


class SXBetExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for ``SXBetExecutionClient`` instances.

    Parameters
    ----------
    api_key : str
        The SX.bet API key for authentication.
    api_key_pool : tuple[str, ...], optional
        SX.bet API keys for realtime/WebSocket-capable surfaces.
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
    dry_run : bool, default False
        If True, build and sign order payloads but do not submit them to SX.bet.
    execution_mode : str, default "taker_fill"
        Execution path for live orders. ``"taker_fill"`` fills displayed liquidity,
        while ``"maker_post"`` posts a maker order to the SX.bet order book.
    odds_slippage : int, default 5
        Slippage tolerance sent to the SX.bet taker fill endpoint.
    fill_poll_interval_secs : PositiveFloat, default 3.0
        Interval for polling SX.bet order status to detect newly matched size and
        emit fills. SX.bet has no authenticated user fill push feed, so fills are
        reconciled by polling the order-status ``fillAmount`` field.
    account_state_interval_secs : PositiveFloat, default 30.0
        Interval for refreshing and re-publishing the SX.bet account state.

    """

    api_key: str = ""
    api_key_pool: tuple[str, ...] | None = None
    private_key: str = ""
    wallet_address: str = ""
    api_url: str | None = None
    ws_url: str | None = None
    instrument_provider: SXBetInstrumentProviderConfig | None = None  # type: ignore[assignment]
    max_retry_attempts: int = 3
    base_currency: str = "USDC"
    dry_run: bool = False
    execution_mode: str = "taker_fill"
    odds_slippage: int = 5
    fill_poll_interval_secs: PositiveFloat = 3.0
    account_state_interval_secs: PositiveFloat = 30.0
