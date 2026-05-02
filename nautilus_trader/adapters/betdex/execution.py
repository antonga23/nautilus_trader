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
BetDex/Monaco execution client.
"""

from __future__ import annotations

import asyncio

from nautilus_trader.adapters.betdex.config import BetDexExecClientConfig
from nautilus_trader.adapters.betdex.constants import BETDEX_SANDBOX_API_BASE_URL
from nautilus_trader.adapters.betdex.constants import BETDEX_VENUE
from nautilus_trader.adapters.betdex.http_client import BetDexHttpClient
from nautilus_trader.adapters.betdex.http_client import BetDexHttpClientError
from nautilus_trader.adapters.betdex.providers import BetDexInstrumentProvider
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
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
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order


class BetDexExecutionError(ValueError):
    """
    Base class for BetDex execution configuration and order errors.
    """


class BetDexProductionExecutionNotAllowed(BetDexExecutionError):
    """
    Raised when production execution is attempted without explicit opt-in.
    """

    def __init__(self) -> None:
        super().__init__(
            "BetDex execution defaults to sandbox; set allow_production_execution=True "
            "to submit orders to a non-sandbox API URL",
        )


class BetDexExecutionClient(LiveExecutionClient):
    """
    Execution client for Monaco orders used by BetDex.

    The default configuration only permits sandbox order submission. Production
    execution requires an explicit configuration opt-in and should remain disabled in
    validation-mode strategy nodes.

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
        config: BetDexExecClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(BETDEX_VENUE.value),
            venue=BETDEX_VENUE,
            oms_type=OmsType.NETTING,
            instrument_provider=instrument_provider,
            account_type=AccountType.BETTING,
            base_currency=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        self._http_client = http_client
        self._instrument_provider = instrument_provider
        self._config = config
        self._logger = logger
        self._validate_config(config)
        self._account_id = AccountId(f"{BETDEX_VENUE.value}-{config.wallet_id[:10]}")
        self._orders: dict[ClientOrderId, dict] = {}
        self._venue_order_ids: dict[ClientOrderId, VenueOrderId] = {}

    async def _connect(self) -> None:
        self._log.info("Connecting BetDexExecutionClient...")
        await self._http_client.connect()
        self._log.info("BetDexExecutionClient connected")

    async def _disconnect(self) -> None:
        await self._http_client.disconnect()

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        instrument = self._instrument_provider.find(order.instrument_id)
        if not isinstance(instrument, CryptoBettingInstrument):
            self._generate_order_rejected(
                order=order,
                reason=f"Instrument not found: {order.instrument_id}",
            )
            return
        info = instrument.info if isinstance(instrument.info, dict) else {}
        outcome_id = str(info.get("outcome_id") or "")
        if not instrument.market_id or not outcome_id:
            self._generate_order_rejected(
                order=order,
                reason="BetDex instrument missing market_id or outcome_id",
            )
            return

        price = (
            float(order.price)
            if order.order_type == OrderType.LIMIT and order.price
            else instrument.price
        )
        stake = float(order.quantity)
        try:
            self._generate_order_submitted(order)
            result = await self._http_client.place_order(
                wallet_id=self._config.wallet_id,
                market_id=instrument.market_id,
                side="For",
                outcome_id=outcome_id,
                price=price,
                stake=stake,
                keep_when_in_play=self._config.keep_when_in_play,
                match_behavior=self._config.match_behavior,
                reference=str(order.client_order_id),
            )
            order_id = self._extract_order_id(result)
            venue_order_id = VenueOrderId(order_id)
            self._orders[order.client_order_id] = {"order_id": order_id, "result": result}
            self._venue_order_ids[order.client_order_id] = venue_order_id
            self._generate_order_accepted(order, venue_order_id)
        except (ValueError, TypeError, KeyError, BetDexHttpClientError) as e:
            self._generate_order_rejected(order=order, reason=str(e))

    async def _cancel_order(self, command: CancelOrder) -> None:
        client_order_id = command.client_order_id
        order_id = self._orders.get(client_order_id, {}).get("order_id")
        if not isinstance(order_id, str) or not order_id:
            self._log.warning(f"BetDex order not found: {client_order_id}")
            return
        try:
            await self._http_client.cancel_order(order_id)
            order = self._cache.order(client_order_id)
            venue_order_id = self._venue_order_ids.get(client_order_id)
            if order is not None and venue_order_id is not None:
                self._generate_order_canceled(order, venue_order_id)
        except BetDexHttpClientError as e:
            self._log.error(f"Failed to cancel BetDex order: {e}")

    async def _modify_order(self, command: ModifyOrder) -> None:
        self._log.warning(
            f"Order modification requires cancel/replace on BetDex: {command.client_order_id}",
        )

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        venue_order_id = command.venue_order_id
        if venue_order_id is None and command.client_order_id is not None:
            venue_order_id = self._venue_order_ids.get(command.client_order_id)
        if venue_order_id is None:
            return None
        cached_order = (
            self._cache.order(command.client_order_id) if command.client_order_id else None
        )
        instrument_id = command.instrument_id or (
            cached_order.instrument_id if cached_order else None
        )
        if instrument_id is None:
            return None
        return OrderStatusReport(
            account_id=self._account_id,
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            client_order_id=command.client_order_id,
            order_side=cached_order.side if cached_order else OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            order_status=OrderStatus.ACCEPTED,
            quantity=cached_order.quantity if cached_order else Quantity.from_int(1),
            filled_qty=Quantity.zero(),
            report_id=UUID4(),
            ts_accepted=self._clock.timestamp_ns(),
            ts_last=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )

    @staticmethod
    def _extract_order_id(payload: dict) -> str:
        orders = payload.get("orders")
        if isinstance(orders, list) and orders:
            order_id = orders[0].get("id")
            if isinstance(order_id, str) and order_id:
                return order_id
        raise BetDexExecutionError("BetDex order response missing order id")

    @staticmethod
    def _validate_config(config: BetDexExecClientConfig) -> None:
        for field_name, value in {
            "app_id": config.app_id,
            "api_key": config.api_key,
            "wallet_id": config.wallet_id,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise BetDexExecutionError(f"BetDex {field_name} must be configured")
        api_url = (config.api_url or BETDEX_SANDBOX_API_BASE_URL).lower()
        if "sandbox" not in api_url and not config.allow_production_execution:
            raise BetDexProductionExecutionNotAllowed

    def _generate_order_submitted(self, order: Order) -> None:
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

    def _generate_order_accepted(self, order: Order, venue_order_id: VenueOrderId) -> None:
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

    def _generate_order_rejected(self, order: Order, reason: str) -> None:
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

    def _generate_order_canceled(self, order: Order, venue_order_id: VenueOrderId) -> None:
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
        self._msgbus.send(endpoint="ExecEngine.process", msg=event)
