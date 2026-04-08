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
Blackbet execution client using web scraping.
"""

import asyncio

from nautilus_trader.adapters.blackbet.browser_client import BlackBetBrowserClient
from nautilus_trader.adapters.blackbet.config import BlackBetExecClientConfig
from nautilus_trader.adapters.blackbet.constants import BLACKBET_VENUE
from nautilus_trader.adapters.blackbet.providers import BlackBetInstrumentProvider
from nautilus_trader.adapters.blackbet.risk_engine import BlackBetRiskEngine
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.events.order import OrderRejected
from nautilus_trader.model.events.order import OrderSubmitted
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.orders import Order


class BlackBetExecutionClient(LiveExecutionClient):
    """
    Execution client for blackbet using web scraping.

    Provides partial functionality for bet placement automation:
    - Can navigate to markets and view bet slip without authentication
    - Actual bet placement requires login (placeholder for future)

    Integrates with BlackBetRiskEngine for rollover validation.

    NOTE: This is a placeholder implementation. Full bet placement
    requires authentication which will be implemented later.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        browser_client: BlackBetBrowserClient,
        instrument_provider: BlackBetInstrumentProvider,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: BlackBetExecClientConfig,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId(BLACKBET_VENUE.value),
            venue=BLACKBET_VENUE,
            oms_type=OmsType.NETTING,
            instrument_provider=instrument_provider,
            account_type=AccountType.BETTING,
            base_currency=None,  # ZAR will be set per instrument
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )

        self._browser_client = browser_client
        self._config = config
        self._account_id = AccountId(f"{BLACKBET_VENUE.value}-001")  # Placeholder

        # Risk engine
        self._risk_engine = BlackBetRiskEngine(
            max_stake_zar=config.max_stake_zar,
        )

        # Order tracking
        self._orders: dict[ClientOrderId, dict] = {}
        self._is_logged_in = False

    async def _connect(self) -> None:
        """
        Connect to blackbet (browser session).
        """
        self._log.info("Connecting BlackBetExecutionClient...")
        await self._browser_client.connect()

        # Attempt login if credentials provided (placeholder)
        if self._config.email and self._config.password:
            self._log.info("Login credentials provided but authentication not implemented yet")
            # TODO: Implement login flow
            # self._is_logged_in = await self._browser_client.login_placeholder(
            #     self._config.email,
            #     self._config.password,
            # )

        self._log.info("BlackBetExecutionClient connected")

    async def _disconnect(self) -> None:
        """
        Disconnect from blackbet.
        """
        self._log.info("Disconnecting BlackBetExecutionClient...")
        await self._browser_client.disconnect()
        self._log.info("BlackBetExecutionClient disconnected")

    async def _submit_order(self, command: SubmitOrder) -> None:
        """
        Submit an order (bet).

        Implementation steps (when auth is available):
        1. Validate with risk engine
        2. Navigate to market page
        3. Click odds button to add to bet slip
        4. Fill stake amount
        5. Click "Place Bet" button
        6. Confirm submission
        7. Generate order events

        Current: Placeholder that rejects orders with auth requirement.

        """
        order = command.order

        # Submit order event
        self._generate_order_submitted(order)

        # Check if logged in
        if not self._is_logged_in:
            self._generate_order_rejected(
                order=order,
                reason="Authentication required. Login not implemented yet.",
            )
            return

        # Get instrument
        instrument = self._instrument_provider.find(order.instrument_id)
        if not instrument:
            self._generate_order_rejected(
                order=order,
                reason=f"Instrument not found: {order.instrument_id}",
            )
            return

        # Risk check
        from decimal import Decimal

        stake = Decimal(str(order.quantity))
        odds = (
            Decimal(str(order.price)) if hasattr(order, "price") and order.price else Decimal("2.0")
        )

        risk_eval = self._risk_engine.evaluate_order(
            stake=stake,
            odds=odds,
            market_type=str(instrument.market_type)
            if hasattr(instrument, "market_type")
            else "unknown",
            currency="ZAR",
        )

        if not risk_eval.approved:
            self._generate_order_rejected(
                order=order,
                reason=f"Risk check failed: {', '.join(risk_eval.violations)}",
            )
            return

        # Log warnings
        for warning in risk_eval.warnings:
            self._log.warning(f"Risk warning: {warning}")

        # Placeholder: Actual bet placement
        self._log.info(
            f"Would place bet: stake={stake} ZAR, odds={odds}, instrument={order.instrument_id}",
        )

        # TODO: Implement bet placement
        # 1. await self._navigate_to_market(instrument)
        # 2. await self._click_odds_button(odds_value)
        # 3. await self._fill_bet_slip(stake)
        # 4. await self._submit_bet()
        # 5. venue_order_id = await self._get_bet_confirmation()
        # 6. self._generate_order_accepted(order, venue_order_id)

        # For now, reject with placeholder message
        self._generate_order_rejected(
            order=order,
            reason="Bet placement automation not fully implemented. Requires DOM inspection.",
        )

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

    def _send_order_event(self, event: OrderEvent) -> None:
        """
        Send an order event via the message bus.
        """
        self._msgbus.send(endpoint="ExecEngine.process", msg=event)

    async def _cancel_order(self, command: CancelOrder) -> None:
        """
        Cancel an order (not supported).
        """
        self._log.warning("Order cancellation not supported for blackbet")

    async def _modify_order(self, command: ModifyOrder) -> None:
        """
        Modify an order (not supported).
        """
        self._log.warning("Order modification not supported for blackbet")

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        """
        Generate order status report (placeholder).
        """
        self._log.warning(
            "Order status reports are not implemented for blackbet "
            f"(instrument_id={command.instrument_id}, "
            f"client_order_id={command.client_order_id}, "
            f"venue_order_id={command.venue_order_id})",
        )
        # TODO: Scrape bet history to get order status
        return None
