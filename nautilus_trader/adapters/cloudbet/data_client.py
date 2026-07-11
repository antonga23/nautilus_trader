# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2023 . All rights reserved.
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

import asyncio
import contextlib
import time
from collections import defaultdict
from typing import Any
from nautilus_trader.adapters.betting.runtime_cache import active_venue_instrument_index_key
from nautilus_trader.adapters.betting.runtime_cache import encode_active_venue_instrument_index
from nautilus_trader.adapters.betting.runtime_cache import encode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import latency_percentiles
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.rust.model import BookType
from nautilus_trader.data.messages import RequestInstrument, RequestInstruments
from nautilus_trader.data.messages import SubscribeInstruments
from nautilus_trader.data.messages import SubscribeQuoteTicks
from nautilus_trader.data.messages import UnsubscribeQuoteTicks
from nautilus_trader.model.data import DataType
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import ClientId, InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.msgbus.bus import MessageBus

from nautilus_trader.adapters.cloudbet.client.core import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.adapters.cloudbet.client.schema import (
    GetEventResponse,
    SelectionId,
    GetLatestOddsResponse,
    SelectionSide,
    SelectionStatus,
)
from nautilus_trader.adapters.cloudbet.config import CloudbetDataClientConfig
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider

from nautilus_trader.adapters.cloudbet.sockets import CloudbetStreamClient
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orderbook import OrderBook

REVALIDATE_EVERY_N_CYCLES = 20


