# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for current Cloudbet API reconciliation.
# -------------------------------------------------------------------------------------------------

import asyncio

import msgspec
import pytest

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import BetResult
from nautilus_trader.adapters.cloudbet.client.schema import BetState
from nautilus_trader.adapters.cloudbet.client.schema import GetBetHistoryResponse
from nautilus_trader.adapters.cloudbet.client.schema import GetBetsResponse
from nautilus_trader.adapters.cloudbet.client.schema import RejectionCode
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger


class _Response:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self.data = data
        self.status = status


def _client() -> CloudbetClient:
    loop = asyncio.get_event_loop()
    logger = Logger(clock=LiveClock(), bypass=True)
    return CloudbetClient(loop=loop, logger=logger, api_key="test-key", api_url="https://sports-api.cloudbet.com/pub")


def test_get_bets_response_decodes_current_v4_shape():
    payload = {
        "items": [
            {
                "betType": "STRAIGHT",
                "betId": "21b40aef-01ed-41cb-8254-27aeba7d8133",
                "betslipId": "betslip-1",
                "positionId": "position-1",
                "currency": "BTC",
                "createTime": "2026-04-27T00:00:00Z",
                "state": "COMPLETED",
                "result": "WIN",
                "channel": "WEB",
                "stake": "0.1000",
                "potentialReturnAmount": "0.1648",
                "selection": {
                    "eventId": "5945044",
                    "marketUrl": "soccer.total_goals/over?total=3",
                    "price": "1.648",
                    "result": "WIN",
                    "marketName": "Total Goals",
                    "outcomeName": "Over 3",
                },
            },
            {
                "betType": "STRAIGHT",
                "betId": "31b40aef-01ed-41cb-8254-27aeba7d8133",
                "betslipId": "betslip-2",
                "positionId": "position-2",
                "currency": "BTC",
                "createTime": "2026-04-27T00:00:00Z",
                "state": "REJECTED",
                "result": "PENDING",
                "channel": "WEB",
                "stake": "0.1000",
                "potentialReturnAmount": "0.1648",
                "rejectionCode": "PRICE_ABOVE_MARKET",
                "selection": {
                    "eventId": "5945044",
                    "marketUrl": "soccer.match_odds/home",
                    "price": "2.760",
                    "result": "PENDING",
                    "marketName": "Match Odds",
                    "outcomeName": "Home",
                },
            },
        ],
        "hasNext": False,
    }

    decoded = msgspec.json.decode(msgspec.json.encode(payload), type=GetBetsResponse)

    assert decoded.has_next is False
    assert decoded.items[0].state == BetState.COMPLETED
    assert decoded.items[0].result == BetResult.WIN
    assert decoded.items[0].price == "1.648"
    assert decoded.items[0].selection.market_name == "Total Goals"
    assert decoded.items[1].rejection_code == RejectionCode.PRICE_ABOVE_MARKET
    assert decoded.items[1].status.value == "PRICE_ABOVE_MARKET"


@pytest.mark.asyncio
async def test_get_bet_status_uses_v4_bets_endpoint():
    client = _client()
    payload = {
        "items": [
            {
                "betType": "STRAIGHT",
                "betId": "21b40aef-01ed-41cb-8254-27aeba7d8133",
                "betslipId": "betslip-1",
                "positionId": "position-1",
                "currency": "BTC",
                "createTime": "2026-04-27T00:00:00Z",
                "state": "ACCEPTED",
                "result": "PENDING",
                "channel": "WEB",
                "stake": "0.1000",
                "selection": {
                    "eventId": "5945044",
                    "marketUrl": "soccer.match_odds/home",
                    "price": "2.760",
                    "result": "PENDING",
                },
            },
        ],
        "hasNext": False,
    }

    async def fake_get(*, url, params, headers):
        assert url.endswith("/v4/bets")
        assert params["referenceIds"] == ["21b40aef-01ed-41cb-8254-27aeba7d8133"]
        return _Response(msgspec.json.encode(payload))

    client.get = fake_get  # type: ignore[method-assign]

    result = await client.get_bet_status("21b40aef-01ed-41cb-8254-27aeba7d8133")

    assert result.bet_id == "21b40aef-01ed-41cb-8254-27aeba7d8133"
    assert result.market_url == "soccer.match_odds/home"
    assert result.status.value == "ACCEPTED"


@pytest.mark.asyncio
async def test_get_bet_history_wraps_get_bets_response():
    client = _client()
    payload = {
        "items": [
            {
                "betType": "STRAIGHT",
                "betId": "21b40aef-01ed-41cb-8254-27aeba7d8133",
                "betslipId": "betslip-1",
                "positionId": "position-1",
                "currency": "BTC",
                "createTime": "2026-04-27T00:00:00Z",
                "state": "COMPLETED",
                "result": "WIN",
                "channel": "WEB",
                "stake": "0.1000",
                "selection": {
                    "eventId": "5945044",
                    "marketUrl": "soccer.match_odds/home",
                    "price": "2.760",
                    "result": "WIN",
                },
            },
        ],
        "hasNext": True,
    }

    async def fake_get(*, url, params, headers):
        assert params["from"] == "2026-04-01T00:00:00Z"
        assert params["to"] == "2026-04-27T00:00:00Z"
        return _Response(msgspec.json.encode(payload))

    client.get = fake_get  # type: ignore[method-assign]

    history = await client.get_bet_history(
        from_date="2026-04-01T00:00:00Z",
        to_date="2026-04-27T00:00:00Z",
        limit=10,
        offset=0,
    )

    assert isinstance(history, GetBetHistoryResponse)
    assert history.total_bets == "1"
    assert history.has_next is True
    assert history.bets[0].status.value == "WIN"


@pytest.mark.asyncio
async def test_get_all_bets_paginates_v4_history():
    client = _client()
    responses = [
        {
            "items": [
                {
                    "betType": "STRAIGHT",
                    "betId": "bet-1",
                    "betslipId": "betslip-1",
                    "positionId": "position-1",
                    "currency": "BTC",
                    "createTime": "2026-04-27T00:00:00Z",
                    "state": "COMPLETED",
                    "result": "WIN",
                    "selection": {
                        "eventId": "1",
                        "marketUrl": "soccer.match_odds/home",
                        "price": "2.760",
                        "result": "WIN",
                    },
                },
            ],
            "hasNext": True,
        },
        {
            "items": [
                {
                    "betType": "STRAIGHT",
                    "betId": "bet-2",
                    "betslipId": "betslip-2",
                    "positionId": "position-2",
                    "currency": "BTC",
                    "createTime": "2026-04-27T00:00:00Z",
                    "state": "COMPLETED",
                    "result": "LOSS",
                    "selection": {
                        "eventId": "2",
                        "marketUrl": "soccer.match_odds/away",
                        "price": "2.760",
                        "result": "LOSS",
                    },
                },
            ],
            "hasNext": False,
        },
    ]
    seen_offsets: list[int] = []

    async def fake_get(*, url, params, headers):
        assert url.endswith("/v4/bets")
        seen_offsets.append(params["offset"])
        return _Response(msgspec.json.encode(responses[len(seen_offsets) - 1]))

    client.get = fake_get  # type: ignore[method-assign]

    bets = await client.get_all_bets(
        from_date="2026-04-01T00:00:00Z",
        to_date="2026-04-27T00:00:00Z",
        limit=1,
    )

    assert [bet.bet_id for bet in bets] == ["bet-1", "bet-2"]
    assert seen_offsets == [0, 1]
