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
WSB adapter factory functions.
"""

import asyncio

from nautilus_trader.adapters.wsb.browser_client import WSBBrowserClient
from nautilus_trader.adapters.wsb.config import WSBDataClientConfig
from nautilus_trader.adapters.wsb.config import WSBExecClientConfig
from nautilus_trader.adapters.wsb.config import WSBInstrumentProviderConfig
from nautilus_trader.adapters.wsb.constants import WSB_BASE_URL
from nautilus_trader.adapters.wsb.data import WSBDataClient
from nautilus_trader.adapters.wsb.execution import WSBExecutionClient
from nautilus_trader.adapters.wsb.providers import WSBInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus


class WSBLiveDataClientFactory:
    """
    Factory for creating WSB data clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: WSBDataClientConfig,
    ) -> WSBDataClient:
        """
        Create a new WSB data client.
        """
        # Create browser client
        browser_client = WSBBrowserClient(
            base_url=config.base_url or WSB_BASE_URL,
            headless=config.headless,
            use_stealth=config.use_stealth,
            request_delay_min=config.request_delay_min,
            request_delay_max=config.request_delay_max,
            max_requests_per_minute=config.max_requests_per_minute,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or WSBInstrumentProviderConfig()

        instrument_provider = WSBInstrumentProvider(
            browser_client=browser_client,
            config=provider_config,
            logger=logger,
        )

        return WSBDataClient(
            loop=loop,
            browser_client=browser_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )


class WSBLiveExecClientFactory:
    """
    Factory for creating WSB execution clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: WSBExecClientConfig,
    ) -> WSBExecutionClient:
        """
        Create a new WSB execution client.
        """
        # Create browser client
        browser_client = WSBBrowserClient(
            base_url=config.base_url or WSB_BASE_URL,
            headless=config.headless,
            use_stealth=config.use_stealth,
            request_delay_min=config.request_delay_min,
            request_delay_max=config.request_delay_max,
            max_requests_per_minute=config.max_requests_per_minute,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or WSBInstrumentProviderConfig()

        instrument_provider = WSBInstrumentProvider(
            browser_client=browser_client,
            config=provider_config,
            logger=logger,
        )

        return WSBExecutionClient(
            loop=loop,
            browser_client=browser_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )
