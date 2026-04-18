# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SXBetHttpClient.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import secrets
from typing import Any

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

    result = await client.get_markets(sport_id=1, league_id=2, fixture_id="fixture-3", only_active=True)

    assert result == {"ok": True}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/markets/active"
    assert captured["params"] == {
        "sportId": 1,
        "leagueId": 2,
        "fixtureId": "fixture-3",
    }
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_get_markets_uses_legacy_endpoint_for_non_active_queries(monkeypatch):
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

    result = await client.get_markets(only_active=False)

    assert result == {"ok": True}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/markets"
    assert captured["params"] == {"onlyActive": "false"}
    assert captured["data"] is None
