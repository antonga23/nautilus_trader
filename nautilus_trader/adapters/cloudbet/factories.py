import asyncio
import os
from functools import lru_cache
from typing import Optional, Any, Union

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.common.logging import LoggerAdapter

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.config import CloudbetDataClientConfig, CloudbetExecClientConfig
from nautilus_trader.adapters.cloudbet.data_client import CloudbetDataClient
from nautilus_trader.adapters.cloudbet.execution import CloudbetLiveExecutionClient
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.msgbus.bus import MessageBus

from nautilus_trader.model.currency import Currency

CLIENTS: dict[str, CloudbetClient] = {}
INSTRUMENT_PROVIDER = None


@lru_cache(1)
def get_cached_cloudbet_client(
    loop: asyncio.AbstractEventLoop,
    logger: Logger,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
) -> CloudbetClient:
    """
    Cache and return a Cloudbet HTTP client with the given credentials.

    If a cached client with matching credentials already exists, then that
    cached client will be returned.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    logger : Logger
        The logger for the client.
    Returns
    -------
    CloudbetClient

    """
    global CLIENTS

    key: str = "|".join((api_url, api_key))
    if key not in CLIENTS:
        LoggerAdapter("CloudbetFactory", logger).warning(
            "Creating new instance of CloudbetClient",
        )
        client = CloudbetClient(
            loop=loop,
            logger=logger,
            api_key=api_key,
            api_url=api_url,
        )
        CLIENTS[key] = client
    return CLIENTS[key]


@lru_cache(1)
def get_cached_cloudbet_instrument_provider(
    client: CloudbetClient,
    logger: Logger,
    market_filter: tuple,
) -> CloudbetInstrumentProvider:
    """
    Cache and return a CloudbetInstrumentProvider.

    If a cached provider already exists, then that cached provider will be returned.

    Parameters
    ----------
    client : CloudbetClient
        The client for the instrument provider.
    logger : Logger
        The logger for the instrument provider.
    market_filter : tuple
        The market filter to load into the instrument provider.

    Returns
    -------
    CloudbetInstrumentProvider

    """
    global INSTRUMENT_PROVIDER
    if INSTRUMENT_PROVIDER is None:
        LoggerAdapter("CloudbetFactory", logger).warning(
            "Creating new instance of CloudbetInstrumentProvider",
        )
        INSTRUMENT_PROVIDER = CloudbetInstrumentProvider(
            client=client,
            logger=logger,
            filters=market_filter,
        )
    return INSTRUMENT_PROVIDER


class CloudbetLiveDataClientFactory(LiveDataClientFactory):
    """
    Provides a `Cloudbet` live data client factory.
    """

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: CloudbetDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
    ) -> CloudbetDataClient:
        """
        Create a new Cloudbet data client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The client name.
        config : dict[str, Any]
            The configuration dictionary.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock : LiveClock
            The clock for the client.
        logger : Logger
            The logger for the client.

        Returns
        -------
        CloudbetDataClient

        """
        market_filter: tuple = config.market_filter or ()

        # Create client
        client = get_cached_cloudbet_client(
            loop=loop,
            logger=logger,
            api_key=config.api_key,
            api_url=config.api_url
        )
        provider = get_cached_cloudbet_instrument_provider(
            client=client,
            logger=logger,
            market_filter=market_filter,
        )

        data_client = CloudbetDataClient(
            loop=loop,
            client=client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            market_filter=dict(market_filter),
            instrument_provider=provider,
        )
        return data_client


class CloudbetLiveExecClientFactory(LiveExecClientFactory):
    """
    Provides data and execution clients for Cloudbet.
    """

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: CloudbetExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
    ) -> CloudbetLiveExecutionClient:
        """
        Create a new Cloudbet execution client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The client name.
        config : LiveExecClientConfig
            The configuration for the client.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock : LiveClock
            The clock for the client.
        logger : Logger
            The logger for the client.
        base_currency : Union[Currency, None]
            The base currency for the client. Explicitly pass None for multi-currency exec clients.
        Returns
        -------
        CloudbetLiveExecutionClient

        """
        market_filter: tuple = dict or ()

        client = get_cached_cloudbet_client(
            loop=loop,
            logger=logger,
            api_key=config.api_key,
            api_url=config.api_url
        )

        provider = get_cached_cloudbet_instrument_provider(
            client=client,
            logger=logger,
            market_filter=market_filter,
        )

        # Create client
        exec_client = CloudbetLiveExecutionClient(
            loop=loop,
            client=client,
            base_currency=config.base_currency,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            market_filter=market_filter,
            instrument_provider=provider,
            config=config.dict(),
        )
        return exec_client
