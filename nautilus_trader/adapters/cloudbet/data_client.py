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
from pathlib import Path
from typing import Optional, Union, List, Any

from dotenv import dotenv_values
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.rust.model import BookType
from nautilus_trader.model.data.base import DataType
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue
from nautilus_trader.msgbus.bus import MessageBus

from nautilus_trader.adapters.cloudbet.client.core import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import SelectionId, GetEventResponse, GetLatestOddsResponse, \
    EventStatus, SelectionStatus
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.core.uuid import UUID4

from nautilus_trader.core.correctness import PyCondition

from nautilus_trader.adapters.cloudbet.sockets import CloudbetStreamClient
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.currencies import EUR
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orderbook import OrderBook

# load environment variables from .cloudbet_env file
env_path = str(Path().cwd() / '../.cloudbet_env')
cloudbet_secrets = dotenv_values(env_path)


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
        instrument_provider: Optional[CloudbetInstrumentProvider] = None,
        stream_client: Optional[CloudbetStreamClient] = None,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId(CLOUDBET_VENUE.value),
            venue=CLOUDBET_VENUE,
            instrument_provider=instrument_provider or CloudbetInstrumentProvider(client=client, logger=logger,
                                                                                  filters=market_filter),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
        )
        self._client: CloudbetClient = client
        self._market_filter = market_filter
        self._stream = stream_client or CloudbetStreamClient(
            client=client,
            logger=logger,
            message_handler=self.on_market_update)

        # TODO: pass from config and and function to update interval
        self._update_instrument_interval: int = 60 * 5  # Once per hour (hardcode)
        self._update_instruments_task: Optional[asyncio.Task] = None
        self._interval_update_requested: bool = False
        self._updates_received: int = 0  # Counter to track the number of updates
        self._update_event = asyncio.Event()  # native asyncio flag to track cycles/events

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
        # load all instruments asynchronously or load instruments based on config and filters (Default: None)
        await self._instrument_provider.initialize()
        # publish instruments to data engine => Data engine will handle propagating to relevant actors/strategies
        self._send_all_instruments_to_data_engine()
        self._log.info(f"Successfully sent {self._instrument_provider.count} instruments to the Data engine.",
                       LogColor.GREEN)
        self._update_instruments_task = self.create_task(self._update_instruments())

    async def _disconnect(self) -> None:
        if not self.is_connected:
            self._log.error("Cannot disconnect a disconnected data client. Trying connecting first")
            return
        self._log.info("Disconnecting Data Client...")
        await self._reset()

    async def _reset(self) -> None:
        # clear "hot" caches
        self.subscribed_selection_ids = set()
        self.subscribed_orderbooks = {}
        self.subscribed_event_ids: dict[InstrumentId, int] = {}
        self.subscribed_market_names: dict[InstrumentId, str] = {}
        self._update_instruments_task.cancel()
        self._update_instruments_task = None
        # TODO: create and then remove data_client specific cache
        # await self._remove_all_instruments_from_data_engine()

    async def _dispose(self) -> None:
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
        instrument_market_name = instrument_id.symbol.value.split("|")[1],
        instrument_outcome = instrument_id.symbol.value.split("|")[2],
        instrument_params = instrument_id.symbol.value.split("|")[3]
        instrument_selection_id = SelectionId(
            event_id=instrument_event_id,
            market_name=instrument_market_name,
            outcome=instrument_outcome,
            params=instrument_params
        )
        # Update subscription
        self._add_subscription_instrument(instrument_id)
        # Convenience data structures to make querying the exchange simpler
        self.subscribed_market_names.update({instrument_id: instrument_market_name})
        self.subscribed_event_ids.update({instrument_id: instrument_event_id})
        self.subscribed_selection_ids.update({instrument_id: instrument_selection_id})
        self._log.debug(f"Subscribed to ID: {instrument.id}. Sending to Data engine...")

    async def _subscribe_instruments(self) -> None:
        instruments: list[Instrument] = self._instrument_provider.list_all()
        if len(instruments) == 0:
            self._log.debug("No instruments to subscribe to")
            return
        for instrument in instruments:
            instrument_event_id = int(instrument.id.symbol.value.split("|")[0])
            instrument_market_name = instrument.id.symbol.value.split("|")[1],
            instrument_outcome = instrument.id.symbol.value.split("|")[2],
            instrument_params = instrument.id.symbol.value.split("|")[3]
            instrument_selection_id = SelectionId(
                event_id=instrument_event_id,
                market_name=instrument_market_name,
                outcome=instrument_outcome,
                params=instrument_params
            )
            # Update subscription
            self._add_subscription_instrument(instrument.id)
            # Convenience data structures to make querying the exchange simpler
            self.subscribed_market_names.update({instrument.id: instrument_market_name})
            self.subscribed_event_ids.update({instrument.id: instrument_event_id})
            self.subscribed_selection_ids.update({instrument.id: instrument_selection_id})
            self._log.debug(f"Subscribed to ID: {instrument.id}. Sending to Data engine...")

    async def subscribe_order_book_snapshots(  # noqa (too complex)
        self,
        instrument_id: InstrumentId,
        book_type: BookType,
        # generally BookType.L1_MBP or BookType.L3_MBO since cloudbet only supports top-level orderbook
        depth: Optional[int] = None,
        kwargs: Optional[dict[str, Any]] = None,  # generally update_speed
    ) -> None:
        # if self._stream is None:
        #     self._log.error("Cannot subscribe to order book snapshots: no stream client.")
        #     return
        # # TODO: implement sending a susbcription message for an OrderBook to the websocket server
        if kwargs.get('update_speed') is not None:
            update_speed = kwargs.get('update_speed')  # default 0 ms for futures.
            valid_speeds = [60, 300, 600, 1800]
            if update_speed not in valid_speeds:
                self._log.error(
                    "Cannot subscribe to order book:"
                    f"invalid `update_speed`, was {update_speed}. "
                    f"Valid update speeds are {valid_speeds} seconds.",
                )
                return
        elif kwargs.get('update_speed') is None:
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
        depth: Optional[int] = None,
        kwargs: Optional[dict] = None,
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

    async def _unsubscribe_instrument(self, instrument_id: InstrumentId) -> None:
        if instrument_id in self.subscribed_instruments:
            self._remove_subscription_instrument(instrument_id)
            self.subscribed_market_names.pop(instrument_id, None)
            self.subscribed_event_ids.pop(instrument_id, None)
            instrument_event_id = int(instrument_id.symbol.value.split("|")[0])
            instrument_market_name = instrument_id.symbol.value.split("|")[1],
            instrument_outcome = instrument_id.symbol.value.split("|")[2],
            instrument_params = instrument_id.symbol.value.split("|")[3]
            instrument_selection_id = SelectionId(
                event_id=instrument_event_id,
                market_name=instrument_market_name,
                outcome=instrument_outcome,
                params=instrument_params
            )
            if instrument_selection_id in self.subscribed_selection_ids:
                self.subscribed_selection_ids.remove(instrument_selection_id)
            self._log.info(f"Unsubscribed from {instrument_id}")
        else:
            self._log.debug(f"Cannot unsubscribe from {instrument_id}. No subscription exists.", LogColor.YELLOW)

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
        if kwargs.get('instruments') is not None:
            instruments: List[Union[Instrument, CryptoBettingInstrument]] = kwargs.get('instruments')
            for instrument in instruments:
                self._handle_data(instrument)
                self._log.debug(f"Sending {instrument.id} to Data engine...", LogColor.GREEN)
        else:
            self._log.debug(f"Loading {self._instrument_provider.count} instruments from provider into cache, ")
            for instrument in self._instrument_provider.get_all().values():
                self._handle_data(instrument)

        self._log.debug(
            f"DataEngine has {len(self._cache.instruments(CLOUDBET_VENUE))} Cloudbet instruments",
        )
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)

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
                await self._instrument_provider.load_ids_async(self.subscribed_instruments)
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
            self._log.error(f"An error occurred during `update_instruments` task: {str(e)}")

    def update_interval(self, new_interval: int):
        """Update the interval at which instruments are updated."""
        # Check if new interval is valid
        if self._update_instruments_task is None:
            self._log.debug(" Failed to set new update interval. No update instruments task is running")
            return
        if new_interval and (new_interval <= 0 or new_interval > 3600):
            self._log.error("Update interval must be greater than 0 seconds and less than 3600 seconds.")
            raise ValueError("Update interval must be greater than 0 seconds.")
        self._update_instrument_interval = new_interval
        self._interval_update_requested = True  # Signal that an interval update has been requested
        # self._log.debug("Update interval set to {x}. It will be applied after the current interval update".format(x=new_interval))

    # -- STREAMS ----------------------------------------------------------------------------------
    def on_market_update(self, raw: bytes):  # required method
        pass

    # -- REQUESTS ---------------------------------------------------------------------------------

    async def _request_instrument(self, instrument_id: InstrumentId, correlation_id: UUID4) -> None:
        """Request Instrument data for the given instrument id."""
        self._log.debug(f"RequestID: {correlation_id} ... Requesting instrument {instrument_id}...")
        instrument: Optional[Union[Instrument, CryptoBettingInstrument]] = self._instrument_provider.find(instrument_id)
        if instrument is not None:
            self._log.debug(
                f"RequestID: {correlation_id} ... Found instrument {instrument_id} in cache. Fetching latest data...")
            try:
                market_url = instrument.market_name + '/' + instrument.outcome + '?' + instrument.params if instrument.params is not None else instrument.market_name + '/' + instrument.outcome
                odds: GetLatestOddsResponse = await self._client.get_latest_odds(event_id=instrument.event_id, market_url=market_url)
                instrument.max_size = odds.max_stake
                instrument.min_size = odds.min_stake
                instrument.price = odds.price
                instrument.enabled = True if odds.status == SelectionStatus.ENABLED else False
                self._instrument_provider.add(instrument)
                self._handle_instrument(
                    instrument,
                    correlation_id,
                )
            except Exception as e:
                self._log.error(f"Error fetching instrument data for {instrument_id}. {e}")
                return

        if instrument is None:
            self._log.warning(
                f"Cannot find instrument for {instrument_id}. in the provider. Load instrument first. Returning")
            return
    async def _request_instruments(self, venue: Venue, correlation_id: UUID4) -> None:
        """Request all Instrument data for the given venue."""
        instruments: List[Union[Instrument, CryptoBettingInstrument]] = await self._instrument_provider.list_all()
        for instrument in instruments:
            self._log.debug(f"Fetching latest data for {instrument.id}...")
            await self._instrument_provider.load_ids_async([instruments.id for instruments in instruments])
            updated_instruments: list[Instrument] = self._instrument_provider.list_all()
            self._handle_instruments(
                venue,
                updated_instruments,
                correlation_id,
            )

    # -- DATA HANDLERS ---------------------------------------------------------------------------------

    def _handle_account_data(self, data_type: DataType) -> None:
        raise NotImplementedError(f"Cannot handle {data_type.type} (not implemented).")  # Do nothing further

    def _handle_cb_events(self, data_type: DataType) -> None:
        raise NotImplementedError(f"Cannot handle {data_type.type} (not implemented).")

    def _handle_fixtures(self, data_type: DataType) -> None:
        raise NotImplementedError(f"Cannot handle {data_type.type} (not implemented).")

    def _handle_competitions(self, data_type: DataType) -> None:
        raise NotImplementedError(f"Cannot handle {data_type.type} (not implemented).")
