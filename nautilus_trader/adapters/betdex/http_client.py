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
BetDex/Monaco HTTP client.
"""

from __future__ import annotations

import os
import ssl
from datetime import UTC
from datetime import datetime
from typing import Any

import aiohttp

from nautilus_trader.adapters.betdex.constants import BETDEX_ENDPOINTS
from nautilus_trader.adapters.betdex.constants import BETDEX_SANDBOX_API_BASE_URL
from nautilus_trader.common.component import Logger


HTTP_STATUS_OK_MIN = 200
HTTP_STATUS_REDIRECT_MIN = 300


def _betdex_ssl_context() -> ssl.SSLContext:
    cafile: str | None = None
    try:
        import certifi
    except ModuleNotFoundError:
        certifi = None

    if certifi is not None:
        cafile = certifi.where()
    elif os.path.exists("/etc/ssl/cert.pem"):
        cafile = "/etc/ssl/cert.pem"

    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


class BetDexHttpClientError(Exception):
    """
    Raised for BetDex/Monaco API failures.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BetDexAuthenticationError(BetDexHttpClientError):
    """
    Raised when credentials are required for an authenticated endpoint.
    """

    def __init__(self, operation: str) -> None:
        super().__init__(f"BetDex app_id and api_key are required for {operation}")


class BetDexHttpClient:
    """
    REST client for the Monaco Exchange API used by BetDex.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        api_key: str | None = None,
        api_url: str | None = None,
        request_timeout_secs: float = 30.0,
        logger: Logger | None = None,
    ) -> None:
        self._app_id = app_id.strip() if isinstance(app_id, str) else None
        self._api_key = api_key.strip() if isinstance(api_key, str) else None
        self._api_url = (api_url or BETDEX_SANDBOX_API_BASE_URL).rstrip("/")
        self._request_timeout_secs = request_timeout_secs
        self._log = logger
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None
        self._access_expires_at: datetime | None = None

    @property
    def connected(self) -> bool:
        return self._session is not None and not self._session.closed

    @property
    def access_token(self) -> str | None:
        return self._access_token

    async def connect(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self._request_timeout_secs)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=_betdex_ssl_context()),
        )
        if self._app_id and self._api_key:
            await self.create_session()
        elif self._log:
            self._log.warning("BetDexHttpClient connected without credentials")

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._access_token = None
        self._access_expires_at = None

    async def create_session(self) -> dict[str, Any]:
        self._require_credentials("create_session")
        payload = await self._request(
            "POST",
            BETDEX_ENDPOINTS["sessions"],
            json_body={
                "appId": self._app_id,
                "apiKey": self._api_key,
            },
            authenticated=False,
        )
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, list) or not sessions:
            raise BetDexHttpClientError("BetDex session response did not include a session")
        session = sessions[0]
        token = session.get("accessToken")
        if not isinstance(token, str) or not token:
            raise BetDexHttpClientError("BetDex session response missing accessToken")
        self._access_token = token
        expires_at = session.get("accessExpiresAt")
        if isinstance(expires_at, str):
            self._access_expires_at = self._parse_datetime(expires_at)
        return payload

    async def get_events(self, **params: Any) -> dict[str, Any]:
        return await self._request("GET", BETDEX_ENDPOINTS["events"], params=params)

    async def get_markets(self, **params: Any) -> dict[str, Any]:
        return await self._request("GET", BETDEX_ENDPOINTS["markets"], params=params)

    async def get_market_prices(
        self,
        market_ids: list[str] | tuple[str, ...],
        *,
        include_empty: bool = False,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            BETDEX_ENDPOINTS["market_prices"],
            params={
                "marketIds": list(market_ids),
                "includeEmpty": str(include_empty).lower(),
            },
        )

    async def get_wallets(self) -> dict[str, Any]:
        return await self._request("GET", BETDEX_ENDPOINTS["wallets"])

    async def place_order(
        self,
        *,
        wallet_id: str,
        market_id: str,
        side: str,
        outcome_id: str,
        price: float,
        stake: float,
        keep_when_in_play: bool,
        match_behavior: str,
        reference: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "walletId": wallet_id,
            "marketId": market_id,
            "side": side,
            "outcomeId": outcome_id,
            "price": price,
            "stake": stake,
            "keepWhenInPlay": keep_when_in_play,
            "matchBehavior": match_behavior,
        }
        if reference:
            payload["reference"] = reference
        return await self._request("POST", BETDEX_ENDPOINTS["orders"], json_body=payload)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        endpoint = BETDEX_ENDPOINTS["order_cancel"].format(order_id=order_id)
        return await self._request("POST", endpoint)

    async def credit_faucet(self, *, wallet_id: str, currency_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            BETDEX_ENDPOINTS["faucet"],
            params={"walletId": wallet_id, "currencyId": currency_id},
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            await self.connect()
        if self._session is None:
            raise BetDexHttpClientError("BetDex HTTP session is not initialized")
        return self._session

    def _require_credentials(self, operation: str) -> None:
        if not self._app_id or not self._api_key:
            raise BetDexAuthenticationError(operation)

    def _authorization_header(self) -> str:
        if not self._access_token:
            raise BetDexAuthenticationError("authenticated request")
        return f"Bearer {self._access_token}"

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        session = await self._ensure_session()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if authenticated:
            headers["authorization"] = self._authorization_header()

        url = f"{self._api_url}{endpoint}"
        async with session.request(
            method,
            url,
            headers=headers,
            params=self._normalize_params(params or {}),
            json=json_body,
        ) as response:
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                text = await response.text()
                payload = {"message": text}

            if not (HTTP_STATUS_OK_MIN <= response.status < HTTP_STATUS_REDIRECT_MIN):
                message = payload.get("message") if isinstance(payload, dict) else None
                raise BetDexHttpClientError(
                    message or f"BetDex API request failed with status {response.status}",
                    status_code=response.status,
                )

            return payload if isinstance(payload, dict) else {"data": payload}

    @staticmethod
    def _normalize_params(params: dict[str, Any]) -> list[tuple[str, str]]:
        normalized: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, list | tuple | set | frozenset):
                normalized.extend((key, str(item)) for item in value if item is not None)
            else:
                normalized.append((key, str(value)))
        return normalized

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
