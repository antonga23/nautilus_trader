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
from nautilus_trader.adapters.betting.runtime_cache import decode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key
from nautilus_trader.adapters.sxbet.config import SXBetDataClientConfig
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.data import SXBetDataClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage
from nautilus_trader.common.component import Logger
from nautilus_trader.common.functions import get_event_loop
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.data.engine import DataEngineConfig
from nautilus_trader.data.messages import SubscribeQuoteTicks
from nautilus_trader.data.messages import UnsubscribeQuoteTicks
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


EXPECTED_EXECUTABLE_ODDS = 2.5
EXPECTED_ONE_SIDED_ODDS = 2.0


def _make_instrument(
    *,
    market_hash: str = "market-1",
    outcome: str = "home",
    outcome_one: bool = True,
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="fixture-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDT"),
        market_id=market_hash,
        info={"outcome_one": outcome_one},
    )


def _make_provider() -> SXBetInstrumentProvider:
    return SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )


def _make_client(
    *,
    instrument_provider: SXBetInstrumentProvider | None = None,
    config: SXBetDataClientConfig | None = None,
) -> SXBetDataClient:
    return SXBetDataClient(
        loop=get_event_loop(),
        http_client=Mock(),
        instrument_provider=instrument_provider or _make_provider(),
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=config or SXBetDataClientConfig(),
    )


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


def test_market_order_sides_requires_positive_orders_on_both_outcomes():
    orders = [
        {
            "isMakerBettingOutcomeOne": True,
            "percentageOdds": decimal_odds_to_percentage(2.0),
        },
        {
            "isMakerBettingOutcomeOne": False,
            "percentageOdds": decimal_odds_to_percentage(2.1),
        },
        {
            "isMakerBettingOutcomeOne": False,
            "percentageOdds": 0,
        },
    ]

    assert SXBetDataClient._market_order_sides(orders) == (True, True)
    assert SXBetDataClient._market_order_sides(orders[:1]) == (True, False)
    assert SXBetDataClient._market_order_sides([]) == (False, False)


def test_has_valid_spread_rejects_locked_or_crossed_quotes():
    assert SXBetDataClient._has_valid_spread(2.4, 2.5) is True
    assert SXBetDataClient._has_valid_spread(2.5, 2.5) is False
    assert SXBetDataClient._has_valid_spread(2.6, 2.5) is False


@pytest.mark.asyncio
async def test_fetch_and_publish_quotes_emits_one_sided_quote():
    instrument = _make_instrument()

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
    instrument_provider = _make_provider()
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

    published, orders = await client._fetch_and_publish_quotes("market-1")

    assert published == 1
    assert orders == 1
    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.bid_price.as_decimal() == EXPECTED_ONE_SIDED_ODDS
    assert quote.ask_price.as_decimal() == 0
    assert quote.bid_size.as_decimal() == 100
    assert quote.ask_size.as_decimal() == 0


@pytest.mark.asyncio
async def test_fetch_and_publish_quote_stats_reports_two_sided_liquidity():
    instrument_one = _make_instrument(outcome="home", outcome_one=True)
    instrument_two = _make_instrument(outcome="away", outcome_one=False)

    http_client = Mock()
    http_client.get_order_book = AsyncMock(
        return_value={
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
        },
    )
    instrument_provider = _make_provider()
    instruments_by_id = {
        instrument_one.id: instrument_one,
        instrument_two.id: instrument_two,
    }
    instrument_provider.find = Mock(side_effect=instruments_by_id.get)
    instrument_provider.find_by_market_hash = Mock(return_value=[instrument_one, instrument_two])
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
    client._subscribed_instruments = {instrument_one.id, instrument_two.id}
    client._handle_data = Mock()

    (
        published,
        orders,
        has_outcome_one,
        has_outcome_two,
        elapsed,
        failed,
        rate_limited,
        error,
    ) = await client._fetch_and_publish_quote_stats("market-1")

    assert published == 2
    assert orders == 2
    assert has_outcome_one is True
    assert has_outcome_two is True
    assert elapsed >= 0
    assert failed is False
    assert rate_limited is False
    assert error is None
    for call in client._handle_data.call_args_list:
        quote = call.args[0]
        assert quote.ts_event > 0
        assert quote.ts_init >= quote.ts_event


