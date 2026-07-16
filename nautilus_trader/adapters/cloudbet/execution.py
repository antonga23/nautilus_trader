# ruff: noqa: C901, D213, D401, D406, D407, D409, D410, D411, F401, F541, F841, PIE790, RUF010, UP006, UP007, UP035, UP045
import asyncio
import uuid
from typing import Optional, Any, List, Union, Set

import pandas as pd
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.rust.model import LiquiditySide
from nautilus_trader.core.rust.model import OrderSide
from nautilus_trader.core.rust.model import OrderStatus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.execution.reports import TradeReport
from nautilus_trader.model.currencies import PLAY_EUR
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import AccountId, PositionId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money, AccountBalance, Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.msgbus.bus import MessageBus

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.adapters.cloudbet.client.schema import (
    AcceptPriceChange,
    GetAccountCurrencies,
    GetAccountBalance,
    SelectionSide,
    GetBetResponse,
    BetStatus,
    GetBetHistoryResponse,
    GetAccountInfoResponse,
)
from nautilus_trader.adapters.cloudbet.client.util import (
    bet_to_trade_report,
    cb_bet_to_order_status_report,
    datetime_to_cloudbet_timestamp,
    cb_bet_to_position_report,
)
from nautilus_trader.adapters.betting.common.settlement import BET_SETTLEMENTS_TOPIC
from nautilus_trader.adapters.betting.common.settlement import BetSettlement
from nautilus_trader.adapters.betting.common.settlement import SettlementResult
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.adapters.cloudbet.sockets import CloudbetStreamClient
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orders import Order


# Cloudbet bet statuses that mean the stake is matched (money is live at the venue): a matched
# straight bet plus every settled outcome. Any of these => the leg is real exposure, so it must be
# resolved to a fill and can never be treated as a cancel.
_CLOUDBET_MATCHED_STATUSES = frozenset(
    {
        BetStatus.ACCEPTED,
        BetStatus.PARTIAL,
        BetStatus.WIN,
        BetStatus.LOSS,
        BetStatus.HALF_WIN,
        BetStatus.HALF_LOSS,
        BetStatus.PUSH,
        BetStatus.COMPLETED,
    },
)

