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
SX.bet market data client.
"""

import asyncio
import contextlib
import time

from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.config import SXBetDataClientConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.signing import percentage_to_decimal_odds
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import DataType
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


class SXBetDataClient(LiveMarketDataClient):
    """
    Provides a data client for the SX.bet venue.

    Uses polling for order book updates (WebSocket would be preferred).

    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        loop: asyncio.AbstractEventLoop,
        http_client: SXBetHttpClient,
        instrument_provider: SXBetInstrumentProvider,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: SXBetDataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(SXBET_VENUE.value),
            venue=SXBET_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
        )

        self._http_client = http_client
        self._config = config
        self._instrument_provider: SXBetInstrumentProvider = instrument_provider
        self._subscribed_instruments: set[InstrumentId] = set()
        self._polling_task: asyncio.Task | None = None
        self._polling_interval = float(config.order_book_poll_interval_secs)
        self._poll_summary_interval = float(config.order_book_poll_summary_interval_secs)
        self._order_book_concurrency = int(config.order_book_concurrency)
        self._last_poll_summary_at = 0.0
        self._running = False
        self._logger = logger

    async def _connect(self) -> None:
        """
        Connect to the data source.
        """
        started_at = time.perf_counter()
        self._log.info("Connecting SXBetDataClient...")
        http_started_at = time.perf_counter()
        await self._http_client.connect()
        self._log.info(
            f"SXBetHttpClient connect completed in {time.perf_counter() - http_started_at:.2f}s",
        )

        # Load instruments
        filters = {}
        if self._config.sport_ids:
            filters["sport_ids"] = self._config.sport_ids

        load_started_at = time.perf_counter()
        await self._instrument_provider.load_all_async(filters)
        self._log.info(
            f"SX.bet instrument provider load completed in "
            f"{time.perf_counter() - load_started_at:.2f}s",
        )
        publish_started_at = time.perf_counter()
        self._send_all_instruments_to_data_engine()
        self._log.info(
            f"Sent {len(self._instrument_provider.get_all())} SX.bet instruments to DataEngine "
            f"in {time.perf_counter() - publish_started_at:.2f}s",
        )
        subscribe_started_at = time.perf_counter()
        self._auto_subscribe_loaded_instruments()
        self._log.info(
            f"SX.bet auto-subscription completed in {time.perf_counter() - subscribe_started_at:.2f}s",
        )

        self._log.info(f"SXBetDataClient connected in {time.perf_counter() - started_at:.2f}s")

    async def _disconnect(self) -> None:
        """
        Disconnect from the data source.
        """
        self._log.info("Disconnecting SXBetDataClient...")

        self._running = False

        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task

        await self._http_client.disconnect()
        self._log.info("SXBetDataClient disconnected")

    async def _subscribe_quote_ticks(self, instrument_id: InstrumentId) -> None:
        """
        Subscribe to quote ticks for an instrument.
        """
        self._subscribed_instruments.add(instrument_id)
        msg = f"Subscribed to quote ticks: {instrument_id}"
        self._log.debug(msg)

        if not self._running:
            self._running = True
            self._polling_task = asyncio.create_task(self._poll_order_books())

    async def _unsubscribe_quote_ticks(self, instrument_id: InstrumentId) -> None:
        """
        Unsubscribe from quote ticks.
        """
        self._subscribed_instruments.discard(instrument_id)
        msg = f"Unsubscribed from quote ticks: {instrument_id}"
        self._log.debug(msg)

        if not self._subscribed_instruments:
            self._running = False

    async def _poll_order_books(self) -> None:
        """
        Poll SX.bet order books for subscribed instruments.
        """
        self._log.info("Starting SX.bet order-book polling loop")

        while self._running:
            try:
                await self._poll_order_books_once()
                await asyncio.sleep(self._polling_interval)

            except asyncio.CancelledError:
                break
            except (RuntimeError, ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
                msg = f"Error in SX.bet order-book polling: {e}"
                self._log.error(msg)
                await asyncio.sleep(self._polling_interval)

        self._log.info("Stopped SX.bet order-book polling loop")

    async def _poll_order_books_once(self) -> None:
        market_hashes = self._subscribed_market_hashes()
        if not market_hashes:
            return

        cycle_started_at = time.perf_counter()
        results = await self._fetch_order_book_results(market_hashes)
        quote_count = 0
        order_count = 0
        empty_count = 0
        one_sided_count = 0
        two_sided_count = 0
        max_latency = 0.0
        for published, orders, has_outcome_one, has_outcome_two, elapsed in results:
            quote_count += published
            order_count += orders
            max_latency = max(max_latency, elapsed)
            if orders == 0:
                empty_count += 1
            elif has_outcome_one and has_outcome_two:
                two_sided_count += 1
            elif has_outcome_one or has_outcome_two:
                one_sided_count += 1

        self._log_poll_summary(
            market_count=len(market_hashes),
            order_count=order_count,
            quote_count=quote_count,
            empty_count=empty_count,
            one_sided_count=one_sided_count,
            two_sided_count=two_sided_count,
            max_latency=max_latency,
            cycle_elapsed=time.perf_counter() - cycle_started_at,
        )

    def _subscribed_market_hashes(self) -> set[str]:
        market_hashes: set[str] = set()
        for instrument_id in list(self._subscribed_instruments):
            instrument = self._instrument_provider.find(instrument_id)
            if isinstance(instrument, CryptoBettingInstrument):
                market_hashes.add(instrument.event_id)
        return market_hashes

    async def _fetch_order_book_results(
        self,
        market_hashes: set[str],
    ) -> list[tuple[int, int, bool, bool, float]]:
        semaphore = asyncio.Semaphore(max(1, self._order_book_concurrency))

        async def _fetch(market_hash: str) -> tuple[int, int, bool, bool, float]:
            async with semaphore:
                return await self._fetch_and_publish_quote_stats(market_hash)

        return await asyncio.gather(*[_fetch(market_hash) for market_hash in sorted(market_hashes)])

    def _send_all_instruments_to_data_engine(self) -> None:
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    def _auto_subscribe_loaded_instruments(self) -> int:
        if not self._config.auto_subscribe_quote_ticks:
            return 0

        loaded_instruments = [
            instrument
            for instrument in self._instrument_provider.get_all().values()
            if isinstance(instrument, CryptoBettingInstrument)
        ]
        loaded_instruments.sort(key=lambda instrument: str(instrument.id))
        limit = self._config.quote_subscription_limit
        selected_instruments = (
            loaded_instruments[:limit] if limit is not None else loaded_instruments
        )
        for instrument in selected_instruments:
            self._subscribed_instruments.add(instrument.id)

        selected_count = len(selected_instruments)
        if selected_count == 0:
            self._log.warning("SX.bet auto-subscription enabled but no instruments were loaded")
            return 0

        self._log.info(
            f"Auto-subscribed {selected_count} of {len(loaded_instruments)} loaded "
            "SX.bet instruments for order-book polling",
        )

        if not self._running:
            self._running = True
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._poll_order_books())
        return selected_count

    def _log_poll_summary(
        self,
        *,
        market_count: int,
        order_count: int,
        quote_count: int,
        empty_count: int,
        one_sided_count: int,
        two_sided_count: int,
        max_latency: float,
        cycle_elapsed: float,
    ) -> None:
        now = time.monotonic()
        if now - self._last_poll_summary_at < self._poll_summary_interval:
            return
        self._last_poll_summary_at = now
        self._log.info(
            "SX.bet order-book poll cycle: "
            f"markets={market_count} orders={order_count} quotes={quote_count} "
            f"empty_markets={empty_count} one_sided_markets={one_sided_count} "
            f"two_sided_markets={two_sided_count} "
            f"subscribed_instruments={len(self._subscribed_instruments)} "
            f"concurrency={self._order_book_concurrency} "
            f"max_latency={max_latency:.2f}s cycle_elapsed={cycle_elapsed:.2f}s",
        )

    async def _fetch_and_publish_best_odds(self, market_hashes: set[str]) -> None:
        try:
            payload = await self._http_client.get_best_odds(
                market_hashes=sorted(market_hashes),
                base_token=SXBET_TOKENS["USDC"],
                log_api_error=False,
            )
            best_odds = payload.get("data", {}).get("bestOdds", [])
            best_odds_by_hash = {
                entry["marketHash"]: entry
                for entry in best_odds
                if isinstance(entry, dict) and isinstance(entry.get("marketHash"), str)
            }

            for market_hash in market_hashes:
                best_odds_entry = best_odds_by_hash.get(market_hash)
                if best_odds_entry is None:
                    continue

                instruments = self._instrument_provider.find_by_market_hash(market_hash)
                for instrument in instruments:
                    if instrument.id not in self._subscribed_instruments:
                        continue

                    quote = self._build_best_odds_quote(instrument, best_odds_entry)
                    if quote is not None:
                        self._handle_data(quote)
        except (ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
            self._log.warning(f"Failed to fetch SX.bet best odds: {e}")

    def _build_best_odds_quote(
        self,
        instrument: CryptoBettingInstrument,
        best_odds_entry: dict[str, object],
    ) -> QuoteTick | None:
        key = "outcomeOne" if self._instrument_is_outcome_one(instrument) else "outcomeTwo"
        outcome_payload = best_odds_entry.get(key)
        if not isinstance(outcome_payload, dict):
            return None

        percentage_odds = outcome_payload.get("percentageOdds")
        if percentage_odds in (None, ""):
            return None

        best_bid = percentage_to_decimal_odds(int(str(percentage_odds)))
        if best_bid <= 0:
            return None

        return QuoteTick(
            instrument_id=instrument.id,
            bid_price=Price(best_bid, precision=2),
            ask_price=Price(0, precision=2),
            bid_size=Quantity.from_int(100),
            ask_size=Quantity.zero(),
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )

    async def _fetch_and_publish_quotes(self, market_hash: str) -> tuple[int, int]:
        """
        Fetch and publish quotes for a market.
        """
        (
            published,
            orders,
            _has_outcome_one,
            _has_outcome_two,
            _elapsed,
        ) = await self._fetch_and_publish_quote_stats(market_hash)
        return published, orders

    async def _fetch_and_publish_quote_stats(
        self,
        market_hash: str,
    ) -> tuple[int, int, bool, bool, float]:
        """
        Fetch and publish quotes for a market with liquidity statistics.
        """
        started_at = time.perf_counter()
        try:
            order_book = await self._http_client.get_order_book(market_hash)
            orders = order_book.get("data", {}).get("orders", [])
            has_outcome_one, has_outcome_two = self._market_order_sides(orders)

            # Find instruments for this market
            instruments = self._instrument_provider.find_by_market_hash(market_hash)

            published = 0
            for instrument in instruments:
                if instrument.id not in self._subscribed_instruments:
                    continue

                is_outcome_one = self._instrument_is_outcome_one(instrument)
                best_bid, best_ask = self._best_bid_ask(orders, is_outcome_one)

                if best_bid <= 0 and best_ask <= 0:
                    continue

                if best_bid > 0 and best_ask > 0 and not self._has_valid_spread(best_bid, best_ask):
                    self._log.warning(
                        f"Skipping locked/crossed SX.bet quote for {instrument.id}: "
                        f"bid={best_bid}, ask={best_ask}",
                    )
                    continue

                quote = QuoteTick(
                    instrument_id=instrument.id,
                    bid_price=Price(best_bid, precision=2),
                    ask_price=Price(best_ask, precision=2),
                    bid_size=Quantity.from_int(100) if best_bid > 0 else Quantity.zero(),
                    ask_size=Quantity.from_int(100) if best_ask > 0 else Quantity.zero(),
                    ts_event=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                )
                self._handle_data(quote)
                published += 1

            if published > 0:
                self._log.debug(
                    f"SX.bet quote publish market={market_hash} orders={len(orders)} "
                    f"quotes={published} elapsed={time.perf_counter() - started_at:.2f}s",
                )
            return (
                published,
                len(orders),
                has_outcome_one,
                has_outcome_two,
                time.perf_counter() - started_at,
            )

        except (ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
            msg = f"Failed to fetch quotes for {market_hash}: {e}"
            self._log.warning(msg)
            return 0, 0, False, False, time.perf_counter() - started_at

    @staticmethod
    def _instrument_is_outcome_one(instrument: CryptoBettingInstrument) -> bool:
        info = getattr(instrument, "info", None)
        if isinstance(info, dict) and "outcome_one" in info:
            return bool(info["outcome_one"])

        outcome = Outcome.from_string(instrument.outcome)
        if outcome in {Outcome.HOME, Outcome.OVER, Outcome.YES}:
            return True
        if outcome in {Outcome.AWAY, Outcome.UNDER, Outcome.NO}:
            return False

        params = instrument.params or ""
        if not isinstance(params, str):
            params = str(params)
        return "outcome_one=True" in params

    @staticmethod
    def _best_bid_ask(orders: list[dict], is_outcome_one: bool) -> tuple[float, float]:
        best_bid = 0.0

        for order in orders:
            percentage = int(order.get("percentageOdds", 0))
            if percentage <= 0:
                continue
            odds = percentage_to_decimal_odds(percentage)
            if order.get("isMakerBettingOutcomeOne") == is_outcome_one:
                best_bid = max(best_bid, odds)

        return best_bid, 0.0

    @staticmethod
    def _market_order_sides(orders: list[dict]) -> tuple[bool, bool]:
        has_outcome_one = False
        has_outcome_two = False
        for order in orders:
            try:
                percentage = int(order.get("percentageOdds", 0))
            except (TypeError, ValueError):
                continue
            if percentage <= 0:
                continue
            if order.get("isMakerBettingOutcomeOne") is True:
                has_outcome_one = True
            elif order.get("isMakerBettingOutcomeOne") is False:
                has_outcome_two = True
        return has_outcome_one, has_outcome_two

    @staticmethod
    def _has_valid_spread(best_bid: float, best_ask: float) -> bool:
        return best_bid > 0 and best_ask > 0 and best_bid < best_ask

    async def _subscribe_instrument(self, instrument_id: InstrumentId) -> None:
        """
        Subscribe to instrument updates.
        """
        self._log.debug(f"Ignoring direct instrument subscription for {instrument_id}")

    async def _subscribe_instruments(self, command: object = None) -> None:
        """
        Subscribe to all instruments.
        """
        self._log.debug(f"Ignoring bulk instrument subscription request: {command!r}")

    async def _request_data(self, data_type: DataType) -> None:
        """
        Request custom data.
        """
        msg = f"Unsupported data type request: {data_type}"
        self._log.warning(msg)

    def subscribed_quote_ticks(self) -> list[InstrumentId]:
        """
        Return subscribed quote tick instrument IDs.
        """
        return list(self._subscribed_instruments)
