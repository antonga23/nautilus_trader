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
BetDex/Monaco live client factories.
"""

from __future__ import annotations

import asyncio

from nautilus_trader.adapters.betdex.config import BetDexDataClientConfig
from nautilus_trader.adapters.betdex.config import BetDexExecClientConfig
from nautilus_trader.adapters.betdex.config import BetDexInstrumentProviderConfig
from nautilus_trader.adapters.betdex.constants import BETDEX_SANDBOX_API_BASE_URL
from nautilus_trader.adapters.betdex.data import BetDexDataClient
from nautilus_trader.adapters.betdex.execution import BetDexExecutionClient
from nautilus_trader.adapters.betdex.http_client import BetDexHttpClient
from nautilus_trader.adapters.betdex.providers import BetDexInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus


class BetDexLiveDataClientFactory:
    """
    Factory for BetDex market data clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: BetDexDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> BetDexDataClient:
        logger = Logger(name=f"BetDexDataClient-{name}")
        http_client = BetDexHttpClient(
            app_id=config.app_id,
            api_key=config.api_key,
            api_url=config.api_url or BETDEX_SANDBOX_API_BASE_URL,
            logger=logger,
        )
        provider_config = config.instrument_provider or BetDexInstrumentProviderConfig(
            app_id=config.app_id,
            api_key=config.api_key,
            api_url=config.api_url,
        )
        provider = BetDexInstrumentProvider(
            http_client=http_client,
            config=provider_config,
            logger=logger,
        )
        return BetDexDataClient(
            loop=loop,
            http_client=http_client,
            instrument_provider=provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )


class BetDexLiveExecClientFactory:
    """
    Factory for BetDex execution clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: BetDexExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> BetDexExecutionClient:
        logger = Logger(name=f"BetDexExecutionClient-{name}")
        http_client = BetDexHttpClient(
            app_id=config.app_id,
            api_key=config.api_key,
            api_url=config.api_url or BETDEX_SANDBOX_API_BASE_URL,
            logger=logger,
        )
        provider_config = config.instrument_provider or BetDexInstrumentProviderConfig(
            app_id=config.app_id,
            api_key=config.api_key,
            api_url=config.api_url,
        )
        provider = BetDexInstrumentProvider(
            http_client=http_client,
            config=provider_config,
            logger=logger,
        )
        return BetDexExecutionClient(
            loop=loop,
            http_client=http_client,
            instrument_provider=provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )
