import asyncio
import hashlib
from collections import defaultdict
from typing import Optional, Any, List, Union

import pandas as pd
from nautilus_trader.core.rust.model import ContingencyType, OrderStatus
from nautilus_trader.model.orders.base import Decimal

from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.core import uuid

from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import QueryOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.execution.reports import TradeReport

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import GetAccountCurrencies, GetAccountBalance, SelectionSide, \
    GetBetResponse, BetStatus
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.adapters.cloudbet.sockets import CloudbetStreamClient
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import VenueOrderId

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.adapters.betfair.client.core import BetfairClient
from nautilus_trader.adapters.betfair.client.exceptions import BetfairAPIError
from nautilus_trader.adapters.betfair.common import B2N_ORDER_STREAM_SIDE
from nautilus_trader.adapters.betfair.common import BETFAIR_VENUE
from nautilus_trader.adapters.betfair.orderbook import betfair_float_to_price
from nautilus_trader.adapters.betfair.orderbook import betfair_float_to_quantity
from nautilus_trader.adapters.betfair.parsing.common import betfair_instrument_id
from nautilus_trader.adapters.betfair.parsing.requests import bet_to_order_status_report
from nautilus_trader.adapters.betfair.parsing.requests import betfair_account_to_account_state
from nautilus_trader.adapters.betfair.parsing.requests import order_cancel_all_to_betfair
from nautilus_trader.adapters.betfair.parsing.requests import order_cancel_to_betfair
from nautilus_trader.adapters.betfair.parsing.requests import order_submit_to_betfair
from nautilus_trader.adapters.betfair.parsing.requests import order_update_to_betfair
from nautilus_trader.adapters.betfair.parsing.requests import parse_handicap
from nautilus_trader.adapters.betfair.providers import BetfairInstrumentProvider
from nautilus_trader.adapters.betfair.sockets import BetfairOrderStreamClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.datetime import millis_to_nanos
from nautilus_trader.core.datetime import nanos_to_secs
from nautilus_trader.core.datetime import secs_to_nanos
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.execution.reports import TradeReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currency import Currency
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money, AccountBalance, Quantity

from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orders import Order
from nautilus_trader.msgbus.bus import MessageBus


# The 'pragma: no cover' comment excludes a method from test coverage.
# https://coverage.readthedocs.io/en/coverage-4.3.3/excluding.html
# The reason for their use is to reduce redundant/needless tests which simply
# assert that a `NotImplementedError` is raised when calling abstract methods.
# These tests are expensive to maintain (as they must be kept in line with any
# refactorings), and offer little to no benefit in return. However, the intention
# is for all method implementations to be fully covered by tests.