@pytest.mark.asyncio
async def test_poll_order_books_once_records_provider_poll_stats():
    instrument_one = _make_instrument(outcome="home", outcome_one=True)
    instrument_two = _make_instrument(outcome="away", outcome_one=False)

    http_client = Mock()
    http_client.get_order_book = AsyncMock(
        return_value={
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
        },
    )
    instrument_provider = _make_provider()
    instruments_by_id = {
        instrument_one.id: instrument_one,
        instrument_two.id: instrument_two,
    }
    instrument_provider.find = Mock(side_effect=instruments_by_id.get)
    instrument_provider.find_by_market_hash = Mock(return_value=[instrument_one, instrument_two])
    cache = TestComponentStubs.cache()
    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=cache,
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(order_book_concurrency=1),
    )
    client._subscribed_instruments = {instrument_one.id, instrument_two.id}
    client._handle_data = Mock()

    await client._poll_order_books_once()

    stats = decode_venue_quote_poll_stats(cache.get(venue_quote_poll_stats_key("SXBET")))
    assert stats is not None
    assert stats.venue == "SXBET"
    assert stats.cycle_id == 1
    assert stats.source == "rest_order_book_poll"
    assert stats.subscribed_instrument_count == 2
    assert stats.market_count == 1
    assert stats.quote_count == 2
    assert stats.order_count == 2
    assert stats.two_sided_market_count == 1
    assert stats.concurrency == 1
    assert stats.poll_interval_secs == 3.0
    assert stats.failure_count == 0
    assert stats.rate_limit_count == 0


@pytest.mark.asyncio
async def test_poll_order_books_once_can_use_batched_best_odds_for_live_latency():
    instrument_one = _make_instrument(outcome="home", outcome_one=True)
    instrument_two = _make_instrument(outcome="away", outcome_one=False)

    http_client = Mock()
    http_client.get_order_book = AsyncMock()
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
                            "percentageOdds": str(decimal_odds_to_percentage(2.1)),
                        },
                    },
                ],
            },
        },
    )
    instrument_provider = _make_provider()
    instruments_by_id = {
        instrument_one.id: instrument_one,
        instrument_two.id: instrument_two,
    }
    instrument_provider.find = Mock(side_effect=instruments_by_id.get)
    instrument_provider.find_by_market_hash = Mock(return_value=[instrument_one, instrument_two])
    cache = TestComponentStubs.cache()
    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=cache,
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(
            order_book_concurrency=4,
            order_book_poll_mode="best_odds_batch",
            order_book_best_odds_batch_size=30,
        ),
    )
    client._subscribed_instruments = {instrument_one.id, instrument_two.id}
    client._handle_data = Mock()

    await client._poll_order_books_once()

    http_client.get_order_book.assert_not_awaited()
    http_client.get_best_odds.assert_awaited_once_with(
        market_hashes=["market-1"],
        base_token=SXBET_TOKENS["USDC"],
        log_api_error=False,
    )
    stats = decode_venue_quote_poll_stats(cache.get(venue_quote_poll_stats_key("SXBET")))
    assert stats is not None
    assert stats.source == "rest_best_odds_batch"
    assert stats.market_count == 1
    assert stats.request_count == 1
    assert stats.backlog_count == 0
    assert stats.quote_count == 2
    assert stats.order_count == 0
    assert stats.two_sided_market_count == 1
    assert client._handle_data.call_count == 2


def test_rejects_unknown_order_book_poll_mode():
    with pytest.raises(ValueError, match="order_book_poll_mode"):
        _make_client(config=SXBetDataClientConfig(order_book_poll_mode="websocket"))


@pytest.mark.asyncio
async def test_poll_order_books_once_records_rate_limit_failures():
    instrument = _make_instrument()
    instrument_provider = _make_provider()
    instrument_provider.find = Mock(return_value=instrument)
    instrument_provider.find_by_market_hash = Mock(return_value=[instrument])
    client = _make_client(
        instrument_provider=instrument_provider,
        config=SXBetDataClientConfig(order_book_concurrency=1),
    )
    client._http_client.get_order_book = AsyncMock(
        side_effect=SXBetHttpClientError("rate limited", status_code=429),
    )
    client._subscribed_instruments = {instrument.id}

    await client._poll_order_books_once()

    stats = decode_venue_quote_poll_stats(
        client._cache.get(venue_quote_poll_stats_key("SXBET")),
    )
    assert stats is not None
    assert stats.failure_count == 1
    assert stats.rate_limit_count == 1
    assert stats.backoff_secs == 1.0
    assert stats.last_error == "rate limited"


@pytest.mark.asyncio
async def test_fetch_and_publish_quotes_ignores_opposite_outcome_orders():
    instrument = _make_instrument()

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
    instrument_provider = _make_provider()
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

    published, orders = await client._fetch_and_publish_quotes("market-1")

    assert published == 1
    assert orders == 2
    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.bid_price.as_decimal() == EXPECTED_EXECUTABLE_ODDS
    assert quote.ask_price.as_decimal() == 0


@pytest.mark.asyncio
async def test_connect_sends_loaded_instruments_to_data_engine():
    http_client = Mock()
    http_client.connect = AsyncMock()
    provider = _make_provider()
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


