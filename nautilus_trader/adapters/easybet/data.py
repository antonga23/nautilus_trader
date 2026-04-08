# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet data client.
# -------------------------------------------------------------------------------------------------

import asyncio

from nautilus_trader.adapters.easybet.browser_client import EasybetBrowserClient
from nautilus_trader.adapters.easybet.config import EasybetDataClientConfig
from nautilus_trader.adapters.easybet.providers import EasybetInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


class EasybetDataClient(LiveMarketDataClient):
    """
    Data client for Easybet web scraping.

    Implements polling-based quote updates using Playwright browser automation.

    """

    def __init__(
        self,
        loop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: EasybetDataClientConfig,
        instrument_provider: EasybetInstrumentProvider,
    ):
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,  # type: ignore[arg-type]
        )

        self._config = config
        self._easybet_instrument_provider = instrument_provider

        # Browser client
        self._browser_client = EasybetBrowserClient(
            base_url=config.base_url,
            use_iframe_source=config.use_iframe_source,
            headless=config.headless,
            use_stealth=config.use_stealth,
            request_delay_min=config.request_delay_min,
            request_delay_max=config.request_delay_max,
            max_requests_per_minute=config.max_requests_per_minute,
        )

        # Polling state
        self._poll_task: asyncio.Task | None = None
        self._subscribed_instruments: set[InstrumentId] = set()

    async def _connect(self) -> None:
        """
        Connect to browser.
        """
        self._log.info("Connecting Easybet data client")
        await self._browser_client.connect()
        await self._browser_client.navigate_to_sportsbook()

        # Start polling
        self._poll_task = self._loop.create_task(self._poll_quotes())

    async def _disconnect(self) -> None:
        """
        Disconnect browser.
        """
        self._log.info("Disconnecting Easybet data client")

        # Stop polling
        if self._poll_task:
            self._poll_task.cancel()

        await self._browser_client.disconnect()

    async def _subscribe_quote_ticks(self, instrument_id: InstrumentId) -> None:
        """
        Subscribe to quote ticks for instrument.
        """
        self._subscribed_instruments.add(instrument_id)
        self._log.info(f"Subscribed to {instrument_id}")

    async def _unsubscribe_quote_ticks(self, instrument_id: InstrumentId) -> None:
        """
        Unsubscribe from quote ticks.
        """
        self._subscribed_instruments.discard(instrument_id)
        self._log.info(f"Unsubscribed from {instrument_id}")

    async def _poll_quotes(self) -> None:
        """
        Poll for quote updates (placeholder).
        """
        while True:
            try:
                await self._loop.create_task(self._update_quotes())
                await asyncio.sleep(self._config.quote_poll_interval)
            except Exception as e:
                self._log.error(f"Error polling quotes: {e}")
                await asyncio.sleep(5.0)

    async def _update_quotes(self) -> None:
        """
        Update quotes for subscribed instruments (placeholder).
        """
        if not self._subscribed_instruments:
            return

        # TODO: Implement actual DOM scraping here
        # For now, just log
        self._log.debug(f"Updating quotes for {len(self._subscribed_instruments)} instruments")

        # Placeholder: generate mock quote for demonstration
        for instrument_id in self._subscribed_instruments:
            instrument = self._easybet_instrument_provider.get(instrument_id)
            if not instrument:
                continue

            # Mock quote tick
            quote = QuoteTick(
                instrument_id=instrument_id,
                bid_price=Price.from_str("1.90"),
                ask_price=Price.from_str("1.92"),
                bid_size=Quantity.from_int(1000),
                ask_size=Quantity.from_int(1000),
                ts_event=self._clock.timestamp_ns(),
                ts_init=self._clock.timestamp_ns(),
            )

            self._handle_data(quote)
