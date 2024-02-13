# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2023 Nautech Systems Pty Ltd. All rights reserved.
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

import asyncio

import pytest

from nautilus_trader.common.enums import LogLevel
from nautilus_trader.network.socket import SocketClient
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.adapters.cloudbet.common import VENUE
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetTestStubs
from tests.integration_tests.network.conftest import socket_server


pytestmark = pytest.mark.skip(reason="WIP: Socket server")

@pytest.fixture()
def venue() -> Venue:
    return VENUE


@pytest.fixture()
def socket_client(event_loop, logger, host, port, handler) -> SocketClient:
    client = SocketClient(
        host=host,
        port=port,
        loop=event_loop,
        handler=handler,
        logger=TestComponentStubs.logger(),
        ssl=False,
    )
    return client
@pytest.fixture(autouse=False)
def cloudbet_client(event_loop, logger):
    return CloudbetTestStubs.cloudbet_client(event_loop, logger)


@pytest.fixture()
def instrument_provider(cloudbet_client):
    return CloudbetTestStubs.instrument_provider(cloudbet_client=cloudbet_client)


@pytest.mark.asyncio()
async def test_heartbeat():
    # Test the heartbeat functionality
    # You might want to mock the websocket and make sure "heartbeat" is sent every 120 seconds
    # check heartbeat is received,
    # check heartbeat task has been called once
    # check heartbeat task has been scheduled
    pass


@pytest.mark.asyncio()
async def test_server_connect():
    # check component state is READY, RUNNING after connect
    #
    pass


@pytest.mark.asyncio()
async def test_server_disconnect():
    # check component state is READY, RUNNING after connect
    #
    pass

@pytest.mark.asyncio()
async def test_socket_base_connect(event_loop, venue):
    messages = []
    sdfsdf = []

    def handler(raw):
        messages.append(raw)
        if len(messages) > 5:
            _client.stop()

    host, port = socket_server

    await client.connect()
    await asyncio.sleep(3)
    assert messages == [b"hello"] * 6
    await asyncio.sleep(1)


@pytest.mark.asyncio()
async def test_socket_base_reconnect_on_incomplete_read(closing_socket_server, event_loop):
    messages = []

    def handler(raw):
        messages.append(raw)

    host, port = closing_socket_server
    client = SocketClient(
        host=host,
        port=port,
        loop=event_loop,
        handler=handler,
        logger=TestComponentStubs.logger(level=LogLevel.DEBUG),
        ssl=False,
    )
    # mock_post_conn = mock.patch.object(client, "post_connection")
    await client.connect()
    await asyncio.sleep(0.1)
    assert messages == [b"hello"] * 1

    # Reconnect and receive another message
    await asyncio.sleep(1)
    assert client._reconnection_count >= 1