def test_auto_subscribe_loaded_instruments_respects_limit(monkeypatch):
    task = Mock()
    task.done.return_value = False
    create_task = Mock(return_value=task)
    monkeypatch.setattr("nautilus_trader.adapters.sxbet.data.asyncio.create_task", create_task)
    instruments = {
        "inst-1": _make_instrument(market_hash="market-1", outcome="home", outcome_one=True),
        "inst-2": _make_instrument(market_hash="market-1", outcome="away", outcome_one=False),
        "inst-3": _make_instrument(market_hash="market-2", outcome="home", outcome_one=True),
    }
    provider = _make_provider()
    provider.get_all = Mock(return_value=instruments)
    client = _make_client(
        instrument_provider=provider,
        config=SXBetDataClientConfig(
            auto_subscribe_quote_ticks=True,
            quote_subscription_limit=2,
        ),
    )

    selected_count = client._auto_subscribe_loaded_instruments()

    assert selected_count == 2
    subscribed = client.subscribed_quote_ticks()
    assert isinstance(subscribed, list)
    assert len(subscribed) == 2
    assert client._polling_task is task
    create_task.assert_called_once()


@pytest.mark.asyncio
async def test_subscribe_quote_ticks_accepts_nautilus_command(monkeypatch):
    task = Mock()
    task.done.return_value = False
    create_task = Mock(return_value=task)
    monkeypatch.setattr("nautilus_trader.adapters.sxbet.data.asyncio.create_task", create_task)
    instrument = _make_instrument()
    client = _make_client()
    command = SubscribeQuoteTicks(
        instrument_id=instrument.id,
        client_id=None,
        venue=Venue("SXBET"),
        command_id=UUID4(),
        ts_init=TestComponentStubs.clock().timestamp_ns(),
    )

    await client._subscribe_quote_ticks(command)

    assert client.subscribed_quote_ticks() == [instrument.id]
    assert client._polling_task is task


@pytest.mark.asyncio
async def test_unsubscribe_quote_ticks_accepts_nautilus_command():
    instrument = _make_instrument()
    client = _make_client()
    client._subscribed_instruments.add(instrument.id)
    command = UnsubscribeQuoteTicks(
        instrument_id=instrument.id,
        client_id=None,
        venue=Venue("SXBET"),
        command_id=UUID4(),
        ts_init=TestComponentStubs.clock().timestamp_ns(),
    )

    await client._unsubscribe_quote_ticks(command)

    assert client.subscribed_quote_ticks() == []


def test_data_engine_subscribe_quote_ticks_routes_to_sxbet_client(monkeypatch):
    task = Mock()
    task.done.return_value = False
    create_task = Mock(return_value=task)
    monkeypatch.setattr("nautilus_trader.adapters.sxbet.data.asyncio.create_task", create_task)
    clock = TestComponentStubs.clock()
    msgbus = TestComponentStubs.msgbus()
    cache = TestComponentStubs.cache()
    data_engine = DataEngine(
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        config=DataEngineConfig(),
    )
    instrument = _make_instrument()
    provider = _make_provider()
    provider.find = Mock(return_value=instrument)
    client = SXBetDataClient(
        loop=get_event_loop(),
        http_client=Mock(),
        instrument_provider=provider,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        logger=Logger(name="test-sxbet-data"),
        config=SXBetDataClientConfig(),
    )
    client.create_task = Mock()
    data_engine.register_client(client)
    data_engine.process(instrument)
    command = SubscribeQuoteTicks(
        instrument_id=instrument.id,
        client_id=None,
        venue=Venue("SXBET"),
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )

    data_engine.execute(command)

    assert data_engine.subscribed_quote_ticks() == [instrument.id]
    assert client.subscribed_quote_ticks() == [instrument.id]
    client.create_task.assert_called_once()
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_and_publish_best_odds_uses_market_hash_batch():
    instrument = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="fixture-1",
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
        market_id="market-1",
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
    instrument_provider = _make_provider()
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
        log_api_error=False,
    )
    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.bid_price.as_decimal() == EXPECTED_ONE_SIDED_ODDS


@pytest.mark.asyncio
async def test_fetch_and_publish_best_odds_uses_outcome_two_and_skips_unsubscribed():
    subscribed = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="fixture-1",
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
        market_id="market-1",
        info={"outcome_one": False},
    )
    unsubscribed = CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id="fixture-1",
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
        market_id="market-1",
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
    instrument_provider = _make_provider()
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
        log_api_error=False,
    )
    client._handle_data.assert_called_once()
    quote = client._handle_data.call_args.args[0]
    assert quote.instrument_id == subscribed.id
    assert quote.bid_price.as_decimal() == 3
