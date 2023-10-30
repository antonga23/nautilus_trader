import asyncio
from typing import Optional, Any, List, Union

import pandas as pd
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.correctness import PyCondition
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
from nautilus_trader.model.objects import Money, AccountBalance
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
        self._watch_stream_task: Optional[asyncio.Task] = None
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
        try:
            if self._client.connected is False:
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
            await asyncio.gather(*aws)
            # TODO: check if stream watch task is already running...if not self.stream.is_running_task(self.watch_stream):
            if self._watch_stream_task is None:
                self._log.info("Starting stream watch task...")
                self._watch_stream_task = asyncio.create_task(self.watch_stream())
        except Exception as e:
            self._log.error(f"An error occurred during the connection process: { str(e)}")

    async def _disconnect(self) -> None:
        # Close socket
        self._log.info("Closing streaming socket...")
        await self.stream.disconnect()

        # Ensure client closed
        self._log.info("Closing CloudbetClient...")
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
            if not self.stream.is_connected:
                await self.stream.connect()
            await asyncio.sleep(1)

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
                base_currency=self._client.currency,
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
        PyCondition.not_none(instrument_id, "instrument_id") or PyCondition.not_none(venue_order_id, 'venue_order_id') \
        or PyCondition.not_none(start, "start") and PyCondition.not_none(end, "end")
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
                self._log.error(f"Could not fetch bet history from Cloudbet:", bet_history)
                return []
            for bet in bet_history.bets:
                if bet.status not in [BetStatus.ACCEPTED, BetStatus.WIN, BetStatus.LOSS, BetStatus.HALF_WIN,
                                      BetStatus.HALF_LOSS, BetStatus.PARTIAL, BetStatus.PUSH]:
                    # if bet is not settled, skip
                    continue
                self._log.info(f"Processing bet: {bet}")
                venue_order_id: VenueOrderId = VenueOrderId(bet.reference_id)
                client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                if client_order_id is None:
                    self._log.warning(
                        f"Attempting to query order that does not exist in the cache, Venue Order ID: {venue_order_id}",
                    )
                    continue
                cached_order: Order = self._cache.order(
                    client_order_id)  # no need to assert not None, an Order must have a client_order_id on init
                if instrument_id is None:  # no instrument_id specified, we must query the cache
                    instrument_id: InstrumentId = cached_order.instrument_id
                report = bet_to_trade_report(
                    order=cached_order,
                    account_id=self.account_id if self.account_id is not None else await self.set_account_id(account_id=None),
                    instrument_id=instrument_id,
                    bet_response=bet,
                    ts_init=self._clock.timestamp_ns(),
                    venue_order_id=venue_order_id,
                    report_id=UUID4(),
                    client_order_id=client_order_id,
                )
                if report is not None:
                    self._log.debug(f"Received {report}.")
                    report_list.append(report)
        else:  # no time-range is specified, we must construct the report from the cache
            # we're interested in getting all the trades for a given instrument_id regardless of time
            # TODO: check if instrument_id is None or venue_order_id is None, then use the cache to extract the missing parameter
            if venue_order_id is None:
                # use the instrument_id to query the cache for client_order_ids
                unique_client_ids: set[ClientOrderId] = self._cache.client_order_ids(venue=CLOUDBET_VENUE,
                                                                                     instrument_id=instrument_id)

                for client_order_id in unique_client_ids:
                    cached_order: Order = self._cache.order(
                        client_order_id)
                    venue_order_id: VenueOrderId = cached_order.venue_order_id
                    # use the venue_order_id to query the bet_status endpoint
                    bet_status: GetBetResponse = await self._client.get_bet_status(venue_order_id.value)  # pass str
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
                # a Trade ~= Order on cloudbet, so we can use the venue_order_id to query the bet_status endpoint, as a Trade is guranteed to only have one order
                bet_response: GetBetResponse = await self._client.get_bet_status(venue_order_id.value)  # pass str
                # check bet has been settled
                if bet_response.status not in [BetStatus.ACCEPTED, BetStatus.WIN, BetStatus.LOSS, BetStatus.HALF_WIN,
                                               BetStatus.HALF_LOSS, BetStatus.PARTIAL, BetStatus.PUSH]:
                    # if bet is not settled, skip
                    return []
                client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                cached_order: Order = self._cache.order(client_order_id)
                report: TradeReport = bet_to_trade_report(
                    order=cached_order,
                    account_id=self.account_id if self.account_id is not None else await self.set_account_id(account_id=None),
                    instrument_id=instrument_id,
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
        PyCondition.not_none(instrument_id, "instrument_id") or PyCondition.not_none(start,
                                                                                     "start") and PyCondition.not_none(
            end, "end")
        self._log.info(f"Generating PositionStatusReport for {self.id}...")
        report_list: List[TradeReport] = []
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
            for bet in bet_history.bets:
                if bet.status not in [BetStatus.ACCEPTED]:
                    # if bet is not settled, skip
                    continue
                self._log.info(f"Processing bet: {bet}")
                venue_order_id: VenueOrderId = VenueOrderId(bet.reference_id)
                # if instrument_id is None:  # no instrument_id specified, we must query the cache
                client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                # we can query the positions, using the client_order_id derived from the venue_order_id
                if client_order_id is None:
                    self._log.warning(
                        f"Attempting to query order that does not exist in the cache, Venue Order ID: {venue_order_id}",
                    )
                    continue
                # TODO: decide if we want to use the cache to query the position or dynamically query the endpoint to generate PositionStatusReport

                # position: Position = self._cache.position_for_order(client_order_id)
                # if position is None:
                #     self._log.warning(
                #         f"Attempting to query position that does not exist in the cache, Client Order ID: {client_order_id}",
                #     )
                #     continue
                cached_order = self._cache.order(client_order_id)
                report = cb_bet_to_position_report(
                    order=cached_order,
                    account_id=self.account_id if self.account_id is not None
                    else await self.set_account_id(account_id=None),
                    instrument_id=instrument_id,
                    bet_response=bet,
                    ts_init=self._clock.timestamp_ns(),
                    venue_order_id=venue_order_id,
                    report_id=UUID4(),
                    client_order_id=client_order_id,
                )
                if report is not None:
                    self._log.debug(f"Received {report}.")
                    report_list.append(report)
        else:
            # no time-range is specified, we must construct the report from the bet_status and/or cache
            # we're interested in getting all the positions for a given instrument_id regardless of time
            # use the instrument_id to query the cache for client_order_ids
            unique_client_ids: set[ClientOrderId] = self._cache.client_order_ids(venue=CLOUDBET_VENUE,
                                                                                 instrument_id=instrument_id)

            for client_order_id in unique_client_ids:
                cached_order: Order = self._cache.order(
                    client_order_id)
                venue_order_id: VenueOrderId = cached_order.venue_order_id
                # use the venue_order_id to query the bet_status endpoint
                bet_status: GetBetResponse = await self._client.get_bet_status(venue_order_id.value)  # pass str

                # TODO: decide if we want to use the cache to query the position or dynamically query the endpoint to generate PositionStatusReport

                # position: Position = self._cache.position_for_order(client_order_id)
                # if position is None:
                #     self._log.warning(
                #         f"Attempting to query position that does not exist in the cache, Client Order ID: {client_order_id}",
                #     )
                #     continue
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

    # -- COMMAND HANDLERS -------------------------------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        self._log.debug(f"Received submit_order {command}")
        self.generate_order_submitted(
            instrument_id=command.instrument_id,
            strategy_id=command.strategy_id,
            client_order_id=command.order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        self._log.debug("Generated _generate_order_submitted")

        instrument: CryptoBettingInstrument = self._cache.instrument(command.instrument_id)
        PyCondition.not_none(instrument, "instrument")
        PyCondition.true(command.order.has_price)  # check OrderType has price, else we can't trade
        # PyCondition.type(command, LimitOrder) possible replacement for has price check and validates parametre type
        client_order_id = command.order.client_order_id

        # prepare data for client place bet
        market_url = instrument.market_name + '/' + instrument.outcome + '?' + instrument.params if instrument.params is not None else instrument.market_name + '/' + instrument.outcome
        price: float = command.order.price.as_double()
        # TODO: handle other types of SelectionSide eg.yes/no; odd/even market
        side = SelectionSide.BACK if command.order.is_buy else SelectionSide.LAY  # for now optimistically assume we only trade BACK/LAY markets
        stake: float = Order.quantity.as_double()  # test if as_decimal or to_str if more reliable than as_double
        try:
            place_bet_response: GetBetResponse = await self._client.place_bets(
                event_id=instrument.event_id,
                market_url=market_url,
                price=price,  # assumes Order has price eg. Limit Order
                side=side,
                stake=stake,
            )
        except Exception as e:
            if isinstance(CloudbetAPIError):
                await self.on_api_exception(error=e)
            self._log.warning(f"Submit failed: {e}")
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                reason=place_bet_response.status.value if place_bet_response.status else "client error/exception",
                ts_event=self._clock.timestamp_ns(),
            )
            return  # end execution
        self._log.debug(f"result={place_bet_response}")
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
