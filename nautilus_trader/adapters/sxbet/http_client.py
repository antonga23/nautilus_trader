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
SX.bet HTTP client for REST API interactions.
"""

import asyncio
import os
import secrets
import ssl
from datetime import UTC
from datetime import datetime
from email.utils import parsedate_to_datetime
from itertools import chain
from typing import Any

import aiohttp

from nautilus_trader.adapters.sxbet.constants import SXBET_API_BASE_URL
from nautilus_trader.adapters.sxbet.constants import SXBET_ENDPOINTS
from nautilus_trader.common.component import Logger


HTTP_STATUS_OK_MIN = 200
HTTP_STATUS_REDIRECT_MIN = 300
HTTP_STATUS_TOO_MANY_REQUESTS = 429


def _sxbet_ssl_context() -> ssl.SSLContext:
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


class SXBetHttpClientError(Exception):
    """
    Exception raised for SX.bet API errors.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SXBetHttpClientSessionError(SXBetHttpClientError):
    """
    Raised when the HTTP client cannot initialize its underlying session.
    """

    def __init__(self) -> None:
        super().__init__("Failed to initialize SX.bet HTTP session")


class SXBetHttpClientRateLimitError(SXBetHttpClientError):
    """
    Raised when SX.bet continues returning rate-limit responses after retries.
    """

    def __init__(self, max_retries: int, status_code: int) -> None:
        super().__init__(
            f"Rate limit exceeded after {max_retries} retries",
            status_code=status_code,
        )


class SXBetHttpClientAuthenticationError(SXBetHttpClientError):
    """
    Raised when an authenticated SX.bet endpoint is called without an API key.
    """

    def __init__(self, operation: str) -> None:
        super().__init__(f"API key required for {operation}")


