# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SXBet instrument provider normalization.
# -------------------------------------------------------------------------------------------------

from collections import Counter
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_SPORT_IDS
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.providers import SXBET_MARKET_BATCH_SIZE
from nautilus_trader.adapters.sxbet.providers import SXBET_MARKET_PAGE_SIZE
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage


TWO_INSTRUMENTS = 2
FOUR_INSTRUMENTS = 4


def test_sxbet_static_sport_ids_match_current_active_taxonomy():
    assert SXBET_SPORT_IDS[2] == "ice_hockey"
    assert SXBET_SPORT_IDS[3] == "baseball"
    assert SXBET_SPORT_IDS[1] == "basketball"
    assert SXBET_SPORT_IDS[5] == "soccer"
    assert SXBET_SPORT_IDS[6] == "tennis"
    assert SXBET_SPORT_IDS[20] == "rugby_league"
    assert SXBET_SPORT_IDS[26] == "australian_rules"


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
async def test_sxbet_provider_handicap_favourite_prices_short_from_best_odds_complement():
    # Money-path regression for the SX.bet handicap-favourite mispricing.
    #
    # ``bestOdds.outcomeX.percentageOdds`` is the best *maker* implied probability
    # resting on outcome X, so a taker backing an outcome matches the makers on the
    # *opposite* outcome and takes the complement of their odds. The provider used
    # to read the same-side field and apply ``1 / maker_implied`` with no
    # complement, which inflated both legs of every hydrated two-sided market into
    # longshot prices. On a real baseball -3.5 run line that priced the away
    # favourite (outcomeOne, +3.5) at 5.0896 and the home underdog (outcomeTwo,
    # -3.5) at 5.9920 -- a raw probability sum of ~0.36 (a ~175% "arbitrage" that
    # passed the same-venue dry-run gate) instead of a healthy overround.
    live_favourite_phantom_odds = 5.0896  # AWAY +3.5 leg, pre-fix
    live_underdog_phantom_odds = 5.9920  # HOME -3.5 leg, pre-fix

    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(),
    )
    await provider._process_market(
        {
            "marketHash": "0x3565fb2a6e64c8",
            "teamOneName": "Athletics",  # away favourite
            "teamTwoName": "Dodgers",  # home underdog
            "sportId": 3,
            "leagueName": "MLB",
            "type": 3,
            "line": 3.5,
            "outcomeOneName": "Athletics +3.5",
            "outcomeTwoName": "Dodgers -3.5",
            # decimal_odds_to_percentage(d) == round((1 / d) * 1e20), i.e. the maker
            # implied probability. These are the live maker implieds whose reciprocal
            # produced the pre-fix phantom odds above.
            "bestOdds": {
                "outcomeOne": {
                    "percentageOdds": str(decimal_odds_to_percentage(live_favourite_phantom_odds)),
                },
                "outcomeTwo": {
                    "percentageOdds": str(decimal_odds_to_percentage(live_underdog_phantom_odds)),
                },
            },
        },
    )

    instruments = list(provider.get_all().values())
    favourite = next(i for i in instruments if i.info["outcome_one"] is True)
    underdog = next(i for i in instruments if i.info["outcome_one"] is False)

    # The favourite (outcomeOne) now prices from the complement of the opposite
    # (outcomeTwo) maker implied: 1 / (1 - 1 / 5.9920) ~= 1.20.
    assert float(favourite.price) == pytest.approx(
        1 / (1 - 1 / live_underdog_phantom_odds),
        rel=1e-4,
    )
    assert float(underdog.price) == pytest.approx(
        1 / (1 - 1 / live_favourite_phantom_odds),
        rel=1e-4,
    )

    # Favourite prices short (pre-fix it was the 5.0896 phantom longshot).
    assert float(favourite.price) < 1.5
    assert float(favourite.price) != pytest.approx(live_favourite_phantom_odds, rel=1e-3)

    # The complementary pair is a healthy overround, not a standing phantom arb.
    implied_sum = 1 / float(favourite.price) + 1 / float(underdog.price)
    assert implied_sum > 1.0


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
async def test_sxbet_provider_flags_draw_capable_match_odds_as_three_way():
    # SX.bet lists a soccer 1X2 as binary "Team / Not Team" decomposition markets that
    # all map to match_odds. Flagging them two-way drops the draw and lets the home and
    # away legs be mistaken for a complementary pair, so a draw-capable sport must stay
    # three-way while a genuine two-way sport (basketball) is unchanged.
    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(),
    )

    async def process(sport_id: int) -> list:
        provider._instruments.clear()
        await provider._process_market(
            {
                "marketHash": f"market-{sport_id}",
                "teamOneName": "Levski Sofia",
                "teamTwoName": "Borac Banja Luka",
                "sportId": sport_id,
                "leagueName": "Test League",
                "type": 1,
                "gameTime": 1_784_050_200,
                "outcomeOneName": "Levski Sofia",
                "outcomeTwoName": "Not Levski Sofia",
                "bestOdds": {
                    "outcomeOne": {"percentageOdds": str(decimal_odds_to_percentage(1.72))},
                    "outcomeTwo": {"percentageOdds": str(decimal_odds_to_percentage(2.2))},
                },
            },
        )
        return list(provider.get_all().values())

    soccer = await process(5)
    basketball = await process(1)

    assert soccer
    assert all(i.market_name == "match_odds" for i in soccer)
    assert all(i.info["is_two_way_market"] is False for i in soccer)
    assert all(i.info["is_two_way_market"] is True for i in basketball)


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
    assert all(instrument.event_id.startswith("sxbet-") for instrument in instruments)
    assert all(instrument.market_id == "market-live" for instrument in instruments)
    assert all(
        instrument.info["sxbet_event_id_source"] == "derived_fixture_key"
        for instrument in instruments
    )


