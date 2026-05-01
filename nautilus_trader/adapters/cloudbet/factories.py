import asyncio
from functools import lru_cache

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.common.logging import LoggerAdapter

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.config import (
    CloudbetDataClientConfig,
    CloudbetExecClientConfig,
)
from nautilus_trader.adapters.cloudbet.data_client import CloudbetDataClient
from nautilus_trader.adapters.cloudbet.execution import CloudbetLiveExecutionClient
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.msgbus.bus import MessageBus

CLIENTS: dict[str, CloudbetClient] = {}
INSTRUMENT_PROVIDER = None


@lru_cache(1)
def get_cached_cloudbet_client(
    loop: asyncio.AbstractEventLoop,
    logger: Logger,
    api_key: str | None = None,
    api_url: str | None = None,
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
    api_key : str, optional
        The Cloudbet API key.
    api_url : str, optional
        The Cloudbet API URL.

    Returns
    -------
    CloudbetClient

    """
    global CLIENTS

    key: str = "|".join((api_url or "", api_key or ""))
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


def get_cached_cloudbet_instrument_provider(
    client: CloudbetClient,
    logger: Logger,
    config: InstrumentProviderConfig | None = None,
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
    config : InstrumentProviderConfig, optional
        The instrument provider configuration.

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
            config=config,
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
        logger: Logger | None = None,
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
        logger = logger or Logger(name=f"CloudbetDataClient-{name}")
        market_filter: tuple = config.market_filter or ()

        client = get_cached_cloudbet_client(
            loop=loop, logger=logger, api_key=config.api_key, api_url=config.api_url
        )
        provider = get_cached_cloudbet_instrument_provider(
            client=client,
            logger=logger,
            config=config.instrument_provider,
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
            config=config,
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
        logger: Logger | None = None,
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
        base_currency : Currency, optional
            The base currency for the client. Explicitly pass None for multi-currency exec clients.

        Returns
        -------
        CloudbetLiveExecutionClient

        """
        logger = logger or Logger(name=f"CloudbetExecutionClient-{name}")
        market_filter: tuple = ()

        client = get_cached_cloudbet_client(
            loop=loop, logger=logger, api_key=config.api_key, api_url=config.api_url
        )

        provider = get_cached_cloudbet_instrument_provider(client=client, logger=logger)

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
            config=config,
        )
        return exec_client