class SXBetHttpClient:
    """
    HTTP client for SX.bet REST API.

    Parameters
    ----------
    api_key : str, optional
        The SX.bet API key (not required for market data).
    api_url : str, optional
        The API base URL. Defaults to production.
    logger : Logger, optional
        The logger instance.

    """

    def __init__(
        self,
        api_key: str | None = None,
        api_key_pool: tuple[str, ...] | list[str] | None = None,
        api_url: str | None = None,
        max_retries: int = 5,
        request_timeout_secs: float = 30.0,
        logger: Logger | None = None,
    ) -> None:
        self._api_keys = self._normalize_api_key_pool(api_key=api_key, api_key_pool=api_key_pool)
        self._api_url = api_url or SXBET_API_BASE_URL
        self._max_retries = max_retries
        self._request_timeout_secs = request_timeout_secs
        self._log = logger
        self._session: Any = None
        self._request_timeout: Any = None
        self._api_key_index = 0

    @property
    def headers(self) -> dict[str, str]:
        """
        Get request headers with authentication.
        """
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_keys:
            headers["x-api-key"] = self._api_keys[0]
        return headers

    @staticmethod
    def _normalize_api_key_pool(
        *,
        api_key: str | None,
        api_key_pool: tuple[str, ...] | list[str] | None,
    ) -> tuple[str, ...]:
        keys: list[str] = []
        for value in api_key_pool or ():
            key = value.strip()
            if key and key not in keys:
                keys.append(key)
        if api_key:
            key = api_key.strip()
            if key and key not in keys:
                keys.insert(0, key)
        return tuple(keys)

    def _next_api_key(self) -> str | None:
        if not self._api_keys:
            return None
        key = self._api_keys[self._api_key_index % len(self._api_keys)]
        self._api_key_index += 1
        return key

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        api_key = self._next_api_key()
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    async def connect(self) -> None:
        """
        Connect to the API (initialize session).
        """
        self._request_timeout = aiohttp.ClientTimeout(total=self._request_timeout_secs)
        self._session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=self._request_timeout,
            connector=aiohttp.TCPConnector(ssl=_sxbet_ssl_context()),
        )
        if self._log:
            self._log.info("SXBetHttpClient connected")

    async def disconnect(self) -> None:
        """
        Disconnect from the API (close session).
        """
        if self._session:
            await self._session.close()
            self._session = None
        if self._log:
            self._log.info("SXBetHttpClient disconnected")

    def _raise_api_error(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        *,
        log_api_error: bool = True,
    ) -> None:
        error_message = f"SX.bet API request failed with status {status_code}"
        if self._log and log_api_error:
            self._log.error(f"{error_message} for {method} {endpoint}")
        raise SXBetHttpClientError(error_message, status_code=status_code)

    async def _ensure_session(self) -> Any:
        if not self._session:
            await self.connect()
            if self._session is None:
                raise SXBetHttpClientSessionError
        return self._session

    def _require_api_key(self, operation: str) -> None:
        if not self._api_keys:
            raise SXBetHttpClientAuthenticationError(operation)

    def _raise_rate_limit_error(self, status_code: int) -> None:
        raise SXBetHttpClientRateLimitError(
            max_retries=self._max_retries,
            status_code=status_code,
        )

    @staticmethod
    def _wrap_list_response(
        payload: dict[str, Any],
        item_key: str,
        *,
        single_item_key: str | None = None,
    ) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, dict) and item_key in data:
            return payload
        if not isinstance(data, list):
            return payload

        wrapped: dict[str, Any] = {item_key: data}
        if single_item_key is not None and len(data) == 1:
            wrapped[single_item_key] = data[0]

        return {
            **payload,
            "data": wrapped,
        }

    async def _get_active_fixture_batch(
        self,
        league_id: int,
        *,
        from_time: int | None = None,
        to_time: int | None = None,
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            SXBET_ENDPOINTS["fixtures"],
            params={"leagueId": league_id},
        )
        fixtures = payload.get("data", [])
        if not isinstance(fixtures, list):
            return []

        if from_time is None and to_time is None:
            return fixtures

        filtered: list[dict[str, Any]] = []
        for fixture in fixtures:
            game_time = fixture.get("gameTime")
            if not isinstance(game_time, int):
                filtered.append(fixture)
                continue
            if from_time is not None and game_time < from_time:
                continue
            if to_time is not None and game_time > to_time:
                continue
            filtered.append(fixture)
        return filtered

    @staticmethod
    def _parse_retry_after(value: str | None) -> float:
        if value is None:
            return 1.0

        normalized = value.strip()
        if not normalized:
            return 1.0

        try:
            return max(float(normalized), 1.0)
        except ValueError:
            pass

        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, IndexError, OverflowError):
            return 1.0

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        else:
            retry_at = retry_at.astimezone(UTC)

        delay = (retry_at - datetime.now(UTC)).total_seconds()
        return max(delay, 1.0)

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        data: Any | None = None,
        *,
        log_api_error: bool = True,
    ) -> dict[str, Any]:
        """
        Make an HTTP request to the API.
        """
        session = await self._ensure_session()

        url = f"{self._api_url}{endpoint}"

        attempt = 0
        while True:
            try:
                async with session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=data,
                    headers=self._request_headers(),
                    timeout=self._request_timeout,
                ) as response:
                    if response.status == HTTP_STATUS_TOO_MANY_REQUESTS:
                        # Rate limited
                        if attempt >= self._max_retries:
                            self._raise_rate_limit_error(response.status)
                        retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                        if self._log:
                            self._log.warning(
                                f"Rate limited, waiting {retry_after}s (attempt {attempt + 1})",
                            )
                        jitter = secrets.randbelow(500) / 1000
                        await asyncio.sleep(retry_after + jitter)
                        attempt += 1
                        continue

                    if not HTTP_STATUS_OK_MIN <= response.status < HTTP_STATUS_REDIRECT_MIN:
                        await response.text()
                        self._raise_api_error(
                            method,
                            endpoint,
                            response.status,
                            log_api_error=log_api_error,
                        )

                    return await response.json()

            except SXBetHttpClientError:
                raise
            except Exception as e:
                if self._log:
                    self._log.error(
                        f"Request failed for {method} {endpoint}: {type(e).__name__}",
                    )
                raise SXBetHttpClientError(
                    f"Request failed for {method} {endpoint}: {type(e).__name__}",
                ) from e

    async def get_sports(self) -> dict[str, Any]:
        """
        Get list of available sports.
        """
        return await self._request("GET", SXBET_ENDPOINTS["sports"])

    async def get_active_sports(self) -> dict[str, Any]:
        """
        Get list of sports with active markets.
        """
        sports_payload, active_leagues_payload = await asyncio.gather(
            self.get_sports(),
            self.get_active_leagues(),
        )
        sports = sports_payload.get("data", [])
        active_leagues = active_leagues_payload.get("data", [])
        if not isinstance(sports, list) or not isinstance(active_leagues, list):
            return {"status": "success", "data": []}

        active_sport_ids = {
            league.get("sportId")
            for league in active_leagues
            if isinstance(league, dict) and league.get("sportId") is not None
        }
        return {
            "status": sports_payload.get("status", "success"),
            "data": [
                sport
                for sport in sports
                if isinstance(sport, dict) and sport.get("sportId") in active_sport_ids
            ],
        }

    async def get_leagues(self, sport_id: int | None = None) -> dict[str, Any]:
        """
        Get leagues, optionally filtered by sport.
        """
        params = {}
        if sport_id:
            params["sportId"] = sport_id
        return await self._request("GET", SXBET_ENDPOINTS["leagues"], params=params)

    async def get_active_leagues(self, sport_id: int | None = None) -> dict[str, Any]:
        """
        Get leagues with active markets.
        """
        params = {"sportId": sport_id} if sport_id is not None else None
        return await self._request("GET", SXBET_ENDPOINTS["active_leagues"], params=params)

    async def get_fixtures(
        self,
        sport_id: int | None = None,
        league_id: int | None = None,
        from_time: int | None = None,
        to_time: int | None = None,
    ) -> dict[str, Any]:
        """
        Get fixtures/events.

        Parameters
        ----------
        sport_id : int, optional
            Filter by sport.
        league_id : int, optional
            Filter by league.
        from_time : int, optional
            Start time (Unix timestamp).
        to_time : int, optional
            End time (Unix timestamp).

        """
        if league_id is not None:
            return {
                "status": "success",
                "data": await self._get_active_fixture_batch(
                    league_id,
                    from_time=from_time,
                    to_time=to_time,
                ),
            }

        leagues_payload = await self.get_active_leagues(sport_id=sport_id)
        leagues = leagues_payload.get("data", [])
        if not isinstance(leagues, list):
            return {"status": "success", "data": []}

        fixture_batches = await asyncio.gather(
            *[
                self._get_active_fixture_batch(
                    league["leagueId"],
                    from_time=from_time,
                    to_time=to_time,
                )
                for league in leagues
                if isinstance(league, dict) and isinstance(league.get("leagueId"), int)
            ],
        )
        return {
            "status": leagues_payload.get("status", "success"),
            "data": list(chain.from_iterable(fixture_batches)),
        }

    async def get_markets(
        self,
        sport_id: int | None = None,
        league_id: int | None = None,
        fixture_id: str | None = None,
        only_active: bool = True,
        pagination_key: str | None = None,
        page_size: int | None = None,
        only_main_line: bool | None = None,
        live_only: bool | None = None,
    ) -> dict[str, Any]:
        """
        Get betting markets.

        Parameters
        ----------
        sport_id : int, optional
            Filter by sport.
        league_id : int, optional
            Filter by league.
        fixture_id : str, optional
            Filter by fixture.
        only_active : bool, default True
            Only return active markets.
        pagination_key : str, optional
            Pagination cursor returned from a prior active-markets response.
        page_size : int, optional
            Requested page size for paginated active-market queries.
        only_main_line : bool, optional
            Restrict responses to current main-line markets.
        live_only : bool, optional
            Restrict responses to markets currently available in-play.

        """
        if not only_active:
            raise SXBetHttpClientError(
                "Non-active SX.bet market queries are not exposed by the live REST API",
            )

        endpoint = SXBET_ENDPOINTS["active_markets"]
        params: dict[str, Any] = {}
        if sport_id is not None:
            params["sportIds"] = sport_id
        if league_id is not None:
            params["leagueId"] = league_id
        if fixture_id:
            params["eventId"] = fixture_id
        if pagination_key:
            params["paginationKey"] = pagination_key
        if page_size is not None:
            params["pageSize"] = page_size
        if only_main_line is not None:
            params["onlyMainLine"] = only_main_line
        if live_only is not None:
            params["liveOnly"] = live_only

        return await self._request("GET", endpoint, params=params)

    async def get_market(self, market_hash: str) -> dict[str, Any]:
        """
        Get a specific market by hash.
        """
        payload = await self._request(
            "GET",
            SXBET_ENDPOINTS["market_lookup"],
            params={"marketHashes": market_hash},
        )
        return self._wrap_list_response(payload, "markets", single_item_key="market")

    async def get_order_book(
        self,
        market_hash: str | None = None,
    ) -> dict[str, Any]:
        """
        Get active orders.

        Parameters
        ----------
        market_hash : str, optional
            Filter by market hash.

        """
        if market_hash is None:
            raise SXBetHttpClientError("market_hash is required when querying SX.bet orders")

        payload = await self._request(
            "GET",
            SXBET_ENDPOINTS["order_book"],
            params={"marketHashes": market_hash},
        )
        return self._wrap_list_response(payload, "orders")

    async def get_best_odds(
        self,
        *,
        market_hashes: list[str] | None = None,
        league_ids: list[int] | None = None,
        base_token: str,
        log_api_error: bool = True,
    ) -> dict[str, Any]:
        """
        Get the best currently available odds for the given markets or leagues.
        """
        if not market_hashes and not league_ids:
            raise SXBetHttpClientError(
                "market_hashes or league_ids is required when querying SX.bet best odds",
            )
        if market_hashes and league_ids:
            raise SXBetHttpClientError(
                "market_hashes and league_ids cannot both be set when querying SX.bet best odds",
            )

        params: dict[str, Any] = {"baseToken": base_token}
        if market_hashes:
            params["marketHashes"] = ",".join(market_hashes)
        if league_ids:
            params["leagueIds"] = ",".join(str(league_id) for league_id in league_ids)

        return await self._request(
            "GET",
            SXBET_ENDPOINTS["best_odds"],
            params=params,
            log_api_error=log_api_error,
        )

    async def get_realtime_token(self) -> dict[str, Any]:
        """
        Fetch a realtime WebSocket token using an API key.
        """
        self._require_api_key("fetching realtime WebSocket token")
        return await self._request("GET", SXBET_ENDPOINTS["realtime_token"])

    async def place_order(  # pylint: disable=too-many-arguments
        self,
        market_hash: str,
        total_bet_size: int,
        percentage_odds: int,
        expiry: int,
        salt: int,
        is_maker_betting_outcome_one: bool,
        signature: str,
        base_token: str,
    ) -> dict[str, Any]:
        """
        Place a new order.

        Parameters
        ----------
        market_hash : str
            The market hash.
        total_bet_size : int
            Total bet size in wei.
        percentage_odds : int
            Percentage odds (implied probability scaled by ``10^20``).
        expiry : int
            Order expiry timestamp.
        salt : int
            Random salt for order uniqueness.
        is_maker_betting_outcome_one : bool
            If True, maker bets on outcome 1.
        signature : str
            EIP712 signature.
        base_token : str
            Token address for betting.

        """
        self._require_api_key("placing orders")

        data = {
            "marketHash": market_hash,
            "totalBetSize": str(total_bet_size),
            "percentageOdds": str(percentage_odds),
            "expiry": expiry,
            "salt": str(salt),
            "isMakerBettingOutcomeOne": is_maker_betting_outcome_one,
            "signature": signature,
            "baseToken": base_token,
        }

        return await self._request("POST", SXBET_ENDPOINTS["place_order"], data=data)

    async def cancel_order(self, order_hash: str) -> dict[str, Any]:
        """
        Cancel an order.
        """
        self._require_api_key("cancelling orders")

        data = {"orderHash": order_hash}
        return await self._request("POST", SXBET_ENDPOINTS["cancel_order"], data=data)

    async def fill_order(  # pylint: disable=too-many-arguments
        self,
        *,
        market: str,
        taker: str,
        base_token: str,
        is_taker_betting_outcome_one: bool,
        stake_wei: int,
        desired_odds: int,
        odds_slippage: int,
        taker_sig: str,
        fill_salt: int,
        message: str,
    ) -> dict[str, Any]:
        """
        Fill existing SX.bet order-book liquidity as a taker.
        """
        self._require_api_key("filling orders")
        data = {
            "market": market,
            "taker": taker,
            "baseToken": base_token,
            "isTakerBettingOutcomeOne": is_taker_betting_outcome_one,
            "stakeWei": str(stake_wei),
            "desiredOdds": str(desired_odds),
            "oddsSlippage": int(odds_slippage),
            "takerSig": taker_sig,
            "fillSalt": str(fill_salt),
            "message": message,
        }
        return await self._request("POST", SXBET_ENDPOINTS["fill_order"], data=data)

    async def cancel_all_orders(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Cancel all maker orders using a pre-signed SX.bet cancellation payload.
        """
        self._require_api_key("cancelling all orders")
        return await self._request("POST", SXBET_ENDPOINTS["cancel_all_orders"], data=payload)

    async def get_user_orders(self, wallet_address: str) -> dict[str, Any]:
        """
        Get orders for a specific wallet.
        """
        self._require_api_key("user orders")

        payload = await self._request(
            "GET",
            SXBET_ENDPOINTS["user_orders"],
            params={"maker": wallet_address},
        )
        return self._wrap_list_response(payload, "orders")

    async def get_user_trades(
        self,
        wallet_address: str,
        settled: bool | None = None,
    ) -> dict[str, Any]:
        """
        Get trades for a specific wallet, optionally filtered by settlement status.
        """
        self._require_api_key("user trades")

        params = {"bettor": wallet_address}
        if settled is not None:
            # aiohttp rejects bool query params; SX.bet expects "true"/"false".
            params["settled"] = "true" if settled else "false"
        return await self._request("GET", SXBET_ENDPOINTS["user_trades"], params=params)

    async def get_balance(self, wallet_address: str, token: str) -> dict[str, Any]:
        """
        Get token balance for a wallet.
        """
        raise SXBetHttpClientError(
            "SX.bet does not expose wallet balance via the current public REST API",
        )
