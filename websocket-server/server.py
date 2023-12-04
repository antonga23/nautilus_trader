from typing import Optional, Any
from nautilus_trader.common.component import Component
from nautilus_trader.core.rust.common import LogLevel

from nautilus_trader.model.identifiers import Venue, TraderId, ComponentId
from nautilus_trader.common.clock import LiveClock, Clock
from nautilus_trader.common.logging import Logger
import websockets
import asyncio
from nautilus_trader.msgbus.bus import MessageBus
from nautilus_trader.common.enums import ComponentState
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.core.rust.common import LogColor
from nautilus_trader.core.rust.common import LogLevel
from nautilus_trader.network.http import HttpClient
from nautilus_trader.data.client import DataClient

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.providers import InstrumentProvider


class SocketServer(Component):
    """

    The base class for all websocket servers.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the component.
    clock : Clock
        The clock for the component.
    logger : Logger
        The logger for the component.
    venue : Venue,
        The client venue.
    msgbus : MessageBus
        The message bus for the component (required before initialized).
    component_name : str
        The custom component name. Required for the component_id ex "My-Component" must have a dash.
    socket_url : str, optional
        The websocket url. (default localhost)
    socket_port: int, optional (default 8765)
        The websocket port.
    cache : Cache, optional
        The cache for the component.
    data_client : DataClient, optional
        The data client for the component. Not required on initialization.
    config : dict[str, Any], optional
        The configuration for the component.

     Warnings
    --------
    This class should not be used directly, but through a concrete subclass.

    Attributes
    ----------
        _loop : asyncio.AbstractEventLoop
            The event loop for the component.
        _venue : Venue
            The client venue.
        _id : str
            The custom component name. Required for the component_id ex "My-Component" must have a dash
        _socket_url : str, optional
            The websocket url. (default localhost)
        _socket_port: int, optional (default 8765)
            The websocket port.
        _server : Optional[Task]
            The websocket server task.
        _cache: Optional[Cache]
            The cache for the component.
        _data_client : DataClient
            The data client for the component. Not required on initialization.
        _log : LoggerAdapter
            The logger adapter used for the component (implicitly created by parent Component class).
        _state : ComponentState
            The state of the component.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, clock: Clock, logger: Logger, venue: Venue,
                 msgbus: MessageBus, component_name: str, socket_url: str = 'localhost', socket_port: int = 8765,
                 cache: Optional[Cache] = None, data_client: Optional[DataClient] = None,
                 config: Optional[dict[str, Any]] = None, ):
        self._loop = loop
        self._venue = venue
        self._msgbus = msgbus
        self._socket_url = socket_url
        self._socket_port = socket_port
        self._server = None
        self._cache = cache
        self._data_client = data_client

        super().__init__(clock=clock,
                         logger=logger,
                         trader_id=trader_id,
                         component_id=ComponentId(component_name),
                         component_name=component_name,
                         msgbus=msgbus,
                         config=config)

    async def heartbeat(self, websocket, sleep_time: int = 120):
        while True:
            self._log.info(f"Sending heartbeat from {self.id}")
            await websocket.send("heartbeat")
            await asyncio.sleep(sleep_time)  # Send heartbeat every {sleep_time} seconds
            await self._post_heartbeat()

    async def load_data_client(self, data_client: Optional[DataClient] = None):

        if self._data_client is None:
            self._log.info("Loading data client")
            self._data_client = data_client
        else:
            self._log.info("Data client already loaded")

    async def load_cache(self, cache: Optional[Cache] = None):
        if self._cache is None:
            self._log.info("Loading cache client")
            self._cache = cache
        else:
            self._log.info("Data client already loaded")

    async def server_coro(self, websocket):
        # Schedule the heartbeat coroutine for this connection
        self._log.info(f"Creating heartbeat task for {self.id}")
        heartbeat_task = asyncio.create_task(self.heartbeat(websocket))
        try:
            while ComponentState.READY <= self.state <= ComponentState.RUNNING:  # see state map for component states
                recv_text = await websocket.recv()  # Receive incoming messages (and print them)
                self._log.info(f"Received: {recv_text}")
                await websocket.send("Hello Client")
        except websockets.ConnectionClosed:
            self._log.info("Connection with client closed")
            heartbeat_task.cancel()  # Cancel the heartbeat task when the client disconnects
            try:
                await heartbeat_task  # Wait for the heartbeat task to finish cleanup
            except asyncio.CancelledError:
                pass  # It's normal to get a CancelledError when cancelling a task
                self._log.info("Heartbeat task cancelled")
        except Exception as e:
            self._log.error(f"Exception: {e}")

    async def start_server(self) -> None:
        async with websockets.serve(self.server_coro, self._socket_url, self._socket_port):
            await asyncio.Future()  # This will never complete, so the server runs forever

    async def _start(self) -> None:
        self._server = self._loop.create_task(self.start_server())
        await self._post_start()

    async def _stop(self) -> None:
        if self._server is not None:
            self._server.cancel()  # Cancel the server task
            try:
                self._loop.run_until_complete(self._server)  # Wait for the server task to finish cleanup
            except asyncio.CancelledError:
                pass  # It's normal to get a CancelledError when cancelling a task
            await self._post_stop()

    def _register(self):
        """
        Actions to be performed on registration.

        """
        pass

    def _deregister(self):
        """
        Actions to be performed on registeration.

        """
        pass

    def _dispose(self):
        pass  # You will need to implement disposal logic, closing sockets, releasing resources, etc.

    # -- Co-routines ------------------------------------------------------------------------------

    async def _post_heartbeat(self) -> None:
        """
        Actions to be performed post reconnection.

        """
        # Override to implement additional heartbeat related behaviour
        # (resubscribing etc.).
        pass

    async def _post_start(self) -> None:
        """
        Actions to be performed post start.

        """
        # Override to implement additional behaviour after starting the server
        # (resubscribing etc.).
        pass

    async def _post_stop(self) -> None:
        """
        Actions to be performed post stop.

        """
        # Override to implement additional behaviour after stopping the server
        # (resubscribing etc.).
        # self._server = None
        pass


# -- Co-routines ------------------------------------------------------------------------------
if __name__ == "__main__":
    # Instantiate the server with a clock and logger
    clock = LiveClock()
    trader_id = TraderId('ALATHA-23')
    logger = Logger(clock, trader_id)
    # instantiate a message bus
    msgbus = MessageBus(trader_id, clock, logger)
    server = SocketServer(loop=asyncio.get_event_loop(), clock=clock, logger=logger, venue=CLOUDBET_VENUE,
                          msgbus=msgbus, component_name="websocket-server")
    # Start the server
    server.start()

    # Keep the script running
    asyncio.get_event_loop().run_forever()
