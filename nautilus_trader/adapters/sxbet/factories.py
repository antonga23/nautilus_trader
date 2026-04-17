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
SX.bet adapter factory functions.
"""

import asyncio

from nautilus_trader.adapters.sxbet.config import SXBetDataClientConfig
from nautilus_trader.adapters.sxbet.config import SXBetExecClientConfig
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_API_BASE_URL
from nautilus_trader.adapters.sxbet.data import SXBetDataClient
from nautilus_trader.adapters.sxbet.execution import SXBetExecutionClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus


class SXBetLiveDataClientFactory:
    """
    Factory for creating SX.bet data clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SXBetDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> SXBetDataClient:
        """
        Create a new SX.bet data client.
        """
        logger = Logger(name=f"SXBetDataClient-{name}")

        # Create HTTP client
        http_client = SXBetHttpClient(
            api_key=config.api_key,
            api_url=config.api_url or SXBET_API_BASE_URL,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or SXBetInstrumentProviderConfig(
            api_key=config.api_key,
            api_url=config.api_url,
        )

        instrument_provider = SXBetInstrumentProvider(
            http_client=http_client,
            config=provider_config,
            logger=logger,
        )

        return SXBetDataClient(
            loop=loop,
            http_client=http_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )


class SXBetLiveExecClientFactory:
    """
    Factory for creating SX.bet execution clients.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SXBetExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> SXBetExecutionClient:
        """
        Create a new SX.bet execution client.
        """
        logger = Logger(name=f"SXBetExecutionClient-{name}")

        # Create HTTP client
        http_client = SXBetHttpClient(
            api_key=config.api_key,
            api_url=config.api_url or SXBET_API_BASE_URL,
            logger=logger,
        )

        # Create instrument provider
        provider_config = config.instrument_provider or SXBetInstrumentProviderConfig(
            api_key=config.api_key,
            api_url=config.api_url,
        )

        instrument_provider = SXBetInstrumentProvider(
            http_client=http_client,
            config=provider_config,
            logger=logger,
        )

        return SXBetExecutionClient(
            loop=loop,
            http_client=http_client,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )


def get_sxbet_http_client(
    api_key: str | None = None,
    api_url: str | None = None,
    logger: Logger | None = None,
) -> SXBetHttpClient:
    """
    Create a standalone HTTP client for SX.bet API.
    """
    return SXBetHttpClient(
        api_key=api_key,
        api_url=api_url or SXBET_API_BASE_URL,
        logger=logger,
    )


def get_sxbet_instrument_provider(
    api_key: str | None = None,
    api_url: str | None = None,
    logger: Logger | None = None,
    sport_ids: set[int] | None = None,
    league_ids: set[int] | None = None,
) -> SXBetInstrumentProvider:
    """
    Create a standalone instrument provider for SX.bet.
    """
    http_client = SXBetHttpClient(
        api_key=api_key,
        api_url=api_url or SXBET_API_BASE_URL,
        logger=logger,
    )

    config = SXBetInstrumentProviderConfig(
        api_key=api_key,
        api_url=api_url,
        sport_ids=frozenset(sport_ids) if sport_ids else None,
        league_ids=frozenset(league_ids) if league_ids else None,
    )

    return SXBetInstrumentProvider(
        http_client=http_client,
        config=config,
        logger=logger,
    )
