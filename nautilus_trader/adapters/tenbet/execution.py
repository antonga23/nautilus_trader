# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
10bet execution client using web scraping.
"""

import asyncio
import hashlib
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.tenbet.browser_client import TenBetBrowserClient
from nautilus_trader.adapters.tenbet.config import TenBetExecClientConfig
from nautilus_trader.adapters.tenbet.constants import TENBET_BASE_URL
from nautilus_trader.adapters.tenbet.constants import TENBET_VENUE
from nautilus_trader.adapters.tenbet.providers import TenBetInstrumentProvider
from nautilus_trader.adapters.tenbet.risk_engine import TenBetRiskEngine
from nautilus_trader.adapters.tenbet.selectors import TenBetSelectors
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.events.order import OrderAccepted
from nautilus_trader.model.events.order import OrderRejected
from nautilus_trader.model.events.order import OrderSubmitted
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.orders import Order


class TenBetExecutionClient(LiveExecutionClient):
    """
    Execution client for 10bet using browser automation.

    The client supports two submission modes:
    - validation mode, where it records a synthetic acceptance for CI/dry runs
    - browser mode, where it attempts to click through the bet slip flow

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        browser_client: TenBetBrowserClient,
        instrument_provider: TenBetInstrumentProvider,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: TenBetExecClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(TENBET_VENUE.value),
            venue=TENBET_VENUE,
            oms_type=OmsType.NETTING,
            instrument_provider=instrument_provider,
            account_type=AccountType.BETTING,
            base_currency=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )

        self._browser_client = browser_client
        self._config = config
        self._account_id = AccountId(f"{TENBET_VENUE.value}-001")
        self._venue_risk_policy = TenBetRiskEngine(
            max_stake_zar=config.max_stake_zar,
        )
        self._orders: dict[ClientOrderId, dict[str, Any]] = {}
        self._is_logged_in = False

    async def _connect(self) -> None:
        """
        Connect to 10bet (browser session).
        """
        self._log.info("Connecting TenBetExecutionClient...")
        await self._browser_client.connect()
        login_ok = await self._browser_client.login_placeholder(
            self._config.email,
            self._config.password,
            otp_code=self._config.otp_code,
            allow_synthetic_auth=self._config.allow_synthetic_auth
            or self._config.allow_synthetic_execution,
        )
        self._is_logged_in = bool(login_ok and self._browser_client.is_logged_in)
        self._log.info(
            "TenBetExecutionClient connected "
            f"(auth_mode={self._browser_client.auth_mode}, logged_in={self._is_logged_in})",
        )

    async def _disconnect(self) -> None:
        """
        Disconnect from 10bet.
        """
        self._log.info("Disconnecting TenBetExecutionClient...")
        await self._browser_client.disconnect()
        self._log.info("TenBetExecutionClient disconnected")

    async def _submit_order(self, command: SubmitOrder) -> None:
        """
        Submit an order (bet).
        """
        order = command.order
        self._generate_order_submitted(order)

        if not self._is_logged_in:
            login_ok = await self._browser_client.login_placeholder(
                self._config.email,
                self._config.password,
                otp_code=self._config.otp_code,
                allow_synthetic_auth=self._config.allow_synthetic_auth
                or self._config.allow_synthetic_execution,
            )
            self._is_logged_in = bool(login_ok and self._browser_client.is_logged_in)

        if not self._is_logged_in and not self._config.allow_synthetic_execution:
            self._generate_order_rejected(
                order=order,
                reason="Authentication required. Provide live credentials or enable synthetic execution.",
            )
            return

        instrument = self._instrument_provider.find(order.instrument_id)
        if not isinstance(instrument, CryptoBettingInstrument):
            self._generate_order_rejected(
                order=order,
                reason=f"Instrument not found: {order.instrument_id}",
            )
            return

        stake = Decimal(str(order.quantity))
        odds = self._resolve_odds(order, instrument)
        risk_eval = self._venue_risk_policy.evaluate_order(
            stake=stake,
            odds=odds,
            market_type=str(instrument.market_type),
            currency="ZAR",
        )

        if not risk_eval.approved:
            self._generate_order_rejected(
                order=order,
                reason=f"Risk check failed: {', '.join(risk_eval.violations)}",
            )
            return

        for warning in risk_eval.warnings:
            self._log.warning(f"Risk warning: {warning}")

        if self._config.allow_synthetic_execution:
            venue_order_id = self._synthetic_venue_order_id(order)
            self._store_order(order, {"mode": "synthetic", "venue_order_id": venue_order_id.value})
            self._generate_order_accepted(order, venue_order_id)
            self._log.warning(
                f"Synthetic 10bet order accepted for validation mode: {order.client_order_id}",
            )
            return

        venue_order_id = await self._attempt_browser_submission(order, instrument, stake, odds)
        if venue_order_id is None:
            self._generate_order_rejected(
                order=order,
                reason=(
                    "Bet placement automation could not complete the browser flow. "
                    "Enable synthetic execution for validation or finish DOM wiring for live mode."
                ),
            )
            return

        self._store_order(order, {"mode": "browser", "venue_order_id": venue_order_id.value})
        self._generate_order_accepted(order, venue_order_id)

    async def _attempt_browser_submission(
        self,
        order: Order,
        instrument: CryptoBettingInstrument,
        stake: Decimal,
        odds: Decimal,
    ) -> VenueOrderId | None:
        """
        Attempt a browser-driven bet placement.
        """
        page = getattr(self._browser_client, "_page", None)
        if page is None:
            return None

        try:
            base_url = self._config.base_url or TENBET_BASE_URL
            await self._browser_client.navigate_to(f"{base_url.rstrip('/')}/sports")
            await self._find_and_click_odds_button(page, odds)
            await self._open_bet_slip_if_present(page)
            await self._fill_stake_if_present(page, stake)
            await self._click_submit_if_present(page)
        except Exception as e:
            self._log.warning(f"10bet browser submission failed: {e}")
            return None

        confirmation = await self._read_confirmation(page)
        if confirmation is None:
            return None
        return VenueOrderId(self._synthetic_venue_order_id(order, confirmation=confirmation).value)

    async def _find_and_click_odds_button(self, page: Any, odds: Decimal) -> bool:
        buttons = await page.query_selector_all(TenBetSelectors.ODDS_BUTTON_PARTIAL)
        odds_text = f"{float(odds):.2f}"
        for button in buttons:
            text = await self._extract_element_text(button)
            if text and odds_text in text:
                await button.click()
                return True
        if buttons:
            await buttons[0].click()
            return True
        return False

    async def _open_bet_slip_if_present(self, page: Any) -> bool:
        return await self._click_selector_if_present(page, TenBetSelectors.BET_SLIP_TOGGLE)

    async def _fill_stake_if_present(self, page: Any, stake: Decimal) -> bool:
        return await self._fill_selector_if_present(page, TenBetSelectors.STAKE_INPUT, str(stake))

    async def _click_submit_if_present(self, page: Any) -> bool:
        return await self._click_selector_if_present(page, TenBetSelectors.PLACE_BET_BUTTON)

    async def _read_confirmation(self, page: Any) -> str | None:
        try:
            if await self._has_any_selector(page, TenBetSelectors.BET_CONFIRMATION):
                return await page.content()
        except Exception:
            return None
        return None

    async def _click_selector_if_present(self, page: Any, selector: str) -> bool:
        try:
            element = await page.query_selector(selector)
            if element is None:
                return False
            await element.click()
            return True
        except Exception:
            return False

    async def _fill_selector_if_present(self, page: Any, selector: str, value: str) -> bool:
        try:
            element = await page.query_selector(selector)
            if element is None:
                return False
            await element.fill(value)
            return True
        except Exception:
            return False

    async def _has_any_selector(self, page: Any, selector: str) -> bool:
        try:
            return await page.query_selector(selector) is not None
        except Exception:
            return False

    async def _extract_element_text(self, element: Any) -> str | None:
        if element is None:
            return None
        for accessor in ("text_content", "inner_text"):
            method = getattr(element, accessor, None)
            if method is None:
                continue
            try:
                text = await method()
            except Exception:
                text = None
            if text:
                return str(text).strip()
        return None

    def _resolve_odds(self, order: Order, instrument: CryptoBettingInstrument) -> Decimal:
        if getattr(order, "price", None):
            return Decimal(str(order.price))
        return Decimal(str(instrument.price))

    def _synthetic_venue_order_id(
        self,
        order: Order,
        confirmation: str | None = None,
    ) -> VenueOrderId:
        seed = f"{order.client_order_id.value}:{order.instrument_id.value}:{confirmation or 'synthetic'}"
        return VenueOrderId(hashlib.sha256(seed.encode()).hexdigest()[:32])

    def _store_order(self, order: Order, payload: dict[str, Any]) -> None:
        self._orders[order.client_order_id] = payload

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
        event = OrderSubmitted(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            account_id=self._account_id,
            event_id=UUID4(),
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_accepted(
        self,
        order: Order,
        venue_order_id: VenueOrderId,
    ) -> None:
        event = OrderAccepted(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            account_id=self._account_id,
            event_id=UUID4(),
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _generate_order_rejected(
        self,
        order: Order,
        reason: str,
    ) -> None:
        event = OrderRejected(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            account_id=self._account_id,
            reason=reason,
            event_id=UUID4(),
            ts_event=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
        )
        self._send_order_event(event)

    def _send_order_event(self, event: OrderEvent) -> None:
        self._msgbus.send(endpoint="ExecEngine.process", msg=event)

    async def _cancel_order(self, command: CancelOrder) -> None:
        self._log.warning("Order cancellation not supported for 10bet")

    async def _modify_order(self, command: ModifyOrder) -> None:
        self._log.warning("Order modification not supported for 10bet")

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        self._log.warning(
            "Order status reports are not implemented for 10bet "
            f"(instrument_id={command.instrument_id}, "
            f"client_order_id={command.client_order_id}, "
            f"venue_order_id={command.venue_order_id})",
        )
        return None
