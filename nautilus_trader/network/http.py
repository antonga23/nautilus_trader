from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from nautilus_trader.common.logging import Logger


@dataclass
class ClientResponse:
    status: int
    data: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    text: str | bytes | None = None


class HttpClient:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        logger: Logger | None = None,
        connector_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._log = logger or Logger(name=type(self).__name__)
        self._connector_kwargs = connector_kwargs or {}
        self._session: aiohttp.ClientSession | None = None
        self.connected = False

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(**self._connector_kwargs)
            self._session = aiohttp.ClientSession(connector=connector)
        self.connected = True

    async def disconnect(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self.connected = False

    async def request(self, method: str, url: str, **kwargs: Any) -> ClientResponse:
        await self.connect()
        assert self._session is not None

        body = kwargs.pop("body", None)
        if body is not None:
            kwargs["data"] = body

        async with self._session.request(method=method, url=url, **kwargs) as response:
            data = await response.read()
            try:
                text: str | bytes | None = data.decode()
            except UnicodeDecodeError:
                text = data
            return ClientResponse(
                status=response.status,
                data=data,
                headers=dict(response.headers),
                text=text,
            )

    async def get(self, url: str, **kwargs: Any) -> ClientResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> ClientResponse:
        return await self.request("POST", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> ClientResponse:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> ClientResponse:
        return await self.request("DELETE", url, **kwargs)
