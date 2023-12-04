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

from nautilus_trader.common.providers import InstrumentProvider


class SocketServer(Component):
    """
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
    data_client : DataClient, optional
        The data client for the component. Not required on initialization.
    config : dict[str, Any], optional
        The configuration for the component.

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
        _data_client : DataClient
            The data client for the component. Not required on initialization.
        _log : LoggerAdapter
            The logger adapter used for the component (implicitly created by parent Component class).
        _state : ComponentState
            The state of the component.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, clock: Clock, logger: Logger, venue: Venue,
                 msgbus: MessageBus, component_name: str, socket_url: str = 'localhost', socket_port: int = 8765,
                 data_client: Optional[DataClient] = None, config: Optional[dict[str, Any]] = None,):
        self._loop = loop
        self._venue = venue
        self._socket_url = socket_url
        self._socket_port = socket_port
        self._server = None
        self._data_client = data_client

        super().__init__(clock=clock,
                         logger=logger,
                         trader_id=trader_id,
                         component_id=ComponentId(component_name),
                         component_name=component_name,
                         msgbus=msgbus,
                         config=config)

    async def heartbeat(self, websocket):
        while True:
            self._log.info(f"Sending heartbeat from {self.id}")
            await websocket.send("heartbeat")
            await asyncio.sleep(120)  # Send heartbeat every 5 seconds

    async def load_data_client(self, data_client: Optional[DataClient] = None):

        if self._data_client is None:
            self._log.info("Loading data client")
            self._data_client = data_client
    #         if data_client is not None:
    #             self._data_client = data_client
    #         else:
    #             # throw exception as data client is required
    #             self._log.error("Data client is required")
    #             self._stop()
    #     else:
    #         self._log.info("Data client already loaded")
    #     self._data_client = await self.msgbus.load_data_client(self._venue)

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

    async def start_server(self):
        async with websockets.serve(self.server_coro, self._socket_url, self._socket_port):
            await asyncio.Future()  # This will never complete, so the server runs forever

    def _start(self):
        self._server = self._loop.create_task(self.start_server())

    def _stop(self):
    # You will need to implement graceful shutdown, cancelling pending tasks, etc.
        if self._server is not None:
            self._server.cancel()  # Cancel the server task
            try:
                self._loop.run_until_complete(self._server)  # Wait for the server task to finish cleanup
            except asyncio.CancelledError:
                pass  # It's normal to get a CancelledError when cancelling a task
            self._server = None

    def _dispose(self):
        pass  # You will need to implement disposal logic, closing sockets, releasing resources, etc.


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
