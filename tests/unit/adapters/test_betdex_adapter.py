# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for BetDex/Monaco adapter.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import pytest

from nautilus_trader.adapters.betdex.config import BetDexInstrumentProviderConfig
from nautilus_trader.adapters.betdex.data import BetDexDataClient
from nautilus_trader.adapters.betdex.http_client import BetDexHttpClient
from nautilus_trader.adapters.betdex.providers import BetDexInstrumentProvider
from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.semantics import MarketNormalizer


def _market_payload() -> dict:
    return {
        "markets": [
            {
                "id": "market-1",
                "ownerAppId": "betdex-app",
                "event": {"_ids": ["event-1"]},
                "name": "Full Time Result",
                "marketType": {"_ids": ["type-1x2"]},
                "currencyId": "USDC",
                "published": True,
                "suspended": False,
                "status": "Open",
                "inPlayStatus": "PrePlay",
                "marketOutcomes": {"_ids": ["outcome-home", "outcome-draw", "outcome-away"]},
                "externalReferences": {"_ids": ["101"]},
                "lockAt": "2026-05-10T15:00:00Z",
            },
        ],
        "marketOutcomes": [
            {"id": "outcome-home", "title": "Team A", "ordering": 0},
            {"id": "outcome-draw", "title": "Draw", "ordering": 1},
            {"id": "outcome-away", "title": "Team B", "ordering": 2},
        ],
        "events": [
            {
                "id": "event-1",
                "name": "Team A vs Team B",
                "eventGroup": {"_ids": ["group-1"]},
                "expectedStartTime": "2026-05-10T15:00:00Z",
                "participants": {"_ids": ["participant-a", "participant-b"]},
            },
        ],
        "eventGroups": [
            {"id": "group-1", "name": "Premier League", "subcategory": {"_ids": ["subcat-1"]}},
        ],
        "subcategories": [
            {"id": "subcat-1", "name": "Soccer", "category": {"_ids": ["cat-1"]}},
        ],
        "categories": [{"id": "cat-1", "name": "Soccer"}],
        "participants": [
            {"id": "participant-a", "name": "Team A", "type": "Team"},
            {"id": "participant-b", "name": "Team B", "type": "Team"},
        ],
        "externalReferences": [
            {"id": 101, "source": "sx.bet", "externalReference": "sx-market-1"},
        ],
    }


@pytest.mark.asyncio
async def test_betdex_provider_builds_crypto_betting_instruments_from_market_payload():
    provider = BetDexInstrumentProvider(
        http_client=object(),
        config=BetDexInstrumentProviderConfig(),
    )

    processed = provider._process_market_response(_market_payload(), instrument_limit=None)
    instruments = list(provider.get_all().values())

    assert processed == 1
    assert len(instruments) == 3
    assert [instrument.outcome for instrument in instruments] == [
        Outcome.HOME.value,
        Outcome.DRAW.value,
        Outcome.AWAY.value,
    ]
    assert all(instrument.venue_name.value == "BETDEX" for instrument in instruments)
    assert all(instrument.market_id == "market-1" for instrument in instruments)
    assert all(instrument.event_id == "event-1" for instrument in instruments)
    assert all(instrument.sport_name == "soccer" for instrument in instruments)
    assert all(instrument.competition_name == "Premier League" for instrument in instruments)
    assert all(instrument.side == SelectionSide.BACK for instrument in instruments)
    assert instruments[0].info["aggregator"] is True
    assert instruments[0].info["aggregated_venues"] == ["POLYMARKET", "SXBET"]
    assert instruments[0].info["currency_id"] == "USDC"
    assert instruments[0].info["external_references"] == (
        {"source": "sx.bet", "externalReference": "sx-market-1"},
    )

    normalized = MarketNormalizer.normalize(instruments[0])
    assert normalized.venue == "BETDEX"
    assert normalized.sport == "soccer"
    assert normalized.market_type == "MATCH_ODDS"
    assert normalized.selection == "HOME"


def test_betdex_provider_preserves_unknown_currency_id_and_uses_usdc_currency():
    payload = _market_payload()
    payload["markets"][0]["currencyId"] = "currency-uuid"
    provider = BetDexInstrumentProvider(
        http_client=object(),
        config=BetDexInstrumentProviderConfig(),
    )

    provider._process_market_response(payload, instrument_limit=1)
    instrument = next(iter(provider.get_all().values()))

    assert instrument.quote_currency.code == "USDC"
    assert instrument.info["currency_id"] == "currency-uuid"


@pytest.mark.asyncio
async def test_betdex_provider_loads_events_then_markets():
    class FakeClient:
        @staticmethod
        async def get_events(**params):
            assert params["active"] is True
            assert params["starting"] == "Later"
            return {"events": [{"id": "event-1"}]}

        @staticmethod
        async def get_markets(**params):
            assert params["eventIds"] == ["event-1"]
            payload = _market_payload()
            payload["_params"] = params
            return payload

    provider = BetDexInstrumentProvider(
        http_client=FakeClient(),
        config=BetDexInstrumentProviderConfig(event_discovery_limit=10, page_size=10),
    )

    await provider.load_all_async({})

    assert len(provider.get_all()) == 3


def test_betdex_data_client_selects_best_for_and_against_liquidity():
    bid_price, bid_size, ask_price, ask_size = BetDexDataClient._best_bid_ask(
        [
            {"side": "For", "outcomeId": "home", "price": 2.1, "amount": 8},
            {"side": "For", "outcomeId": "home", "price": 2.2, "amount": 5},
            {"side": "Against", "outcomeId": "home", "price": 2.4, "amount": 3},
            {"side": "Against", "outcomeId": "home", "price": 2.3, "amount": 4},
            {"side": "For", "outcomeId": "away", "price": 1.9, "amount": 7},
        ],
        "home",
    )

    assert bid_price == 2.2
    assert bid_size == 5
    assert ask_price == 2.3
    assert ask_size == 4


def test_betdex_http_client_normalizes_repeated_query_params():
    assert BetDexHttpClient._normalize_params(
        {"marketIds": ["m1", "m2"], "published": True, "skip": None},
    ) == [
        ("marketIds", "m1"),
        ("marketIds", "m2"),
        ("published", "True"),
    ]
