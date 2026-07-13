# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the SX.bet Centrifugo realtime transport.
# -------------------------------------------------------------------------------------------------

import asyncio
import json
from typing import Any
from typing import NamedTuple
from unittest.mock import AsyncMock
from unittest.mock import Mock

import aiohttp
import pytest

from nautilus_trader.adapters.sxbet.realtime import SXBetRealtimeClient
from nautilus_trader.adapters.sxbet.realtime import SXBetRealtimeError
from nautilus_trader.adapters.sxbet.realtime import market_hash_from_channel
from nautilus_trader.adapters.sxbet.realtime import order_book_channel


class _Msg(NamedTuple):
    type: Any
    data: Any


_CHANNEL = order_book_channel("0xabc")


def _connect_reply(*, ping: int = 25, pong: bool = True) -> str:
    return json.dumps({"id": 1, "connect": {"client": "c1", "ping": ping, "pong": pong}})


def _publication(channel: str, data: object, offset: int = 0) -> str:
    pub: dict = {"data": data}
    if offset:
        pub["offset"] = offset
    return json.dumps({"push": {"channel": channel, "pub": pub}})


class _ScriptedWS:
    """
    A fake WebSocket that emits scripted server frames then closes.
    """

    def __init__(self, script: list[str]) -> None:
        self.sent: list[str] = []
        self._incoming = list(script)
        self.closed = False

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)

    async def receive(self) -> _Msg:
        if self.closed or not self._incoming:
            self.closed = True
            return _Msg(aiohttp.WSMsgType.CLOSED, None)
        return _Msg(aiohttp.WSMsgType.TEXT, self._incoming.pop(0))

    async def close(self) -> None:
        self.closed = True


