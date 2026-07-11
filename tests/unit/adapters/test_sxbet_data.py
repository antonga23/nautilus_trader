# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SX.bet market data quoting.
# -------------------------------------------------------------------------------------------------

import asyncio
import time
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


EXPECTED_ONE_SIDED_ODDS = 2.0


def _make_instrument(
    *,
    event_id: str = "fixture-1",
    market_hash: str = "market-1",
    outcome: str = "home",
    outcome_one: bool = True,
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id=event_id,
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


def _maker_pct(implied_prob: float) -> int:
    return round(implied_prob * 10**20)


def test_best_bid_ask_prices_taker_complement_and_sums_opposite_depth():
    # A taker backing outcome one matches makers resting on outcome two and
    # receives the complement of the maker's implied probability; the fillable
    # depth is the summed size of those opposite-side makers.
    orders = [
        {
            "isMakerBettingOutcomeOne": False,
            "percentageOdds": _maker_pct(0.40),
            "totalBetSize": 100_000000,
        },
        {
            "isMakerBettingOutcomeOne": False,
            "percentageOdds": _maker_pct(0.45),
            "totalBetSize": 50_000000,
        },
        # Same-side makers are ignored when pricing the outcome-one taker.
        {
            "isMakerBettingOutcomeOne": True,
            "percentageOdds": _maker_pct(0.50),
            "totalBetSize": 999_000000,
        },
    ]

    best_bid, best_ask, bid_size = SXBetDataClient._best_bid_ask(orders, is_outcome_one=True)

    # Best taker odds come from the highest-implied opposite maker: 1 / (1 - 0.45).
    assert best_bid == pytest.approx(1 / 0.55, rel=1e-9)
    assert best_ask == 0
    assert bid_size == pytest.approx(150.0, rel=1e-9)


def test_two_sided_book_prices_to_overround_not_phantom_arbitrage():
    # Regression: applying a maker's percentage odds directly (without the taker
    # complement) manufactures a phantom overlay on every two-sided book. Correct
    # taker pricing must produce an overround (implied-prob sum > 1), never a
    # standing arbitrage (< 1).
    orders = [
        {
            "isMakerBettingOutcomeOne": True,
            "percentageOdds": _maker_pct(0.48),
            "totalBetSize": 100_000000,
        },
        {
            "isMakerBettingOutcomeOne": False,
            "percentageOdds": _maker_pct(0.48),
            "totalBetSize": 100_000000,
        },
    ]

    bid_one, _, _ = SXBetDataClient._best_bid_ask(orders, is_outcome_one=True)
    bid_two, _, _ = SXBetDataClient._best_bid_ask(orders, is_outcome_one=False)

    assert bid_one == pytest.approx(1 / 0.52, rel=1e-9)
    assert bid_two == pytest.approx(1 / 0.52, rel=1e-9)
    assert (1 / bid_one + 1 / bid_two) > 1.0


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
    # A single outcome-one maker provides taker liquidity for the outcome-two side.
    instrument = _make_instrument(outcome="away", outcome_one=False)

    http_client = Mock()
    http_client.get_order_book = AsyncMock(
        return_value={
            "data": {
                "orders": [
                    {
                        "isMakerBettingOutcomeOne": True,
                        "percentageOdds": _maker_pct(0.50),
                        "totalBetSize": 120_000000,
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
    # Taker complement of a 0.50 maker is 1 / (1 - 0.50) = 2.0.
    assert quote.bid_price.as_decimal() == EXPECTED_ONE_SIDED_ODDS
    assert quote.ask_price.as_decimal() == 0
    assert quote.bid_size.as_decimal() == 120
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
    instrument_three = _make_instrument(
        event_id="fixture-2",
        market_hash="market-2",
        outcome="home",
        outcome_one=True,
    )
    instrument_four = _make_instrument(
        event_id="fixture-2",
        market_hash="market-2",
        outcome="away",
        outcome_one=False,
    )

    http_client = Mock()
    http_client.get_order_book = AsyncMock()

    async def get_best_odds(
        *,
        market_hashes: list[str],
        base_token: str,
        log_api_error: bool,
    ) -> dict[str, dict[str, list[dict[str, object]]]]:
        assert base_token == SXBET_TOKENS["USDC"]
        assert log_api_error is False
        market_hash = market_hashes[0]
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
                    },
                ],
            },
        }

    http_client.get_best_odds = AsyncMock(side_effect=get_best_odds)
    instrument_provider = _make_provider()
    instruments_by_id = {
        instrument_one.id: instrument_one,
        instrument_two.id: instrument_two,
        instrument_three.id: instrument_three,
        instrument_four.id: instrument_four,
    }
    instruments_by_market = {
        "market-1": [instrument_one, instrument_two],
        "market-2": [instrument_three, instrument_four],
    }
    instrument_provider.find = Mock(side_effect=instruments_by_id.get)
    instrument_provider.find_by_market_hash = Mock(side_effect=instruments_by_market.get)
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
            order_book_best_odds_batch_size=1,
            order_book_min_concurrency=2,
            order_book_max_concurrency=8,
            order_book_target_cycle_secs=3.0,
            order_book_adaptive_concurrency=True,
        ),
    )
    client._subscribed_instruments = {
        instrument_one.id,
        instrument_two.id,
        instrument_three.id,
        instrument_four.id,
    }
    client._handle_data = Mock()

    await client._poll_order_books_once()

    http_client.get_order_book.assert_not_awaited()
    assert http_client.get_best_odds.await_count == 2
    stats = decode_venue_quote_poll_stats(cache.get(venue_quote_poll_stats_key("SXBET")))
    assert stats is not None
    assert stats.source == "rest_best_odds_batch"
    assert stats.market_count == 2
    assert stats.request_count == 2
    assert stats.backlog_count == 0
    assert stats.min_concurrency == 2
    assert stats.max_concurrency == 8
    assert stats.poll_target_cycle_secs == 3.0
    assert stats.adaptive_concurrency is True
    assert stats.quote_event_timestamp_source == "poll_cycle_started"
    assert stats.quote_init_timestamp_source == "response_received"
    assert stats.quote_count == 4
    assert stats.order_count == 0
    assert stats.two_sided_market_count == 2
    assert client._handle_data.call_count == 4
    quotes = [call.args[0] for call in client._handle_data.call_args_list]
    assert len({quote.ts_event for quote in quotes}) == 1
    assert all(quote.ts_init >= quote.ts_event for quote in quotes)


