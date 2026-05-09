# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for Cloudbet execution safety guards.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.cloudbet.client.schema import AcceptPriceChange
from nautilus_trader.adapters.cloudbet.client.schema import BetStatus
from nautilus_trader.adapters.cloudbet.config import CloudbetExecClientConfig
from nautilus_trader.adapters.cloudbet.execution import CloudbetLiveExecutionClient
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.common.component import Logger
from nautilus_trader.common.functions import get_event_loop
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


class _Value:
    def __init__(self, value: float) -> None:
        self._value = value

    def as_double(self) -> float:
        return self._value


@pytest.mark.asyncio
async def test_submit_order_dry_run_builds_request_but_does_not_place_bet():
    instrument = CryptoBettingInstrument(
        venue=Venue("CLOUDBET"),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="League",
        market_name="soccer.match.odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.1,
        currency=Currency.from_str("PLAY_EUR"),
        params="",
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    instrument_id = instrument.id
    clock = TestComponentStubs.clock()
    venue_client = Mock()
    venue_client.place_bets = AsyncMock()
    venue_client.connected = False
    provider = CloudbetInstrumentProvider(
        client=venue_client,
        logger=Logger(name="test-cloudbet-provider"),
    )
    client = CloudbetLiveExecutionClient(
        loop=get_event_loop(),
        client=venue_client,
        base_currency=None,
        msgbus=TestComponentStubs.msgbus(),
        cache=cache,
        clock=clock,
        logger=Logger(name="test-cloudbet-execution"),
        market_filter={},
        instrument_provider=provider,
        config=CloudbetExecClientConfig(dry_run=True),
    )

    submitted: dict[str, object] = {}
    rejected: dict[str, object] = {}
    client.generate_order_submitted = lambda **kwargs: submitted.update(kwargs)
    client.generate_order_rejected = lambda **kwargs: rejected.update(kwargs)

    command = SimpleNamespace(
        instrument_id=instrument_id,
        strategy_id="strategy-1",
        order=SimpleNamespace(
            has_price=True,
            price=_Value(2.1),
            quantity=_Value(1.25),
            is_buy=True,
            client_order_id=ClientOrderId("order-dry-run"),
        ),
    )

    await client._submit_order(command)

    venue_client.place_bets.assert_not_awaited()
    assert submitted["instrument_id"] == instrument_id
    assert isinstance(submitted["ts_event"], int)
    assert rejected["reason"] == "dry_run_no_submit"
    assert isinstance(rejected["ts_event"], int)


@pytest.mark.asyncio
async def test_submit_order_live_request_uses_better_price_change_policy():
    instrument = CryptoBettingInstrument(
        venue=Venue("CLOUDBET"),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="League",
        market_name="soccer.match.odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.1,
        currency=Currency.from_str("PLAY_EUR"),
        params="",
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    venue_client = Mock()
    venue_client.place_bets = AsyncMock(
        return_value=SimpleNamespace(status=BetStatus.ACCEPTED, reference_id="venue-ref"),
    )
    venue_client.connected = False
    provider = CloudbetInstrumentProvider(
        client=venue_client,
        logger=Logger(name="test-cloudbet-provider"),
    )
    client = CloudbetLiveExecutionClient(
        loop=get_event_loop(),
        client=venue_client,
        base_currency=None,
        msgbus=TestComponentStubs.msgbus(),
        cache=cache,
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-cloudbet-execution"),
        market_filter={},
        instrument_provider=provider,
        config=CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )
    client.generate_order_submitted = Mock()
    client.generate_order_accepted = Mock()
    client.generate_order_rejected = Mock()
    command = SimpleNamespace(
        instrument_id=instrument.id,
        strategy_id="strategy-1",
        order=SimpleNamespace(
            has_price=True,
            price=_Value(2.1),
            quantity=_Value(1.25),
            is_buy=True,
            client_order_id=ClientOrderId("order-live"),
        ),
    )

    await client._submit_order(command)

    venue_client.place_bets.assert_awaited_once()
    call_kwargs = venue_client.place_bets.await_args.kwargs
    assert call_kwargs["accept_price_change"] == AcceptPriceChange.BETTER
    assert call_kwargs["reference_id"]
    client.generate_order_accepted.assert_called_once()


@pytest.mark.asyncio
async def test_submit_order_polls_pending_acceptance_before_accepting():
    instrument = CryptoBettingInstrument(
        venue=Venue("CLOUDBET"),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="League",
        market_name="soccer.match.odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.1,
        currency=Currency.from_str("PLAY_EUR"),
        params="",
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    venue_client = Mock()
    venue_client.place_bets = AsyncMock(
        return_value=SimpleNamespace(
            status=BetStatus.PENDING_ACCEPTANCE,
            reference_id="venue-ref",
        ),
    )
    venue_client.get_bet_status = AsyncMock(
        return_value=SimpleNamespace(status=BetStatus.ACCEPTED, reference_id="venue-ref"),
    )
    venue_client.connected = False
    provider = CloudbetInstrumentProvider(
        client=venue_client,
        logger=Logger(name="test-cloudbet-provider"),
    )
    client = CloudbetLiveExecutionClient(
        loop=get_event_loop(),
        client=venue_client,
        base_currency=None,
        msgbus=TestComponentStubs.msgbus(),
        cache=cache,
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-cloudbet-execution"),
        market_filter={},
        instrument_provider=provider,
        config=CloudbetExecClientConfig(
            dry_run=False,
            accept_price_change="BETTER",
            pending_acceptance_poll_attempts=1,
            pending_acceptance_poll_interval_secs=0.0,
        ),
    )
    client.generate_order_submitted = Mock()
    client.generate_order_accepted = Mock()
    client.generate_order_rejected = Mock()
    command = SimpleNamespace(
        instrument_id=instrument.id,
        strategy_id="strategy-1",
        order=SimpleNamespace(
            has_price=True,
            price=_Value(2.1),
            quantity=_Value(1.25),
            is_buy=True,
            client_order_id=ClientOrderId("order-pending"),
        ),
    )

    await client._submit_order(command)

    call_kwargs = venue_client.place_bets.await_args.kwargs
    venue_client.get_bet_status.assert_awaited_once_with(call_kwargs["reference_id"])
    client.generate_order_accepted.assert_called_once()
