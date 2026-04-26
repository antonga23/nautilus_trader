# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SXBet instrument provider normalization.
# -------------------------------------------------------------------------------------------------

import pytest

from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.providers import SXBET_MARKET_BATCH_SIZE
from nautilus_trader.adapters.sxbet.providers import SXBET_MARKET_PAGE_SIZE
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage


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
            "type": 3,
            "line": 1.5,
            "outcomeOneName": "Team A +1.5",
            "outcomeTwoName": "Team B -1.5",
            "bestOdds": {
                "outcomeOne": {"percentageOdds": str(decimal_odds_to_percentage(2.0))},
                "outcomeTwo": {"percentageOdds": str(decimal_odds_to_percentage(2.1))},
            },
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
            "type": 52,
            "gameTime": 1_741_890_000,
            "outcomeOneName": "Team A",
            "outcomeTwoName": "Team B",
            "bestOdds": {
                "outcomeOne": {"percentageOdds": str(decimal_odds_to_percentage(2.0))},
                "outcomeTwo": {"percentageOdds": str(decimal_odds_to_percentage(2.2))},
            },
        },
    )

    instruments = list(provider.get_all().values())

    assert [instrument.start_time for instrument in instruments] == [
        "2025-03-13T18:20:00Z",
        "2025-03-13T18:20:00Z",
    ]
    assert all(instrument.info["is_two_way_market"] is True for instrument in instruments)
    assert all(instrument.info["raw_market_type"] == 52 for instrument in instruments)


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
            "type": 52,
            "isLive": False,
            "outcomeOneName": "Team A",
            "outcomeTwoName": "Team B",
            "bestOdds": {
                "outcomeOne": {"percentageOdds": str(decimal_odds_to_percentage(2.0))},
                "outcomeTwo": {"percentageOdds": str(decimal_odds_to_percentage(2.1))},
            },
        },
    )

    await provider._process_market(
        {
            "marketHash": "market-live",
            "teamOneName": "Team A",
            "teamTwoName": "Team B",
            "sportId": 1,
            "leagueName": "Premier League",
            "type": 52,
            "isLive": True,
            "outcomeOneName": "Team A",
            "outcomeTwoName": "Team B",
            "bestOdds": {
                "outcomeOne": {"percentageOdds": str(decimal_odds_to_percentage(2.0))},
                "outcomeTwo": {"percentageOdds": str(decimal_odds_to_percentage(2.1))},
            },
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
            self.best_odds_calls: list[dict[str, object]] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
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
                            "type": 52,
                            "outcomeOneName": "Team A",
                            "outcomeTwoName": "Team B",
                        },
                    ],
                },
            }

        async def get_best_odds(
            self,
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            self.best_odds_calls.append(
                {
                    "market_hashes": market_hashes,
                    "base_token": base_token,
                    "log_api_error": log_api_error,
                },
            )
            return {
                "data": {
                    "bestOdds": [
                        {
                            "marketHash": market_hash,
                            "outcomeOne": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.0)),
                            },
                            "outcomeTwo": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.1)),
                            },
                        }
                        for market_hash in market_hashes
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
    assert http_client.best_odds_calls == [
        {
            "market_hashes": ["market-10", "market-20"],
            "base_token": SXBET_TOKENS["USDC"],
            "log_api_error": False,
        },
    ]
    assert len(provider.get_all()) == FOUR_INSTRUMENTS


@pytest.mark.asyncio
async def test_sxbet_provider_load_all_forwards_each_sport_id():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, int | bool | None]] = []
            self.best_odds_calls: list[dict[str, object]] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
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
                            "type": 52,
                            "outcomeOneName": "Team A",
                            "outcomeTwoName": "Team B",
                        },
                    ],
                },
            }

        async def get_best_odds(
            self,
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            self.best_odds_calls.append(
                {
                    "market_hashes": market_hashes,
                    "base_token": base_token,
                    "log_api_error": log_api_error,
                },
            )
            return {
                "data": {
                    "bestOdds": [
                        {
                            "marketHash": market_hash,
                            "outcomeOne": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.0)),
                            },
                            "outcomeTwo": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.1)),
                            },
                        }
                        for market_hash in market_hashes
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
    assert http_client.best_odds_calls == [
        {
            "market_hashes": ["market-1", "market-2"],
            "base_token": SXBET_TOKENS["USDC"],
            "log_api_error": False,
        },
    ]
    assert len(provider.get_all()) == FOUR_INSTRUMENTS