def test_adaptive_poll_sleep_removes_dead_time_and_raises_slow_concurrency():
    client = _make_client(
        config=SXBetDataClientConfig(
            order_book_concurrency=4,
            order_book_min_concurrency=2,
            order_book_max_concurrency=8,
            order_book_target_cycle_secs=3.0,
            order_book_adaptive_concurrency=True,
        ),
    )

    next_sleep = client._poll_sleep_secs_after_cycle(16.0)

    assert next_sleep == 0.0
    assert client._order_book_concurrency == 8


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


def _make_hung_market_client(
    *,
    config: SXBetDataClientConfig,
) -> tuple[SXBetDataClient, CryptoBettingInstrument]:
    hung_instrument = _make_instrument(event_id="fixture-hung", market_hash="market-hung")
    healthy_instrument = _make_instrument(event_id="fixture-healthy", market_hash="market-healthy")
    hang_forever = asyncio.Event()

    async def fake_get_order_book(market_hash: str) -> dict:
        if market_hash == "market-hung":
            await hang_forever.wait()
        # The healthy instruments are outcome-one, so their taker liquidity is an
        # opposite-side (outcome-two) maker: implied 0.5 -> taker complement 2.0.
        return {
            "data": {
                "orders": [
                    {
                        "isMakerBettingOutcomeOne": False,
                        "percentageOdds": decimal_odds_to_percentage(2.0),
                    },
                ],
            },
        }

    instrument_provider = _make_provider()
    instruments_by_id = {
        hung_instrument.id: hung_instrument,
        healthy_instrument.id: healthy_instrument,
    }
    instruments_by_hash = {
        "market-hung": [hung_instrument],
        "market-healthy": [healthy_instrument],
    }
    instrument_provider.find = Mock(side_effect=instruments_by_id.get)  # type: ignore[method-assign]
    instrument_provider.find_by_market_hash = Mock(  # type: ignore[method-assign]
        side_effect=lambda market_hash: instruments_by_hash.get(market_hash, []),
    )
    client = _make_client(instrument_provider=instrument_provider, config=config)
    client._http_client.get_order_book = AsyncMock(side_effect=fake_get_order_book)  # type: ignore[method-assign]
    client._subscribed_instruments = {hung_instrument.id, healthy_instrument.id}
    client._handle_data = Mock()
    return client, healthy_instrument


