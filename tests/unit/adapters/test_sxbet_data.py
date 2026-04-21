# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SX.bet market data quoting.
# -------------------------------------------------------------------------------------------------

from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.config import SXBetDataClientConfig
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.data import SXBetDataClient
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage
from nautilus_trader.common.component import Logger
from nautilus_trader.common.functions import get_event_loop
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


EXPECTED_EXECUTABLE_ODDS = 2.5
EXPECTED_ONE_SIDED_ODDS = 2.0


def test_best_bid_ask_uses_highest_executable_decimal_odds():
    orders = [
        {
            "isMakerBettingOutcomeOne": True,
            "percentageOdds": decimal_odds_to_percentage(2.0),
        },
        {
            "isMakerBettingOutcomeOne": True,
            "percentageOdds": decimal_odds_to_percentage(2.5),
        },
        {
            "isMakerBettingOutcomeOne": False,
            "percentageOdds": decimal_odds_to_percentage(2.0),
        },
        {
            "isMakerBettingOutcomeOne": False,
            "percentageOdds": decimal_odds_to_percentage(2.5),
        },
    ]

    best_bid, best_ask = SXBetDataClient._best_bid_ask(orders, is_outcome_one=True)

    assert best_bid == EXPECTED_EXECUTABLE_ODDS
    assert best_ask == 0


def test_has_valid_spread_rejects_locked_or_crossed_quotes():
    assert SXBetDataClient._has_valid_spread(2.4, 2.5) is True
    assert SXBetDataClient._has_valid_spread(2.5, 2.5) is False
    assert SXBetDataClient._has_valid_spread(2.6, 2.5) is False


@pytest.mark.asyncio
async def test_fetch_and_publish_quotes_emits_one_sided_quote():
    instrument = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="market-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDT"),
        info={"outcome_one": True},
    )

    http_client = Mock()
    http_client.get_order_book = AsyncMock(
        return_value={
            "data": {
                "orders": [
                    {
                        "isMakerBettingOutcomeOne": True,
                        "percentageOdds": decimal_odds_to_percentage(2.0),
                    },
                ],
            },
        },
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    instrument_provider.find_by_market_hash = Mock(return_value=[instrument])
    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(),
    )
    client._subscribed_instruments = {instrument.id}
    client._handle_data = Mock()

    await client._fetch_and_publish_quotes("market-1")

    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.bid_price.as_decimal() == EXPECTED_ONE_SIDED_ODDS
    assert quote.ask_price.as_decimal() == 0
    assert quote.bid_size.as_decimal() == 100
    assert quote.ask_size.as_decimal() == 0


@pytest.mark.asyncio
async def test_fetch_and_publish_quotes_ignores_opposite_outcome_orders():
    instrument = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="market-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDT"),
        info={"outcome_one": True},
    )

    http_client = Mock()
    http_client.get_order_book = AsyncMock(
        return_value={
            "data": {
                "orders": [
                    {
                        "isMakerBettingOutcomeOne": True,
                        "percentageOdds": decimal_odds_to_percentage(2.5),
                    },
                    {
                        "isMakerBettingOutcomeOne": False,
                        "percentageOdds": decimal_odds_to_percentage(4.0),
                    },
                ],
            },
        },
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    instrument_provider.find_by_market_hash = Mock(return_value=[instrument])
    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(),
    )
    client._subscribed_instruments = {instrument.id}
    client._handle_data = Mock()

    await client._fetch_and_publish_quotes("market-1")

    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.bid_price.as_decimal() == EXPECTED_EXECUTABLE_ODDS
    assert quote.ask_price.as_decimal() == 0


@pytest.mark.asyncio
async def test_connect_sends_loaded_instruments_to_data_engine():
    http_client = Mock()
    http_client.connect = AsyncMock()
    provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    provider.load_all_async = AsyncMock()
    provider.get_all = Mock(return_value={"inst-1": Mock(), "inst-2": Mock()})

    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(),
    )
    client._handle_data = Mock()

    await client._connect()

    provider.load_all_async.assert_awaited_once_with({})
    assert client._handle_data.call_count == 2


@pytest.mark.asyncio
async def test_fetch_and_publish_best_odds_uses_market_hash_batch():
    instrument = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="market-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="match_odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDT"),
        info={"outcome_one": True},
    )

    http_client = Mock()
    http_client.get_best_odds = AsyncMock(
        return_value={
            "data": {
                "bestOdds": [
                    {
                        "marketHash": "market-1",
                        "outcomeOne": {
                            "percentageOdds": str(decimal_odds_to_percentage(2.0)),
                        },
                        "outcomeTwo": {
                            "percentageOdds": str(decimal_odds_to_percentage(3.0)),
                        },
                    },
                ],
            },
        },
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    instrument_provider.find_by_market_hash = Mock(return_value=[instrument])
    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(),
    )
    client._subscribed_instruments = {instrument.id}
    client._handle_data = Mock()

    await client._fetch_and_publish_best_odds({"market-1"})

    http_client.get_best_odds.assert_awaited_once_with(
        market_hashes=["market-1"],
        base_token=SXBET_TOKENS["USDC"],
    )
    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.bid_price.as_decimal() == EXPECTED_ONE_SIDED_ODDS


@pytest.mark.asyncio
async def test_fetch_and_publish_best_odds_uses_outcome_two_and_skips_unsubscribed():
    subscribed = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="market-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="match_odds",
        market_type="match_odds",
        outcome="away",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDT"),
        info={"outcome_one": False},
    )
    unsubscribed = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="market-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="match_odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDT"),
        info={"outcome_one": True},
    )

    http_client = Mock()
    http_client.get_best_odds = AsyncMock(
        return_value={
            "data": {
                "bestOdds": [
                    {
                        "marketHash": "market-1",
                        "outcomeOne": {
                            "percentageOdds": str(decimal_odds_to_percentage(2.0)),
                        },
                        "outcomeTwo": {
                            "percentageOdds": str(decimal_odds_to_percentage(3.0)),
                        },
                    },
                ],
            },
        },
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    instrument_provider.find_by_market_hash = Mock(return_value=[subscribed, unsubscribed])
    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(),
    )
    client._subscribed_instruments = {subscribed.id}
    client._handle_data = Mock()

    await client._fetch_and_publish_best_odds({"market-1"})

    http_client.get_best_odds.assert_awaited_once_with(
        market_hashes=["market-1"],
        base_token=SXBET_TOKENS["USDC"],
    )
    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.instrument_id == subscribed.id
    assert quote.bid_price.as_decimal() == 3
