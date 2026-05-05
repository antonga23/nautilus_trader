# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SXBetHttpClient.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import secrets
from typing import Any
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError


EXPECTED_RETRY_CALLS = 2


class _FakeResponse:
    def __init__(self, status: int, json_data: dict | None = None, text: str = "") -> None:
        self.status = status
        self._json = json_data or {}
        self._text = text
        self.headers: dict[str, str] = {"Retry-After": "0"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.last_request_kwargs: dict[str, Any] | None = None

    def request(self, **_kwargs):
        self.last_request_kwargs = _kwargs
        response = self._responses[self.calls]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_request_retries_on_429(monkeypatch):
    async def _noop_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    client = SXBetHttpClient(max_retries=2)
    client._session = _FakeSession(
        [
            _FakeResponse(429),
            _FakeResponse(200, json_data={"ok": True}),
        ],
    )

    result = await client._request("GET", "/test")

    assert result == {"ok": True}
    assert client._session.calls == EXPECTED_RETRY_CALLS


@pytest.mark.asyncio
async def test_request_raises_after_max_retries(monkeypatch):
    async def _noop_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    client = SXBetHttpClient(max_retries=1)
    client._session = _FakeSession(
        [
            _FakeResponse(429),
            _FakeResponse(429),
        ],
    )

    with pytest.raises(SXBetHttpClientError, match="Rate limit exceeded"):
        await client._request("GET", "/test")

    assert client._session.calls == EXPECTED_RETRY_CALLS


@pytest.mark.asyncio
async def test_request_retries_on_non_integer_retry_after(monkeypatch):
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    monkeypatch.setattr(secrets, "randbelow", lambda _upper: 0)

    client = SXBetHttpClient(max_retries=2)
    response = _FakeResponse(429)
    response.headers["Retry-After"] = "not-a-number"
    client._session = _FakeSession(
        [
            response,
            _FakeResponse(200, json_data={"ok": True}),
        ],
    )

    result = await client._request("GET", "/test")

    assert result == {"ok": True}
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_request_redacts_upstream_error_body():
    client = SXBetHttpClient()
    client._session = _FakeSession(
        [
            _FakeResponse(500, text='{"secret":"value"}'),
        ],
    )

    with pytest.raises(
        SXBetHttpClientError,
        match=r"SX\.bet API request failed with status 500",
    ) as exc_info:
        await client._request("GET", "/test")

    assert "secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_can_suppress_duplicate_api_error_logging():
    logger = Mock()
    client = SXBetHttpClient(logger=logger)
    client._session = _FakeSession(
        [
            _FakeResponse(403, text='{"error":"forbidden"}'),
        ],
    )

    with pytest.raises(SXBetHttpClientError, match=r"SX\.bet API request failed with status 403"):
        await client._request("GET", "/orders/odds/best", log_api_error=False)

    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_request_raises_when_session_init_fails(monkeypatch):
    client = SXBetHttpClient()

    async def _connect_without_session() -> None:
        return None

    monkeypatch.setattr(client, "connect", _connect_without_session)

    with pytest.raises(SXBetHttpClientError, match=r"Failed to initialize SX\.bet HTTP session"):
        await client._request("GET", "/test")


@pytest.mark.asyncio
async def test_request_uses_configured_timeout():
    client = SXBetHttpClient(request_timeout_secs=12.5)
    client._request_timeout = object()
    client._session = _FakeSession(
        [
            _FakeResponse(200, json_data={"ok": True}),
        ],
    )

    result = await client._request("GET", "/test")

    assert result == {"ok": True}
    assert client._session.last_request_kwargs is not None
    assert client._session.last_request_kwargs["timeout"] is client._request_timeout
    assert client._session.last_request_kwargs["url"] == "https://api.sx.bet/test"


@pytest.mark.asyncio
async def test_request_wraps_unexpected_transport_failures():
    class _TimeoutSession:
        def request(self, **_kwargs):
            raise TimeoutError

    client = SXBetHttpClient()
    client._request_timeout = object()
    client._session = _TimeoutSession()

    with pytest.raises(
        SXBetHttpClientError,
        match=r"Request failed for GET /test: TimeoutError",
    ) as exc_info:
        await client._request("GET", "/test")

    assert isinstance(exc_info.value.__cause__, TimeoutError)


@pytest.mark.asyncio
async def test_request_rotates_api_key_pool_without_logging_values():
    client = SXBetHttpClient(api_key_pool=("key-a", "key-b"))
    client._request_timeout = object()
    client._session = _FakeSession(
        [
            _FakeResponse(200, json_data={"first": True}),
            _FakeResponse(200, json_data={"second": True}),
        ],
    )

    first = await client._request("GET", "/first")
    first_headers = client._session.last_request_kwargs["headers"]
    second = await client._request("GET", "/second")
    second_headers = client._session.last_request_kwargs["headers"]

    assert first == {"first": True}
    assert second == {"second": True}
    assert first_headers["x-api-key"] == "key-a"
    assert second_headers["x-api-key"] == "key-b"


@pytest.mark.asyncio
async def test_get_realtime_token_requires_api_key(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, str]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {"token": "realtime-token"}

    client = SXBetHttpClient(api_key_pool=("key-a",))
    monkeypatch.setattr(client, "_request", _fake_request)

    assert await client.get_realtime_token() == {"token": "realtime-token"}
    assert captured == {
        "method": "GET",
        "endpoint": "/user/realtime-token/api-key",
        "params": None,
        "data": None,
    }

    with pytest.raises(SXBetHttpClientError, match="fetching realtime WebSocket token"):
        await SXBetHttpClient().get_realtime_token()


@pytest.mark.asyncio
async def test_place_order_serializes_numeric_fields(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, bool]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {"ok": True}

    client = SXBetHttpClient(api_key="key")
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.place_order(
        market_hash="market",
        total_bet_size=1000,
        percentage_odds=5000,
        expiry=1_700_000_000,
        salt=12345,
        is_maker_betting_outcome_one=True,
        signature="0xsig",
        base_token="0xtoken",
    )

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["data"] == {
        "marketHash": "market",
        "totalBetSize": "1000",
        "percentageOdds": "5000",
        "expiry": 1_700_000_000,
        "salt": "12345",
        "isMakerBettingOutcomeOne": True,
        "signature": "0xsig",
        "baseToken": "0xtoken",
    }


@pytest.mark.asyncio
async def test_get_markets_uses_active_endpoint_without_only_active_param(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, bool]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {"ok": True}

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_markets(
        sport_id=1,
        league_id=2,
        fixture_id="fixture-3",
        only_active=True,
    )

    assert result == {"ok": True}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/markets/active"
    assert captured["params"] == {
        "sportIds": 1,
        "leagueId": 2,
        "eventId": "fixture-3",
    }
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_markets_forwards_pagination_and_live_filters(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, bool]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {"ok": True}

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_markets(
        sport_id=5,
        pagination_key="next-key",
        page_size=50,
        only_main_line=True,
        live_only=True,
    )

    assert result == {"ok": True}
    assert captured["params"] == {
        "sportIds": 5,
        "paginationKey": "next-key",
        "pageSize": 50,
        "onlyMainLine": True,
        "liveOnly": True,
    }


@pytest.mark.asyncio
async def test_get_markets_rejects_non_active_queries(monkeypatch):
    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"ok": True})

    with pytest.raises(
        SXBetHttpClientError,
        match=r"Non-active SX\.bet market queries are not exposed by the live REST API",
    ):
        await client.get_markets(only_active=False)