@pytest.mark.asyncio
async def test_sxbet_provider_load_all_paginates_market_requests():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.market_calls: list[dict[str, int | str | None]] = []
            self.best_odds_calls: list[dict[str, object]] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
        ) -> dict:
            self.market_calls.append(
                {
                    "sport_id": sport_id,
                    "league_id": league_id,
                    "pagination_key": pagination_key,
                    "page_size": page_size,
                },
            )
            if pagination_key is None:
                return {
                    "data": {
                        "markets": [
                            {
                                "marketHash": "market-1",
                                "teamOneName": "Team A",
                                "teamTwoName": "Team B",
                                "sportId": 1,
                                "leagueName": "League One",
                                "type": 52,
                                "outcomeOneName": "Team A",
                                "outcomeTwoName": "Team B",
                            },
                        ],
                        "nextKey": "cursor-1",
                    },
                }

            return {
                "data": {
                    "markets": [
                        {
                            "marketHash": "market-2",
                            "teamOneName": "Team A",
                            "teamTwoName": "Team B",
                            "sportId": 1,
                            "leagueName": "League One",
                            "type": 52,
                            "outcomeOneName": "Team A",
                            "outcomeTwoName": "Team B",
                        },
                    ],
                },
            }

        async def get_best_odds(
            self,
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            self.best_odds_calls.append(
                {
                    "market_hashes": market_hashes,
                    "base_token": base_token,
                    "log_api_error": log_api_error,
                },
            )
            return {
                "data": {
                    "bestOdds": [
                        {
                            "marketHash": market_hash,
                            "outcomeOne": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.0)),
                            },
                            "outcomeTwo": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.1)),
                            },
                        }
                        for market_hash in market_hashes
                    ],
                },
            }

    http_client = RecordingHttpClient()
    provider = SXBetInstrumentProvider(
        http_client=http_client,
        config=SXBetInstrumentProviderConfig(sport_ids=frozenset({1})),
    )

    await provider.load_all_async()

    assert http_client.market_calls == [
        {
            "sport_id": 1,
            "league_id": None,
            "pagination_key": None,
            "page_size": SXBET_MARKET_PAGE_SIZE,
        },
        {
            "sport_id": 1,
            "league_id": None,
            "pagination_key": "cursor-1",
            "page_size": SXBET_MARKET_PAGE_SIZE,
        },
    ]
    assert http_client.best_odds_calls == [
        {
            "market_hashes": ["market-1", "market-2"],
            "base_token": SXBET_TOKENS["USDC"],
            "log_api_error": False,
        },
    ]
    assert len(provider.get_all()) == FOUR_INSTRUMENTS


