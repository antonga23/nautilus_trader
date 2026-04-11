# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet factory functions.
# -------------------------------------------------------------------------------------------------

from nautilus_trader.adapters.easybet.config import EasybetDataClientConfig
from nautilus_trader.adapters.easybet.config import EasybetExecClientConfig
from nautilus_trader.adapters.easybet.data import EasybetDataClient
from nautilus_trader.adapters.easybet.execution import EasybetExecutionClient
from nautilus_trader.adapters.easybet.providers import EasybetInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.identifiers import ClientId


class EasybetLiveDataClientFactory(LiveDataClientFactory):
    """
    Factory for Easybet live data clients.
    """

    @staticmethod
    def create(  # type: ignore[override]
        loop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: EasybetDataClientConfig,
    ) -> EasybetDataClient:
        """
        Create Easybet data client instance.
        """
        # Create instrument provider
        instrument_provider = EasybetInstrumentProvider(logger=logger)

        # Create data client
        return EasybetDataClient(
            loop=loop,
            client_id=client_id,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
            instrument_provider=instrument_provider,
        )


class EasybetLiveExecClientFactory(LiveExecClientFactory):
    """
    Factory for Easybet live execution clients.
    """

    @staticmethod
    def create(  # type: ignore[override]
        loop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        config: EasybetExecClientConfig,
    ) -> EasybetExecutionClient:
        """
        Create Easybet execution client instance.
        """
        return EasybetExecutionClient(
            loop=loop,
            client_id=client_id,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )
