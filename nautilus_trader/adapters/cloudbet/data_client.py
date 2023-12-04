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
        # Parent class has an identical attribute
        # self._instrument_provider: CloudbetInstrumentProvider = instrument_provider or CloudbetInstrumentProvider(
        #     client=client, logger=logger,
        #     filters=market_filter)
        self._stream = stream_client or CloudbetStreamClient(
            client=client,
            logger=logger,
            message_handler=self.on_market_update)

        # TODO: pass from config and aadd function to update interval
        # self._update_instrument_interval: int = 60 * 60  # Once per hour (hardcode)
        # self._update_instruments_task: Optional[asyncio.Task] = None

        # Hot caches
        self.subscribed_orderbook_delta: dict[
            InstrumentId,
            list[Union[OrderBookDelta, OrderBookDeltas]],
        ] = {}
        self.subscribed_orderbooks: dict[InstrumentId, OrderBook] = {}
        self.subscribed_selection_ids: set[SelectionId] = set()
        self.subscribed_instrument_ids: set[InstrumentId] = set()

        # Register Cloudbet Data handlers
        # self._data_handler = {
        #     "account": self._handle_account_data,
        #     "orderbook": self._subscribe_order_book,
        #     "orderbook_deltas": self._subscribe_order_book_deltas,
        # }

        # Subscriptions
        # TODO: test if implicitly set by the parent class
        # self.subscriptions_order_book_delta = self._subscriptions_order_book_delta
        # self.subscriptions_order_book_snapshot = self._subscriptions_order_book_snapshot
        # self.subscriptions_ticker = self._subscriptions_ticker
        # self.subscriptions_quote_tick = self._subscriptions_quote_tick
        # self.subscriptions_trade_tick = self._subscriptions_trade_tick
        # self.subscriptions_bar = self._subscriptions_bar
        # self.subscriptions_venue_status_update = self._subscriptions_venue_status_update
        # self.subscriptions_instrument_status_update = self._subscriptions_instrument_status_update
        # self.subscriptions_instrument_close = self._subscriptions_instrument_close
        # self.subscriptions_instrument = self._subscriptions_instrument

    @property
    def instrument_provider(self) -> CloudbetInstrumentProvider:
        return self._instrument_provider

    async def _connect(self):
        self._log.info("Initialising instruments...")
        # Connect market data socket
        await self._stream.connect()
        # # load all instruments asynchronously or  load_ids_on_start from config (default None)
        # # N.B. if no filter or set loads all instruments by default
        await self._instrument_provider.initialize()
        self._send_all_instruments_to_data_engine()
        self._log.info(f"Successfully sent {self._instrument_provider.count} instruments to the Data engine.",
                       LogColor.GREEN)

    async def _disconnect(self) -> None:
        if not self.is_connected:
            self._log.error("Cannot disconnect a disconnected data client. Trying connecting first")
            return
        self._log.info("Disconnecting Data Client...")
        await self._stream.disconnect()
        await self._reset()

    async def _reset(self) -> None:
        # clear "hot" caches
        self.subscribed_orderbook_delta = {}
        self.subscribed_selection_ids = set()
        self.subscribed_orderbooks = {}
        self.subscribed_instrument_ids: set[InstrumentId] = set()
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
            self._log.warning(f"Cannot find instrument for {instrument_id}.")
            return
        # send data to DataEngine.process endpoint
        # TODO: test if cache contains instrument state
        self.subscribed_instrument_ids.add(instrument_id)
        self._handle_data(instrument)
        # TODO: implement send subscription message via websocket stream client
        self._stream.subscribe_instrument(instrument_id)
        self._log.debug(f"Subscribed to Cloudbet Instrument{instrument.id}. Sending to Data engine...", LogColor.GREEN)

    async def _subscribe_instruments(self) -> None:
        instruments: list[Instrument] = self._instrument_provider.list_all()
        # TODO: implement send bulk subscription message to websocket server
        subscriptions: list[InstrumentId] = []
        for instrument in instruments:
            if instrument.id not in self.subscribed_instrument_ids:
                self.subscribed_instrument_ids.add(instrument.id)
                subscriptions.append(instrument.id)
                self._handle_data(instrument)
                self._log.info(f"Attempting to subscribe  to Cloudbet Instrument{instrument.id}. Sending to Data engine...",
                            LogColor.YELLOW)
            else:
                self._log.warning(f"Already subscribed to Cloudbet Instrument{instrument.id}.", LogColor.YELLOW)
        await self._stream.subscribe_instruments(subscriptions)

    async def subscribe_order_book_snapshots(  # noqa (too complex)
        self,
        instrument_id: InstrumentId,
        book_type: BookType,  # generally BookType.L1_MBP or BookType.L3_MBO since cloudbet only supports top-level orderbook
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
        # if self._stream is None:
        #     self._log.error("Cannot subscribe to order book deltas: no stream client.")
        #     return
        # # TODO: implement handler/method on websocket server to only pass deltas. For now subscribed to as part of Orderbook
        # PyCondition.not_none(instrument_id, "instrument_id")
        # instrument = self._instrument_provider.find(instrument_id)
        # # ToDO: test if this check is redundant. _subscriptions_instrument is a set and should not contain duplicates
        # if instrument.id not in self.subscribed_orderbook_delta:
        #     # If this is the first subscription request we're receiving, schedule a
        #     # subscription after a short delay to allow other strategies to send
        #     # their subscriptions (every change triggers a full snapshot).
        #     # ToDO: implement sending a susbcription message for Orrderbook Deltas to the websocket server
        #     if kwargs.get('update_speed') is not None:
        #         update_speed = kwargs.get('update_speed')  # default 0 ms for futures.
        #         valid_speeds = [60, 300, 600, 1800]
        #         if update_speed not in valid_speeds:
        #             self._log.error(
        #                 "Cannot subscribe to order book delta:"
        #                 f"invalid `update_speed`, was {update_speed}. "
        #                 f"Valid update speeds are {valid_speeds} seconds.",
        #             )
        #             return
        #     elif kwargs.get('update_speed') is None:
        #         update_speed = None  # default user the class level update speed
        #
        #     if depth is None:
        #         depth = 0
        #     self._log.debug(
        #         f"Subscribing to {instrument_id} <OrderBookDelta> BookType {str(book_type.value)}data.",
        #     )
        #     if kwargs.get('orderbook_delta') is not None:
        #         orderbook_delta: Optional[OrderBookDelta] = OrderBookDelta.from_dict(kwargs.get('orderbook_delta'))
        #         # update hot cache
        #         self.subscribed_orderbook_delta[instrument_id] = orderbook_delta
        #         # send OrderbookDelta to DataEngine.process endpoint
        #         self._handle_data(orderbook_delta)
        #
        # if instrument.id in self.subscribed_orderbook_delta:
        #     self._log.warning(
        #         f"Already subscribed to instrument id: {instrument_id} "
        #         f"[Instrument: {instrument_id.symbol}] <OrderBook> data.",
        #     )
        #     return

    async def _subscribe_instrument_status_updates(self, instrument_id: InstrumentId) -> None:
        raise NotImplementedError(  # pragma: no cover
            "Cannot subscribe to Orderbook Delta for  Cloudbet",  # pragma: no cover
        )

    async def _subscribe_instrument_close(self, instrument_id: InstrumentId) -> None:
        raise NotImplementedError(  # pragma: no cover
            "Cannot subscribe to Orderbook Delta for  Cloudbet",  # pragma: no cover
        )

    async def _unsubscribe_instrument(self, instrument_id: InstrumentId) -> None:
        # TODO: pass unsubscribe message data engine which may trigger other actions/events
        if instrument_id in self.subscribed_instrument_ids:
            self.subscribed_instrument_ids.remove(instrument_id)
            self._log.info(f"Attempting to unsubscribe from {instrument_id} <Instrument> data.", LogColor.YELLOW)
            # TODO: send unsubscribe message to websocket server
            await self._stream.unsubscribe_instrument(instrument_id)


    async def _unsubscribe_instruments(self, instrument_id: InstrumentId) -> None:
        # TODO: pass unsubscribe message data engine which may trigger other actions/events
        instruments: list[Instrument] = self._instrument_provider.list_all()
        subscriptions: list[InstrumentId] = []
        for instrument in instruments:
            if instrument.id in self.subscribed_instrument_ids:
                self.subscribed_instrument_ids.remove(instrument_id)
                subscriptions.append(instrument.id)
                self._log.info(f"Attempting to unsubscribe from {instrument_id} <Instrument> data.", LogColor.YELLOW)
            else:
                self._log.warning(f"Cannot unsubscribe from {instrument_id} <Instrument> data.", LogColor.RED)
        # TODO: implement  send unsubscribe message to websocket server
        await self._stream.unsubscribe_instruments(subscriptions)

    async def _unsubscribe_order_book_snapshots(self, instrument_id: InstrumentId) -> None:
        # check if subscruption exists
        if instrument_id in self.subscribed_orderbooks:
            self.subscribed_orderbooks.pop(instrument_id, None)
            # TODO: implement send unsubscribe message to websocket server
            self._stream.unsubscribe_orderbook(instrument_id)
            self._log.info(f"Attempting to unsubscribe from {instrument_id} <OrderBook> data.", LogColor.YELLOW)

        if instrument_id not in self.subscribed_orderbooks:
            self._log.error(f"Cannot unsubscribe from {instrument_id} <OrderBook> data.", LogColor.RED)
            raise NotImplementedError(  # pragma: no cover
                f"Cannot unsubscribe to Orderbook for instrument id: {instrument_id} for  Cloudbet",  # pragma: no cover
            )

    async def _unsubscribe_order_book_deltas(self, instrument_id: InstrumentId) -> None:
        raise NotImplementedError(  # pragma: no cover
            "Cannot subscribe to Orderbook Delta for  Cloudbet",  # pragma: no cover
        )

    async def _unsubscribe_instruments(self) -> None:
        # TODO: remove instruments from cache and data engine
        for instrument in self._instrument_provider.list_all():
            self.subscribed_instrument_ids.remove(instrument.id)
            self._log.debug(f"Unsubscribed from {instrument.id} <Instrument> data.", LogColor.GREEN)
            # TODO: send unsubscribe message to websocket server

    # TODO: test must check if instruments were sent to the cache and data engine
    def _send_all_instruments_to_data_engine(self, **kwargs) -> None:
        if kwargs.get('instruments') is not None:
            instruments: List[Union[Instrument, CryptoBettingInstrument]] = kwargs.get('instruments')
            for instrument in instruments:
                self._handle_data(instrument)
                self._log.debug(f"Sending {instrument.id} to Data engine...", LogColor.GREEN)
        else:
            self._log.debug(f"Loading {self._instrument_provider.count} instruments from provider into cache, ")
            for instrument in self._instrument_provider.get_all().values():
                # ToDO: TEST _handle_instrument method instead and check Data Engine and cache differences
                self._handle_data(instrument)

        self._log.debug(
            f"DataEngine has {len(self._cache.instruments(CLOUDBET_VENUE))} Cloudbet instruments",
        )
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)

    # async def _remove_all_instruments_from_data_engine(self) -> None:
    #     # TODO: cleanup instruments from cache and data engine
    #     # self._log.debug(f"Removing {self._instrument_provider.count} instruments from Data Engine and cache, ")
    #     await self._unsubscribe_instruments()
    #     # self._log.debug(
    #     #     f"DataEngine has {len(self._cache.instruments(CLOUDBET_VENUE))} Cloudbet instruments",
    #     # )

    # async def _update_instruments(self) -> None:
    #     try:
    #         while True:
    #             self._log.debug(
    #                 f"Scheduled `update_instruments` to run in "
    #                 f"{self._update_instrument_interval}s.",
    #             )
    #             await asyncio.sleep(self._update_instrument_interval)
    #             # TODO: test if this is the correct way to update instruments. i.e. will this update relevelant selection-level data or will it create new instruments in provider
    #             await self._instrument_provider.load_ids_async(self.subscribed_instrument_ids)
    #             # TODO: for each subscription in the cold cache (redis)/ hot cache
    #             self._send_all_instruments_to_data_engine()
    #     except asyncio.CancelledError:
    #         self._log.debug("`update_instruments` task was canceled.")

    # -- STREAMS ----------------------------------------------------------------------------------
    def on_market_update(self, raw: bytes):
        pass
        # update = STREAM_DECODER.decode(raw)
        # if isinstance(update, MCM):
        #     self._on_market_update(mcm=update)
        # elif isinstance(update, Connection):
        #     pass
        # elif isinstance(update, Status):
        #     self._handle_status_message(update=update)
        # else:
        #     raise RuntimeError
    #
    # def _on_market_update(self, mcm: MCM):
    #     self._check_stream_unhealthy(update=mcm)
    #     updates = self.parser.parse(mcm=mcm)
    #     for data in updates:
    #         self._log.debug(f"{data}")
    #         if isinstance(data, (BetfairStartingPrice, BSPOrderBookDeltas)):
    #             # Not a regular data type
    #             generic_data = GenericData(
    #                 DataType(data.__class__, {"instrument_id": data.instrument_id}),
    #                 data,
    #             )
    #             self._handle_data(generic_data)
    #         elif isinstance(data, Data):
    #             if self._strict_handling and (
    #                 hasattr(data, "instrument_id")
    #                 and data.instrument_id not in self._subscribed_instrument_ids
    #             ):
    #                 # We receive data for multiple instruments within a subscription, don't emit data if we're not
    #                 # subscribed to this particular instrument as this will trigger a bunch of error logs
    #                 continue
    #             self._handle_data(data)
    #         elif isinstance(data, Event):
    #             self._log.warning(
    #                 f"Received event: {data}, DataEngine not yet setup to send events",
    #             )
    #         else:
    #             raise RuntimeError
    # -- REQUESTS ---------------------------------------------------------------------------------

    async def _request_instrument(self, instrument_id: InstrumentId, correlation_id: UUID4) -> None:
        """Request Instrument data for the given instrument id."""
        self._log.debug(f"RequestID: {correlation_id} ... Requesting instrument {instrument_id}...")
        instrument: Optional[Union[Instrument, CryptoBettingInstrument]] = self._instrument_provider.find(instrument_id)
        if instrument is not None:
            self._log.debug(
                f"RequestID: {correlation_id} ... Found instrument {instrument_id} in cache. Fetching latest data...")
            try:
                odds: GetLatestOddsResponse = await self._client.get_latest_odds(instrument_id)
                instrument.max_size = odds.max_stake
                instrument.min_size = odds.min_stake
                instrument.price = odds.price
                instrument.enabled = True if odds.status == SelectionStatus.ENABLED else False
                self._instrument_provider.add(instrument)
                self._handle_instrument(
                    instrument,
                    correlation_id,
                )
            # TODO: handle exceptions gracefully
            except Exception as e:
                self._log.error(f"Error fetching instrument data for {instrument_id}. {e}")
                return

        if instrument is None:
            self._log.warning(f"Cannot find instrument for {instrument_id}. in the provider. Load instrument first. Returning")
            return
                # await self._instrument_provider.load_async(instrument_id)
                # instrument: Optional[Union[Instrument, CryptoBettingInstrument]] = self._instrument_provider.find(
                #     instrument_id)
                # event: GetEventResponse = await self._client.get_event(event_id=instrument_id.symbol.value.split("|")[0])
                # odds: GetLatestOddsResponse = await self._client.get_latest_odds(instrument_id)
                # instrument = CryptoBettingInstrument(
                #     home_name=event.home.name,
                #     away_name=event.away.name,
                #     sport_name=event.sport.name,
                #     competition_name=event.competition.name,
                #     price=odds.price,
                #     currency=EUR,
                #     event_name=event.name,
                #     market_name=instrument_id.symbol.value.split("|")[1],
                #     venue=CLOUDBET_VENUE,
                #     live=True if event.status in {EventStatus.PRE_TRADING, EventStatus.TRADING, EventStatus.TRADING_LIVE} else False,
                #     enabled=True if odds.status == SelectionStatus.ENABLED else False,
                #     outcome=odds.outcome,
                #     side=odds.side,
                #     params=odds.params,
                #     #TODO: market type is not available in the GetEventResponse or GetLatestOddsResponse so we set it to the market_name for now
                #     market_type=instrument_id.symbol.value.split("|")[1],
                # )
                # self._handle_instrument(
                #     instrument,
                #     correlation_id,
                # )
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
        pass  # Do nothing further

    # -- WEBSOCKET HANDLERS ---------------------------------------------------------------------------------

    # def _handle_ws_message(self, raw: bytes) -> None:
    #     # self._log.info(str(raw), LogColor.CYAN)
    #     wrapper = self._decoder_data_msg_wrapper.decode(raw)
    #     try:
    #         handled = False
    #         for handler in self._ws_handlers:
    #             if handler in wrapper.stream:
    #                 self._ws_handlers[handler](raw)
    #                 handled = True
    #         if not handled:
    #             self._log.error(
    #                 f"Unrecognized websocket message type: {wrapper.stream}",
    #             )
    #     except Exception as e:
    #         self._log.error(f"Error handling websocket message, {e}")
    #
    # def _handle_book_diff_update(self, raw: bytes) -> None:
    #     msg = self._decoder_order_book_msg.decode(raw)
    #     instrument_id: InstrumentId = self._get_cached_instrument_id(msg.data.s)
    #     book_deltas: OrderBookDeltas = msg.data.parse_to_order_book_deltas(
    #         instrument_id=instrument_id,
    #         ts_init=self._clock.timestamp_ns(),
    #     )
    #     book_buffer: Optional[list[Union[OrderBookDelta, OrderBookDeltas]]] = self._book_buffer.get(
    #         instrument_id,
    #     )
    #     if book_buffer is not None:
    #         book_buffer.append(book_deltas)
    #     else:
    #         self._handle_data(book_deltas)
