# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet execution client.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

from nautilus_trader.adapters.easybet.browser_client import EasybetBrowserClient
from nautilus_trader.adapters.easybet.config import EasybetExecClientConfig
from nautilus_trader.adapters.easybet.risk_engine import EasybetRiskEngine
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import VenueOrderId


class EasybetExecutionClient(LiveExecutionClient):
    """
    Execution client for Easybet with risk management.

    Implements partial execution capabilities:
    - Risk engine integration
    - Placeholder bet placement (requires auth)

    """

    def __init__(
        self,
        loop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: EasybetExecClientConfig,
    ):
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=None,
            oms_type=None,
            account_type=None,
            base_currency=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=None,  # type: ignore[arg-type]
        )

        self._config = config

        # Venue-specific order preflight policy
        self._venue_risk_policy = EasybetRiskEngine(
            max_stake_zar=config.max_stake_zar,
            rollover_multiplier=config.rollover_multiplier,
            min_rollover_odds=config.min_rollover_odds,
            bonus_amount=config.bonus_amount,
        )

        # Browser client (for bet placement)
        self._browser_client = EasybetBrowserClient(
            base_url=config.base_url,
            headless=config.headless,
        )

    async def _connect(self) -> None:
        """
        Connect execution client.
        """
        self._log.info("Connecting Eas ybet execution client")
        await self._browser_client.connect()

    async def _disconnect(self) -> None:
        """
        Disconnect execution client.
        """
        self._log.info("Disconnecting Easybet execution client")
        await self._browser_client.disconnect()

    async def _submit_order(self, command: SubmitOrder) -> None:
        """
        Submit order with risk validation.
        """
        order = command.order

        # Extract order details
        stake = Decimal(str(order.quantity))
        # Get odds from order (would be in order.price for limit orders)
        odds = Decimal("2.0")  # Placeholder
        market_type = "match_odds"  # Placeholder

        # Risk evaluation
        eval_result = self._venue_risk_policy.evaluate_order(
            stake=stake,
            odds=odds,
            market_type=market_type,
        )

        if not eval_result.approved:
            self._log.error(f"Order rejected by venue risk policy: {eval_result.violations}")
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason="; ".join(eval_result.violations),
                ts_event=self._clock.timestamp_ns(),
            )
            return

        # Log warnings
        for warning in eval_result.warnings:
            self._log.warning(warning)

        # Placeholder: actual bet placement requires authentication
        self._log.info(f"Placeholder: Would place bet for {order.instrument_id}")

        # Generate accepted event (in reality, would only do this after successful placement)
        venue_order_id = VenueOrderId(str(UUID4()))
        self.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

    async def _modify_order(self, command) -> None:
        """
        Modify order (not supported).
        """
        self._log.warning("Order modification not supported for Easybet")

    async def _cancel_order(self, command) -> None:
        """
        Cancel order (not supported for betting).
        """
        self._log.warning("Order cancellation not supported for betting markets")