class _BlockingWS:
    """
    A fake WebSocket that connects then never sends — parks the read loop.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)

    async def receive(self) -> _Msg:
        await asyncio.sleep(3600)
        return _Msg(aiohttp.WSMsgType.CLOSED, None)

    async def close(self) -> None:
        self.closed = True


def _http_client_with_tokens(counter: dict) -> Mock:
    async def get_token() -> dict:
        counter["n"] += 1
        return {"token": f"t{counter['n']}"}

    http_client = Mock()
    http_client.get_realtime_token = AsyncMock(side_effect=get_token)
    return http_client


# -- pure framing / parsing ------------------------------------------------------------------


def test_channel_helpers_round_trip():
    assert _CHANNEL == "order_book:market_0xabc"
    assert market_hash_from_channel(_CHANNEL) == "0xabc"
    assert market_hash_from_channel("markets:foo") is None


def test_encode_decode_commands_match_centrifugo_json_codec():
    encoded = SXBetRealtimeClient._encode_commands(
        [{"id": 1, "connect": {"token": "jwt"}}, {"id": 2, "subscribe": {"channel": "c"}}],
    )
    # Newline-joined JSON, exactly as centrifuge-python's JSON codec frames commands.
    assert (
        encoded
        == '{"id": 1, "connect": {"token": "jwt"}}\n{"id": 2, "subscribe": {"channel": "c"}}'
    )
    assert SXBetRealtimeClient._decode_replies(encoded + "\n") == [
        {"id": 1, "connect": {"token": "jwt"}},
        {"id": 2, "subscribe": {"channel": "c"}},
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"token": "abc"}, "abc"),
        ({"data": {"token": "abc"}}, "abc"),
        ({"status": "success"}, None),
        ("nope", None),
    ],
)
def test_extract_token_handles_both_response_shapes(payload, expected):
    assert SXBetRealtimeClient._extract_token(payload) == expected


@pytest.mark.asyncio
async def test_missing_token_raises():
    http_client = Mock()
    http_client.get_realtime_token = AsyncMock(return_value={"status": "success"})
    client = SXBetRealtimeClient(http_client, on_publication=Mock())
    with pytest.raises(SXBetRealtimeError):
        await client._fetch_token()


# -- publication dispatch + offset dedup -----------------------------------------------------


@pytest.mark.asyncio
async def test_handle_push_invokes_callback_with_decoded_data():
    received: list[tuple[str, object]] = []

    async def on_pub(channel: str, data: object) -> None:
        received.append((channel, data))

    client = SXBetRealtimeClient(Mock(), on_publication=on_pub)
    orders = [{"orderHash": "0x1", "status": "ACTIVE"}]
    await client._handle_push({"channel": _CHANNEL, "pub": {"data": orders, "offset": 1}})

    assert received == [(_CHANNEL, orders)]


@pytest.mark.asyncio
async def test_handle_push_dedups_and_orders_by_offset():
    calls: list[object] = []

    async def on_pub(channel: str, data: object) -> None:
        calls.append(data)

    client = SXBetRealtimeClient(Mock(), on_publication=on_pub)
    await client._handle_push({"channel": _CHANNEL, "pub": {"data": 5, "offset": 5}})
    await client._handle_push({"channel": _CHANNEL, "pub": {"data": 5, "offset": 5}})  # dup
    await client._handle_push({"channel": _CHANNEL, "pub": {"data": 4, "offset": 4}})  # stale
    await client._handle_push({"channel": _CHANNEL, "pub": {"data": 6, "offset": 6}})  # newer

    assert calls == [5, 6]
    assert client._channel_offsets[_CHANNEL] == 6


@pytest.mark.asyncio
async def test_dispatch_empty_reply_sends_pong_when_negotiated():
    client = SXBetRealtimeClient(Mock(), on_publication=Mock())
    client._send_pong = True
    client._ws = _BlockingWS()
    await client._dispatch_reply({})
    assert client._ws.sent == ["{}"]


# -- full connection lifecycle ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_subscribes_and_streams_publication():
    counter = {"n": 0}
    http_client = _http_client_with_tokens(counter)
    received: list[tuple[str, object]] = []
    done = asyncio.Event()

    async def on_pub(channel: str, data: object) -> None:
        received.append((channel, data))
        done.set()

    orders = [{"orderHash": "0x1", "status": "ACTIVE"}]
    scripted = _ScriptedWS([_connect_reply(), _publication(_CHANNEL, orders, offset=1)])
    blocking = _BlockingWS()
    made: list[object] = []

    async def connector(url: str) -> object:
        ws = scripted if not made else blocking
        made.append(ws)
        return ws

    client = SXBetRealtimeClient(
        http_client,
        on_publication=on_pub,
        ws_connector=connector,
        reconnect_backoff_initial_secs=0.01,
        reconnect_backoff_max_secs=0.02,
    )
    await client.subscribe(_CHANNEL)
    await client.start()
    await asyncio.wait_for(done.wait(), timeout=2.0)

    # Connect command carried the freshly-fetched token.
    assert json.loads(scripted.sent[0])["connect"]["token"] == "t1"
    # Subscribe command was sent for the desired channel with recovery flags.
    subscribe_cmd = json.loads(scripted.sent[1])["subscribe"]
    assert subscribe_cmd["channel"] == _CHANNEL
    assert subscribe_cmd["recoverable"] is True
    assert subscribe_cmd["positioned"] is True
    assert received == [(_CHANNEL, orders)]

    await client.stop()


@pytest.mark.asyncio
async def test_reconnect_refreshes_token_and_resubscribes():
    counter = {"n": 0}
    http_client = _http_client_with_tokens(counter)
    connected: list[bool] = []

    async def on_connected() -> None:
        connected.append(True)

    # Both scripted sockets serve only the connect reply then close, forcing a reconnect.
    first = _ScriptedWS([_connect_reply()])
    second = _ScriptedWS([_connect_reply()])
    blocking = _BlockingWS()
    fakes = [first, second]
    made: list[object] = []

    async def connector(url: str) -> object:
        ws = fakes.pop(0) if fakes else blocking
        made.append(ws)
        return ws

    client = SXBetRealtimeClient(
        http_client,
        on_publication=Mock(),
        ws_connector=connector,
        reconnect_backoff_initial_secs=0.01,
        reconnect_backoff_max_secs=0.02,
        on_connected=on_connected,
    )
    await client.subscribe(_CHANNEL)
    await client.start()

    # Wait until the second connection has completed its subscribe handshake.
    async def _second_subscribed() -> None:
        while not (len(second.sent) >= 2 and len(connected) >= 2):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_second_subscribed(), timeout=3.0)
    await client.stop()

    # Token was re-fetched for the reconnect (exponential backoff between attempts).
    assert counter["n"] >= 2
    assert json.loads(first.sent[0])["connect"]["token"] == "t1"
    assert json.loads(second.sent[0])["connect"]["token"] == "t2"
    # The desired channel was resubscribed on the new connection.
    assert any(
        json.loads(cmd).get("subscribe", {}).get("channel") == _CHANNEL for cmd in second.sent
    )


@pytest.mark.asyncio
async def test_stale_connection_without_ping_raises():
    # A silent socket beyond the negotiated ping interval + margin must be
    # detected as stale so the reconnect path can take over.
    client = SXBetRealtimeClient(Mock(), on_publication=Mock())
    client._ws = _BlockingWS()
    client._ping_interval_secs = 0.05
    client._staleness_margin_secs = 0.05
    client._running = True

    with pytest.raises(SXBetRealtimeError, match="stale"):
        await client._read_loop()
