import asyncio
from typing import Optional, Any, List, Union, Set

import pandas as pd
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.rust.model import OrderStatus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.execution.reports import TradeReport
from nautilus_trader.model.currency import Currency
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import AccountId, PositionId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money, AccountBalance, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.msgbus.bus import MessageBus

from nautilus_trader.adapters.betfair.client.exceptions import BetfairAPIError
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.adapters.cloudbet.client.schema import GetAccountCurrencies, GetAccountBalance, SelectionSide, \
    GetBetResponse, BetStatus, GetBetHistoryResponse, GetAccountInfoResponse
from nautilus_trader.adapters.cloudbet.client.util import bet_to_trade_report, cb_bet_to_order_status_report, \
    datetime_to_cloudbet_timestamp, cb_bet_to_position_report
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.adapters.cloudbet.sockets import CloudbetStreamClient
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orders import Order


class CloudbetLiveExecutionClient(LiveExecutionClient):
    """
    Provides an execution client for Cloudbet.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client : CloudbetClient
        The Cloudbet HttpClient.
    base_currency : Currency
        The account base currency for the client.
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
    instrument_provider : CloudbetInstrumentProvider
        The instrument provider.
    config : dict[str, object], optional
        The configuration for the instance.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: CloudbetClient,
        base_currency: Optional[Currency],  # explicitly pass None for multi-currency or no currency
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        market_filter: dict,
        instrument_provider: CloudbetInstrumentProvider,
        config: Optional[dict[str, Any]] = None,
        account_id: Optional[AccountId] = None # default to None as this should be fetched from the venue
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(CLOUDBET_VENUE.value),
            venue=CLOUDBET_VENUE,
            oms_type=OmsType.HEDGING,
            # HEDGING => multiple positions per instrument. NETTING => only one position per instrument
            account_type=AccountType.BETTING,
            base_currency=base_currency,
            instrument_provider=instrument_provider
                                or CloudbetInstrumentProvider(client=client, logger=logger, filters=market_filter),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )

        self._instrument_provider: CloudbetInstrumentProvider = instrument_provider
        # an asyncio Task to watch the stream
        # self._watch_stream_task: Optional[asyncio.Task] = None
        self._client: CloudbetClient = client
        self.stream: CloudbetStreamClient = CloudbetStreamClient(
            client=self._client,
            logger=logger,
            message_handler=self.handle_order_stream_update,
        )

        self.venue_order_id_to_client_order_id: dict[VenueOrderId, ClientOrderId] = {}
        # self.pending_update_order_client_ids: set[tuple[ClientOrderId, VenueOrderId]] = set()
        # self.published_executions: dict[ClientOrderId, list[TradeId]] = defaultdict(list)
        #
        # self.set_account_id(account_id)
        # AccountFactory.register_calculated_account(BETFAIR_VENUE.value)

    @property
    def instrument_provider(self) -> CloudbetInstrumentProvider:
        return self._instrument_provider

    async def set_account_id(self, account_id: Optional[AccountId] = None) -> AccountId:
        """
        Sets the account ID for the instance. Overrides the base class method to set the account_id to the Cloudbet account_id

        Args:
            account_id (Optional[AccountId]): The account ID to set. Defaults to None.

        Returns:
            None

        Raises:
            None
        """
        if account_id is not None:
            assert isinstance(account_id, AccountId)
            super()._set_account_id(account_id)
            return self.account_id
        else:
            if self._client.connected is False:
                await self._client.connect()  # initialise session
            account_response: GetAccountInfoResponse = await self._client.login()

            # Call the original _set_account_id method with the new account_id
            super()._set_account_id(AccountId(f"{CLOUDBET_VENUE.value}-{account_response.uuid.split('-')[0]}"))
        return self.account_id

    # -- CONNECTION HANDLERS ----------------------------------------------------------------------
    async def _connect(self) -> None:
        """
        Connects to the client and starts the necessary tasks for streaming data.

        This function checks if the client is already connected. If not, it creates an HTTP session and establishes a connection. It also checks if the stream is already connected. If not, it creates a task to connect to the stream and another task to retrieve the account state. If the stream is already connected, it only creates a task to retrieve the account state.

        If the stream watch task is not already running, it starts the task to watch the stream.

        Parameters:
            None

        Returns:
            None
        """
        try:
            if self._client.connected is False: # create HTTP sessions to allow networking calls
                await self._client.connect()
                self._log.debug("Creating a new session...")
                self._log.debug("Cloudbet connect successful.", LogColor.GREEN)
            if self.stream.is_connected is False:
                aws = [
                    asyncio.create_task(self.stream.connect()),
                    asyncio.create_task(self.connection_account_state()),
                ]
            elif self.stream.is_connected:
                aws = [
                    asyncio.create_task(self.connection_account_state()),
                ]
            if aws:
                results: List[Union[None, Exception]] = await asyncio.gather(*aws, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        self._log.error(f"Task encountered an exception: {result}")
                    else:
                        self._log.debug(f"Connection initialisation tasks completed successfully: {result}")
            # TODO: replace _watch_stream_task with an attribute that is set on successful StreamClient connection
            # if self._watch_stream_task is None:
            #     self._log.info("Starting stream watch task...")
            #     self._watch_stream_task = asyncio.create_task(self.watch_stream())
        except Exception as e:
            self._log.error(f"An error occurred during the connection process: { str(e)}")

    async def _disconnect(self) -> None:
        # Close socket
        self._log.info("Closing streaming socket...")
        await self.stream.disconnect()

        # Ensure client closed
        self._log.info("Closing CloudbetClient sessions...")
        await self._client.disconnect()

    def reset(self) -> None:
        # TODO: implement some clean up logic, eg reset/recalculate states
        # pass
        raise NotImplementedError("method not currently implemented")  # pragma: no cover

    def dispose(self) -> None:
        # TODO: implement some clean up logic, eg release resources like stream client
        # pass
        raise NotImplementedError("method not currently implemented")  # pragma: no cover

    async def watch_stream(self) -> None:
        """Ensure socket stream is connected"""
        while not self.stream.is_stopping:
            try:
                if not self.stream.is_connected:
                    await self.stream.connect()
                await asyncio.sleep(1)
            except Exception as e:
                self._log.error(f"Encountered an error while watching the stream: {e}")

    # -- ERROR HANDLING ---------------------------------------------------------------------------
    async def on_api_exception(self, error: BetfairAPIError) -> None:
        # TODO: implement different handlers for CloudbetAPI exceptions eg. duplicate request => use a new UUID
        pass
        # if error.kind == "INVALID_SESSION_INFORMATION":
        #     # Session is invalid, need to reconnect
        #     self._log.warning("Invalid session error, reconnecting..")
        #     await self._client.disconnect()
        #     await self._connect()
        #     self._log.info("Reconnected.")

    # -- ACCOUNT HANDLERS -------------------------------------------------------------------------

    async def connection_account_state(self) -> None:
        """
        Retrieves the account state and sends it to the server.
        """
        try:
            account_response: GetAccountInfoResponse = await self._client.login()
            account_details: GetAccountCurrencies = await self._client.get_account_currencies()  # iterable string
            account_balances: List[AccountBalance] = []
            timestamp = self._clock.timestamp_ns()

            for currency in account_details.currencies:
                # TODO: use asyncio.gather` to make concurrent requests for each currency balance can significantly improve the performance of the `connection_account_state` method
                try:
                    currency_balance: GetAccountBalance = await self._client.get_balances(currency)
                    typed_currency: Currency = Currency.from_str(currency)
                    account_balances.append(
                        AccountBalance(
                            total=Money(currency_balance.amount, typed_currency),
                            locked=Money(0, typed_currency),
                            free=Money(currency_balance.amount, typed_currency),
                        )
                    )
                except Exception as e:
                    self._log.error(f"An error occurred while getting balances for currency {currency}: {str(e)}")
                    continue
            # if there are no account balances, return None
            # TODO: test if this is ever reached
            if len(account_balances) == 0:
                return None
            account_id : AccountId = self.account_id if self.account_id is not None else await self.set_account_id(account_id=None)
            account_state = AccountState(
                account_id=account_id,
                account_type=AccountType.BETTING,
                base_currency=self.base_currency,
                reported=True,
                balances=account_balances,
                margins=[],
                info={},
                event_id=UUID4(),
                ts_event=timestamp,
                ts_init=timestamp,
            )

            self._log.debug(f"Received account state: {account_state}, sending")
            self._send_account_state(account_state)
            self._log.debug("Initial Account state completed")

        except Exception as e:
            self._log.error(f"An error occurred during the connection_account_state process: {str(e)}")
            print(e)
            return None

    async def virtual_connection_account_state(self) -> None:
        """
            Calculate the "True" AccountState, accounting for active orders
        """
        pass

    # -- EXECUTION REPORTS ------------------------------------------------------------------------

    async def generate_order_status_report(
        self,
        instrument_id: InstrumentId,
        client_order_id: Optional[ClientOrderId] = None,
        venue_order_id: Optional[VenueOrderId] = None,
    ) -> Optional[OrderStatusReport]:
        """
        Generate an `OrderStatusReport` for the given order identifier parameter(s).

        If the order is not found, or an error occurs, then logs and returns ``None``.

        Parameters
        ----------
        instrument_id : InstrumentId
            The instrument ID for the report.
        client_order_id : ClientOrderId, optional
            The client order ID for the report.
        venue_order_id : VenueOrderId, optional
            The venue order ID for the report.

        Returns
        -------
        OrderStatusReport or ``None``

        Raises
        ------
        AssertionError
            If both the `client_order_id` and `venue_order_id` are ``None``.
        """
        assert client_order_id is not None or venue_order_id is not None
        # check cloudbet for order ID and bet response
        existing_order: Union[Order, None] = None
        if venue_order_id is not None:
            try:
                bet_status_response: GetBetResponse = await self._client.get_bet_status(venue_order_id)
            except Exception as e:  # TODO: handle exceptions gracefully

                self._log.error(f"Could not fetch bet status from Cloudbet: {str(e)}")
                # we must query the cache => as the order may not have reached the exchange yet or exchange is down
                if client_order_id is not None:
                    self._log.debug(f"Attempting to query the cache for order {client_order_id}")
                    existing_order: Order = self._cache.order(client_order_id)
                    if existing_order is not None:
                        self._log.debug(f"Found order in the cache. Client Order id: {client_order_id}")
                        self._log.debug(f"Generating Order Status Report for order {client_order_id}")
                        report = cb_bet_to_order_status_report(
                            order=existing_order,
                            account_id=self.account_id if self.account_id is not None else await self.set_account_id(
                                account_id=None),
                            instrument_id=instrument_id,
                            bet_response=None,  # for cached Orders we don't need to query the venue
                            ts_init=self._clock.timestamp_ns(),
                            client_order_id=client_order_id,
                            venue_order_id=venue_order_id,
                            report_id=UUID4(),
                        )
                        return report
                    else:
                        self._log.warning(f"Attempting to query order that does not exist in the cache, Client Order ID: {client_order_id}")
                        return None
                else:
                    self._log.debug(
                        f"Unable to fetch Order details from the venue {self.venue} and no Client Order ID was provided",
                    )
                    return None

            report = cb_bet_to_order_status_report(
                order=existing_order,
                account_id=self.account_id if self.account_id is not None else await self.set_account_id(
                    account_id=None),
                instrument_id=instrument_id,
                bet_response=bet_status_response,
                ts_init=self._clock.timestamp_ns(),
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                report_id=UUID4(),
            )
            return report
        elif client_order_id is not None and venue_order_id is None:  # we must query the cache in cases where exchange is unavailable => order must already have been submitted, otherwise no venue_order_id exists
            existing_order: Order = self._cache.order(client_order_id)
            if existing_order is None:
                self._log.warning(
                    f"Attempting to query order that does not exist in the cache, Client Order ID: {client_order_id}",
                )
                return None
            else:
                self._log.debug(f"Found order in the cache. Client Order id: {client_order_id}")
                cached_venue_order_id: Optional[VenueOrderId] = existing_order.venue_order_id
                if cached_venue_order_id is None:
                    self._log.warning(
                        f"Unable to generate a report for Order without a valid VenueOrderID, Client Order ID: {client_order_id}",
                    )
                    return None
                self._log.debug(f"Generating Order Status Report for order Client Order ID: {client_order_id}")
            report = cb_bet_to_order_status_report(
                order=existing_order,
                account_id=self.account_id if self.account_id is not None else await self.set_account_id(account_id=None),
                instrument_id=instrument_id,
                bet_response=None, # for cached Orders we don't need to query the venue
                ts_init=self._clock.timestamp_ns(),
                client_order_id=client_order_id,
                venue_order_id=cached_venue_order_id, #TODO: this should cause a runtime error as cached_venue_order_id is None
                report_id=UUID4(),
            )
        else:
            self._log.debug(
                f"Unable to fetch Order details from the venue {self.venue} and no Client Order ID was provided",
            )
            return None
        return report

    async def generate_order_status_reports(
        self,
        instrument_id: InstrumentId = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        open_only: bool = False,
    ) -> list[OrderStatusReport]:
        """
        Generate a list of `OrderStatusReport` for the given parameters.

        Parameters
        ----------
        instrument_id : InstrumentId
            The instrument ID for the report.
        start : pd.Timestamp, optional
            The start time for the report. If specified, `end` must also be specified.
        end : pd.Timestamp, optional
            The end time for the report. If specified, `start` must also be specified.
        open_only : bool, optional
            If True, only open orders will be returned. If False, all orders will be returned.

        Returns
        -------
        list[OrderStatusReport]
            A list of OrderStatusReports for the given parameters

        Raises
        ------
        ValueError
            If both the `instrument_id` and `venue_order_id` are ``None`` ,or, if either `start` and `end` are ``None``.
        """
        self._log.info(f"Generating OrderStatusReports for {self.id}...")
        # assert instrument_id is not None or (start is not None and end is not None)
        assert instrument_id is not None or (start is not None and end is not None)
        report_list: List[OrderStatusReport] = []
        # if a time-range is specified, we explicitly rely on the venue bet_history endpoint
        if start and end:
            start_date: str = datetime_to_cloudbet_timestamp(start)
            end_date: str = datetime_to_cloudbet_timestamp(end)
            try:
                bet_history: GetBetHistoryResponse = await self._client.get_bet_history(start_date, end_date)
                self._log.info(f"Received bet history: {bet_history}")
            except Exception as e:  # TODO: handle exceptions gracefully
                self._log.error(f"Could not fetch bet history from Cloudbet: {e}")
                return None
            for bet in bet_history.bets:
                report : Optional[OrderStatusReport] = None
                self._log.info(f"Processing bet: {bet}")
                venue_order_id: VenueOrderId = VenueOrderId(bet.reference_id)
                client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                if client_order_id is None:
                    self._log.warning(
                        f"Attempting to query order that does not exist in the cache, Venue Order ID: {venue_order_id}",
                    )
                    continue
                if instrument_id is None:  # no instrument_id specified, we must query the cache
                    cached_order: Order = self._cache.order(
                        client_order_id)  # no need to assert not None, an Order must have a client_order_id on init
                    instrument_id: InstrumentId = cached_order.instrument_id

                if open_only is False:  # we don't care about the order status
                    report = self.generate_order_status_report(
                        instrument_id=instrument_id,
                        client_order_id=client_order_id,
                        venue_order_id=venue_order_id,
                    )
                else:
                    cached_order: Order = self._cache.order(
                        client_order_id)
                    if cached_order.is_open or bet.status == bet.status.PENDING_ACCEPTANCE:
                        report = cb_bet_to_order_status_report(
                            order=cached_order,
                            account_id=self.account_id if self.account_id is not None
                            else await self.set_account_id(account_id=None),
                            instrument_id=instrument_id,
                            bet_response=bet,
                            ts_init=self._clock.timestamp_ns(),
                            client_order_id=client_order_id,
                            venue_order_id=venue_order_id,
                            report_id=UUID4(),
                        )
                if report is not None:
                    report_list.append(report)
        else:
            # no time-range is specified, we must construct the report from the cache
            # we're interested in getting all the orders for a given instrument_id regardless of time
            unique_client_ids: set[ClientOrderId] = self._cache.client_order_ids(venue=CLOUDBET_VENUE,
                                                                                 instrument_id=instrument_id)
            report: Optional[OrderStatusReport] = None
            for client_order_id in unique_client_ids:
                cached_order: Order = self._cache.order(
                    client_order_id)
                venue_order_id: VenueOrderId = cached_order.venue_order_id
                # use the venue_order_id to query the bet_status endpoint
                try:
                    bet_status: GetBetResponse = await self._client.get_bet_status(venue_order_id.value)
                except Exception as e:
                    self._log.error(f"Could not fetch bet status from Cloudbet: {e}")
                    continue
                if open_only is True:
                    if cached_order.is_open or bet_status.status == bet_status.status.PENDING_ACCEPTANCE:
                        report: OrderStatusReport = cb_bet_to_order_status_report(
                            order=cached_order,
                            account_id=self.account_id if self.account_id is not None
                            else await self.set_account_id(account_id=None),
                            instrument_id=instrument_id,
                            bet_response=bet_status,
                            ts_init=self._clock.timestamp_ns(),
                            client_order_id=client_order_id,
                            venue_order_id=venue_order_id,
                            report_id=UUID4(),
                        )
                    else:
                        # skip closed orders
                        continue
                else:
                    report: OrderStatusReport = cb_bet_to_order_status_report(
                        order=cached_order,
                        account_id=self.account_id if self.account_id is not None
                        else await self.set_account_id(account_id=None),
                        instrument_id=instrument_id,
                        bet_response=bet_status,
                        ts_init=self._clock.timestamp_ns(),
                        client_order_id=client_order_id,
                        venue_order_id=venue_order_id,
                        report_id=UUID4(),
                    )
        if report is not None:
            report_list.append(report)
        return report_list

    async def generate_trade_reports(
        self,
        instrument_id: InstrumentId = None,
        venue_order_id: VenueOrderId = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> list[TradeReport]:
        """ Generate a list of TradeReports for the given parameters

        Parameters
        ----------
        instrument_id : InstrumentId
            The instrument ID for the report. If venue_order_id is specified, all Trades associated with the InstrumentID will be returned
        venue_order_id : VenueOrderId, optional
            The venue order ID for the report.  If specified, only a single TradeReport will be returned
        start : pd.Timestamp, optional
            The start time for the report. If specified, `end` must also be specified.
        end : pd.Timestamp, optional
            The end time for the report. If specified, `start` must also be specified.

        Returns
        -------
        list[TradeReport]
            A list of TradeReports for the given parameters

        Raises
        ------
        ValueError
            If both the `instrument_id` and `venue_order_id` are ``None`` ,or, if both `start` and `end` are ``None``.
        Notes
        ------
            A Trade corresponds to an order that has a final result (WIN, LOSS, etc )or has been processed by the exchange. (ACCEPTED)
        """
        # either specify a time range or an instrument_id or a venue order id
        assert instrument_id is not None or venue_order_id is not None or (start is not None and end is not None)
        self._log.info(f"Generating TradeReports for {self.id}...")
        report_list: List[TradeReport] = []
        # if a time-range is specified, we explicitly rely on the venue bet_history endpoint
        if start and end:
            start_date: str = datetime_to_cloudbet_timestamp(start)
            end_date: str = datetime_to_cloudbet_timestamp(end)
            try:
                bet_history: GetBetHistoryResponse = await self._client.get_bet_history(start_date, end_date)
                self._log.info(f"Received bet history: {bet_history}")
            except Exception as e:  # TODO: handle exceptions gracefully
                self._log.error(f"Could not fetch bet history from Cloudbet:", e)
                return []
            for bet in bet_history.bets:
                if bet.status not in [BetStatus.ACCEPTED, BetStatus.WIN, BetStatus.LOSS, BetStatus.HALF_WIN,
                                      BetStatus.HALF_LOSS, BetStatus.PARTIAL, BetStatus.PUSH]:
                    # if bet is not settled, skip
                    continue
                self._log.info(f"Processing bet: {bet}")
                venue_order_id: VenueOrderId = VenueOrderId(bet.reference_id)
                client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                if client_order_id is None and instrument_id is not None:
                    self._log.warning(
                        f"Attempting to query order that does not exist in the cache, Venue Order ID: {venue_order_id}",
                    )
                    report: TradeReport = bet_to_trade_report(
                        order=None,
                        account_id=self.account_id if self.account_id is not None else await self.set_account_id(
                            account_id=None),
                        instrument_id=instrument_id,
                        bet_response=bet,
                        ts_init=self._clock.timestamp_ns(),
                        venue_order_id=venue_order_id,
                        report_id=UUID4(),
                        client_order_id=client_order_id,
                    )
                    report_list.append(report)
                    continue
                elif client_order_id is not None:
                    cached_order: Order = self._cache.order(
                        client_order_id)  # no need to assert not None, an Order must have a client_order_id on init
                    if instrument_id is None:  # no instrument_id passed in, we must query the cache for the instrument id
                        instrument_id: InstrumentId = cached_order.instrument_id
                    report = bet_to_trade_report(
                        order=cached_order,
                        account_id=self.account_id if self.account_id is not None else await self.set_account_id(
                            account_id=None),
                        instrument_id=instrument_id,
                        bet_response=bet,
                        ts_init=self._clock.timestamp_ns(),
                        venue_order_id=venue_order_id,
                        report_id=UUID4(),
                        client_order_id=client_order_id,
                    )
                    if report is None:
                        self._log.warning("Did not received `TradeReport` from request.")

                else:
                    self._log.warning(
                        f"Attempting to query order that does not exist in the cache, Venue Order ID: {venue_order_id}",
                    )
                    continue
                report_list.append(report)
        else:  # no time-range is specified, we must construct the report from the cache
            # we're interested in getting all the trades for a given instrument_id regardless of time
            # TODO: check if instrument_id is None or venue_order_id is None, then use the cache to extract the missing parameter
            if venue_order_id is None:
                # use the instrument_id to query the cache for client_order_ids
                unique_client_ids: set[ClientOrderId] = self._cache.client_order_ids(venue=CLOUDBET_VENUE,
                                                                                     instrument_id=instrument_id)
                if unique_client_ids is set():
                    self._log.warning(f"No trades found for instrument_id: {instrument_id}")
                    return []
                for client_order_id in unique_client_ids:
                    cached_order: Order = self._cache.order(
                        client_order_id)
                    venue_order_id: VenueOrderId = cached_order.venue_order_id
                    # use the venue_order_id to query the bet_status endpoint
                    try:
                        bet_status: GetBetResponse = await self._client.get_bet_status(venue_order_id.value)  # pass str
                    except Exception as e:
                        self._log.error(f"Could not fetch bet status from Cloudbet: {e}")
                        # unable to retrieve bet status, so we check the cached order status
                        if cached_order.status in [OrderStatus.ACCEPTED, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]:
                            report: TradeReport = bet_to_trade_report(
                                order=cached_order,
                                account_id=self.account_id if self.account_id is not None
                                else await self.set_account_id(account_id=None),
                                instrument_id=instrument_id,
                                bet_response=None, # exception encountered, we will use the order to build TradeReport
                                ts_init=self._clock.timestamp_ns(),
                                venue_order_id=venue_order_id,
                                report_id=UUID4(),
                                client_order_id=client_order_id,
                            )
                            report_list.append(report)
                        continue
                    if bet_status.status not in [BetStatus.ACCEPTED, BetStatus.WIN, BetStatus.LOSS, BetStatus.HALF_WIN,
                                          BetStatus.HALF_LOSS, BetStatus.PARTIAL, BetStatus.PUSH]:
                        # if bet is not settled, skip
                        continue
                    report: TradeReport = bet_to_trade_report(
                        order=cached_order,
                        account_id=self.account_id if self.account_id is not None
                        else await self.set_account_id(account_id=None),
                        instrument_id=instrument_id,
                        bet_response=bet_status,
                        ts_init=self._clock.timestamp_ns(),
                        venue_order_id=venue_order_id,
                        report_id=UUID4(),
                        client_order_id=client_order_id,
                    )
                    if report is not None:
                        report_list.append(report)
            else:
                # only a venue_order_id is specified, so we can use the venue_order_id to query the bet_status endpoint
                # NB: a Trade ~= Order on cloudbet, as a Trade is guranteed to only have one order
                try:
                    bet_response: GetBetResponse = await self._client.get_bet_status(venue_order_id.value)  # pass str
                except Exception as e:
                    self._log.error(f"Could not fetch bet status from Cloudbet: {e}")
                    client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                    if client_order_id is not None:
                        cached_order: Order = self._cache.order(client_order_id)
                        instrument_id: InstrumentId = cached_order.instrument_id
                        if cached_order.status in [OrderStatus.ACCEPTED, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]:
                            report: TradeReport = bet_to_trade_report(
                                order=cached_order,
                                account_id=self.account_id if self.account_id is not None else await self.set_account_id(account_id=None),
                                instrument_id=instrument_id,
                                bet_response=None, # exception encountered, we will use the order to build TradeReport
                                ts_init=self._clock.timestamp_ns(),
                                venue_order_id=venue_order_id,
                                report_id=UUID4(),
                                client_order_id=client_order_id,
                            )
                            report_list.append(report)
                    self._log.debug(f"Could not fetch order from Cache: VenueOrderID{venue_order_id.value}")
                    return []
                # check bet has been settled
                if bet_response.status not in [BetStatus.ACCEPTED, BetStatus.WIN, BetStatus.LOSS, BetStatus.HALF_WIN,
                                               BetStatus.HALF_LOSS, BetStatus.PARTIAL, BetStatus.PUSH]:
                    # if bet is not settled, skip
                    return []
                client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                if client_order_id is None:
                    self._log.warning(
                        f"Attempting to query order that does not exist in the cache, Venue Order ID: {venue_order_id}",
                    )
                    return []
                else:
                    cached_order: Order = self._cache.order(client_order_id)
                    report: TradeReport = bet_to_trade_report(
                        order=cached_order,
                        account_id=self.account_id if self.account_id is not None else await self.set_account_id(account_id=None),
                        instrument_id=cached_order.instrument_id,
                        bet_response=bet_response,
                        ts_init=self._clock.timestamp_ns(),
                        venue_order_id=venue_order_id,
                        report_id=UUID4(),
                        client_order_id=client_order_id,
                    )
                if report is not None:
                    report_list.append(report)

        self._log.info(f"Generated {len(report_list)} TradeReports.")
        return report_list

    async def generate_position_status_reports(
        self,
        instrument_id: InstrumentId = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> list[PositionStatusReport]:
        """
        Generate a list of `PositionStatusReport`s with optional query filters.
        The returned list may be empty if no positions match the given parameters.

        Parameters
        ----------
        instrument_id : InstrumentId, optional
            The instrument ID query filter.
        start : pd.Timestamp, optional
            The start datetime query filter.
        end : pd.Timestamp, optional
            The end datetime query filter.

        Returns
        -------
        list[PositionStatusReport]

        Raises
        ------
        ValueError
            If the `instrument_id` is ``None`` ,or, if either `start` and `end` are ``None``.
        Note: A Position corresponds to an order that has been accepted by the venue but hasn't resulted
        """
        # either specify a time range or an instrument_id or a venue order id
        assert instrument_id is not None or (start is not None and end is not None)
        self._log.info(f"Generating PositionStatusReport for {self.id}...")
        report_list: List[PositionStatusReport] = []
        # if a time-range is specified, we explicitly rely on the venue bet_history endpoint
        if start and end:
            start_date: str = datetime_to_cloudbet_timestamp(start)
            end_date: str = datetime_to_cloudbet_timestamp(end)
            try:
                bet_history: GetBetHistoryResponse = await self._client.get_bet_history(start_date, end_date)
                self._log.info(f"Received bet history: {bet_history}")
            except Exception as e:  # TODO: handle exceptions gracefully
                self._log.error(f"Could not fetch bet history from Cloudbet:", bet_history)
                return []
            if not bet_history.bets:
                # log no bets were found
                self._log.info(f"No bets were found in the bet history for start date: {start_date} and end date: {end_date}.")
                return []
            list_bet_reference_id: List[str] = [
                bet.reference_id  # Extract reference_id from the bet
                for bet in bet_history.bets  # Iterate over each bet in bet_history
                if bet.status in [BetStatus.PARTIAL, BetStatus.HALF_LOSS, BetStatus.HALF_WIN,
                                  BetStatus.PUSH, BetStatus.LOSS, BetStatus.WIN, BetStatus.ACCEPTED]
                # Check if bet status is one of the specified statuses
            ]
            if not list_bet_reference_id:
                self._log.info(f"No bets were found in the bet history that meet the bet status criteria for valid positions.")
                return []
            if instrument_id is not None:
                # a valid instrument_id has been passed in, we will query the cache for all positions for this instrument
                # we can optimistically assume venue_order_id ~= VenueOrderId(bet.reference_id) for Cloudbet
                positions: list[Position] = self._cache.positions(venue=CLOUDBET_VENUE, instrument_id=instrument_id)
                if not positions:
                    return []
                # Convert each bet_reference_id to a VenueOrderId object and store them in a set for faster lookups.
                cb_venue_order_ids: Set[VenueOrderId] = {VenueOrderId(bet_reference_id) for bet_reference_id in list_bet_reference_id}

                # Filter the positions:
                # For each position in positions, check if any of its venue_order_ids is present in cb_venue_order_ids.
                # If at least one venue_order_id is found in cb_venue_order_ids, include the position in the filtered list.
                filtered_positions: List[Position] = [
                    position  # Include this position in the filtered list
                    for position in positions  # Iterate over each position in the original list
                    if any(
                        # The 'any' function checks if at least one of the conditions (venue_order_id in cb_venue_order_ids) is True.
                        # If at least one True condition is found, 'any' returns True, causing the current position to be included in the filtered list.
                        venue_order_id in cb_venue_order_ids  # Check if this venue_order_id is in cb_venue_order_ids
                        for venue_order_id in position.venue_order_ids
                        # Iterate over each venue_order_id of the current position
                    )
                ]
                # A Position for an Instrument may have  multiple venue_order_ids (Unqiue Orders),
                # we want to extract only the relevant Orders for that Position
                # .i.e only Orders that strictly match the venue_order_ids in cb_venue_order_ids
                # Now extract the orders for each venue_order_id in the filtered positions
                filtered_orders: list[Order] = [
                    self._cache.order(client_order_id)
                    for position in filtered_positions
                    for venue_order_id in position.venue_order_ids
                    if venue_order_id in cb_venue_order_ids
                    # Include only orders with venue_order_ids present in cb_venue_order_ids
                    if (client_order_id := self._cache.client_order_id(venue_order_id)) is not None # :=  available in Python >= 3.8
                    # Proceed to get the order only if client_order_id is not None
                ]

                for order in filtered_orders:
                    report = cb_bet_to_position_report(
                        order=order,
                        account_id=self.account_id if self.account_id is not None
                        else await self.set_account_id(account_id=None),
                        instrument_id=instrument_id,
                        bet_response=None,
                        ts_init=self._clock.timestamp_ns(),
                        venue_order_id=order.venue_order_id,
                        report_id=UUID4(),
                        client_order_id=order.client_order_id,
                        position=None,
                    )
                    report_list.append(report)
                return report_list
            else: # no instrument_id passed in, we must query the cache
                for bet in bet_history.bets:
                    venue_order_id: VenueOrderId = VenueOrderId(bet.reference_id) # NB: the reference_id is the venue_order_id on cloudbet
                    cached_client_order_id = self._cache.client_order_id(venue_order_id)
                    if cached_client_order_id  is None:
                        self._log.warning(
                            f"Unable to determine instrument ID for the bet response. Venue Order ID: {bet.reference_id}",
                        )
                        continue
                    cached_order: Order = self._cache.order(cached_client_order_id)
                    if cached_order is None: # this should never be true, if client_order_id exists then the Order must exist
                        self._log.warning(
                            f"Unable to determine instrument ID from the bet response. Venue Order ID: {bet.reference_id}",
                        )
                        continue
                    # we should have a cached order at this point if we're querying the cache
                    # TODO: add this report to the report list
                    report = cb_bet_to_position_report(
                        order=None,
                        account_id=self.account_id if self.account_id is not None
                        else await self.set_account_id(account_id=None),
                        instrument_id=cached_order.instrument_id,
                        bet_response=bet,
                        ts_init=self._clock.timestamp_ns(),
                        venue_order_id=cached_order.venue_order_id if cached_order.venue_order_id else VenueOrderId(bet.reference_id),
                        report_id=UUID4(),
                        client_order_id=cached_order.client_order_id,
                    )
                    report_list.append(report)
            return report_list
        else:
            # no time-range is specified, we must construct the report from the bet_status and/or cache
            # we're interested in getting all the positions for a given instrument_id regardless of time
            # use the instrument_id to query the cache for all positions for this instrument
            # we can optimistically assume venue_order_id ~= VenueOrderId(bet.reference_id) for Cloudbet
            positions: list[Position] = self._cache.positions(venue=CLOUDBET_VENUE, instrument_id=instrument_id)
            if not positions:
                self._log.warning(
                    f"No positions were found for instrument_id: {instrument_id}."
                )
                return []

            # # A Position for an Instrument may have  multiple venue_order_ids (unqiue Orders),
            # ideally we want to extract each Order for that Position and use that to build the report
            for position in positions:
                report = cb_bet_to_position_report(
                    order=None,
                    account_id=self.account_id if self.account_id is not None
                    else await self.set_account_id(account_id=None),
                    instrument_id=instrument_id,
                    bet_response=None,
                    ts_init=self._clock.timestamp_ns(),
                    venue_order_id=position.venue_order_ids[-1], # use only the last venue_order_id
                    report_id=UUID4(),
                    client_order_id=position.client_order_ids[-1],
                    position=position,
                )
                report_list.append(report)
        return report_list

    # -- COMMAND HANDLERS -------------------------------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        """
        Submits an order to the system.

        Args:
            command (SubmitOrder): The command object containing the order information.

        Returns:
            None
        """
        self._log.debug(f"Received submit_order {command}")

        instrument: CryptoBettingInstrument = self._cache.instrument(command.instrument_id)
        PyCondition.not_none(instrument, "instrument")
        PyCondition.true(command.order.has_price, fail_msg="Order must have a price")  # check OrderType has price, else we can't trade
        # PyCondition.type(command, LimitOrder) possible replacement for has price check and validates parametre type
        client_order_id = command.order.client_order_id

        # prepare data for client place bet
        market_url = instrument.market_name + '/' + instrument.outcome + '?' + instrument.params if instrument.params is not None else instrument.market_name + '/' + instrument.outcome
        price: float = command.order.price.as_double()
        # TODO: handle other types of SelectionSide eg.yes/no; odd/even market
        # check market name, outcome, etc and if it has yes/no use that to extract the side
        side = SelectionSide.BACK if command.order.is_buy else SelectionSide.LAY  # for now optimistically assume we only trade BACK/LAY markets
        stake: float = command.order.quantity.as_double() # test if as_decimal or to_str if more reliable than as_double
        try:
            self.generate_order_submitted(
                instrument_id=command.instrument_id,
                strategy_id=command.strategy_id,
                client_order_id=command.order.client_order_id,
                ts_event=self._clock.timestamp_ns(),
            )
            self._log.debug("Generated _generate_order_submitted")
            place_bet_response: GetBetResponse = await self._client.place_bets(
                event_id=instrument.event_id,
                market_url=market_url,
                price=price,  # assumes Order has price eg. Limit Order
                side=side,
                stake=stake,
            )
        except Exception as e:
            # if isinstance(CloudbetAPIError):
            #     await self.on_api_exception(error=e)
            self._log.warning(f"Submit failed: {e}")
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                reason=f"client error/exception",
                ts_event=self._clock.timestamp_ns(),
            )
            return  # end execution
        self._log.debug(f"result={place_bet_response.status.value}")
        # using the message bus, notify relevant components of results eg. RiskEngine, DataEngine, etc
        if place_bet_response.status is BetStatus.ACCEPTED:
            venue_order_id = VenueOrderId(place_bet_response.reference_id)
            self._log.debug(
                f"Matching venue_order_id: {venue_order_id} to client_order_id: {client_order_id}",
            )
            self.venue_order_id_to_client_order_id[venue_order_id] = client_order_id
            self.generate_order_accepted(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                ts_event=self._clock.timestamp_ns(),
            )
            self._log.debug("Generated _generate_order_accepted")
        else:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                reason=place_bet_response.status.value if place_bet_response.status else "client error/exception",
                ts_event=self._clock.timestamp_ns(),
            )

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        raise NotImplementedError(
            "submitting multiple orders simultaneously isn't supported on Cloudbet")  # pragma: no cover

    async def _modify_order(self, command: ModifyOrder) -> None:
        # TODO : message the cloudbet team about resending a BetRequest with the same referenceID
        raise NotImplementedError(
            "submitting multiple orders simultaneously isn't supported on Cloudbet")  # pragma: no cover

    async def _cancel_order(self, command: CancelOrder) -> None:
        # TODO : message the cloudbet team about cancelling a Bet that hasn't been accepted yet or is only partially fileld
        raise NotImplementedError("Cloudbet doesn't support cancelling an order")  # pragma: no cover

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        # TODO : message the cloudbet team about cancelling a Bet that hasn't been accepted yet or is only partially fileld
        raise NotImplementedError("Cloudbet doesn't support bulk cancelling orders")  # pragma: no cover

    # -- ORDER STREAM API -------------------------------------------------------------------------

    def handle_order_stream_update(self, raw: bytes) -> None:
        """Handle an update from the order stream socket"""
        pass
        # update = STREAM_DECODER.decode(raw)
        # if isinstance(update, OCM):
        #     self.create_task(self._handle_order_stream_update(update))
        # elif isinstance(update, Connection):
        #     pass
        # elif isinstance(update, Status):
        #     self._handle_status_message(update=update)
        # else:
        #     raise RuntimeError