# Cloudbet grading outcomes that finalize a bet, mapped to the venue-neutral settlement result the
# strategy books P&L from. Cloudbet's half-lines (quarter-ball Asian handicaps) settle half the
# stake at odds and refund the other half; these now map to the dedicated HALF_WON / HALF_LOST
# results so the strategy realizes exactly half the full-win / full-loss payoff per leg rather than
# rounding a half to its dominant side. PUSH refunds the full stake (no P&L) and maps to its own
# PUSH result rather than collapsing into VOID. The signed venue figure still rides on
# ``settle_value`` for reconciliation. COMPLETED / ACCEPTED / PARTIAL are matched-but-ungraded and
# are not settlements.
_CLOUDBET_SETTLEMENT_RESULTS: dict[BetStatus, SettlementResult] = {
    BetStatus.WIN: SettlementResult.WON,
    BetStatus.HALF_WIN: SettlementResult.HALF_WON,
    BetStatus.LOSS: SettlementResult.LOST,
    BetStatus.HALF_LOSS: SettlementResult.HALF_LOST,
    BetStatus.PUSH: SettlementResult.PUSH,
}


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
        config: Optional[LiveExecClientConfig] = None,
        account_id: Optional[
            AccountId
        ] = None,  # default to None as this should be fetched from the venue
    ) -> None:
        resolved_base_currency = base_currency or PLAY_EUR
        super().__init__(
            loop=loop,
            client_id=ClientId(CLOUDBET_VENUE.value),
            venue=CLOUDBET_VENUE,
            oms_type=OmsType.HEDGING,
            # HEDGING => multiple positions per instrument. NETTING => only one position per instrument
            account_type=AccountType.BETTING,
            base_currency=resolved_base_currency,
            instrument_provider=instrument_provider
            or CloudbetInstrumentProvider(client=client, logger=logger),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )

        self._instrument_provider: CloudbetInstrumentProvider = instrument_provider
        self._config = config
        self._set_account_id(account_id or AccountId(f"{CLOUDBET_VENUE.value}-001"))
        # an asyncio Task to watch the stream
        # self._watch_stream_task: Optional[asyncio.Task] = None
        self._client: CloudbetClient = client
        self.stream: CloudbetStreamClient = CloudbetStreamClient(
            client=self._client,
            logger=logger,
            message_handler=self.handle_order_stream_update,
        )

        self.venue_order_id_to_client_order_id: dict[VenueOrderId, ClientOrderId] = {}
        # Client order IDs already filled; guards against double-emitting a fill for the same
        # bet across the submit path, pending-acceptance reconciliation, and cancel resolution.
        self._filled_client_order_ids: Set[ClientOrderId] = set()
        # Orders whose settlement was already published, so grading never re-emits.
        self._settled_client_order_ids: Set[ClientOrderId] = set()
        # Matched stake and currency code per filled order, recorded at fill time so locked-funds
        # modeling never re-queries the venue per account-state tick.
        self._matched_stakes: dict[ClientOrderId, tuple[float, str]] = {}
        self._settlement_poll_task: Optional[asyncio.Task] = None
        self._account_state_task: Optional[asyncio.Task] = None
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
            super()._set_account_id(
                AccountId(f"{CLOUDBET_VENUE.value}-{account_response.uuid.split('-')[0]}")
            )
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
            if self._client.connected is False:  # create HTTP sessions to allow networking calls
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
                results: List[Union[None, Exception]] = await asyncio.gather(
                    *aws, return_exceptions=True
                )
                for result in results:
                    if isinstance(result, Exception):
                        self._log.error(f"Task encountered an exception: {result}")
                    else:
                        self._log.debug(
                            f"Connection initialisation tasks completed successfully: {result}"
                        )
            # TODO: replace _watch_stream_task with an attribute that is set on successful StreamClient connection
            # if self._watch_stream_task is None:
            #     self._log.info("Starting stream watch task...")
            #     self._watch_stream_task = asyncio.create_task(self.watch_stream())

            # Cloudbet exposes no push feed for grading, so settlements are reconciled by polling
            # the bet-status endpoint for tracked orders (mirrors the SXBET settlement poll).
            if self._settlement_poll_task is None:
                self._settlement_poll_task = asyncio.create_task(self._settlement_poll_loop())

            # Periodic real-money balance/caps visibility (mirrors the SXBET account-state poll).
            if self._account_state_task is None:
                self._account_state_task = asyncio.create_task(self._account_state_loop())
        except Exception as e:
            self._log.error(f"An error occurred during the connection process: {str(e)}")

    async def _disconnect(self) -> None:
        # Stop the settlement poll
        if self._settlement_poll_task is not None and not self._settlement_poll_task.done():
            self._settlement_poll_task.cancel()
        self._settlement_poll_task = None

        # Stop the account-state poll
        if self._account_state_task is not None and not self._account_state_task.done():
            self._account_state_task.cancel()
        self._account_state_task = None

        # Close socket
        self._log.info("Closing streaming socket...")
        await self.stream.disconnect()

        # Ensure client closed
        self._log.info("Closing CloudbetClient sessions...")
        await self._client.disconnect()

    def reset(self) -> None:
        self.venue_order_id_to_client_order_id.clear()
        self._filled_client_order_ids.clear()
        self._settled_client_order_ids.clear()
        self._matched_stakes.clear()
        if self._account_state_task is not None and not self._account_state_task.done():
            self._account_state_task.cancel()
        self._account_state_task = None

    def dispose(self) -> None:
        async def close_resources() -> None:
            await self._disconnect()

        try:
            if self._loop.is_closed():
                return
            if self._loop.is_running():
                self._loop.create_task(close_resources())
            else:
                self._loop.run_until_complete(close_resources())
        except Exception as e:  # pragma: no cover - defensive shutdown path
            self._log.warning(f"Error disposing Cloudbet execution resources: {e}")

    async def watch_stream(self) -> None:
        """Ensure socket stream is connected"""
        while not self.stream.is_stopping:
            try:
                if not self.stream.is_connected:
                    await self.stream.connect()
                await asyncio.sleep(1)
            except Exception as e:
                self._log.error(f"Encountered an error while watching the stream: {e}")

    # -- SETTLEMENT -------------------------------------------------------------------------------
    async def _settlement_poll_loop(self) -> None:
        interval = float(getattr(self._config, "settlement_poll_interval_secs", 30.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._reconcile_settlements()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log.error(f"Cloudbet settlement reconciliation failed: {e}")

    async def _reconcile_settlements(self) -> None:
        """
        Poll graded Cloudbet bets and publish one ``BetSettlement`` per graded order.

        Cross-venue arbitrage pairs realize P&L venue-agnostically: each execution client
        publishes a ``BetSettlement`` for every graded leg on ``BET_SETTLEMENTS_TOPIC`` and the
        strategy books the pair once every leg has graded. Cloudbet has no push feed for grading,
        so this reads the bet-status endpoint for each tracked (matched) order still awaiting
        settlement and maps its terminal Cloudbet status to a venue-neutral settlement result
        (WON / LOST / VOID / HALF_WON / HALF_LOST / PUSH).

        Idempotent: a settled order is tracked in ``_settled_client_order_ids`` and never re-emits,
        across polls and across repeated grading reads. A grading pays out or refunds the wallet, so
        the account state is refreshed once after any settlement is published.

        """
        pending = {
            venue_order_id: client_order_id
            for venue_order_id, client_order_id in self.venue_order_id_to_client_order_id.items()
            if client_order_id not in self._settled_client_order_ids
        }
        if not pending:
            return

        emitted = 0
        for venue_order_id, client_order_id in pending.items():
            if client_order_id in self._settled_client_order_ids:
                continue
            try:
                bet_response: GetBetResponse = await self._client.get_bet_status(
                    venue_order_id.value,
                )
            except Exception as e:
                self._log.error(
                    f"Could not fetch Cloudbet bet status for settlement "
                    f"(venue_order_id={venue_order_id}): {e}",
                )
                continue

            result = self._settlement_result(bet_response)
            if result is None:
                continue

            self._settled_client_order_ids.add(client_order_id)
            self._publish_settlement(client_order_id, result, bet_response)
            emitted += 1

        if emitted:
            try:
                await self.connection_account_state()
            except Exception as e:  # pragma: no cover - best-effort balance refresh
                self._log.warning(f"Account state refresh after settlement failed: {e}")

    @staticmethod
    def _settlement_result(bet_response: GetBetResponse) -> Optional[SettlementResult]:
        """
        Derive the venue-neutral settlement result from a graded Cloudbet bet (WON / LOST / VOID /
        HALF_WON / HALF_LOST / PUSH), or ``None`` if not yet graded.
        """
        return _CLOUDBET_SETTLEMENT_RESULTS.get(bet_response.status)

    def _publish_settlement(
        self,
        client_order_id: ClientOrderId,
        result: SettlementResult,
        bet_response: GetBetResponse,
    ) -> None:
        order: Optional[Order] = self._cache.order(client_order_id)
        # winLoss is Cloudbet's signed net P&L for the bet; it is passed through untouched as a
        # diagnostic (realized P&L is booked by the strategy from tracked fills, never from this).
        settle_value = self._as_float(bet_response.win_loss)
        if settle_value is None:
            settle_value = self._as_float(bet_response.return_amount)
        settlement = BetSettlement(
            venue=CLOUDBET_VENUE.value,
            client_order_id=client_order_id.value,
            instrument_id=str(order.instrument_id) if order is not None else None,
            result=result,
            settle_value=settle_value,
            ts_event=self._clock.timestamp_ns(),
        )
        self._log.info(
            f"Cloudbet bet settled: {client_order_id} {result} "
            f"(status={bet_response.status.value}, winLoss={bet_response.win_loss})",
        )
        self._msgbus.publish(topic=BET_SETTLEMENTS_TOPIC, msg=settlement)

    @staticmethod
    def _as_float(value: Union[float, str, None]) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # -- ERROR HANDLING ---------------------------------------------------------------------------
    async def on_api_exception(self, error: Exception) -> None:
        # TODO: implement different handlers for CloudbetAPI exceptions eg. duplicate request => use a new UUID
        pass
        # if error.kind == "INVALID_SESSION_INFORMATION":
        #     # Session is invalid, need to reconnect
        #     self._log.warning("Invalid session error, reconnecting..")
        #     await self._client.disconnect()
        #     await self._connect()
        #     self._log.info("Reconnected.")

    # -- ACCOUNT HANDLERS -------------------------------------------------------------------------

    async def _account_state_loop(self) -> None:
        interval = float(getattr(self._config, "account_state_interval_secs", 30.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.connection_account_state()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A failed refresh leaves the last-known account state standing.
                self._log.error(f"Cloudbet account state refresh failed: {e}")

    def _open_stakes_by_currency(self) -> dict[str, float]:
        """
        Sum the matched stakes of open (matched but not yet settled) bets per currency code.

        Reads only state the client already tracks (stakes recorded at fill time, settlements
        from the settlement poll) — no venue queries per tick.
        """
        locked: dict[str, float] = {}
        for client_order_id, (stake, code) in self._matched_stakes.items():
            if client_order_id in self._settled_client_order_ids:
                continue
            locked[code] = locked.get(code, 0.0) + stake
        return locked

    async def connection_account_state(self) -> None:
        """
        Retrieves the account state and sends it to the server.
        """
        try:
            account_response: GetAccountInfoResponse = await self._client.login()
            account_id = AccountId(f"{CLOUDBET_VENUE.value}-{account_response.uuid.split('-')[0]}")
            self._set_account_id(account_id)
            account_details: GetAccountCurrencies = (
                await self._client.get_account_currencies()
            )  # iterable string
            account_balances: List[AccountBalance] = []
            timestamp = self._clock.timestamp_ns()
            locked_by_currency = self._open_stakes_by_currency()

            for currency in account_details.currencies:
                # TODO: use asyncio.gather` to make concurrent requests for each currency balance can significantly improve the performance of the `connection_account_state` method
                try:
                    currency_balance: GetAccountBalance = await self._client.get_balances(currency)
                    typed_currency: Currency = Currency.from_str(currency)
                    total = Money(currency_balance.amount, typed_currency)
                    locked = Money(
                        locked_by_currency.get(typed_currency.code, 0.0),
                        typed_currency,
                    )
                    if locked > total:
                        # The venue-reported total is authoritative; floor free at 0.
                        self._log.warning(
                            f"Cloudbet open stakes {locked} exceed the reported total {total}; "
                            f"flooring free balance at 0",
                        )
                        locked = total
                    account_balances.append(
                        AccountBalance(
                            total=total,
                            locked=locked,
                            free=Money(total.as_decimal() - locked.as_decimal(), typed_currency),
                        )
                    )
                except Exception as e:
                    self._log.error(
                        f"An error occurred while getting balances for currency {currency}: {str(e)}"
                    )
                    continue
            # if there are no account balances, return None
            # TODO: test if this is ever reached
            if len(account_balances) == 0:
                return None
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
            self._log.error(
                f"An error occurred during the connection_account_state process: {str(e)}"
            )
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
        instrument_id: InstrumentId | GenerateOrderStatusReport,
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
        if isinstance(instrument_id, GenerateOrderStatusReport):
            command = instrument_id
            instrument_id = command.instrument_id
            client_order_id = command.client_order_id
            venue_order_id = command.venue_order_id

        assert client_order_id is not None or venue_order_id is not None
        # check cloudbet for order ID and bet response
        existing_order: Union[Order, None] = None
        if venue_order_id is not None:
            try:
                bet_status_response: GetBetResponse = await self._client.get_bet_status(
                    venue_order_id
                )
            except Exception as e:  # TODO: handle exceptions gracefully
                self._log.error(f"Could not fetch bet status from Cloudbet: {str(e)}")
                # we must query the cache => as the order may not have reached the exchange yet or exchange is down
                if client_order_id is not None:
                    self._log.debug(f"Attempting to query the cache for order {client_order_id}")
                    existing_order: Order = self._cache.order(client_order_id)
                    if existing_order is not None:
                        self._log.debug(
                            f"Found order in the cache. Client Order id: {client_order_id}"
                        )
                        self._log.debug(
                            f"Generating Order Status Report for order {client_order_id}"
                        )
                        report = cb_bet_to_order_status_report(
                            order=existing_order,
                            account_id=self.account_id
                            if self.account_id is not None
                            else await self.set_account_id(account_id=None),
                            instrument_id=instrument_id,
                            bet_response=None,  # for cached Orders we don't need to query the venue
                            ts_init=self._clock.timestamp_ns(),
                            client_order_id=client_order_id,
                            venue_order_id=venue_order_id,
                            report_id=UUID4(),
                        )
                        return report
                    else:
                        self._log.warning(
                            f"Attempting to query order that does not exist in the cache, Client Order ID: {client_order_id}"
                        )
                        return None
                else:
                    self._log.debug(
                        f"Unable to fetch Order details from the venue {self.venue} and no Client Order ID was provided",
                    )
                    return None

            report = cb_bet_to_order_status_report(
                order=existing_order,
                account_id=self.account_id
                if self.account_id is not None
                else await self.set_account_id(account_id=None),
                instrument_id=instrument_id,
                bet_response=bet_status_response,
                ts_init=self._clock.timestamp_ns(),
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                report_id=UUID4(),
            )
            return report
        elif (
            client_order_id is not None and venue_order_id is None
        ):  # we must query the cache in cases where exchange is unavailable => order must already have been submitted, otherwise no venue_order_id exists
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
                self._log.debug(
                    f"Generating Order Status Report for order Client Order ID: {client_order_id}"
                )
            report = cb_bet_to_order_status_report(
                order=existing_order,
                account_id=self.account_id
                if self.account_id is not None
                else await self.set_account_id(account_id=None),
                instrument_id=instrument_id,
                bet_response=None,  # for cached Orders we don't need to query the venue
                ts_init=self._clock.timestamp_ns(),
                client_order_id=client_order_id,
                venue_order_id=cached_venue_order_id,  # TODO: this should cause a runtime error as cached_venue_order_id is None
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
        instrument_id: InstrumentId | GenerateOrderStatusReports = None,
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
        if isinstance(instrument_id, GenerateOrderStatusReports):
            command = instrument_id
            instrument_id = command.instrument_id
            start = command.start
            end = command.end
            open_only = command.open_only

        self._log.info(f"Generating OrderStatusReports for {self.id}...")
        report_list: List[OrderStatusReport] = []
        if (start is None) != (end is None):
            self._log.debug(
                "Cannot generate Cloudbet order status reports with a partial time range",
            )
            return report_list

        if instrument_id is None and start is None and end is None:
            self._log.debug(
                "No Cloudbet order status filters supplied; returning no reports",
            )
            return report_list

        # if a time-range is specified, we explicitly rely on the venue bet_history endpoint
        if start and end:
            start_date: str = datetime_to_cloudbet_timestamp(start)
            end_date: str = datetime_to_cloudbet_timestamp(end)
            try:
                bet_history: GetBetHistoryResponse = await self._client.get_bet_history(
                    start_date, end_date
                )
                self._log.info(f"Received bet history: {bet_history}")
            except Exception as e:  # TODO: handle exceptions gracefully
                self._log.error(f"Could not fetch bet history from Cloudbet: {e}")
                return report_list
            for bet in bet_history.bets:
                report: Optional[OrderStatusReport] = None
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
                        client_order_id
                    )  # no need to assert not None, an Order must have a client_order_id on init
                    if cached_order is None:
                        self._log.warning(
                            f"Attempting to query order that does not exist in the cache, Client Order ID: {client_order_id}",
                        )
                        continue
                    instrument_id: InstrumentId = cached_order.instrument_id

                if open_only is False:  # we don't care about the order status
                    report = await self.generate_order_status_report(
                        instrument_id=instrument_id,
                        client_order_id=client_order_id,
                        venue_order_id=venue_order_id,
                    )
                else:
                    cached_order: Order = self._cache.order(client_order_id)
                    if cached_order is None:
                        self._log.warning(
                            f"Attempting to query order that does not exist in the cache, Client Order ID: {client_order_id}",
                        )
                        continue
                    if cached_order.is_open or bet.status == bet.status.PENDING_ACCEPTANCE:
                        report = cb_bet_to_order_status_report(
                            order=cached_order,
                            account_id=self.account_id
                            if self.account_id is not None
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
            unique_client_ids: set[ClientOrderId] = self._cache.client_order_ids(
                venue=CLOUDBET_VENUE, instrument_id=instrument_id
            )
            report: Optional[OrderStatusReport] = None
            for client_order_id in unique_client_ids:
                cached_order: Order = self._cache.order(client_order_id)
                if cached_order is None:
                    self._log.warning(
                        f"Attempting to query order that does not exist in the cache, Client Order ID: {client_order_id}",
                    )
                    continue
                venue_order_id: VenueOrderId = cached_order.venue_order_id
                if venue_order_id is None:
                    self._log.warning(
                        f"Unable to generate a Cloudbet report for order without a valid VenueOrderId, Client Order ID: {client_order_id}",
                    )
                    continue
                # use the venue_order_id to query the bet_status endpoint
                try:
                    bet_status: GetBetResponse = await self._client.get_bet_status(
                        venue_order_id.value
                    )
                except Exception as e:
                    self._log.error(f"Could not fetch bet status from Cloudbet: {e}")
                    continue
                if open_only is True:
                    if (
                        cached_order.is_open
                        or bet_status.status == bet_status.status.PENDING_ACCEPTANCE
                    ):
                        report: OrderStatusReport = cb_bet_to_order_status_report(
                            order=cached_order,
                            account_id=self.account_id
                            if self.account_id is not None
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
                        account_id=self.account_id
                        if self.account_id is not None
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

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        return await self.generate_trade_reports(
            instrument_id=command.instrument_id,
            venue_order_id=command.venue_order_id,
            start=command.start,
            end=command.end,
        )

    async def generate_trade_reports(
        self,
        instrument_id: InstrumentId = None,
        venue_order_id: VenueOrderId = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> list[TradeReport]:
        """Generate a list of TradeReports for the given parameters

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
        self._log.info(f"Generating TradeReports for {self.id}...")
        report_list: List[TradeReport] = []
        if (start is None) != (end is None):
            self._log.debug(
                "Cannot generate Cloudbet trade reports with a partial time range",
            )
            return report_list

        if instrument_id is None and venue_order_id is None and start is None and end is None:
            self._log.debug("No Cloudbet trade report filters supplied; returning no reports")
            return report_list

        # if a time-range is specified, we explicitly rely on the venue bet_history endpoint
        if start and end:
            start_date: str = datetime_to_cloudbet_timestamp(start)
            end_date: str = datetime_to_cloudbet_timestamp(end)
            try:
                bet_history: GetBetHistoryResponse = await self._client.get_bet_history(
                    start_date, end_date
                )
                self._log.info(f"Received bet history: {bet_history}")
            except Exception as e:  # TODO: handle exceptions gracefully
                self._log.error(f"Could not fetch bet history from Cloudbet:", e)
                return []
            for bet in bet_history.bets:
                if bet.status not in [
                    BetStatus.ACCEPTED,
                    BetStatus.WIN,
                    BetStatus.LOSS,
                    BetStatus.HALF_WIN,
                    BetStatus.HALF_LOSS,
                    BetStatus.PARTIAL,
                    BetStatus.PUSH,
                ]:
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
                        account_id=self.account_id
                        if self.account_id is not None
                        else await self.set_account_id(account_id=None),
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
                        client_order_id
                    )  # no need to assert not None, an Order must have a client_order_id on init
                    if (
                        instrument_id is None
                    ):  # no instrument_id passed in, we must query the cache for the instrument id
                        instrument_id: InstrumentId = cached_order.instrument_id
                    report = bet_to_trade_report(
                        order=cached_order,
                        account_id=self.account_id
                        if self.account_id is not None
                        else await self.set_account_id(account_id=None),
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
                unique_client_ids: set[ClientOrderId] = self._cache.client_order_ids(
                    venue=CLOUDBET_VENUE, instrument_id=instrument_id
                )
                if unique_client_ids is set():
                    self._log.warning(f"No trades found for instrument_id: {instrument_id}")
                    return []
                for client_order_id in unique_client_ids:
                    cached_order: Order = self._cache.order(client_order_id)
                    venue_order_id: VenueOrderId = cached_order.venue_order_id
                    # use the venue_order_id to query the bet_status endpoint
                    try:
                        bet_status: GetBetResponse = await self._client.get_bet_status(
                            venue_order_id.value
                        )  # pass str
                    except Exception as e:
                        self._log.error(f"Could not fetch bet status from Cloudbet: {e}")
                        # unable to retrieve bet status, so we check the cached order status
                        if cached_order.status in [
                            OrderStatus.ACCEPTED,
                            OrderStatus.FILLED,
                            OrderStatus.PARTIALLY_FILLED,
                        ]:
                            report: TradeReport = bet_to_trade_report(
                                order=cached_order,
                                account_id=self.account_id
                                if self.account_id is not None
                                else await self.set_account_id(account_id=None),
                                instrument_id=instrument_id,
                                bet_response=None,  # exception encountered, we will use the order to build TradeReport
                                ts_init=self._clock.timestamp_ns(),
                                venue_order_id=venue_order_id,
                                report_id=UUID4(),
                                client_order_id=client_order_id,
                                commission_currency=self._cache.instrument(
                                    cached_order.instrument_id,
                                ).quote_currency,
                            )
                            report_list.append(report)
                        continue
                    if bet_status.status not in [
                        BetStatus.ACCEPTED,
                        BetStatus.WIN,
                        BetStatus.LOSS,
                        BetStatus.HALF_WIN,
                        BetStatus.HALF_LOSS,
                        BetStatus.PARTIAL,
                        BetStatus.PUSH,
                    ]:
                        # if bet is not settled, skip
                        continue
                    report: TradeReport = bet_to_trade_report(
                        order=cached_order,
                        account_id=self.account_id
                        if self.account_id is not None
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
                    bet_response: GetBetResponse = await self._client.get_bet_status(
                        venue_order_id.value
                    )  # pass str
                except Exception as e:
                    self._log.error(f"Could not fetch bet status from Cloudbet: {e}")
                    client_order_id: ClientOrderId = self._cache.client_order_id(venue_order_id)
                    if client_order_id is not None:
                        cached_order: Order = self._cache.order(client_order_id)
                        instrument_id: InstrumentId = cached_order.instrument_id
                        if cached_order.status in [
                            OrderStatus.ACCEPTED,
                            OrderStatus.FILLED,
                            OrderStatus.PARTIALLY_FILLED,
                        ]:
                            report: TradeReport = bet_to_trade_report(
                                order=cached_order,
                                account_id=self.account_id
                                if self.account_id is not None
                                else await self.set_account_id(account_id=None),
                                instrument_id=instrument_id,
                                bet_response=None,  # exception encountered, we will use the order to build TradeReport
                                ts_init=self._clock.timestamp_ns(),
                                venue_order_id=venue_order_id,
                                report_id=UUID4(),
                                client_order_id=client_order_id,
                                commission_currency=self._cache.instrument(
                                    cached_order.instrument_id,
                                ).quote_currency,
                            )
                            report_list.append(report)
                    self._log.debug(
                        f"Could not fetch order from Cache: VenueOrderID{venue_order_id.value}"
                    )
                    return []
                # check bet has been settled
                if bet_response.status not in [
                    BetStatus.ACCEPTED,
                    BetStatus.WIN,
                    BetStatus.LOSS,
                    BetStatus.HALF_WIN,
                    BetStatus.HALF_LOSS,
                    BetStatus.PARTIAL,
                    BetStatus.PUSH,
                ]:
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
                        account_id=self.account_id
                        if self.account_id is not None
                        else await self.set_account_id(account_id=None),
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
        instrument_id: InstrumentId | GeneratePositionStatusReports = None,
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
        if isinstance(instrument_id, GeneratePositionStatusReports):
            command = instrument_id
            instrument_id = command.instrument_id
            start = command.start
            end = command.end

        self._log.info(f"Generating PositionStatusReport for {self.id}...")
        report_list: List[PositionStatusReport] = []
        if (start is None) != (end is None):
            self._log.debug(
                "Cannot generate Cloudbet position status reports with a partial time range",
            )
            return report_list

        if instrument_id is None and start is None and end is None:
            self._log.debug(
                "No Cloudbet position status filters supplied; returning no reports",
            )
            return report_list

        # if a time-range is specified, we explicitly rely on the venue bet_history endpoint
        if start and end:
            start_date: str = datetime_to_cloudbet_timestamp(start)
            end_date: str = datetime_to_cloudbet_timestamp(end)
            try:
                bet_history: GetBetHistoryResponse = await self._client.get_bet_history(
                    start_date, end_date
                )
                self._log.info(f"Received bet history: {bet_history}")
            except Exception as e:  # TODO: handle exceptions gracefully
                self._log.error(f"Could not fetch bet history from Cloudbet:", bet_history)
                return []
            if not bet_history.bets:
                # log no bets were found
                self._log.info(
                    f"No bets were found in the bet history for start date: {start_date} and end date: {end_date}."
                )
                return []
            list_bet_reference_id: List[str] = [
                bet.reference_id  # Extract reference_id from the bet
                for bet in bet_history.bets  # Iterate over each bet in bet_history
                if bet.status
                in [
                    BetStatus.PARTIAL,
                    BetStatus.HALF_LOSS,
                    BetStatus.HALF_WIN,
                    BetStatus.PUSH,
                    BetStatus.LOSS,
                    BetStatus.WIN,
                    BetStatus.ACCEPTED,
                ]
                # Check if bet status is one of the specified statuses
            ]
            if not list_bet_reference_id:
                self._log.info(
                    f"No bets were found in the bet history that meet the bet status criteria for valid positions."
                )
                return []
            if instrument_id is not None:
                # a valid instrument_id has been passed in, we will query the cache for all positions for this instrument
                # we can optimistically assume venue_order_id ~= VenueOrderId(bet.reference_id) for Cloudbet
                positions: list[Position] = self._cache.positions(
                    venue=CLOUDBET_VENUE, instrument_id=instrument_id
                )
                if not positions:
                    return []
                # Convert each bet_reference_id to a VenueOrderId object and store them in a set for faster lookups.
                cb_venue_order_ids: Set[VenueOrderId] = {
                    VenueOrderId(bet_reference_id) for bet_reference_id in list_bet_reference_id
                }

                # Filter the positions:
                # For each position in positions, check if any of its venue_order_ids is present in cb_venue_order_ids.
                # If at least one venue_order_id is found in cb_venue_order_ids, include the position in the filtered list.
                filtered_positions: List[Position] = [
                    position  # Include this position in the filtered list
                    for position in positions  # Iterate over each position in the original list
                    if any(
                        # The 'any' function checks if at least one of the conditions (venue_order_id in cb_venue_order_ids) is True.
                        # If at least one True condition is found, 'any' returns True, causing the current position to be included in the filtered list.
                        venue_order_id
                        in cb_venue_order_ids  # Check if this venue_order_id is in cb_venue_order_ids
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
                    if (client_order_id := self._cache.client_order_id(venue_order_id))
                    is not None  # :=  available in Python >= 3.8
                    # Proceed to get the order only if client_order_id is not None
                ]

                for order in filtered_orders:
                    report = cb_bet_to_position_report(
                        order=order,
                        account_id=self.account_id
                        if self.account_id is not None
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
            else:  # no instrument_id passed in, we must query the cache
                for bet in bet_history.bets:
                    venue_order_id: VenueOrderId = VenueOrderId(
                        bet.reference_id
                    )  # NB: the reference_id is the venue_order_id on cloudbet
                    cached_client_order_id = self._cache.client_order_id(venue_order_id)
                    if cached_client_order_id is None:
                        self._log.warning(
                            f"Unable to determine instrument ID for the bet response. Venue Order ID: {bet.reference_id}",
                        )
                        continue
                    cached_order: Order = self._cache.order(cached_client_order_id)
                    if (
                        cached_order is None
                    ):  # this should never be true, if client_order_id exists then the Order must exist
                        self._log.warning(
                            f"Unable to determine instrument ID from the bet response. Venue Order ID: {bet.reference_id}",
                        )
                        continue
                    # we should have a cached order at this point if we're querying the cache
                    # TODO: add this report to the report list
                    report = cb_bet_to_position_report(
                        order=None,
                        account_id=self.account_id
                        if self.account_id is not None
                        else await self.set_account_id(account_id=None),
                        instrument_id=cached_order.instrument_id,
                        bet_response=bet,
                        ts_init=self._clock.timestamp_ns(),
                        venue_order_id=cached_order.venue_order_id
                        if cached_order.venue_order_id
                        else VenueOrderId(bet.reference_id),
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
            positions: list[Position] = self._cache.positions(
                venue=CLOUDBET_VENUE, instrument_id=instrument_id
            )
            if not positions:
                self._log.warning(f"No positions were found for instrument_id: {instrument_id}.")
                return []

            # # A Position for an Instrument may have  multiple venue_order_ids (unqiue Orders),
            # ideally we want to extract each Order for that Position and use that to build the report
            for position in positions:
                report = cb_bet_to_position_report(
                    order=None,
                    account_id=self.account_id
                    if self.account_id is not None
                    else await self.set_account_id(account_id=None),
                    instrument_id=instrument_id,
                    bet_response=None,
                    ts_init=self._clock.timestamp_ns(),
                    venue_order_id=position.venue_order_ids[-1],  # use only the last venue_order_id
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
        PyCondition.is_true(
            command.order.has_price, "Order must have a price"
        )  # check OrderType has price, else we can't trade
        # PyCondition.type(command, LimitOrder) possible replacement for has price check and validates parametre type
        client_order_id = command.order.client_order_id

        # prepare data for client place bet
        market_url = (
            instrument.market_name + "/" + instrument.outcome + "?" + instrument.params
            if instrument.params is not None
            else instrument.market_name + "/" + instrument.outcome
        )
        price: float = command.order.price.as_double()
        # TODO: handle other types of SelectionSide eg.yes/no; odd/even market
        # check market name, outcome, etc and if it has yes/no use that to extract the side
        side = (
            SelectionSide.BACK if command.order.is_buy else SelectionSide.LAY
        )  # for now optimistically assume we only trade BACK/LAY markets
        stake: float = (
            command.order.quantity.as_double()
        )  # test if as_decimal or to_str if more reliable than as_double
        try:
            self.generate_order_submitted(
                instrument_id=command.instrument_id,
                strategy_id=command.strategy_id,
                client_order_id=command.order.client_order_id,
                ts_event=self._clock.timestamp_ns(),
            )
            self._log.debug("Generated _generate_order_submitted")
            if getattr(self._config, "dry_run", False):
                self._log.info(
                    "Cloudbet dry-run execution enabled; bet request was built but not submitted",
                )
                self.generate_order_rejected(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=client_order_id,
                    reason="dry_run_no_submit",
                    ts_event=self._clock.timestamp_ns(),
                )
                return
            accept_price_change = self._accept_price_change_policy()
            reference_id = str(uuid.uuid4())
            place_bet_response: GetBetResponse = await self._client.place_bets(
                event_id=instrument.event_id,
                market_url=market_url,
                price=price,  # assumes Order has price eg. Limit Order
                side=side,
                stake=stake,
                reference_id=reference_id,
                accept_price_change=accept_price_change,
            )
            place_bet_response = await self._resolve_pending_acceptance(
                reference_id,
                place_bet_response,
            )
            if place_bet_response.status is BetStatus.PENDING_ACCEPTANCE:
                # The poll window is exhausted but the reference is still LIVE at Cloudbet and can
                # match after we stop polling. Re-resolve to the true terminal state before ever
                # trusting a reject; the caller fails toward "still exposed" if this stays pending.
                place_bet_response = await self._reconcile_pending_acceptance(
                    reference_id,
                    place_bet_response,
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
        status = place_bet_response.status
        if status in _CLOUDBET_MATCHED_STATUSES or status is BetStatus.PENDING_ACCEPTANCE:
            # A matched/settled status means the stake is live at Cloudbet (money on the leg);
            # in-play markets can settle inside the poll+reconcile window, so a re-query can surface
            # PARTIAL/COMPLETED/WIN/etc. right here. PENDING_ACCEPTANCE only reaches this branch
            # after reconciliation could not confirm a genuine reject, so the reference may still be
            # live. Both accept the leg (fail toward "still exposed") rather than rejecting a bet
            # that is or could be matched and leaving a naked leg — mirroring _cancel_order's
            # matched-status handling, where the same statuses resolve to a fill (never a cancel).
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
            if status in _CLOUDBET_MATCHED_STATUSES:
                # Matched/settled => real exposure. Emit the fill immediately (idempotent via the
                # _filled_client_order_ids guard, so a later reconcile/cancel query cannot double
                # -fill). PENDING_ACCEPTANCE is left un-filled: it is "still live", not yet matched.
                self._emit_bet_fill(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    bet_response=place_bet_response,
                )
        else:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                reason=status.value if status else "client error/exception",
                ts_event=self._clock.timestamp_ns(),
            )

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        raise NotImplementedError(
            "submitting multiple orders simultaneously isn't supported on Cloudbet"
        )  # pragma: no cover

    async def _modify_order(self, command: ModifyOrder) -> None:
        # TODO : message the cloudbet team about resending a BetRequest with the same referenceID
        raise NotImplementedError(
            "submitting multiple orders simultaneously isn't supported on Cloudbet"
        )  # pragma: no cover

    async def _cancel_order(self, command: CancelOrder) -> None:
        # Cloudbet sportsbook bets are NOT cancelable once matched: core.py exposes no cancel
        # endpoint. Fabricating an OrderCanceled would tell the strategy the leg is gone while
        # real money is on it => an unwind must FLATTEN the exposure, not cancel it. So instead
        # of raising, resolve the order to its true terminal state at the venue.
        client_order_id = command.client_order_id
        venue_order_id = command.venue_order_id
        if venue_order_id is None:
            cached_order: Optional[Order] = self._cache.order(client_order_id)
            venue_order_id = cached_order.venue_order_id if cached_order is not None else None
        if venue_order_id is None:
            # No venue reference exists => the bet was never placed; canceling is safe (no exposure).
            self.generate_order_canceled(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=None,
                ts_event=self._clock.timestamp_ns(),
            )
            return

        try:
            bet_response: GetBetResponse = await self._client.get_bet_status(venue_order_id.value)
        except Exception as e:
            # Fail toward "still exposed": if we cannot confirm the terminal state, never fabricate
            # a cancel — the bet could be live. Leave the order untouched for the next reconcile.
            self._log.warning(
                f"Cloudbet cancel resolution failed for {venue_order_id}; leaving order live: {e!r}",
            )
            return

        status = bet_response.status
        if status in _CLOUDBET_MATCHED_STATUSES:
            # Matched/settled => terminal and live. Resolve to a fill (idempotent), never a cancel.
            self._log.info(
                f"Cloudbet cannot cancel matched bet {venue_order_id}; resolving to fill",
            )
            self._emit_bet_fill(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                bet_response=bet_response,
            )
        elif status is BetStatus.PENDING_ACCEPTANCE:
            # Still resolving at the venue => could still match. Do not cancel; fail toward exposed.
            self._log.info(
                f"Cloudbet bet {venue_order_id} still pending; not canceling (may still match)",
            )
        else:
            # Genuinely not placed / rejected => no exposure, so the cancel request is honoured.
            self.generate_order_canceled(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        # TODO : message the cloudbet team about cancelling a Bet that hasn't been accepted yet or is only partially fileld
        raise NotImplementedError(
            "Cloudbet doesn't support bulk cancelling orders"
        )  # pragma: no cover

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

    def _accept_price_change_policy(self) -> AcceptPriceChange:
        raw_policy = getattr(self._config, "accept_price_change", "BETTER")
        normalized = str(raw_policy or "BETTER").strip().upper()
        try:
            return AcceptPriceChange(normalized)
        except ValueError:
            self._log.warning(
                f"Invalid Cloudbet accept_price_change={raw_policy!r}; using BETTER",
            )
            return AcceptPriceChange.BETTER

    async def _resolve_pending_acceptance(
        self,
        reference_id: str,
        response: GetBetResponse,
    ) -> GetBetResponse:
        if response.status is not BetStatus.PENDING_ACCEPTANCE:
            return response
        attempts = max(0, int(getattr(self._config, "pending_acceptance_poll_attempts", 3)))
        interval_secs = max(
            0.0,
            float(getattr(self._config, "pending_acceptance_poll_interval_secs", 0.5)),
        )
        resolved = response
        for _ in range(attempts):
            if interval_secs:
                await asyncio.sleep(interval_secs)
            try:
                resolved = await self._client.get_bet_status(reference_id)
            except Exception as e:
                self._log.warning(f"Cloudbet pending acceptance polling failed: {e!r}")
                break
            if resolved.status is not BetStatus.PENDING_ACCEPTANCE:
                break
        return resolved

    async def _reconcile_pending_acceptance(
        self,
        reference_id: str,
        response: GetBetResponse,
    ) -> GetBetResponse:
        """
        Authoritatively re-resolve a still-PENDING_ACCEPTANCE reference to its true terminal state.

        A pending reference remains LIVE at Cloudbet after the poll window and can still match, so
        the strategy must never be told it was rejected while the bet could match (that would hide a
        naked leg). This performs one final query of the reference: only an explicit venue status is
        trusted. If the query fails or the venue still reports pending, the original (pending)
        response is returned so the caller fails toward "still exposed". Safe to call repeatedly.
        """
        try:
            reconciled = await self._client.get_bet_status(reference_id)
        except Exception as e:
            self._log.warning(
                f"Cloudbet pending-acceptance reconciliation failed for {reference_id}; "
                f"treating as still live: {e!r}",
            )
            return response
        return reconciled

    def _matched_stake(self, bet_response: GetBetResponse, order: Order) -> float:
        """
        Return the matched (accepted) stake for a fill.

        Cloudbet accepts a bet at the actually-matched stake, which for a partial match is less than
        the requested stake; the response ``stake`` carries that matched amount. Falls back to the
        order's requested quantity only when the venue omits the stake.
        """
        raw_stake = getattr(bet_response, "stake", None)
        if raw_stake is None:
            return order.quantity.as_double()
        return float(raw_stake)

    def _emit_bet_fill(
        self,
        strategy_id: Any,
        instrument_id: InstrumentId,
        client_order_id: ClientOrderId,
        venue_order_id: VenueOrderId,
        bet_response: GetBetResponse,
    ) -> None:
        """
        Emit a single `OrderFilled` for a matched Cloudbet bet.

        Idempotent: a client order is filled at most once (Cloudbet matches a straight bet atomically
        at ACCEPTED, so there is no incremental-fill loop), guarding against double-emit across the
        submit path, pending-acceptance reconciliation, and cancel resolution.
        """
        if client_order_id in self._filled_client_order_ids:
            self._log.debug(f"Fill already emitted for {client_order_id}; skipping")
            return

        order: Optional[Order] = self._cache.order(client_order_id)
        if order is None:
            self._log.warning(
                f"Cannot emit Cloudbet fill; order not in cache ({client_order_id})",
            )
            return

        instrument = self._cache.instrument(order.instrument_id)
        matched_stake = self._matched_stake(bet_response, order)
        last_qty: Quantity = instrument.make_qty(matched_stake)
        last_px: Price = instrument.make_price(float(bet_response.price))
        order_side: OrderSide = order.side
        liquidity_side = LiquiditySide.MAKER if order_side == OrderSide.BUY else LiquiditySide.TAKER
        quote_currency = self._cache.instrument(order.instrument_id).quote_currency
        # Deterministic trade ID keyed on the venue reference keeps repeated resolutions idempotent.
        trade_id = TradeId(venue_order_id.value)
        info = bet_response.to_dict() if hasattr(bet_response, "to_dict") else None

        self.generate_order_filled(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=trade_id,
            order_side=order_side,
            order_type=order.order_type,
            last_qty=last_qty,
            last_px=last_px,
            quote_currency=quote_currency,
            commission=Money(0, quote_currency),
            liquidity_side=liquidity_side,
            ts_event=self._clock.timestamp_ns(),
            info=info,
        )
        self._filled_client_order_ids.add(client_order_id)
        # Record the matched stake under the instrument's quote currency — the denomination the
        # venue balances (and connection_account_state's locked lookup) are keyed in — so the
        # account-state refresh always locks the stake in the wallet it is actually held against.
        # The bet response's own `currency` defaults to "EUR", which would mis-key non-EUR wallets
        # (e.g. PLAY_EUR) into a bucket the balances never report, over-stating free funds.
        stake_currency = quote_currency.code
        self._matched_stakes[client_order_id] = (matched_stake, stake_currency)
        self._log.debug(f"Generated OrderFilled for {client_order_id} qty={last_qty} px={last_px}")