@pytest.mark.asyncio
async def test_sxbet_provider_uses_fixture_event_id_and_keeps_market_hash_as_market_id():
    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(),
    )

    await provider._process_market(
        {
            "marketHash": "market-1",
            "eventId": 123456,
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
    assert all(instrument.event_id == "123456" for instrument in instruments)
    assert all(instrument.market_id == "market-1" for instrument in instruments)
    assert all(instrument.info["sxbet_market_hash"] == "market-1" for instrument in instruments)
    assert all(instrument.info["sxbet_event_id_source"] == "eventId" for instrument in instruments)
    assert provider.find_by_market_hash("market-1") == instruments


@pytest.mark.asyncio
async def test_sxbet_provider_scoped_match_winner_market_sets_period_params():
    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(),
    )

    await provider._process_market(
        {
            "marketHash": "market-q1",
            "teamOneName": "Team A",
            "teamTwoName": "Team B",
            "sportId": 1,
            "leagueName": "Premier League",
            "type": 202,
            "outcomeOneName": "Team A (1st Quarter)",
            "outcomeTwoName": "Team B (1st Quarter)",
        },
    )
    await provider._process_market(
        {
            "marketHash": "market-set1",
            "teamOneName": "Player A",
            "teamTwoName": "Player B",
            "sportId": 6,
            "leagueName": "ATP",
            "type": 202,
            "outcomeOneName": "Player A (1st Set)",
            "outcomeTwoName": "Player B (1st Set)",
        },
    )

    instruments = list(provider.get_all().values())
    basketball = [inst for inst in instruments if inst.market_id == "market-q1"]
    tennis = [inst for inst in instruments if inst.market_id == "market-set1"]

    assert len(basketball) == TWO_INSTRUMENTS
    assert len(tennis) == TWO_INSTRUMENTS
    assert all(instrument.market_type == "match_odds" for instrument in basketball)
    assert all(instrument.params == "period=q1" for instrument in basketball)
    assert all(instrument.market_type == "match_odds" for instrument in tennis)
    assert all(instrument.params == "set=1,period=set1" for instrument in tennis)


@pytest.mark.asyncio
async def test_sxbet_provider_refreshes_sport_labels_from_active_sports():
    class RecordingHttpClient:
        @staticmethod
        async def get_active_sports() -> dict:
            return {
                "data": [
                    {"sportId": 1, "label": "Basketball"},
                    {"sportId": 5, "label": "Soccer"},
                    {"sportId": 26, "label": "AFL"},
                ],
            }

        @staticmethod
        async def get_markets(
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
        ) -> dict:
            assert sport_id == 26
            assert league_id is None
            assert only_active is True
            return {
                "data": {
                    "markets": [
                        {
                            "marketHash": "market-afl",
                            "teamOneName": "Team A",
                            "teamTwoName": "Team B",
                            "sportId": 26,
                            "leagueName": "AFL",
                            "type": 52,
                            "outcomeOneName": "Team A",
                            "outcomeTwoName": "Team B",
                        },
                    ],
                },
            }

        @staticmethod
        async def get_best_odds(
            *,
            market_hashes: list[str],
            base_token: str,
            log_api_error: bool = True,
        ) -> dict:
            return {"data": {"bestOdds": []}}

    provider = SXBetInstrumentProvider(
        http_client=RecordingHttpClient(),
        config=SXBetInstrumentProviderConfig(sport_ids=frozenset({26})),
    )

    await provider.load_all_async()

    instruments = list(provider.get_all().values())
    assert len(instruments) == TWO_INSTRUMENTS
    assert all(instrument.sport_name == "australian_rules" for instrument in instruments)


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
async def test_sxbet_provider_balances_market_discovery_across_configured_sports():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.market_calls: list[int | None] = []

        async def get_markets(
            self,
            sport_id: int | None = None,
            league_id: int | None = None,
            only_active: bool = True,
            pagination_key: str | None = None,
            page_size: int | None = None,
        ) -> dict:
            self.market_calls.append(sport_id)
            return {
                "data": {
                    "markets": [
                        {
                            "marketHash": f"market-{sport_id}-{index}",
                            "teamOneName": f"Sport {sport_id} Team {index}A",
                            "teamTwoName": f"Sport {sport_id} Team {index}B",
                            "sportId": sport_id,
                            "leagueName": f"League {sport_id}",
                            "type": 52,
                            "outcomeOneName": f"Sport {sport_id} Team {index}A",
                            "outcomeTwoName": f"Sport {sport_id} Team {index}B",
                        }
                        for index in range(3)
                    ],
                },
            }

        @staticmethod
        async def get_best_odds(
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
            sport_ids=frozenset({1, 5, 6}),
            market_discovery_limit=6,
            prefer_liquid_markets=False,
        ),
    )

    await provider.load_all_async()

    sport_counts = Counter(instrument.sport_name for instrument in provider.get_all().values())
    assert http_client.market_calls == [1, 5, 6]
    assert sport_counts == {
        "basketball": 4,
        "soccer": 4,
        "tennis": 4,
    }


