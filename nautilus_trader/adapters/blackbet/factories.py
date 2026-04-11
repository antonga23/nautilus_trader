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
Blackbet adapter factory functions.
"""

import asyncio

from nautilus_trader.adapters.blackbet.browser_client import BlackBetBrowserClient
from nautilus_trader.adapters.blackbet.config import BlackBetDataClientConfig
from nautilus_trader.adapters.blackbet.config import BlackBetExecClientConfig
from nautilus_trader.adapters.blackbet.config import BlackBetInstrumentProviderConfig
from nautilus_trader.adapters.blackbet.constants import BLACKBET_BASE_URL
from nautilus_trader.adapters.blackbet.data import BlackBetDataClient
from nautilus_trader.adapters.blackbet.execution import BlackBetExecutionClient
from nautilus_trader.adapters.blackbet.providers import BlackBetInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus


class BlackBetLiveDataClientFactory:
    """
    Factory for creating blackbet data clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: BlackBetDataClientConfig,
    ) -> BlackBetDataClient:
        """
        Create a new blackbet data client.
        """
        # Create browser client
        browser_client = BlackBetBrowserClient(
            base_url=config.base_url or BLACKBET_BASE_URL,
            headless=config.headless,
            use_stealth=config.use_stealth,
            request_delay_min=config.request_delay_min,
            request_delay_max=config.request_delay_max,
            max_requests_per_minute=config.max_requests_per_minute,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or BlackBetInstrumentProviderConfig()

        instrument_provider = BlackBetInstrumentProvider(
            browser_client=browser_client,
            config=provider_config,
            logger=logger,
        )

        return BlackBetDataClient(
            loop=loop,
            browser_client=browser_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )


class BlackBetLiveExecClientFactory:
    """
    Factory for creating blackbet execution clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: BlackBetExecClientConfig,
    ) -> BlackBetExecutionClient:
        """
        Create a new blackbet execution client.
        """
        # Create browser client
        browser_client = BlackBetBrowserClient(
            base_url=config.base_url or BLACKBET_BASE_URL,
            headless=config.headless,
            use_stealth=config.use_stealth,
            request_delay_min=config.request_delay_min,
            request_delay_max=config.request_delay_max,
            max_requests_per_minute=config.max_requests_per_minute,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or BlackBetInstrumentProviderConfig()

        instrument_provider = BlackBetInstrumentProvider(
            browser_client=browser_client,
            config=provider_config,
            logger=logger,
        )

        return BlackBetExecutionClient(
            loop=loop,
            browser_client=browser_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )
