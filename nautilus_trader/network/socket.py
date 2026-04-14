from __future__ import annotations

import asyncio
from typing import Any

from nautilus_trader.common.logging import Logger


class SocketClient:
    is_connected: bool = False

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        logger: Logger | None = None,
        host: str | None = None,
        port: int | None = None,
        handler: Any | None = None,
        ssl: bool | None = None,
        crlf: bytes | None = None,
        encoding: str | None = None,
    ) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._log = logger or Logger(name=type(self).__name__)
        self._host = host or "localhost"
        self._port = port or 0
        self._handler = handler
        self._ssl = ssl
        self._crlf = crlf or b"\r\n"
        self._encoding = encoding or "utf-8"
        self.is_connected = False

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def close(self) -> None:
        await self.disconnect()

    async def send(self, data: bytes) -> None:
        return None