def test_sxbet_provider_prioritizes_near_horizon_markets_within_balanced_sports():
    provider = SXBetInstrumentProvider(
        http_client=object(),
        config=SXBetInstrumentProviderConfig(max_resolution_horizon_hours=48),
    )
    now = datetime.now(UTC)
    markets = [
        {
            "marketHash": "basketball-far",
            "sportId": 1,
            "gameTime": int((now + timedelta(hours=72)).timestamp()),
        },
        {
            "marketHash": "basketball-near",
            "sportId": 1,
            "gameTime": int((now + timedelta(hours=2)).timestamp()),
        },
        {
            "marketHash": "soccer-far",
            "sportId": 5,
            "gameTime": int((now + timedelta(hours=72)).timestamp()),
        },
        {
            "marketHash": "soccer-near",
            "sportId": 5,
            "gameTime": int((now + timedelta(hours=3)).timestamp()),
        },
    ]

    selected = provider._balanced_market_sequence(markets, sport_order=(1, 5), limit=2)

    assert [market["marketHash"] for market in selected] == [
        "basketball-near",
        "soccer-near",
    ]


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
            data: dict[str, object] = {"markets": markets}
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
async def test_sxbet_provider_tolerates_liquidity_probe_timeout():
    class RecordingHttpClient:
        def __init__(self) -> None:
            self.order_book_calls: list[str] = []

        async def get_order_book(self, market_hash: str) -> dict:
            self.order_book_calls.append(market_hash)
            if market_hash == "market-0":
                raise SXBetHttpClientError(
                    "Request failed for GET /orders: TimeoutError",
                )
            return {
                "data": {
                    "orders": [
                        {
                            "isMakerBettingOutcomeOne": True,
                            "percentageOdds": decimal_odds_to_percentage(2.0),
                        },
                        {
                            "isMakerBettingOutcomeOne": False,
                            "percentageOdds": decimal_odds_to_percentage(2.1),
                        },
                    ],
                },
            }

    markets = [{"marketHash": f"market-{index}"} for index in range(3)]
    http_client = RecordingHttpClient()
    provider = SXBetInstrumentProvider(
        http_client=http_client,
        config=SXBetInstrumentProviderConfig(
            instrument_load_limit=6,
            prefer_liquid_markets=True,
            liquidity_probe_limit=3,
            min_two_sided_markets=1,
        ),
    )

    selected = await provider._select_markets_for_processing(markets, target_market_count=2)

    assert [market["marketHash"] for market in selected] == ["market-1", "market-2"]
    assert http_client.order_book_calls == ["market-0", "market-1", "market-2"]


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
async def test_sxbet_provider_uses_placeholder_prices_when_best_odds_not_executable():
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
            "bestOdds": {
                "outcomeOne": {"percentageOdds": str(decimal_odds_to_percentage(1.0))},
                "outcomeTwo": {"percentageOdds": str(decimal_odds_to_percentage(2.1))},
            },
        },
    )

    instruments = list(provider.get_all().values())
    home, away = instruments
    # The outcomeOne (home) taker prices off the executable opposite (outcomeTwo)
    # maker at implied 1 / 2.1: complement 1 / (1 - 1 / 2.1) ~= 1.909.
    assert home.price == pytest.approx(1 / (1 - 1 / 2.1))
    assert home.info["has_best_odds"] is True
    # The outcomeTwo (away) taker would price off the non-executable outcomeOne
    # maker (implied 1.0), so it falls back to the placeholder price.
    assert away.price == 2.0
    assert away.info["has_best_odds"] is False


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
