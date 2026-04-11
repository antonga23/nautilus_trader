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
SX.bet execution client.
"""

import asyncio
from decimal import Decimal

from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.config import SXBetExecClientConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage
from nautilus_trader.adapters.sxbet.signing import generate_salt
from nautilus_trader.adapters.sxbet.signing import get_expiry
from nautilus_trader.adapters.sxbet.signing import sign_eip712_order
from nautilus_trader.adapters.sxbet.signing import to_wei
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.events.order import OrderAccepted
from nautilus_trader.model.events.order import OrderCanceled
from nautilus_trader.model.events.order import OrderRejected
from nautilus_trader.model.events.order import OrderSubmitted
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order


class SXBetExecutionError(ValueError):
    """
    Base class for SX.bet execution validation errors.
    """


class SXBetMissingOrderHashError(SXBetExecutionError):
    """
    Raised when the venue order response does not include a usable order hash.
    """

    def __init__(self) -> None:
        super().__init__("SX.bet response missing a valid orderHash")


class SXBetUnsupportedBaseCurrencyError(SXBetExecutionError):
    """
    Raised when the configured execution base currency is unsupported.
    """

    def __init__(self, base_currency: str) -> None:
        super().__init__(
            "SX.bet execution currently supports only USDC base_currency; "
            f"received {base_currency!r}",
        )


class SXBetInvalidConfigError(SXBetExecutionError):
    """
    Raised when required SX.bet execution credentials are missing or malformed.
    """

    def __init__(self, field_name: str, reason: str) -> None:
        super().__init__(f"Invalid SX.bet execution config for {field_name}: {reason}")


class SXBetExecutionClient(LiveExecutionClient):
    """
    Provides an execution client for the SX.bet venue.

    Uses EIP712 signed orders for blockchain-based betting.

    """

    _SUPPORTED_BASE_CURRENCY = "USDC"

    def __init__(  # pylint: disable=too-many-arguments
        self,
        loop: asyncio.AbstractEventLoop,
        http_client: SXBetHttpClient,
        instrument_provider: SXBetInstrumentProvider,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: SXBetExecClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(SXBET_VENUE.value),
            venue=SXBET_VENUE,
            oms_type=OmsType.NETTING,
            instrument_provider=instrument_provider,
            account_type=AccountType.BETTING,
            base_currency=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )

        self._http_client = http_client
        self._config = config
        self._instrument_provider = instrument_provider
        self._logger = logger
        self._validate_config(config)
        self._account_id = AccountId(f"{SXBET_VENUE.value}-{config.wallet_address[:10]}")
        self._base_token = self._resolve_base_token(config.base_currency)
        # Order tracking
        self._orders: dict[ClientOrderId, dict] = {}
        self._venue_order_ids: dict[ClientOrderId, VenueOrderId] = {}

    async def _connect(self) -> None:
        """
        Connect to the execution venue.
        """
        self._log.info("Connecting SXBetExecutionClient...")
        await self._http_client.connect()

        # Get balance
        try:
            balance = await self._http_client.get_balance(
                self._config.wallet_address,
                self._base_token,
            )
            if balance:
                self._log.info("Retrieved SX.bet wallet balance")
        except SXBetHttpClientError as e:
            msg = f"Could not get balance: {e}"
            self._log.warning(msg)

        self._log.info("SXBetExecutionClient connected")

    async def _disconnect(self) -> None:
        """
        Disconnect from the execution venue.
        """
        self._log.info("Disconnecting SXBetExecutionClient...")
        await self._http_client.disconnect()
        self._log.info("SXBetExecutionClient disconnected")

    async def _submit_order(self, command: SubmitOrder) -> None:
        """
        Submit an order to the venue.
        """
        order = command.order
        instrument_id = order.instrument_id

        # Get instrument
        instrument = self._instrument_provider.find(instrument_id)
        if not isinstance(instrument, CryptoBettingInstrument):
            self._generate_order_rejected(
                order=order,
                reason=f"Instrument not found: {instrument_id}",
            )
            return

        # Get market hash (stored as event_id)
        market_hash = instrument.event_id

        # Determine outcome
        is_outcome_one = self._instrument_is_outcome_one(instrument)

        # Get price from order
        if order.order_type == OrderType.LIMIT and order.price:
            decimal_odds = float(order.price)
        else:
            decimal_odds = instrument.price

        # Convert to SX.bet format
        percentage_odds = decimal_odds_to_percentage(decimal_odds)
        stake_wei = to_wei(Decimal(str(order.quantity)), decimals=6)  # USDC has 6 decimals

        # Build order
        salt = generate_salt()
        expiry = get_expiry(hours=24)

        order_data = {
            "marketHash": market_hash,
            "maker": self._config.wallet_address,
            "totalBetSize": stake_wei,
            "percentageOdds": percentage_odds,
            "expiry": expiry,
            "baseToken": self._base_token,
            "salt": salt,
            "isMakerBettingOutcomeOne": is_outcome_one,
        }

        self._log.info(f"Submitting SX.bet order for {instrument_id}")

        try:
            # Sign the order
            signature = sign_eip712_order(
                order=order_data,
                private_key=self._config.private_key,
            )

            # Submit order event once the payload is signed and ready for venue submission.
            self._generate_order_submitted(order)

            # Place order via API
            result = await self._http_client.place_order(
                market_hash=market_hash,
                total_bet_size=stake_wei,
                percentage_odds=percentage_odds,
                expiry=expiry,
                salt=salt,
                is_maker_betting_outcome_one=is_outcome_one,
                signature=signature,
                base_token=self._base_token,
            )

            # Parse result
            order_hash = self._extract_order_hash(result)

            # Store order info
            self._orders[order.client_order_id] = {
                "order_hash": order_hash,
                "result": result,
            }
            venue_order_id = VenueOrderId(order_hash)
            self._venue_order_ids[order.client_order_id] = venue_order_id

            # Order accepted
            self._generate_order_accepted(order, venue_order_id)

        except (ImportError, ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
            msg = f"Failed to submit order: {e}"
            self._log.error(msg)
            self._generate_order_rejected(order=order, reason=str(e))

    @staticmethod
    def _resolve_base_token(base_currency: str) -> str:
        normalized_currency = base_currency.upper()
        if normalized_currency != SXBetExecutionClient._SUPPORTED_BASE_CURRENCY:
            raise SXBetUnsupportedBaseCurrencyError(base_currency)
        return SXBET_TOKENS[normalized_currency]

    @staticmethod
    def _validate_config(config: SXBetExecClientConfig) -> None:
        required_fields = {
            "api_key": config.api_key,
            "private_key": config.private_key,
            "wallet_address": config.wallet_address,
        }
        for field_name, value in required_fields.items():
            if not isinstance(value, str) or not value.strip():
                raise SXBetInvalidConfigError(field_name, "must be a non-empty string")

        wallet_address = config.wallet_address.strip()
        if not wallet_address.startswith("0x") or len(wallet_address) != 42:
            raise SXBetInvalidConfigError(
                "wallet_address",
                "must be a 42-character 0x-prefixed address",
            )

        private_key = config.private_key.strip()
        if not private_key.startswith("0x") or len(private_key) != 66:
            raise SXBetInvalidConfigError(
                "private_key",
                "must be a 66-character 0x-prefixed private key",
            )

    @staticmethod
    def _extract_order_hash(result: dict) -> str:
        order_hash = result.get("data", {}).get("orderHash")
        if not isinstance(order_hash, str) or not order_hash.strip():
            raise SXBetMissingOrderHashError
        return order_hash

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

    def _generate_order_submitted(self, order: Order) -> None:
        """
        Generate and send order submitted event.
        """
        event = OrderSubmitted(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            account_id=self._account_id,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_accepted(
        self,
        order: Order,
        venue_order_id: VenueOrderId,
    ) -> None:
        """
        Generate and send order accepted event.
        """
        event = OrderAccepted(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            account_id=self._account_id,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_rejected(
        self,
        order: Order,
        reason: str,
    ) -> None:
        """
        Generate and send order rejected event.
        """
        event = OrderRejected(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            account_id=self._account_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_canceled(
        self,
        order: Order,
        venue_order_id: VenueOrderId,
    ) -> None:
        """
        Generate and send order canceled event.
        """
        event = OrderCanceled(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            account_id=self._account_id,
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _send_order_event(self, event: OrderEvent) -> None:
        """
        Send an order event via the message bus.
        """
        self._msgbus.send(endpoint="ExecEngine.process", msg=event)

    async def _cancel_order(self, command: CancelOrder) -> None:
        """
        Cancel an order.
        """
        client_order_id = command.client_order_id

        if client_order_id not in self._orders:
            msg = f"Order not found: {client_order_id}"
            self._log.warning(msg)
            return

        order_info = self._orders[client_order_id]
        order_hash = order_info.get("order_hash")
        if order_hash is None:
            msg = f"Order hash missing for {client_order_id}"
            self._log.error(msg)
            return

        try:
            await self._http_client.cancel_order(order_hash)

            order = self._cache.order(client_order_id)
            if order:
                venue_order_id = self._venue_order_ids.get(client_order_id)
                if venue_order_id:
                    self._generate_order_canceled(order, venue_order_id)

        except (ValueError, TypeError, SXBetHttpClientError) as e:
            msg = f"Failed to cancel order: {e}"
            self._log.error(msg)

    async def _modify_order(self, command: ModifyOrder) -> None:
        """
        Modify an order (cancel and replace).
        """
        # SX.bet doesn't support order modification, must cancel and replace
        self._log.warning(
            f"Order modification requires cancel/replace on SX.bet: {command.client_order_id}",
        )

    def _resolve_status_query(
        self,
        command: GenerateOrderStatusReport,
    ) -> tuple[VenueOrderId | None, Order | None, InstrumentId | None]:
        client_order_id = command.client_order_id
        venue_order_id = command.venue_order_id
        instrument_id = command.instrument_id
        cached_order = self._cache.order(client_order_id) if client_order_id is not None else None

        if venue_order_id is None and client_order_id is not None:
            venue_order_id = self._venue_order_ids.get(client_order_id)
        if instrument_id is None and cached_order is not None:
            instrument_id = cached_order.instrument_id

        return venue_order_id, cached_order, instrument_id

    @staticmethod
    def _map_order_status(status: str) -> OrderStatus:
        status = status.upper()
        if status == "ACTIVE":
            return OrderStatus.ACCEPTED
        if status == "FILLED":
            return OrderStatus.FILLED
        if status == "CANCELLED":
            return OrderStatus.CANCELED
        return OrderStatus.SUBMITTED

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        """
        Generate an order status report.
        """
        venue_order_id, cached_order, instrument_id = self._resolve_status_query(command)
        if venue_order_id is None:
            self._log.warning(
                "Cannot query SX.bet order status without a venue order id "
                f"(client_order_id={command.client_order_id})",
            )
            return None

        if instrument_id is None:
            self._log.warning(
                "Cannot build SX.bet order status report without an instrument id "
                f"(venue_order_id={venue_order_id})",
            )
            return None

        try:
            orders = await self._http_client.get_user_orders(
                self._config.wallet_address,
            )

            for order_data in orders.get("data", {}).get("orders", []):
                if order_data.get("orderHash") != str(venue_order_id):
                    continue

                return OrderStatusReport(
                    account_id=self._account_id,
                    instrument_id=instrument_id,
                    venue_order_id=venue_order_id,
                    client_order_id=command.client_order_id,
                    order_side=cached_order.side if cached_order else OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    order_status=self._map_order_status(order_data.get("status", "")),
                    quantity=Quantity.from_int(1),
                    filled_qty=Quantity.zero(),
                    report_id=UUID4(),
                    ts_accepted=self._clock.timestamp_ns(),
                    ts_last=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                )

        except (ValueError, TypeError, KeyError, SXBetHttpClientError) as e:
            msg = f"Failed to get order status: {e}"
            self._log.error(msg)

        return None