@pytest.mark.asyncio
async def test_get_active_leagues_uses_live_endpoint_and_optional_sport_filter(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, Any]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {"status": "success", "data": []}

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_active_leagues(sport_id=29)

    assert result == {"status": "success", "data": []}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/leagues/active"
    assert captured["params"] == {"sportId": 29}
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_best_odds_uses_market_hash_query(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
        *,
        log_api_error: bool = True,
    ) -> dict[str, Any]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["log_api_error"] = log_api_error
        return {"status": "success", "data": {"bestOdds": []}}

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_best_odds(
        market_hashes=["hash-a", "hash-b"],
        base_token="0xtoken",
        log_api_error=False,
    )

    assert result == {"status": "success", "data": {"bestOdds": []}}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/orders/odds/best"
    assert captured["params"] == {
        "baseToken": "0xtoken",
        "marketHashes": "hash-a,hash-b",
    }
    assert captured["log_api_error"] is False


@pytest.mark.asyncio
async def test_get_best_odds_uses_league_id_query(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
        *,
        log_api_error: bool = True,
    ) -> dict[str, Any]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["log_api_error"] = log_api_error
        return {"status": "success", "data": {"bestOdds": []}}

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_best_odds(
        league_ids=[10, 20],
        base_token="0xtoken",
    )

    assert result == {"status": "success", "data": {"bestOdds": []}}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/orders/odds/best"
    assert captured["params"] == {
        "baseToken": "0xtoken",
        "leagueIds": "10,20",
    }
    assert captured["log_api_error"] is True