@pytest.mark.asyncio
async def test_sxbet_provider_decouples_market_discovery_from_instrument_limit():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.market_calls: list[dict[str, int | str | None]] = []
            self.order_book_calls: list[str] = []
            self.best_odds_calls: list[list[str]] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
        ) -> dict:
            self.market_calls.append(
                {
                    "sport_id": sport_id,
                    "league_id": league_id,
                    "pagination_key": pagination_key,
                    "page_size": page_size,
                },
            )
            page = int(pagination_key or 0)
            start = page * SXBET_MARKET_PAGE_SIZE
            markets = [
                {
                    "marketHash": f"market-{index}",
                    "teamOneName": f"Team {index}A",
                    "teamTwoName": f"Team {index}B",
                    "sportId": 1,
                    "leagueName": "League One",
                    "type": 52,
                    "outcomeOneName": f"Team {index}A",
                    "outcomeTwoName": f"Team {index}B",
                }
                for index in range(start, start + SXBET_MARKET_PAGE_SIZE)
            ]
            return {
                "data": {
                    "markets": markets,
                    "nextKey": str(page + 1),
                },
            }

        async def get_order_book(self, market_hash: str) -> dict:
            self.order_book_calls.append(market_hash)
            return {
                "data": {
                    "orders": [
                        {
                            "isMakerBettingOutcomeOne": True,
                            "percentageOdds": decimal_odds_to_percentage(2.0),
                        },
                        {
                            "isMakerBettingOutcomeOne": False,
                            "percentageOdds": decimal_odds_to_percentage(2.0),
                        },
                    ],
                },
            }

        async def get_best_odds(
            self,
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            self.best_odds_calls.append(market_hashes)
            return {"data": {"bestOdds": []}}

    http_client = RecordingHttpClient()
    provider = SXBetInstrumentProvider(
        http_client=http_client,
        config=SXBetInstrumentProviderConfig(
            sport_ids=frozenset({1}),
            instrument_load_limit=50,
            market_discovery_limit=150,
            prefer_liquid_markets=True,
            liquidity_probe_limit=100,
        ),
    )

    await provider.load_all_async()

    assert [call["pagination_key"] for call in http_client.market_calls] == [
        None,
        "1",
        "2",
    ]
    assert len(http_client.order_book_calls) == 25
    assert len(provider.get_all()) == 50


@pytest.mark.asyncio
async def test_sxbet_provider_scans_until_pagination_end_when_discovery_limit_is_none():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.market_calls: list[str | None] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
        ) -> dict:
            self.market_calls.append(pagination_key)
            page = int(pagination_key or 0)
            start = page * SXBET_MARKET_PAGE_SIZE
            markets = [
                {
                    "marketHash": f"market-{index}",
                    "teamOneName": f"Team {index}A",
                    "teamTwoName": f"Team {index}B",
                    "sportId": 1,
                    "leagueName": "League One",
                    "type": 52,
                    "outcomeOneName": f"Team {index}A",
                    "outcomeTwoName": f"Team {index}B",
                }
                for index in range(start, start + SXBET_MARKET_PAGE_SIZE)
            ]
            data = {"markets": markets}
            if page < 2:
                data["nextKey"] = str(page + 1)
            return {"data": data}

        async def get_order_book(self, market_hash: str) -> dict:
            return {
                "data": {
                    "orders": [
                        {
                            "isMakerBettingOutcomeOne": True,
                            "percentageOdds": decimal_odds_to_percentage(2.0),
                        },
                        {
                            "isMakerBettingOutcomeOne": False,
                            "percentageOdds": decimal_odds_to_percentage(2.0),
                        },
                    ],
                },
            }

        async def get_best_odds(
            self,
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            return {"data": {"bestOdds": []}}

    http_client = RecordingHttpClient()
    provider = SXBetInstrumentProvider(
        http_client=http_client,
        config=SXBetInstrumentProviderConfig(
            sport_ids=frozenset({1}),
            instrument_load_limit=50,
            market_discovery_limit=None,
            prefer_liquid_markets=True,
            liquidity_probe_limit=100,
        ),
    )

    await provider.load_all_async()

    assert http_client.market_calls == [None, "1", "2"]
    assert len(provider.get_all()) == 50


@pytest.mark.asyncio
async def test_sxbet_provider_prefers_two_sided_liquid_markets():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.order_book_calls: list[str] = []

        async def get_order_book(self, market_hash: str) -> dict:
            self.order_book_calls.append(market_hash)
            orders_by_market = {
                "market-0": [
                    {
                        "isMakerBettingOutcomeOne": True,
                        "percentageOdds": decimal_odds_to_percentage(2.0),
                    },
                ],
                "market-2": [
                    {
                        "isMakerBettingOutcomeOne": True,
                        "percentageOdds": decimal_odds_to_percentage(2.0),
                    },
                    {
                        "isMakerBettingOutcomeOne": False,
                        "percentageOdds": decimal_odds_to_percentage(2.1),
                    },
                ],
                "market-3": [
                    {
                        "isMakerBettingOutcomeOne": False,
                        "percentageOdds": decimal_odds_to_percentage(2.2),
                    },
                ],
                "market-4": [
                    {
                        "isMakerBettingOutcomeOne": True,
                        "percentageOdds": decimal_odds_to_percentage(2.0),
                    },
                    {
                        "isMakerBettingOutcomeOne": False,
                        "percentageOdds": decimal_odds_to_percentage(2.1),
                    },
                ],
            }
            return {"data": {"orders": orders_by_market.get(market_hash, [])}}

    markets = [{"marketHash": f"market-{index}"} for index in range(6)]
    provider = SXBetInstrumentProvider(
        http_client=RecordingHttpClient(),
        config=SXBetInstrumentProviderConfig(
            instrument_load_limit=4,
            prefer_liquid_markets=True,
            liquidity_probe_limit=6,
            min_two_sided_markets=2,
        ),
    )

    selected = await provider._select_markets_for_processing(markets, target_market_count=2)

    assert [market["marketHash"] for market in selected] == ["market-2", "market-4"]


@pytest.mark.asyncio
async def test_sxbet_provider_hydrates_best_odds_in_batches():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.best_odds_calls: list[dict[str, object]] = []

        async def get_best_odds(
            self,
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            self.best_odds_calls.append(
                {
                    "market_hashes": market_hashes,
                    "base_token": base_token,
                    "log_api_error": log_api_error,
                },
            )
            return {
                "data": {
                    "bestOdds": [
                        {
                            "marketHash": market_hash,
                            "outcomeOne": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.0)),
                            },
                            "outcomeTwo": {
                                "percentageOdds": str(decimal_odds_to_percentage(2.1)),
                            },
                        }
                        for market_hash in market_hashes
                    ],
                },
            }

    markets = [{"marketHash": f"market-{index}"} for index in range(SXBET_MARKET_BATCH_SIZE + 1)]
    http_client = RecordingHttpClient()
    provider = SXBetInstrumentProvider(
        http_client=http_client,
        config=SXBetInstrumentProviderConfig(),
    )

    await provider._hydrate_best_odds(markets)

    assert len(http_client.best_odds_calls) == 2
    assert http_client.best_odds_calls[0] == {
        "market_hashes": [f"market-{index}" for index in range(SXBET_MARKET_BATCH_SIZE)],
        "base_token": SXBET_TOKENS["USDC"],
        "log_api_error": False,
    }
    assert http_client.best_odds_calls[1] == {
        "market_hashes": [f"market-{SXBET_MARKET_BATCH_SIZE}"],
        "base_token": SXBET_TOKENS["USDC"],
        "log_api_error": False,
    }
    assert markets[0]["bestOdds"]["marketHash"] == "market-0"
    assert markets[-1]["bestOdds"]["marketHash"] == f"market-{SXBET_MARKET_BATCH_SIZE}"


