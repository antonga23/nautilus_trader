# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SXBet instrument provider normalization.
# -------------------------------------------------------------------------------------------------

import pytest

from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider


TWO_INSTRUMENTS = 2
FOUR_INSTRUMENTS = 4


@pytest.mark.asyncio
async def test_sxbet_provider_normalizes_outcomes_and_market_params():
    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(),
    )

    await provider._process_market(
        {
            "marketHash": "market-1",
            "teamOneName": "Team A",
            "teamTwoName": "Team B",
            "sportId": 1,
            "leagueName": "Premier League",
            "type": 1,
            "line": 1.5,
            "orders": [
                {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
            ],
        },
    )

    instruments = list(provider.get_all().values())

    assert [instrument.outcome for instrument in instruments] == ["home", "away"]
    assert all(instrument.params == "line=1.5" for instrument in instruments)
    assert [instrument.info["outcome_one"] for instrument in instruments] == [True, False]


@pytest.mark.asyncio
async def test_sxbet_provider_sets_start_time_from_game_time():
    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(),
    )

    await provider._process_market(
        {
            "marketHash": "market-1",
            "teamOneName": "Team A",
            "teamTwoName": "Team B",
            "sportId": 1,
            "leagueName": "Premier League",
            "type": 0,
            "gameTime": 1_741_890_000,
            "orders": [
                {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
            ],
        },
    )

    instruments = list(provider.get_all().values())

    assert [instrument.start_time for instrument in instruments] == [
        "2025-03-13T18:20:00Z",
        "2025-03-13T18:20:00Z",
    ]
    assert all(instrument.info["is_two_way_market"] is True for instrument in instruments)
    assert all(instrument.info["raw_market_type"] == 0 for instrument in instruments)


@pytest.mark.asyncio
async def test_sxbet_provider_sets_live_flag_and_honors_live_only():
    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(live_only=True),
    )

    await provider._process_market(
        {
            "marketHash": "market-pre",
            "teamOneName": "Team A",
            "teamTwoName": "Team B",
            "sportId": 1,
            "leagueName": "Premier League",
            "type": 0,
            "isLive": False,
            "orders": [
                {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
            ],
        },
    )

    await provider._process_market(
        {
            "marketHash": "market-live",
            "teamOneName": "Team A",
            "teamTwoName": "Team B",
            "sportId": 1,
            "leagueName": "Premier League",
            "type": 0,
            "isLive": True,
            "orders": [
                {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
            ],
        },
    )

    instruments = list(provider.get_all().values())

    assert len(instruments) == TWO_INSTRUMENTS
    assert all(instrument.live is True for instrument in instruments)
    assert all(instrument.event_id == "market-live" for instrument in instruments)


@pytest.mark.asyncio
async def test_sxbet_provider_load_all_forwards_each_league_id():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, int | bool | None]] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
        ) -> dict:
            self.calls.append(
                {
                    "sport_id": sport_id,
                    "league_id": league_id,
                    "only_active": only_active,
                },
            )
            return {
                "data": {
                    "markets": [
                        {
                            "marketHash": f"market-{league_id}",
                            "teamOneName": "Team A",
                            "teamTwoName": "Team B",
                            "sportId": 1,
                            "leagueName": f"League {league_id}",
                            "type": 0,
                            "orders": [
                                {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                                {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
                            ],
                        },
                    ],
                },
            }

    http_client = RecordingHttpClient()
    provider = SXBetInstrumentProvider(
        http_client=http_client,
        config=SXBetInstrumentProviderConfig(
            sport_ids=frozenset({1}),
            league_ids=frozenset({10, 20}),
        ),
    )

    await provider.load_all_async()

    assert {call["league_id"] for call in http_client.calls} == {10, 20}
    assert all(call["sport_id"] == 1 for call in http_client.calls)
    assert all(call["only_active"] is True for call in http_client.calls)
    assert len(provider.get_all()) == FOUR_INSTRUMENTS


@pytest.mark.asyncio
async def test_sxbet_provider_load_all_forwards_each_sport_id():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, int | bool | None]] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
        ) -> dict:
            self.calls.append(
                {
                    "sport_id": sport_id,
                    "league_id": league_id,
                    "only_active": only_active,
                },
            )
            return {
                "data": {
                    "markets": [
                        {
                            "marketHash": f"market-{sport_id}",
                            "teamOneName": "Team A",
                            "teamTwoName": "Team B",
                            "sportId": sport_id,
                            "leagueName": f"League {sport_id}",
                            "type": 0,
                            "orders": [
                                {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                                {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
                            ],
                        },
                    ],
                },
            }

    http_client = RecordingHttpClient()
    provider = SXBetInstrumentProvider(
        http_client=http_client,
        config=SXBetInstrumentProviderConfig(
            sport_ids=frozenset({1, 2}),
        ),
    )

    await provider.load_all_async()

    assert {call["sport_id"] for call in http_client.calls} == {1, 2}
    assert all(call["league_id"] is None for call in http_client.calls)
    assert all(call["only_active"] is True for call in http_client.calls)
    assert len(provider.get_all()) == FOUR_INSTRUMENTS
