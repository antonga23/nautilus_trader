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
SX.bet realtime order-book streaming over the Centrifugo client protocol.

SX.bet exposes realtime updates through a Centrifugo server (the Ably feed it
replaced was deprecated 2026-07-01). This module speaks the Centrifugo
bidirectional client protocol (JSON variant) directly over a raw WebSocket, so
no proprietary SDK dependency is required — the ``aiohttp`` stack already used by
:class:`SXBetHttpClient` is reused for the socket.

The protocol framing implemented here matches the ``centrifuge-python`` /
``centrifuge-js`` JSON codec:

* Commands are newline-joined JSON objects; replies are newline-split JSON.
* ``connect`` bootstraps the session and negotiates the server ``ping`` interval
  and whether a ``pong`` is expected.
* ``subscribe`` rides the same socket per channel (``recoverable``/``positioned``
  for offset-based recovery on reconnect).
* Publications arrive as ``{"push": {"channel": ..., "pub": {"data": ..., "offset": ...}}}``
  with no reply id.
* A bare ``{}`` reply is a server ping; the client answers with a bare ``{}``.

This class is transport-only: it maintains the connection, offset-deduplicates
publications, and hands decoded order-object payloads to a callback. Order-book
state and quote pricing live in :mod:`nautilus_trader.adapters.sxbet.data` so the
streaming and polling paths share one pricing implementation.