class CloudbetDataClient(LiveMarketDataClient):
    """
    Provides a data client of common methods for Cloudbet adapter.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client : CloudbetClient
        The Cloudbet HttpClient
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    logger : Logger
        The logger for the client.
    market_filter : dict
        The market filter.
    instrument_provider : CloudbetInstrumentProvider, optional
        The instrument provider.
    strict_handling : bool
        If strict handling mode is enabled.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: CloudbetClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        market_filter: dict,
        instrument_provider: CloudbetInstrumentProvider | None = None,
        stream_client: CloudbetStreamClient | None = None,
        config: CloudbetDataClientConfig | None = None,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId(CLOUDBET_VENUE.value),
            venue=CLOUDBET_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider
            or CloudbetInstrumentProvider(
                client=client,
                logger=logger,
            ),
            config=config,
        )
        self._client: CloudbetClient = client
        self._config = config or CloudbetDataClientConfig()
        self._market_filter = market_filter
        self._stream = stream_client or CloudbetStreamClient(
            client=client, logger=logger, message_handler=self.on_market_update
        )

        # TODO: pass from config and and function to update interval
        self._update_instrument_interval: int = 60 * 5  # Once per hour (hardcode)
        self._update_instruments_task: asyncio.Task | None = None
        self._interval_update_requested: bool = False
        self._updates_received: int = 0  # Counter to track the number of updates
        self._update_event = asyncio.Event()  # native asyncio flag to track cycles/events
        self._subscribed_quote_instruments: set[InstrumentId] = set()
        self._requested_quote_instruments: set[InstrumentId] = set()
        self._quote_polling_task: asyncio.Task | None = None
        self._quote_polling_enabled = bool(
            getattr(self._config, "auto_subscribe_quote_ticks", False),
        )
        self._quote_polling_interval = float(
            getattr(self._config, "quote_poll_interval_secs", 10.0),
        )
        self._quote_poll_summary_interval = float(
            getattr(self._config, "quote_poll_summary_interval_secs", 30.0),
        )
        self._quote_poll_concurrency = int(getattr(self._config, "quote_poll_concurrency", 4))
        self._quote_poll_min_concurrency = max(
            1,
            int(getattr(self._config, "quote_poll_min_concurrency", 1)),
        )
        self._quote_poll_max_concurrency = max(
            self._quote_poll_min_concurrency,
            int(getattr(self._config, "quote_poll_max_concurrency", 16)),
        )
        self._quote_poll_target_cycle_secs = max(
            0.1,
            float(getattr(self._config, "quote_poll_target_cycle_secs", 5.0)),
        )
        self._quote_poll_adaptive_concurrency = bool(
            getattr(self._config, "quote_poll_adaptive_concurrency", True),
        )
        self._quote_poll_event_batching = bool(
            getattr(self._config, "quote_poll_event_batching", True),
        )
        self._quote_poll_missing_prune_threshold = max(
            1,
            int(getattr(self._config, "quote_poll_missing_prune_threshold", 3)),
        )
        self._quote_poll_missing_counts: dict[InstrumentId, int] = defaultdict(int)
        self._quote_poll_absent_market_cycles: dict[InstrumentId, int] = {}
        self._quote_poll_concurrency = min(
            max(self._quote_poll_min_concurrency, self._quote_poll_concurrency),
            self._quote_poll_max_concurrency,
        )
        self._next_quote_poll_sleep_secs = self._quote_polling_interval
        self._last_quote_poll_summary_at = 0.0
        self._quote_polling_running = False
        self._quote_poll_cycle_id = 0

        # Hot caches
        self.subscribed_orderbooks: dict[InstrumentId, OrderBook] = {}
        self.subscribed_selection_ids: set[SelectionId] = set()
        self.subscribed_event_ids: dict[InstrumentId, int] = {}
        self.subscribed_market_names: dict[InstrumentId, str] = {}

        # Register Cloudbet Data handlers
        # self._data_handler = {
        #     "account": self._handle_account_data,
        #     "orderbook": self._subscribe_order_book,
        #     "orderbook_deltas": self._subscribe_order_book_deltas,
        #      "events": self._subscribe_events, #TODO: add subscriptions for Events, Fixtures, Competitions
        # }

    @property
    def instrument_provider(self) -> CloudbetInstrumentProvider:
        return self._instrument_provider

    def get_updates_received(self) -> int:
        """Get the number of updates received."""
        return self._updates_received

    def get_update_event(self):
        return self._update_event

    async def _connect(self):
        self._log.info("Initialising instruments...")
        await self._client.connect()
        # load all instruments asynchronously or load instruments based on config and filters (Default: None)
        await self._instrument_provider.initialize()
        # publish instruments to data engine => Data engine will handle propagating to relevant actors/strategies
        self._send_all_instruments_to_data_engine()
        self._log.info(
            f"Successfully sent {self._instrument_provider.count} instruments to the Data engine.",
            LogColor.GREEN,
        )
        self._auto_subscribe_loaded_instruments()
        self._update_instruments_task = self.create_task(self._update_instruments())

    async def _disconnect(self) -> None:
        if not self.is_connected:
            self._log.error("Cannot disconnect a disconnected data client. Trying connecting first")
            return
        self._log.info("Disconnecting Data Client...")
        await self._reset()
        await self._client.disconnect()

    async def _reset(self) -> None:
        # clear "hot" caches
        self.subscribed_selection_ids = set()
        self.subscribed_orderbooks = {}
        self.subscribed_event_ids: dict[InstrumentId, int] = {}
        self.subscribed_market_names: dict[InstrumentId, str] = {}
        if self._update_instruments_task is not None:
            self._update_instruments_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_instruments_task
        self._update_instruments_task = None
        self._quote_polling_running = False
        self._subscribed_quote_instruments.clear()
        self._requested_quote_instruments.clear()
        self._quote_poll_missing_counts.clear()
        if self._quote_polling_task is not None:
            self._quote_polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._quote_polling_task
        self._quote_polling_task = None
        # TODO: create and then remove data_client specific cache
        # await self._remove_all_instruments_from_data_engine()

    def _dispose(self) -> None:
        if self.is_connected:
            self._log.error("Cannot dispose a connected data client.")
            return

    # -- SUBSCRIPTIONS --------------------------------------------------------------------------------
    async def _subscribe(self, data_type: DataType) -> None:
        # general method for subscribing to data and passing it to the data engine and cache
        # e.g. use it to pass adapter specific data like CyrptoBettingInstrument
        # self._data_handler[data_handler_key](param)
        pass  # Do nothing further

    async def _subscribe_instrument(self, instrument_id: InstrumentId) -> None:
        # Only subscribe to instruments that are in the instrument provider
        instrument: CryptoBettingInstrument = self._instrument_provider.find(instrument_id)
        if instrument is None:
            self._log.debug(f"Cannot find instrument for {instrument_id}.")
            return
        instrument_event_id = int(instrument_id.symbol.value.split("|")[0])
        instrument_market_name = (instrument_id.symbol.value.split("|")[1],)
        instrument_outcome = (instrument_id.symbol.value.split("|")[2],)
        instrument_params = instrument_id.symbol.value.split("|")[3]
        instrument_selection_id = SelectionId(
            event_id=instrument_event_id,
            market_name=instrument_market_name,
            outcome=instrument_outcome,
            params=instrument_params,
        )
        # Update subscription
        self._add_subscription_instrument(instrument_id)
        # Convenience data structures to make querying the exchange simpler
        self.subscribed_market_names.update({instrument_id: instrument_market_name})
        self.subscribed_event_ids.update({instrument_id: instrument_event_id})
        self.subscribed_selection_ids.update({instrument_id: instrument_selection_id})
        self._log.debug(f"Subscribed to ID: {instrument.id}. Sending to Data engine...")

    async def _subscribe_instruments(self, command: SubscribeInstruments) -> None:
        instruments: list[Instrument] = self._instrument_provider.list_all()
        if len(instruments) == 0:
            self._log.debug("No Cloudbet instruments loaded; initializing provider")
            await self._instrument_provider.initialize()
            instruments = self._instrument_provider.list_all()
        if len(instruments) == 0:
            self._log.debug("No instruments to subscribe to")
            return
        for instrument in instruments:
            instrument_event_id = int(instrument.id.symbol.value.split("|")[0])
            instrument_market_name = (instrument.id.symbol.value.split("|")[1],)
            instrument_outcome = (instrument.id.symbol.value.split("|")[2],)
            instrument_params = instrument.id.symbol.value.split("|")[3]
            instrument_selection_id = SelectionId(
                event_id=instrument_event_id,
                market_name=instrument_market_name,
                outcome=instrument_outcome,
                params=instrument_params,
            )
            # Update subscription
            self._add_subscription_instrument(instrument.id)
            # Convenience data structures to make querying the exchange simpler
            self.subscribed_market_names.update({instrument.id: instrument_market_name})
            self.subscribed_event_ids.update({instrument.id: instrument_event_id})
            self.subscribed_selection_ids.update({instrument.id: instrument_selection_id})
            self._log.debug(f"Subscribed to ID: {instrument.id}. Sending to Data engine...")
        self._send_all_instruments_to_data_engine(instruments=instruments)

    async def subscribe_order_book_snapshots(
        self,
        instrument_id: InstrumentId,
        book_type: BookType,
        # generally BookType.L1_MBP or BookType.L3_MBO since cloudbet only supports top-level orderbook
        depth: int | None = None,
        kwargs: dict[str, Any] | None = None,  # generally update_speed
    ) -> None:
        # if self._stream is None:
        #     self._log.error("Cannot subscribe to order book snapshots: no stream client.")
        #     return
        # # TODO: implement sending a susbcription message for an OrderBook to the websocket server
        if kwargs.get("update_speed") is not None:
            update_speed = kwargs.get("update_speed")  # default 0 ms for futures.
            valid_speeds = [60, 300, 600, 1800]
            if update_speed not in valid_speeds:
                self._log.error(
                    "Cannot subscribe to order book:"
                    f"invalid `update_speed`, was {update_speed}. "
                    f"Valid update speeds are {valid_speeds} seconds.",
                )
                return
        elif kwargs.get("update_speed") is None:
            update_speed = None  # default user the class level update speed

            if depth is None:
                depth = 0

        # If this is the first subscription request we're receiving, schedule a
        # subscription after a short delay to allow other strategies to send
        # their subscriptions (every change triggers a full snapshot).
        if instrument_id not in self.subscribed_orderbooks:
            self._log.debug(
                f"Subscribing to {instrument_id} [Instrument: {instrument_id.symbol}] <OrderBook> data.",
            )
            orderbook: OrderBook = OrderBook(instrument_id, book_type)
            # update hot cache
            self.subscribed_orderbooks[instrument_id] = orderbook
            # TODO: implement send subscription message via websocket stream client
            self._stream.subscribe_orderbook(orderbook)
            # send Orderbook to DataEngine.process endpoint
            self._handle_data(orderbook)
        # check if orderbook is already subscribed to
        if instrument_id in self.subscribed_orderbooks:
            self._log.warning(
                f"Already subscribed to instrument id: {instrument_id} "
                f"[Instrument: {instrument_id.symbol}] <OrderBook> data.",
            )
            return

    async def _subscribe_order_book_deltas(
        self,
        instrument_id: InstrumentId,
        book_type: BookType,
        depth: int | None = None,
        kwargs: dict | None = None,
    ) -> None:
        raise NotImplementedError(  # pragma: no cover
            "Cannot subscribe to Orderbook Delta for  Cloudbet",  # pragma: no cover
        )

    async def _subscribe_instrument_status_updates(self, instrument_id: InstrumentId) -> None:
        raise NotImplementedError(  # pragma: no cover
            "Cannot subscribe to Orderbook Delta for  Cloudbet",  # pragma: no cover
        )

    async def _subscribe_instrument_close(self, instrument_id: InstrumentId) -> None:
        raise NotImplementedError(  # pragma: no cover
            "Cannot subscribe to Orderbook Delta for  Cloudbet",  # pragma: no cover
        )

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        instrument_id = command.instrument_id
        self._subscribed_quote_instruments.add(instrument_id)
        self._requested_quote_instruments.add(instrument_id)
        self._quote_poll_missing_counts.pop(instrument_id, None)
        self._log.debug(f"Subscribed to quote ticks: {instrument_id}")
        self._start_quote_polling()

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        instrument_id = command.instrument_id
        self._subscribed_quote_instruments.discard(instrument_id)
        self._requested_quote_instruments.discard(instrument_id)
        self._quote_poll_missing_counts.pop(instrument_id, None)
        self._log.debug(f"Unsubscribed from quote ticks: {instrument_id}")
        if not self._subscribed_quote_instruments:
            self._quote_polling_running = False

    async def _unsubscribe_instrument(self, instrument_id: InstrumentId) -> None:
        if instrument_id in self.subscribed_instruments():
            self._remove_subscription_instrument(instrument_id)
            self.subscribed_market_names.pop(instrument_id, None)
            self.subscribed_event_ids.pop(instrument_id, None)
            instrument_event_id = int(instrument_id.symbol.value.split("|")[0])
            instrument_market_name = (instrument_id.symbol.value.split("|")[1],)
            instrument_outcome = (instrument_id.symbol.value.split("|")[2],)
            instrument_params = instrument_id.symbol.value.split("|")[3]
            instrument_selection_id = SelectionId(
                event_id=instrument_event_id,
                market_name=instrument_market_name,
                outcome=instrument_outcome,
                params=instrument_params,
            )
            if instrument_selection_id in self.subscribed_selection_ids:
                self.subscribed_selection_ids.remove(instrument_selection_id)
            self._log.info(f"Unsubscribed from {instrument_id}")
        else:
            self._log.debug(
                f"Cannot unsubscribe from {instrument_id}. No subscription exists.", LogColor.YELLOW
            )

    async def _unsubscribe_instruments(self, instrument_id: InstrumentId) -> None:
        instruments: list[Instrument] = self._instrument_provider.list_all()
        if len(instruments) == 0:
            self._log.debug("No instruments to unsubscribe to")
            return
        for instrument in instruments:
            await self._unsubscribe_instrument(instrument.id)

    async def _unsubscribe_order_book_snapshots(self, instrument_id: InstrumentId) -> None:
        raise NotImplementedError(  # pragma: no cover
            f"Cannot unsubscribe to Orderbook for instrument id: {instrument_id} for  Cloudbet",  # pragma: no cover
        )

    async def _unsubscribe_order_book_deltas(self, instrument_id: InstrumentId) -> None:
        raise NotImplementedError(  # pragma: no cover
            "Cannot subscribe to Orderbook Delta for  Cloudbet",  # pragma: no cover
        )

    def _send_all_instruments_to_data_engine(self, **kwargs) -> None:
        if kwargs.get("instruments") is not None:
            instruments: list[Instrument | CryptoBettingInstrument] = kwargs.get("instruments")
            for instrument in instruments:
                self._handle_data(instrument)
                self._log.debug(f"Sending {instrument.id} to Data engine...", LogColor.GREEN)
        else:
            self._log.debug(
                f"Loading {self._instrument_provider.count} instruments from provider into cache, "
            )
            for instrument in self._instrument_provider.get_all().values():
                self._handle_data(instrument)

        self._log.debug(
            f"DataEngine has {len(self._cache.instruments(CLOUDBET_VENUE))} Cloudbet instruments",
        )
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)

    def _auto_subscribe_loaded_instruments(self) -> int:
        if not self._quote_polling_enabled:
            return 0

        loaded_instruments = [
            instrument
            for instrument in self._instrument_provider.list_all()
            if isinstance(instrument, CryptoBettingInstrument)
        ]
        loaded_instruments.sort(key=self._quote_subscription_priority_key)
        limit = getattr(self._config, "quote_subscription_limit", None)
        selected_instruments = (
            loaded_instruments[:limit] if limit is not None else loaded_instruments
        )
        for instrument in selected_instruments:
            self._subscribed_quote_instruments.add(instrument.id)

        selected_count = len(selected_instruments)
        if selected_count == 0:
            self._log.warning("Cloudbet auto-subscription enabled but no instruments were loaded")
            return 0

        self._log.info(
            f"Auto-subscribed {selected_count} of {len(loaded_instruments)} loaded "
            "Cloudbet instruments for quote polling",
        )
        self._start_quote_polling()
        return selected_count

    @staticmethod
    def _quote_subscription_priority_key(
        instrument: CryptoBettingInstrument,
    ) -> tuple[int, int, float, str]:
        enabled_rank = 0 if bool(getattr(instrument, "enabled", True)) else 1
        status = str(getattr(instrument, "trading_status", "") or "").strip().upper()
        inactive_rank = 1 if status in {"CANCELLED", "CLOSED", "RESULTED", "SUSPENDED"} else 0
        try:
            max_size = float(getattr(instrument, "max_size", 0) or 0)
        except (TypeError, ValueError):
            max_size = 0.0
        return (enabled_rank, inactive_rank, -max_size, str(instrument.id))

    def _start_quote_polling(self) -> None:
        if not self._subscribed_quote_instruments:
            return
        self._quote_polling_running = True
        if self._quote_polling_task is None or self._quote_polling_task.done():
            self._quote_polling_task = asyncio.create_task(self._poll_quote_ticks())

    async def _poll_quote_ticks(self) -> None:
        self._log.info("Starting Cloudbet quote polling loop")
        while self._quote_polling_running:
            try:
                await self._poll_quote_ticks_once()
                await asyncio.sleep(self._next_quote_poll_sleep_secs)
            except asyncio.CancelledError:
                break
            except (RuntimeError, ValueError, TypeError, KeyError) as e:
                self._log.warning(f"Error in Cloudbet quote polling: {e}")
                await asyncio.sleep(self._quote_polling_interval)
        self._log.info("Stopped Cloudbet quote polling loop")

    async def _poll_quote_ticks_once(self) -> tuple[int, int]:
        instrument_ids = sorted(self._subscribed_quote_instruments, key=str)
        if not instrument_ids:
            return (0, 0)

        started_at = time.perf_counter()
        if self._quote_poll_event_batching:
            (
                results,
                request_count,
                event_request_count,
                line_request_count,
            ) = await self._fetch_quote_ticks_event_batched(instrument_ids)
        else:
            results, line_request_count = await self._fetch_quote_ticks_individual(instrument_ids)
            event_request_count = 0
            request_count = line_request_count
        published = 0
        fetch_latencies_secs: list[float] = []
        max_fetch_latency_secs = 0.0
        failure_count = 0
        rate_limit_count = 0
        last_error: str | None = None
        pruned_subscription_count = 0
        pruned_instrument_ids: set[InstrumentId] = set()
        for instrument_id, quote, error in results:
            if error is not None:
                failure_count += 1
                last_error = error
                if "429" in error or "code='429'" in error or "code=429" in error:
                    rate_limit_count += 1
                continue
            if quote is None:
                if self._record_missing_quote_subscription(instrument_id):
                    pruned_subscription_count += 1
                    pruned_instrument_ids.add(instrument_id)
                continue
            self._quote_poll_missing_counts.pop(instrument_id, None)
            self._handle_data(quote)
            published += 1
            max_fetch_latency_secs = max(
                max_fetch_latency_secs,
                max(0.0, (quote.ts_init - quote.ts_event) / 1_000_000_000),
            )
            fetch_latencies_secs.append(max(0.0, (quote.ts_init - quote.ts_event) / 1_000_000_000))
        refilled_subscription_count = 0
        if pruned_subscription_count > 0:
            refilled_subscription_count = self._refill_quote_subscriptions(
                exclude=pruned_instrument_ids,
                max_added=pruned_subscription_count,
            )

        cycle_elapsed = time.perf_counter() - started_at
        self._adapt_quote_poll_schedule(
            instrument_count=len(instrument_ids),
            cycle_elapsed=cycle_elapsed,
            rate_limit_count=rate_limit_count,
            failure_count=failure_count,
        )
        self._record_quote_poll_stats(
            instrument_count=len(instrument_ids),
            quote_count=published,
            request_count=request_count,
            event_request_count=event_request_count,
            line_request_count=line_request_count,
            pruned_subscription_count=pruned_subscription_count,
            refilled_subscription_count=refilled_subscription_count,
            cycle_elapsed=cycle_elapsed,
            max_fetch_latency_secs=max_fetch_latency_secs,
            fetch_latency_percentiles=latency_percentiles(fetch_latencies_secs),
            failure_count=failure_count,
            rate_limit_count=rate_limit_count,
            backoff_secs=float(rate_limit_count),
            last_error=last_error,
        )
        self._log_quote_poll_summary(
            instrument_count=len(instrument_ids),
            quote_count=published,
            request_count=request_count,
            cycle_elapsed=cycle_elapsed,
        )
        return published, len(instrument_ids)

    def _refill_quote_subscriptions(
        self,
        *,
        exclude: set[InstrumentId],
        max_added: int,
    ) -> int:
        if max_added <= 0:
            return 0
        limit = getattr(self._config, "quote_subscription_limit", None)
        if limit is not None:
            slots_available = max(0, int(limit) - len(self._subscribed_quote_instruments))
            max_added = min(max_added, slots_available)
        if max_added <= 0:
            return 0
        candidates = [
            instrument
            for instrument in self._instrument_provider.list_all()
            if isinstance(instrument, CryptoBettingInstrument)
            and instrument.id not in self._subscribed_quote_instruments
            and instrument.id not in exclude
        ]
        candidates.sort(key=self._quote_subscription_priority_key)
        added = 0
        for instrument in candidates:
            self._subscribed_quote_instruments.add(instrument.id)
            self._quote_poll_missing_counts.pop(instrument.id, None)
            added += 1
            if added >= max_added:
                break
        if added > 0:
            self._quote_polling_running = True
            self._log.info(
                "Refilled Cloudbet quote subscriptions after pruning: "
                f"added={added} subscribed={len(self._subscribed_quote_instruments)}",
            )
        return added

    async def _fetch_quote_ticks_individual(
        self,
        instrument_ids: list[InstrumentId],
    ) -> tuple[list[tuple[InstrumentId, QuoteTick | None, str | None]], int]:
        semaphore = asyncio.Semaphore(max(1, self._quote_poll_concurrency))

        async def _fetch(
            instrument_id: InstrumentId,
        ) -> tuple[InstrumentId, QuoteTick | None, str | None]:
            async with semaphore:
                try:
                    return instrument_id, await self._fetch_quote_tick(instrument_id), None
                except CloudbetAPIError as exc:
                    self._log.warning(f"Cloudbet quote poll failed for {instrument_id}: {exc}")
                    return instrument_id, None, str(exc)
                except (ValueError, TypeError, KeyError) as exc:
                    self._log.warning(f"Cloudbet quote poll failed for {instrument_id}: {exc}")
                    return instrument_id, None, str(exc)

        results = await asyncio.gather(*[_fetch(instrument_id) for instrument_id in instrument_ids])
        return list(results), len(instrument_ids)

    async def _fetch_quote_ticks_event_batched(
        self,
        instrument_ids: list[InstrumentId],
    ) -> tuple[list[tuple[InstrumentId, QuoteTick | None, str | None]], int, int, int]:
        event_groups, line_fallback_ids = self._group_quote_instruments_by_event(instrument_ids)
        semaphore = asyncio.Semaphore(max(1, self._quote_poll_concurrency))
        event_results = await asyncio.gather(
            *[
                self._fetch_quote_ticks_for_event_group(
                    event_id,
                    group_instrument_ids,
                    semaphore,
                )
                for event_id, group_instrument_ids in event_groups.items()
            ],
        )
        results: list[tuple[QuoteTick | None, str | None]] = []
        line_request_count = 0
        for group_results, fallback_count in event_results:
            results.extend(group_results)
            line_request_count += fallback_count

        if line_fallback_ids:
            fallback_results, fallback_count = await self._fetch_quote_ticks_individual(
                line_fallback_ids,
            )
            results.extend(fallback_results)
            line_request_count += fallback_count

        event_request_count = len(event_groups)
        request_count = event_request_count + line_request_count
        return results, request_count, event_request_count, line_request_count

    def _group_quote_instruments_by_event(
        self,
        instrument_ids: list[InstrumentId],
    ) -> tuple[dict[int, list[InstrumentId]], list[InstrumentId]]:
        event_groups: dict[int, list[InstrumentId]] = defaultdict(list)
        line_fallback_ids: list[InstrumentId] = []
        for instrument_id in instrument_ids:
            instrument = self._instrument_provider.find(instrument_id)
            if not isinstance(instrument, CryptoBettingInstrument):
                line_fallback_ids.append(instrument_id)
                continue
            try:
                event_id = int(str(instrument.event_id))
            except (TypeError, ValueError):
                line_fallback_ids.append(instrument_id)
                continue
            event_groups[event_id].append(instrument_id)
        return event_groups, line_fallback_ids

    async def _fetch_quote_ticks_for_event_group(
        self,
        event_id: int,
        group_instrument_ids: list[InstrumentId],
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[tuple[InstrumentId, QuoteTick | None, str | None]], int]:
        async with semaphore:
            request_started_ns = self._clock.timestamp_ns()
            try:
                event = await self._client.get_event(event_id)
            except CloudbetAPIError as exc:
                self._log.warning(f"Cloudbet event quote poll failed for event {event_id}: {exc}")
                return await self._fetch_quote_ticks_individual(group_instrument_ids)
            except (ValueError, TypeError, KeyError) as exc:
                self._log.warning(f"Cloudbet event quote poll failed for event {event_id}: {exc}")
                return await self._fetch_quote_ticks_individual(group_instrument_ids)
            response_received_ns = self._clock.timestamp_ns()

        group_results: list[tuple[InstrumentId, QuoteTick | None, str | None]] = []
        unresolved_ids: list[InstrumentId] = []
        for instrument_id in group_instrument_ids:
            instrument = self._instrument_provider.find(instrument_id)
            if not isinstance(instrument, CryptoBettingInstrument):
                group_results.append((instrument_id, None, None))
                continue
            quote = self._quote_tick_from_event(
                instrument=instrument,
                event=event,
                request_started_ns=request_started_ns,
                response_received_ns=response_received_ns,
            )
            if quote is not None:
                self._quote_poll_absent_market_cycles.pop(instrument_id, None)
                group_results.append((instrument_id, quote, None))
                continue
            if event.markets.get(str(instrument.market_name)) is not None:
                self._quote_poll_absent_market_cycles.pop(instrument_id, None)
                unresolved_ids.append(instrument_id)
                continue
            confirmed_at = self._quote_poll_absent_market_cycles.get(instrument_id)
            if (
                confirmed_at is not None
                and self._quote_poll_cycle_id - confirmed_at < REVALIDATE_EVERY_N_CYCLES
            ):
                group_results.append((instrument_id, None, None))
                continue
            unresolved_ids.append(instrument_id)

        fallback_count = 0
        if unresolved_ids:
            fallback_results, fallback_count = await self._fetch_quote_ticks_individual(
                unresolved_ids,
            )
            group_results.extend(fallback_results)
            for instrument_id, quote, _ in fallback_results:
                if quote is not None:
                    self._quote_poll_absent_market_cycles.pop(instrument_id, None)
                    continue
                instrument = self._instrument_provider.find(instrument_id)
                if (
                    isinstance(instrument, CryptoBettingInstrument)
                    and event.markets.get(str(instrument.market_name)) is None
                ):
                    # The market key is structurally absent from the event payload
                    # AND the per-line endpoint (same market path) found nothing:
                    # skip the redundant per-line request on subsequent cycles.
                    self._quote_poll_absent_market_cycles[instrument_id] = (
                        self._quote_poll_cycle_id
                    )
        return group_results, fallback_count

    def _record_missing_quote_subscription(self, instrument_id: InstrumentId) -> bool:
        if instrument_id not in self._subscribed_quote_instruments:
            return False
        if instrument_id in self._requested_quote_instruments:
            # DataEngine/strategy explicitly requested this instrument (e.g. a cross-venue
            # counterpart). Never prune it on transient missing quotes: the strategy has no
            # re-subscription path, and silently swapping in an unrelated size-ranked
            # instrument would leave the requested leg permanently dark. Hold the miss count
            # at zero so it keeps being polled when the market un-suspends.
            self._quote_poll_missing_counts.pop(instrument_id, None)
            return False
        missing_count = self._quote_poll_missing_counts[instrument_id] + 1
        if missing_count < self._quote_poll_missing_prune_threshold:
            self._quote_poll_missing_counts[instrument_id] = missing_count
            return False
        self._quote_poll_missing_counts.pop(instrument_id, None)
        self._subscribed_quote_instruments.discard(instrument_id)
        self._log.info(
            "Pruned Cloudbet quote subscription after consecutive missing polls: "
            f"instrument_id={instrument_id} misses={missing_count}",
        )
        if not self._subscribed_quote_instruments:
            self._quote_polling_running = False
        return True

    def _adapt_quote_poll_schedule(
        self,
        *,
        instrument_count: int,
        cycle_elapsed: float,
        rate_limit_count: int,
        failure_count: int,
    ) -> None:
        if not self._quote_poll_adaptive_concurrency:
            self._next_quote_poll_sleep_secs = self._quote_polling_interval
            return

        target = self._quote_poll_target_cycle_secs
        if rate_limit_count > 0:
            self._quote_poll_concurrency = max(
                self._quote_poll_min_concurrency,
                self._quote_poll_concurrency // 2,
            )
        elif instrument_count > self._quote_poll_concurrency and cycle_elapsed > target:
            scale = min(2.0, max(1.1, cycle_elapsed / target))
            self._quote_poll_concurrency = min(
                self._quote_poll_max_concurrency,
                max(self._quote_poll_concurrency + 1, int(self._quote_poll_concurrency * scale)),
            )
        elif (
            failure_count == 0
            and instrument_count <= self._quote_poll_concurrency // 2
            and cycle_elapsed < target * 0.35
        ):
            self._quote_poll_concurrency = max(
                self._quote_poll_min_concurrency,
                self._quote_poll_concurrency - 1,
            )

        self._next_quote_poll_sleep_secs = min(
            self._quote_polling_interval,
            max(0.25, target - cycle_elapsed),
        )

    async def _fetch_quote_tick(self, instrument_id: InstrumentId) -> QuoteTick | None:
        instrument = self._instrument_provider.find(instrument_id)
        if not isinstance(instrument, CryptoBettingInstrument):
            return None

        market_url = self._market_url_for_instrument(instrument)
        request_started_ns = self._clock.timestamp_ns()
        odds = await self._client.get_latest_odds(
            event_id=instrument.event_id,
            market_url=market_url,
        )
        response_received_ns = self._clock.timestamp_ns()
        return self._quote_tick_from_odds(
            instrument=instrument,
            odds=odds,
            request_started_ns=request_started_ns,
            response_received_ns=response_received_ns,
        )

    def _quote_tick_from_event(
        self,
        *,
        instrument: CryptoBettingInstrument,
        event: GetEventResponse,
        request_started_ns: int,
        response_received_ns: int,
    ) -> QuoteTick | None:
        market = event.markets.get(str(instrument.market_name))
        if market is None:
            return None

        submarkets = market.submarkets
        preferred_submarket = self._preferred_submarket_key(instrument)
        submarket_items = []
        if preferred_submarket and preferred_submarket in submarkets:
            submarket_items.append((preferred_submarket, submarkets[preferred_submarket]))
        submarket_items.extend(
            (key, value) for key, value in submarkets.items() if key != preferred_submarket
        )
        expected_outcome = self._normalize_cloudbet_selection_field(instrument.outcome)
        expected_params = self._normalize_cloudbet_selection_field(instrument.params)
        for _, submarket in submarket_items:
            for selection in submarket.selections:
                if self._normalize_cloudbet_selection_field(selection.outcome) != expected_outcome:
                    continue
                if self._normalize_cloudbet_selection_field(selection.params) != expected_params:
                    continue
                odds = GetLatestOddsResponse(
                    max_stake=selection.maxStake,
                    min_stake=selection.minStake,
                    price=selection.price,
                    status=self._selection_status(selection.status),
                    outcome=selection.outcome,
                    params=selection.params,
                    probability=selection.probability,
                    side=self._selection_side(selection.side),
                )
                return self._quote_tick_from_odds(
                    instrument=instrument,
                    odds=odds,
                    request_started_ns=request_started_ns,
                    response_received_ns=response_received_ns,
                )
        return None

    def _quote_tick_from_odds(
        self,
        *,
        instrument: CryptoBettingInstrument,
        odds: GetLatestOddsResponse,
        request_started_ns: int,
        response_received_ns: int,
    ) -> QuoteTick | None:
        price = float(odds.price)
        if price <= 0:
            return None

        max_stake = float(odds.max_stake or 0)
        return QuoteTick(
            instrument_id=instrument.id,
            bid_price=Price(0, precision=2),
            ask_price=Price(price, precision=2),
            bid_size=Quantity(0, precision=2),
            ask_size=Quantity(max_stake, precision=2)
            if max_stake > 0
            else Quantity(0, precision=2),
            ts_event=request_started_ns,
            ts_init=response_received_ns,
        )

    @staticmethod
    def _market_url_for_instrument(instrument: CryptoBettingInstrument) -> str:
        return (
            instrument.market_name + "/" + instrument.outcome + "?" + instrument.params
            if instrument.params is not None
            else instrument.market_name + "/" + instrument.outcome
        )

    @staticmethod
    def _preferred_submarket_key(instrument: CryptoBettingInstrument) -> str | None:
        market_type = str(getattr(instrument, "market_type", "") or "")
        market_name = str(getattr(instrument, "market_name", "") or "")
        prefix = f"{market_name}_"
        if market_type.startswith(prefix):
            return market_type[len(prefix) :]
        return None

    @staticmethod
    def _normalize_cloudbet_selection_field(value: object) -> str:
        return str(value or "").strip()

    @staticmethod
    def _selection_status(value: object) -> SelectionStatus:
        if isinstance(value, SelectionStatus):
            return value
        try:
            return SelectionStatus(str(value))
        except ValueError:
            return SelectionStatus.DISABLED

    @staticmethod
    def _selection_side(value: object) -> SelectionSide:
        if isinstance(value, SelectionSide):
            return value
        try:
            return SelectionSide(str(value))
        except ValueError:
            return SelectionSide.UNDEFINED

    def _log_quote_poll_summary(
        self,
        *,
        instrument_count: int,
        quote_count: int,
        request_count: int,
        cycle_elapsed: float,
    ) -> None:
        now = time.monotonic()
        if now - self._last_quote_poll_summary_at < self._quote_poll_summary_interval:
            return
        self._last_quote_poll_summary_at = now
        self._log.info(
            "Cloudbet quote poll cycle: "
            f"instruments={instrument_count} quotes={quote_count} "
            f"requests={request_count} "
            f"concurrency={self._quote_poll_concurrency} "
            f"cycle_elapsed={cycle_elapsed:.2f}s "
            f"next_sleep={self._next_quote_poll_sleep_secs:.2f}s",
        )

    def _record_quote_poll_stats(
        self,
        *,
        instrument_count: int,
        quote_count: int,
        request_count: int,
        event_request_count: int,
        line_request_count: int,
        pruned_subscription_count: int,
        refilled_subscription_count: int,
        cycle_elapsed: float,
        max_fetch_latency_secs: float,
        fetch_latency_percentiles: tuple[float, float, float] = (0.0, 0.0, 0.0),
        failure_count: int = 0,
        rate_limit_count: int = 0,
        backoff_secs: float = 0.0,
        last_error: str | None = None,
    ) -> None:
        self._quote_poll_cycle_id += 1
        backlog_count = max(0, instrument_count - max(1, self._quote_poll_concurrency))
        self._cache.add(
            venue_quote_poll_stats_key(CLOUDBET_VENUE.value),
            encode_venue_quote_poll_stats(
                venue=CLOUDBET_VENUE.value,
                updated_at_ns=self._clock.timestamp_ns(),
                cycle_id=self._quote_poll_cycle_id,
                source="rest_event_poll" if self._quote_poll_event_batching else "rest_line_poll",
                subscribed_instrument_count=len(self._subscribed_quote_instruments),
                market_count=instrument_count,
                quote_count=quote_count,
                request_count=request_count,
                event_request_count=event_request_count,
                line_request_count=line_request_count,
                pruned_subscription_count=pruned_subscription_count,
                refilled_subscription_count=refilled_subscription_count,
                concurrency=self._quote_poll_concurrency,
                backlog_count=backlog_count,
                cycle_elapsed_secs=cycle_elapsed,
                max_fetch_latency_secs=max_fetch_latency_secs,
                poll_interval_secs=self._quote_polling_interval,
                fetch_latency_p50_secs=fetch_latency_percentiles[0],
                fetch_latency_p95_secs=fetch_latency_percentiles[1],
                fetch_latency_p99_secs=fetch_latency_percentiles[2],
                poll_target_cycle_secs=self._quote_poll_target_cycle_secs,
                next_poll_sleep_secs=self._next_quote_poll_sleep_secs,
                min_concurrency=self._quote_poll_min_concurrency,
                max_concurrency=self._quote_poll_max_concurrency,
                adaptive_concurrency=self._quote_poll_adaptive_concurrency,
                quote_event_timestamp_source="request_started",
                quote_init_timestamp_source="response_received",
                failure_count=failure_count,
                rate_limit_count=rate_limit_count,
                backoff_secs=backoff_secs,
                last_error=last_error,
            ),
        )

    # async def _remove_all_instruments_from_data_engine(self) -> None:
    #     # TODO: cleanup instruments from provider, cache and data engine
    #     TODO: add remove instruments method on provider
    #     # self._log.debug(f"Removing {self._instrument_provider.count} instruments from Data Engine and cache, ")
    #     await self._unsubscribe_instruments()
    #     # self._log.debug(
    #     #     f"DataEngine has {len(self._cache.instruments(CLOUDBET_VENUE))} Cloudbet instruments",
    #     # )

    async def _update_instruments(self) -> None:
        try:
            while True:
                # Reset the event at the start of each cycle
                self._update_event.clear()
                self._log.debug(
                    f"Scheduled `update_instruments` to run in "
                    f"{self._update_instrument_interval}s.",
                )
                await asyncio.sleep(self._update_instrument_interval)
                await self._instrument_provider.load_ids_async(self.subscribed_instruments())
                # send to Data Engine for processing => add to cache and propagate to subscriptions
                self._send_all_instruments_to_data_engine()
                # Signal completion of the update cycle
                self._update_event.set()
                # Mark the update cycle as completed and increment the update counter
                self._updates_received += 1
                # Check if an interval update was requested
                if self._interval_update_requested:
                    self._interval_update_requested = False  # Reset the flag
                    self._log.debug("Interval update applied.")
        except asyncio.CancelledError:
            self._log.debug("`update_instruments` task was canceled.")
        except Exception as e:
            self._log.error(f"An error occurred during `update_instruments` task: {e!s}")

    def update_interval(self, new_interval: int):
        """Update the interval at which instruments are updated."""
        # Check if new interval is valid
        if self._update_instruments_task is None:
            self._log.debug(
                " Failed to set new update interval. No update instruments task is running"
            )
            return
        if new_interval and (new_interval <= 0 or new_interval > 3600):
            self._log.error(
                "Update interval must be greater than 0 seconds and less than 3600 seconds."
            )
            raise ValueError("Update interval must be greater than 0 seconds.")
        self._update_instrument_interval = new_interval
        self._interval_update_requested = True  # Signal that an interval update has been requested
        # self._log.debug("Update interval set to {x}. It will be applied after the current interval update".format(x=new_interval))

    # -- STREAMS ----------------------------------------------------------------------------------
    def on_market_update(self, raw: bytes):  # required method
        pass

    # -- REQUESTS ---------------------------------------------------------------------------------

    async def _request_instrument(self, request: RequestInstrument) -> None:
        """Request instrument data for the given instrument ID."""
        instrument_id = request.instrument_id
        self._log.debug(f"RequestID: {request.id} ... Requesting instrument {instrument_id}...")
        instrument: Instrument | CryptoBettingInstrument | None = self._instrument_provider.find(
            instrument_id
        )
        if instrument is not None:
            self._log.debug(
                f"RequestID: {request.id} ... Found instrument {instrument_id} in cache. Fetching latest data...",
            )
            try:
                market_url = (
                    instrument.market_name + "/" + instrument.outcome + "?" + instrument.params
                    if instrument.params is not None
                    else instrument.market_name + "/" + instrument.outcome
                )
                odds: GetLatestOddsResponse = await self._client.get_latest_odds(
                    event_id=instrument.event_id,
                    market_url=market_url,
                )
                instrument.max_size = odds.max_stake
                instrument.min_size = odds.min_stake
                instrument.price = odds.price
                instrument.enabled = odds.status == SelectionStatus.ENABLED
                self._instrument_provider.add(instrument)
                self._handle_instrument(
                    instrument,
                    request.id,
                    request.start,
                    request.end,
                    request.params,
                )
            except Exception as e:
                self._log.error(f"Error fetching instrument data for {instrument_id}. {e}")
                return

        if instrument is None:
            self._log.warning(
                f"Cannot find instrument for {instrument_id}. in the provider. Load instrument first. Returning"
            )
            return

    async def _request_instruments(self, request: RequestInstruments) -> None:
        """Request all instrument data for the given venue."""
        refresh_all = bool((request.params or {}).get("semantic_refresh"))
        if refresh_all:
            before_count = self._instrument_provider.count
            await self._instrument_provider.load_all_async()
            self._log.info(
                "Refreshed Cloudbet instrument catalog: "
                f"before={before_count} after={self._instrument_provider.count}",
            )
        else:
            instruments: list[Instrument | CryptoBettingInstrument] = (
                self._instrument_provider.list_all()
            )
            for instrument in instruments:
                self._log.debug(f"Fetching latest data for {instrument.id}...")
            await self._instrument_provider.load_ids_async(
                [instrument.id for instrument in instruments],
            )
        updated_instruments: list[Instrument] = self._instrument_provider.list_all()
        self._cache.add(
            active_venue_instrument_index_key(str(request.venue)),
            encode_active_venue_instrument_index(
                venue=str(request.venue),
                instrument_ids=[str(instrument.id) for instrument in updated_instruments],
                updated_at_ns=self._clock.timestamp_ns(),
            ),
        )
        self._handle_instruments(
            request.venue,
            updated_instruments,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    # -- DATA HANDLERS ---------------------------------------------------------------------------------

    def _handle_account_data(self, data_type: DataType) -> None:
        raise NotImplementedError(
            f"Cannot handle {data_type.type} (not implemented)."
        )  # Do nothing further

    def _handle_cb_events(self, data_type: DataType) -> None:
        raise NotImplementedError(f"Cannot handle {data_type.type} (not implemented).")

    def _handle_fixtures(self, data_type: DataType) -> None:
        raise NotImplementedError(f"Cannot handle {data_type.type} (not implemented).")

    def _handle_competitions(self, data_type: DataType) -> None:
        raise NotImplementedError(f"Cannot handle {data_type.type} (not implemented).")
