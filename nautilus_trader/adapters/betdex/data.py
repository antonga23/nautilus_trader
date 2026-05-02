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
BetDex/Monaco market data client.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from nautilus_trader.adapters.betdex.config import BetDexDataClientConfig
from nautilus_trader.adapters.betdex.constants import BETDEX_VENUE
from nautilus_trader.adapters.betdex.http_client import BetDexHttpClient
from nautilus_trader.adapters.betdex.http_client import BetDexHttpClientError
from nautilus_trader.adapters.betdex.providers import BetDexInstrumentProvider
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.data.messages import SubscribeQuoteTicks
from nautilus_trader.data.messages import UnsubscribeQuoteTicks
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import DataType
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


class BetDexDataClient(LiveMarketDataClient):
    """
    Polling market data client for BetDex/Monaco prices.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        http_client: BetDexHttpClient,
        instrument_provider: BetDexInstrumentProvider,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: BetDexDataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(BETDEX_VENUE.value),
            venue=BETDEX_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
        )
        self._http_client = http_client
        self._instrument_provider = instrument_provider
        self._betdex_instrument_provider = instrument_provider
        self._config = config
        self._subscribed_instruments: set[InstrumentId] = set()
        self._polling_task: asyncio.Task | None = None
        self._running = False
        self._polling_interval = float(config.quote_poll_interval_secs)
        self._poll_summary_interval = float(config.quote_poll_summary_interval_secs)
        self._poll_concurrency = int(config.quote_poll_concurrency)
        self._last_poll_summary_at = 0.0
        self._logger = logger

    async def _connect(self) -> None:
        started_at = time.perf_counter()
        self._log.info("Connecting BetDexDataClient...")
        await self._http_client.connect()
        await self._instrument_provider.load_all_async({})
        self._send_all_instruments_to_data_engine()
        self._auto_subscribe_loaded_instruments()
        self._log.info(f"BetDexDataClient connected in {time.perf_counter() - started_at:.2f}s")

    async def _disconnect(self) -> None:
        self._running = False
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
        await self._http_client.disconnect()

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        self._subscribed_instruments.add(command.instrument_id)
        if not self._running:
            self._running = True
            self._polling_task = asyncio.create_task(self._poll_prices())

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        self._subscribed_instruments.discard(command.instrument_id)
        if not self._subscribed_instruments:
            self._running = False

    async def _poll_prices(self) -> None:
        self._log.info("Starting BetDex price polling loop")
        while self._running:
            try:
                await self._poll_prices_once()
                await asyncio.sleep(self._polling_interval)
            except asyncio.CancelledError:
                break
            except (RuntimeError, ValueError, TypeError, KeyError, BetDexHttpClientError) as e:
                self._log.warning(f"Error in BetDex price polling: {e}")
                await asyncio.sleep(self._polling_interval)
        self._log.info("Stopped BetDex price polling loop")

    async def _poll_prices_once(self) -> None:
        market_ids = self._subscribed_market_ids()
        if not market_ids:
            return
        cycle_started_at = time.perf_counter()
        published = 0
        liquidity_count = 0
        max_latency = 0.0
        for chunk in self._chunks(sorted(market_ids), max(1, self._poll_concurrency)):
            request_started_ns = self._clock.timestamp_ns()
            started_at = time.perf_counter()
            payload = await self._http_client.get_market_prices(chunk)
            response_received_ns = self._clock.timestamp_ns()
            max_latency = max(max_latency, time.perf_counter() - started_at)
            prices = payload.get("prices", [])
            for market_prices in prices:
                market_id = str(market_prices.get("marketId") or "")
                levels = market_prices.get("prices") or []
                liquidity_count += len(levels)
                published += self._publish_market_quotes(
                    market_id=market_id,
                    levels=levels,
                    request_started_ns=request_started_ns,
                    response_received_ns=response_received_ns,
                )
        self._log_poll_summary(
            market_count=len(market_ids),
            quote_count=published,
            liquidity_count=liquidity_count,
            max_latency=max_latency,
            cycle_elapsed=time.perf_counter() - cycle_started_at,
        )

    def _publish_market_quotes(
        self,
        *,
        market_id: str,
        levels: list[dict[str, Any]],
        request_started_ns: int,
        response_received_ns: int,
    ) -> int:
        if not market_id:
            return 0
        instruments = self._betdex_instrument_provider.find_by_market_id(market_id)
        published = 0
        for instrument in instruments:
            if instrument.id not in self._subscribed_instruments:
                continue
            info = instrument.info if isinstance(instrument.info, dict) else {}
            outcome_id = str(info.get("outcome_id") or "")
            bid_price, bid_size, ask_price, ask_size = self._best_bid_ask(levels, outcome_id)
            if bid_price <= 0 and ask_price <= 0:
                continue
            quote = QuoteTick(
                instrument_id=instrument.id,
                bid_price=Price(bid_price, precision=2),
                ask_price=Price(ask_price, precision=2),
                bid_size=Quantity(bid_size, precision=2) if bid_size > 0 else Quantity.zero(),
                ask_size=Quantity(ask_size, precision=2) if ask_size > 0 else Quantity.zero(),
                ts_event=request_started_ns,
                ts_init=response_received_ns,
            )
            self._handle_data(quote)
            published += 1
        return published

    @staticmethod
    def _best_bid_ask(
        levels: list[dict[str, Any]],
        outcome_id: str,
    ) -> tuple[float, float, float, float]:  # skipcq
        bid_price = bid_size = ask_price = ask_size = 0.0
        for level in levels:
            parsed = BetDexDataClient._liquidity_level(level)
            if parsed is None:
                continue
            level_outcome_id, side, price, amount = parsed
            if level_outcome_id != outcome_id:
                continue
            if side == "For" and price > bid_price:
                bid_price = price
                bid_size = amount
            elif side == "Against" and (ask_price <= 0 or price < ask_price):
                ask_price = price
                ask_size = amount
        return bid_price, bid_size, ask_price, ask_size

    @staticmethod
    def _liquidity_level(level: dict[str, Any]) -> tuple[str, str, float, float] | None:
        try:
            price = float(level.get("price") or 0)
            amount = float(level.get("amount") or level.get("liquidity") or 0)
        except (TypeError, ValueError):
            return None
        if price <= 0 or amount <= 0:
            return None
        return str(level.get("outcomeId") or ""), str(level.get("side") or ""), price, amount

    def _subscribed_market_ids(self) -> set[str]:
        market_ids: set[str] = set()
        for instrument_id in list(self._subscribed_instruments):
            instrument = self._instrument_provider.find(instrument_id)
            if isinstance(instrument, CryptoBettingInstrument) and instrument.market_id:
                market_ids.add(instrument.market_id)
        return market_ids

    def _send_all_instruments_to_data_engine(self) -> None:
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    def _auto_subscribe_loaded_instruments(self) -> int:
        if not self._config.auto_subscribe_quote_ticks:
            return 0
        instruments = [
            instrument
            for instrument in self._instrument_provider.get_all().values()
            if isinstance(instrument, CryptoBettingInstrument)
        ]
        instruments.sort(key=lambda instrument: str(instrument.id))
        limit = self._config.quote_subscription_limit
        selected = instruments[:limit] if limit is not None else instruments
        for instrument in selected:
            self._subscribed_instruments.add(instrument.id)
        if selected and not self._running:
            self._running = True
            self._polling_task = asyncio.create_task(self._poll_prices())
        return len(selected)

    def _log_poll_summary(
        self,
        *,
        market_count: int,
        quote_count: int,
        liquidity_count: int,
        max_latency: float,
        cycle_elapsed: float,
    ) -> None:
        now = time.monotonic()
        if now - self._last_poll_summary_at < self._poll_summary_interval:
            return
        self._last_poll_summary_at = now
        self._log.info(
            "BetDex price poll cycle: "
            f"markets={market_count} liquidity_levels={liquidity_count} quotes={quote_count} "
            f"subscribed_instruments={len(self._subscribed_instruments)} "
            f"concurrency={self._poll_concurrency} max_latency={max_latency:.2f}s "
            f"cycle_elapsed={cycle_elapsed:.2f}s",
        )

    @staticmethod
    def _chunks(values: list[str], chunk_size: int) -> list[list[str]]:
        return [values[index : index + chunk_size] for index in range(0, len(values), chunk_size)]

    async def _subscribe_instrument(self, instrument_id: InstrumentId) -> None:
        self._log.debug(f"Ignoring direct BetDex instrument subscription: {instrument_id}")

    async def _subscribe_instruments(self, command: object = None) -> None:
        self._log.debug(f"Ignoring BetDex bulk instrument subscription request: {command!r}")

    async def _request_data(self, data_type: DataType) -> None:
        self._log.warning(f"Unsupported BetDex data request: {data_type}")

    def subscribed_quote_ticks(self) -> list[InstrumentId]:
        subscriptions = set(super().subscribed_quote_ticks())
        subscriptions.update(self._subscribed_instruments)
        return sorted(subscriptions, key=str)