@pytest.mark.asyncio
async def test_sxbet_provider_uses_placeholder_prices_when_best_odds_missing():
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
            "type": 52,
            "outcomeOneName": "Team A",
            "outcomeTwoName": "Team B",
        },
    )

    instruments = list(provider.get_all().values())
    assert len(instruments) == TWO_INSTRUMENTS
    assert all(instrument.price == 2.0 for instrument in instruments)
    assert all(instrument.info["has_best_odds"] is False for instrument in instruments)


@pytest.mark.asyncio
async def test_sxbet_provider_load_all_continues_when_best_odds_hydration_fails():
    class RecordingHttpClient:
        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
        ) -> dict:
            return {
                "data": {
                    "markets": [
                        {
                            "marketHash": "market-1",
                            "teamOneName": "Team A",
                            "teamTwoName": "Team B",
                            "sportId": 1,
                            "leagueName": "Premier League",
                            "type": 52,
                            "outcomeOneName": "Team A",
                            "outcomeTwoName": "Team B",
                        },
                    ],
                },
            }

        async def get_best_odds(
            self,
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            assert log_api_error is False
            raise SXBetHttpClientError(
                "SX.bet API request failed with status 403",
                status_code=403,
            )

    provider = SXBetInstrumentProvider(
        http_client=RecordingHttpClient(),
        config=SXBetInstrumentProviderConfig(sport_ids=frozenset({1})),
    )

    await provider.load_all_async()

    instruments = list(provider.get_all().values())
    assert len(instruments) == TWO_INSTRUMENTS
    assert all(instrument.price == 2.0 for instrument in instruments)
    assert all(instrument.info["has_best_odds"] is False for instrument in instruments)