# *** THESE PRAGMA: NO COVER COMMENTS MUST BE REMOVED IN ANY IMPLEMENTATION. ***


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
        # self._set_account_id(AccountId(f"{BETFAIR_VENUE}-001"))
        # AccountFactory.register_calculated_account(BETFAIR_VENUE.value)

    @property
    def instrument_provider(self) -> BetfairInstrumentProvider:
        return self._instrument_provider

    # -- CONNECTION HANDLERS ----------------------------------------------------------------------
    async def _connect(self) -> None:
        if self._client.connected is False:
            await self._client.connect()
            self._log.info("Creating a new session...")
            self._log.info("Cloudbet connect successful.", LogColor.GREEN)
        aws = [
            self.stream.connect(),
            self.connection_account_state(),
        ]
        await asyncio.gather(*aws)
        self.create_task(self.watch_stream())

    def _disconnect(self) -> None:
        # Close socket
        self._log.info("Closing streaming socket...")
        await self.stream.disconnect()

        # Ensure client closed
        self._log.info("Closing CloudbetClient...")
        await self._client.disconnect()

    def reset(self) -> None:
        # TODO: implement some clean up logic, eg reset/recalculate states
        # pass
        raise NotImplementedError("method must be implemented in the subclass")  # pragma: no cover

    def dispose(self) -> None:
        # TODO: implement some clean up logic, eg release resources like stream client
        # pass
        raise NotImplementedError("method must be implemented in the subclass")  # pragma: no cover

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
        # TODO: add a method to calculate "virtual" balance on initilisation
        account_uuid: uuid = await self._client.login().uuid  # acces  the uuid field from the GetAccountInfoResponse
        account_details: GetAccountCurrencies = await self._client.get_account_currencies()  # iterable string
        account_balances: List[AccountBalance] = []
        timestamp = self._clock.timestamp_ns()
        for currency in account_details:
            currency_balance: GetAccountBalance = await self._client.get_balances(currency)
            # strict, will attempt to create a new currency if "currency" is not present in nautilius currency listtyped_currency
            typed_currency: Currency = Currency.from_str(currency, strict=False)
            account_balances.append(
                AccountBalance(
                    total=Money(currency_balance.amount, typed_currency),
                    locked=Money(0, typed_currency),  # Cloudbet doesn't monitor "locked" funds / active bets
                    free=Money(currency_balance.amount, typed_currency),  # on Cloudbet total === free
                )
            )
        account_state = AccountState(
            account_id=AccountId(f"{CLOUDBET_VENUE.value}-{account_uuid}"),
            account_type=AccountType.BETTING,
            base_currency=self._client.currency,
            # in general, base currency is the only currency that we should use to trade.
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
        ValueError
            If both the `client_order_id` and `venue_order_id` are ``None``.

        """
        PyCondition.not_none(client_order_id, "client_order_id") # tres important
        PyCondition.not_none(venue_order_id, "venue_order_id")
        existing_order : Order = self._cache.order(client_order_id)
        self._log.debug(f"Found order in the cache. Client Order id: {client_order_id}")

        if existing_order is None:
            self._log.warning(
                f"Attempting to query order that does not exist in the cache, Client Order ID: {client_order_id}",
            )
            return None
        # check cloudbet for order and bet response
        try:
            bet_status_response : GetBetResponse = await self._client.get_bet_status(venue_order_id)
        except Exception as e: # TODO: handle exceptions gracefully
            self._log.error(f"Could not fetch bet status from Cloudbet:", bet_status_response)
            return None
        self._log.debug(f"Generating Order Status Report for order {client_order_id}")
        report = bet_to_order_status_report(
            order=existing_order,
            account_id=self._account_id,
            instrument_id=instrument_id,
            bet_response=bet_status_response,
            ts_init=self._clock.timestamp_ns(),
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            report_id=UUID4(),
        )
        return report

    async def generate_order_status_reports(
        self,
        instrument_id: InstrumentId = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        open_only: bool = False,
    ) -> list[OrderStatusReport]:
        raise NotImplementedError("method must be implemented in the subclass")  # pragma: no cover

    async def generate_trade_reports(
        self,
        instrument_id: InstrumentId = None,
        venue_order_id: VenueOrderId = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> list[TradeReport]:
        raise NotImplementedError("method must be implemented in the subclass")  # pragma: no cover

    async def generate_position_status_reports(
        self,
        instrument_id: InstrumentId = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> list[PositionStatusReport]:
        raise NotImplementedError("method must be implemented in the subclass")  # pragma: no cover

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
        PyCondition.true(command.order.has_price()) # check OrderType has price, else we can't trade
        # PyCondition.type(command, LimitOrder) possible replacement for has price check and validates parametre type
        client_order_id = command.order.client_order_id

        # prepare data for client place bet
        market_url = instrument.market_name + '/' + instrument.outcome + '?' + instrument.params if instrument.params is not None else instrument.market_name + '/' + instrument.outcome
        price: float = command.order.price.as_double()
        # TODO: handle other types of SelectionSide eg.yes/no; odd/even market
        side = SelectionSide.BACK if command.order.is_buy else SelectionSide.LAY  # for now optimistically assume we only trade BACK/LAY markets
        stake: float = Order.quantity.as_double()  # test if as_decimal or to_str if more reliable than as_double
        try:
            place_bet_response : GetBetResponse = await self._client.place_bets(
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
            return # end execution
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
        raise NotImplementedError("submitting multiple orders simulataneously isn't supported on Cloubet")  # pragma: no cover

    async def _modify_order(self, command: ModifyOrder) -> None:
        # TODO : message the cloudbet team about resending a BetRequest with the same referenceID
        raise NotImplementedError("submitting multiple orders simulataneously isn't supported on Cloubet")  # pragma: no cover


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