@pytest.mark.asyncio
async def test_poll_order_books_once_times_out_hung_fetch_and_publishes_healthy_markets():
    client, healthy_instrument = _make_hung_market_client(
        config=SXBetDataClientConfig(
            order_book_poll_interval_secs=0.2,
            fetch_timeout_secs=0.1,
            cycle_deadline_secs=0.4,
        ),
    )

    started_at = time.perf_counter()
    await client._poll_order_books_once()
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.4
    client._handle_data.assert_called_once()
    assert client._handle_data.call_args.args[0].instrument_id == healthy_instrument.id
    stats = decode_venue_quote_poll_stats(
        client._cache.get(venue_quote_poll_stats_key("SXBET")),
    )
    assert stats is not None
    assert stats.failure_count >= 1
    assert stats.quote_count == 1
    assert stats.last_error == "request_timeout>0.1s"


@pytest.mark.asyncio
async def test_poll_order_books_once_enforces_cycle_deadline_on_hung_fetch():
    client, healthy_instrument = _make_hung_market_client(
        config=SXBetDataClientConfig(
            order_book_poll_interval_secs=0.1,
            fetch_timeout_secs=60.0,
            cycle_deadline_secs=0.2,
        ),
    )

    started_at = time.perf_counter()
    await client._poll_order_books_once()
    elapsed = time.perf_counter() - started_at

    assert elapsed < 1.0
    client._handle_data.assert_called_once()
    assert client._handle_data.call_args.args[0].instrument_id == healthy_instrument.id
    stats = decode_venue_quote_poll_stats(
        client._cache.get(venue_quote_poll_stats_key("SXBET")),
    )
    assert stats is not None
    assert stats.failure_count >= 1
    assert stats.quote_count == 1
    assert stats.last_error == "cycle_deadline_exceeded"


def test_fetch_timeout_and_cycle_deadline_default_from_poll_interval():
    client = _make_client(config=SXBetDataClientConfig(order_book_poll_interval_secs=3.0))

    assert client._fetch_timeout_secs == 3.0
    assert client._cycle_deadline_secs == 6.0


@pytest.mark.asyncio
async def test_fetch_and_publish_quotes_ignores_same_outcome_orders():
    # An outcome-one taker prices off opposite-outcome (isMakerBettingOutcomeOne=False)
    # makers and ignores same-outcome makers entirely.
    instrument = _make_instrument()  # outcome_one=True

    http_client = Mock()
    http_client.get_order_book = AsyncMock(
        return_value={
            "data": {
                "orders": [
                    # Same-side maker: ignored when pricing the outcome-one taker.
                    {
                        "isMakerBettingOutcomeOne": True,
                        "percentageOdds": decimal_odds_to_percentage(2.5),
                    },
                    # Opposite-side maker at implied 1/4.0 = 0.25.
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
    # Taker complement of the 0.25 opposite maker: 1 / (1 - 0.25) = 1.333..., at precision 2.
    assert float(quote.bid_price) == round(1 / (1 - 1 / 4.0), 2)
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
async def test_fetch_and_publish_best_odds_skips_non_executable_decimal_odds():
    instrument = _make_instrument(market_hash="market-1", outcome_one=True)
    http_client = Mock()
    http_client.get_best_odds = AsyncMock(
        return_value={
            "data": {
                "bestOdds": [
                    {
                        "marketHash": "market-1",
                        "outcomeOne": {
                            "percentageOdds": str(decimal_odds_to_percentage(1.0)),
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

    client._handle_data.assert_not_called()


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