"""

import asyncio
import contextlib
import json
import secrets
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import aiohttp

from nautilus_trader.adapters.sxbet.constants import SXBET_ORDER_BOOK_CHANNEL_TEMPLATE
from nautilus_trader.adapters.sxbet.constants import SXBET_REALTIME_WS_URL
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.common.component import Logger


PublicationCallback = Callable[[str, Any], Awaitable[None] | None]
HealthCallback = Callable[[], Awaitable[None] | None]


def order_book_channel(market_hash: str) -> str:
    """
    Return the Centrifugo order-book-update channel name for a market hash.
    """
    return SXBET_ORDER_BOOK_CHANNEL_TEMPLATE.format(market_hash=market_hash)


def market_hash_from_channel(channel: str) -> str | None:
    """
    Extract the market hash from an ``order_book:market_{hash}`` channel name.
    """
    prefix = "order_book:market_"
    if channel.startswith(prefix):
        return channel[len(prefix) :]
    return None


class SXBetRealtimeError(Exception):
    """
    Raised for SX.bet realtime streaming failures.
    """


class SXBetRealtimeClient:
    """
    Centrifugo realtime client for SX.bet order-book streaming.

    Parameters
    ----------
    http_client : SXBetHttpClient
        Used to fetch a fresh realtime JWT for every (re)connect.
    on_publication : PublicationCallback
        Invoked as ``(channel, data)`` for each order-book publication, where
        ``data`` is the decoded JSON payload (an array of order objects).
    logger : Logger, optional
        Logger instance.
    ws_url : str, optional
        Centrifugo WebSocket URL. Defaults to the SX mainnet endpoint.
    ws_connector : callable, optional
        Coroutine returning a connected WebSocket-like object. Injected in tests
        to avoid live network; defaults to an ``aiohttp`` connector.
    reconnect_backoff_initial_secs : float, default 1.0
        Initial reconnect delay (doubled each failed attempt).
    reconnect_backoff_max_secs : float, default 30.0
        Ceiling for the exponential reconnect delay.
    max_reconnect_attempts : int, optional
        Stop reconnecting after this many consecutive failures. ``None`` retries
        indefinitely.
    connect_timeout_secs : float, default 10.0
        Timeout for the connect handshake.
    staleness_margin_secs : float, default 10.0
        Slack added to the negotiated ping interval before a silent connection is
        treated as stale and force-reconnected.
    on_connected : HealthCallback, optional
        Invoked after a successful connect handshake (streaming is healthy).
    on_disconnected : HealthCallback, optional
        Invoked when the connection drops (fallback should take over).

    """

    def __init__(
        self,
        http_client: SXBetHttpClient,
        on_publication: PublicationCallback,
        logger: Logger | None = None,
        *,
        ws_url: str | None = None,
        ws_connector: Callable[[str], Awaitable[Any]] | None = None,
        reconnect_backoff_initial_secs: float = 1.0,
        reconnect_backoff_max_secs: float = 30.0,
        max_reconnect_attempts: int | None = None,
        connect_timeout_secs: float = 10.0,
        staleness_margin_secs: float = 10.0,
        on_connected: HealthCallback | None = None,
        on_disconnected: HealthCallback | None = None,
    ) -> None:
        self._http_client = http_client
        self._on_publication = on_publication
        self._log = logger
        self._ws_url = ws_url or SXBET_REALTIME_WS_URL
        self._ws_connector = ws_connector or self._default_ws_connect
        self._reconnect_backoff_initial_secs = reconnect_backoff_initial_secs
        self._reconnect_backoff_max_secs = reconnect_backoff_max_secs
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connect_timeout_secs = connect_timeout_secs
        self._staleness_margin_secs = staleness_margin_secs
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._ws: Any = None
        self._run_task: asyncio.Task | None = None
        self._running = False
        self._connected = False
        self._command_id = 0
        self._send_pong = True
        self._ping_interval_secs = 0.0
        self._desired_channels: set[str] = set()
        self._channel_offsets: dict[str, int] = {}
        self._session: aiohttp.ClientSession | None = None

    @property
    def is_connected(self) -> bool:
        """
        Return whether the Centrifugo session is currently established.
        """
        return self._connected

    async def start(self) -> None:
        """
        Start the realtime client connection loop.
        """
        if self._running:
            return
        self._running = True
        self._run_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        """
        Stop the realtime client and close the connection.
        """
        self._running = False
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
        await self._close_ws()
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def subscribe(self, channel: str) -> None:
        """
        Subscribe to a channel, sending the command immediately if connected.
        """
        if channel in self._desired_channels:
            return
        self._desired_channels.add(channel)
        if self._connected:
            await self._send_subscribe(channel)

    async def unsubscribe(self, channel: str) -> None:
        """
        Unsubscribe from a channel.
        """
        self._desired_channels.discard(channel)
        self._channel_offsets.pop(channel, None)
        if self._connected:
            await self._send_command({"unsubscribe": {"channel": channel}})

    # -- connection loop ---------------------------------------------------------------------

    async def _run_forever(self) -> None:
        attempt = 0
        backoff = self._reconnect_backoff_initial_secs
        while self._running:
            try:
                await self._connect_once()
                attempt = 0
                backoff = self._reconnect_backoff_initial_secs
                await self._read_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._log:
                    self._log.warning(f"SX.bet realtime connection error: {type(e).__name__}: {e}")
            finally:
                await self._mark_disconnected()

            if not self._running:
                break

            attempt += 1
            if self._max_reconnect_attempts is not None and attempt > self._max_reconnect_attempts:
                if self._log:
                    self._log.error(
                        f"SX.bet realtime giving up after {attempt - 1} reconnect attempts",
                    )
                break

            jitter = secrets.randbelow(500) / 1000
            delay = min(backoff, self._reconnect_backoff_max_secs) + jitter
            if self._log:
                self._log.info(
                    f"SX.bet realtime reconnecting in {delay:.2f}s (attempt {attempt})",
                )
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self._reconnect_backoff_max_secs)

    async def _connect_once(self) -> None:
        token = await self._fetch_token()
        self._ws = await asyncio.wait_for(
            self._ws_connector(self._ws_url),
            timeout=self._connect_timeout_secs,
        )
        self._command_id = 0
        connect_reply = await self._request({"connect": {"token": token, "name": "nautilus"}})
        connect_result = connect_reply.get("connect", {}) if isinstance(connect_reply, dict) else {}
        self._send_pong = bool(connect_result.get("pong", False))
        self._ping_interval_secs = float(connect_result.get("ping", 0) or 0)
        self._connected = True
        if self._log:
            self._log.info(
                "SX.bet realtime connected "
                f"(ping={self._ping_interval_secs}s pong={self._send_pong})",
            )
        for channel in sorted(self._desired_channels):
            await self._send_subscribe(channel)
        if self._on_connected is not None:
            await self._maybe_await(self._on_connected())

    async def _read_loop(self) -> None:
        # Staleness detection: if the server ping interval is known, a silent
        # socket beyond ping + margin is treated as dead and force-reconnected.
        recv_timeout = (
            self._ping_interval_secs + self._staleness_margin_secs
            if self._ping_interval_secs > 0
            else None
        )
        while self._running and self._ws is not None:
            try:
                raw = await asyncio.wait_for(self._recv(), timeout=recv_timeout)
            except TimeoutError as e:
                raise SXBetRealtimeError("realtime connection stale (no server ping)") from e
            if raw is None:
                raise SXBetRealtimeError("realtime connection closed by server")
            for reply in self._decode_replies(raw):
                await self._dispatch_reply(reply)

    async def _dispatch_reply(self, reply: dict[str, Any]) -> None:
        if not reply:
            # Bare {} is a server ping; answer with a bare {} pong when required.
            if self._send_pong:
                await self._send_command({})
            return
        push = reply.get("push")
        if push:
            await self._handle_push(push)

    async def _handle_push(self, push: dict[str, Any]) -> None:
        pub = push.get("pub")
        if pub is None:
            # join / leave / unsubscribe / disconnect pushes are not needed here.
            return
        channel = push.get("channel", "")
        offset = int(pub.get("offset", 0) or 0)
        if offset and offset <= self._channel_offsets.get(channel, 0):
            # Duplicate or out-of-order replay from recovery; already applied.
            return
        if offset:
            self._channel_offsets[channel] = offset
        data = pub.get("data")
        await self._maybe_await(self._on_publication(channel, data))

    async def _send_subscribe(self, channel: str) -> None:
        subscribe: dict[str, Any] = {"channel": channel, "recoverable": True, "positioned": True}
        last_offset = self._channel_offsets.get(channel)
        if last_offset:
            subscribe["recover"] = True
            subscribe["offset"] = last_offset
        await self._send_command({"subscribe": subscribe})

    # -- framing helpers ---------------------------------------------------------------------

    def _next_id(self) -> int:
        self._command_id += 1
        return self._command_id

    async def _request(self, command: dict[str, Any]) -> dict[str, Any]:
        command_id = self._next_id()
        command = {"id": command_id, **command}
        await self._raw_send(self._encode_commands([command]))
        # Read replies until the matching id arrives, answering pings meanwhile.
        while True:
            raw = await asyncio.wait_for(self._recv(), timeout=self._connect_timeout_secs)
            if raw is None:
                raise SXBetRealtimeError("connection closed during handshake")
            for reply in self._decode_replies(raw):
                if reply.get("id") == command_id:
                    if reply.get("error"):
                        raise SXBetRealtimeError(f"command error: {reply['error']}")
                    return reply
                await self._dispatch_reply(reply)

    async def _send_command(self, command: dict[str, Any]) -> None:
        if "id" not in command and command != {}:
            command = {"id": self._next_id(), **command}
        await self._raw_send(self._encode_commands([command]))

    @staticmethod
    def _encode_commands(commands: list[dict[str, Any]]) -> str:
        return "\n".join(json.dumps(command) for command in commands)

    @staticmethod
    def _decode_replies(data: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in data.strip().split("\n") if line]

    # -- transport ---------------------------------------------------------------------------

    async def _default_ws_connect(self, url: str) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return await self._session.ws_connect(url, heartbeat=None)

    async def _raw_send(self, payload: str) -> None:
        if self._ws is None:
            raise SXBetRealtimeError("connection is not initialized")
        await self._ws.send_str(payload)

    async def _recv(self) -> str | None:
        msg = await self._ws.receive()
        msg_type = getattr(msg, "type", None)
        if msg_type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            data = msg.data
            return data.decode("utf-8") if isinstance(data, bytes) else data
        return None

    async def _fetch_token(self) -> str:
        payload = await self._http_client.get_realtime_token()
        token = self._extract_token(payload)
        if not token:
            raise SXBetRealtimeError("realtime token endpoint returned no token")
        return token

    @staticmethod
    def _extract_token(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        token = payload.get("token")
        if isinstance(token, str) and token:
            return token
        data = payload.get("data")
        if isinstance(data, dict):
            token = data.get("token")
            if isinstance(token, str) and token:
                return token
        return None

    async def _close_ws(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def _mark_disconnected(self) -> None:
        was_connected = self._connected
        self._connected = False
        await self._close_ws()
        if was_connected and self._on_disconnected is not None:
            await self._maybe_await(self._on_disconnected())

    @staticmethod
    async def _maybe_await(result: Any) -> None:
        if asyncio.iscoroutine(result):
            await result