@pytest.mark.asyncio
async def test_get_best_odds_rejects_invalid_query_shape():
    client = SXBetHttpClient()

    with pytest.raises(SXBetHttpClientError, match="market_hashes or league_ids is required"):
        await client.get_best_odds(base_token="0xtoken")

    with pytest.raises(SXBetHttpClientError, match="cannot both be set"):
        await client.get_best_odds(
            market_hashes=["hash-a"],
            league_ids=[1],
            base_token="0xtoken",
        )


@pytest.mark.asyncio
async def test_get_active_sports_derives_from_active_leagues(monkeypatch):
    client = SXBetHttpClient()

    async def _fake_get_sports() -> dict[str, object]:
        return {
            "status": "success",
            "data": [
                {"sportId": 5, "label": "Soccer"},
                {"sportId": 7, "label": "MMA"},
            ],
        }

    async def _fake_get_active_leagues(
        sport_id: int | None = None,
    ) -> dict[str, object]:
        assert sport_id is None
        return {
            "status": "success",
            "data": [
                {"leagueId": 1, "sportId": 5},
                {"leagueId": 2, "sportId": 5},
            ],
        }

    monkeypatch.setattr(client, "get_sports", _fake_get_sports)
    monkeypatch.setattr(client, "get_active_leagues", _fake_get_active_leagues)

    result = await client.get_active_sports()

    assert result == {
        "status": "success",
        "data": [{"sportId": 5, "label": "Soccer"}],
    }


@pytest.mark.asyncio
async def test_get_fixtures_uses_active_fixture_endpoint_for_league(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, object]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {"status": "success", "data": [{"eventId": "fixture-1"}]}

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_fixtures(league_id=42)

    assert result == {"status": "success", "data": [{"eventId": "fixture-1"}]}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/fixture/active"
    assert captured["params"] == {"leagueId": 42}
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_market_uses_market_lookup_and_wraps_single_result(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, object]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {
            "status": "success",
            "data": [{"marketHash": "0xabc"}],
        }

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_market("0xabc")

    assert result == {
        "status": "success",
        "data": {
            "markets": [{"marketHash": "0xabc"}],
            "market": {"marketHash": "0xabc"},
        },
    }
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/markets/find"
    assert captured["params"] == {"marketHashes": "0xabc"}
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_order_book_uses_orders_endpoint_and_wraps_response(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, object]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {
            "status": "success",
            "data": [{"orderHash": "0xorder"}],
        }

    client = SXBetHttpClient()
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_order_book("0xmarket")

    assert result == {
        "status": "success",
        "data": {
            "orders": [{"orderHash": "0xorder"}],
        },
    }
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/orders"
    assert captured["params"] == {"marketHashes": "0xmarket"}
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_user_orders_uses_maker_filter_and_wraps_response(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, object]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {
            "status": "success",
            "data": [{"orderHash": "0xorder"}],
        }

    client = SXBetHttpClient(api_key="key")
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_user_orders("0xmaker")

    assert result == {
        "status": "success",
        "data": {
            "orders": [{"orderHash": "0xorder"}],
        },
    }
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/orders"
    assert captured["params"] == {"maker": "0xmaker"}
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_user_trades_uses_bettor_filter(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_request(
        method: str,
        endpoint: str,
        params: Any = None,
        data: Any = None,
    ) -> dict[str, object]:
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["data"] = data
        return {"status": "success", "data": {"trades": []}}

    client = SXBetHttpClient(api_key="key")
    monkeypatch.setattr(client, "_request", _fake_request)

    result = await client.get_user_trades("0xmaker")

    assert result == {"status": "success", "data": {"trades": []}}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/trades"
    assert captured["params"] == {"bettor": "0xmaker"}
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_balance_raises_explanatory_error():
    client = SXBetHttpClient(api_key="key")

    with pytest.raises(
        SXBetHttpClientError,
        match=r"SX\.bet does not expose wallet balance via the current public REST API",
    ):
        await client.get_balance("0xmaker", "0xtoken")
