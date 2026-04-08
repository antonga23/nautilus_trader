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
10bet adapter factory functions.
"""

import asyncio

from nautilus_trader.adapters.tenbet.browser_client import TenBetBrowserClient
from nautilus_trader.adapters.tenbet.config import TenBetDataClientConfig
from nautilus_trader.adapters.tenbet.config import TenBetExecClientConfig
from nautilus_trader.adapters.tenbet.config import TenBetInstrumentProviderConfig
from nautilus_trader.adapters.tenbet.constants import TENBET_BASE_URL
from nautilus_trader.adapters.tenbet.data import TenBetDataClient
from nautilus_trader.adapters.tenbet.execution import TenBetExecutionClient
from nautilus_trader.adapters.tenbet.providers import TenBetInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus


class TenBetLiveDataClientFactory:
    """
    Factory for creating 10bet data clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: TenBetDataClientConfig,
    ) -> TenBetDataClient:
        """
        Create a new 10bet data client.
        """
        # Create browser client
        browser_client = TenBetBrowserClient(
            base_url=config.base_url or TENBET_BASE_URL,
            headless=config.headless,
            use_stealth=config.use_stealth,
            request_delay_min=config.request_delay_min,
            request_delay_max=config.request_delay_max,
            max_requests_per_minute=config.max_requests_per_minute,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or TenBetInstrumentProviderConfig()

        instrument_provider = TenBetInstrumentProvider(
            browser_client=browser_client,
            config=provider_config,
            logger=logger,
        )

        return TenBetDataClient(
            loop=loop,
            browser_client=browser_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )


class TenBetLiveExecClientFactory:
    """
    Factory for creating 10bet execution clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: TenBetExecClientConfig,
    ) -> TenBetExecutionClient:
        """
        Create a new 10bet execution client.
        """
        # Create browser client
        browser_client = TenBetBrowserClient(
            base_url=config.base_url or TENBET_BASE_URL,
            headless=config.headless,
            use_stealth=config.use_stealth,
            request_delay_min=config.request_delay_min,
            request_delay_max=config.request_delay_max,
            max_requests_per_minute=config.max_requests_per_minute,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or TenBetInstrumentProviderConfig()

        instrument_provider = TenBetInstrumentProvider(
            browser_client=browser_client,
            config=provider_config,
            logger=logger,
        )

        return TenBetExecutionClient(
            loop=loop,
            browser_client=browser_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )
