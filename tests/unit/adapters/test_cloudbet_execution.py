# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for Cloudbet execution safety guards.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.settlement import BET_SETTLEMENTS_TOPIC
from nautilus_trader.adapters.betting.common.settlement import BetSettlement
from nautilus_trader.adapters.betting.common.settlement import SettlementResult
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.client.schema import AcceptPriceChange
from nautilus_trader.adapters.cloudbet.client.schema import BetStatus
from nautilus_trader.adapters.cloudbet.client.schema import GetAccountBalance
from nautilus_trader.adapters.cloudbet.client.schema import GetAccountCurrencies
from nautilus_trader.adapters.cloudbet.client.schema import GetAccountInfoResponse
from nautilus_trader.adapters.cloudbet.client.schema import GetBetResponse
from nautilus_trader.adapters.cloudbet.client.schema import SelectionSide as CloudbetSelectionSide
from nautilus_trader.adapters.cloudbet.config import CloudbetExecClientConfig
from nautilus_trader.adapters.cloudbet.execution import CloudbetLiveExecutionClient
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.common.component import Logger
from nautilus_trader.common.functions import get_event_loop
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs


def _betting_instrument() -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
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


def _make_exec_client(venue_client, cache, msgbus, config):
    provider = CloudbetInstrumentProvider(
        client=venue_client,
        logger=Logger(name="test-cloudbet-provider"),
    )
    return CloudbetLiveExecutionClient(
        loop=get_event_loop(),
        client=venue_client,
        base_currency=None,
        msgbus=msgbus,
        cache=cache,
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-cloudbet-execution"),
        market_filter={},
        instrument_provider=provider,
        config=config,
    )


def _bet_response(
    status: BetStatus,
    *,
    stake: str,
    price: float = 2.1,
    reference_id: str = "venue-ref",
    win_loss: str | None = None,
    return_amount: str | None = None,
    currency: str = "PLAY_EUR",
):
    return GetBetResponse(
        legacy_status=status,
        legacy_price=price,
        legacy_side=CloudbetSelectionSide.BACK,
        stake=stake,
        currency=currency,
        reference_id=reference_id,
        create_time="2024-01-01T00:00:00Z",
        win_loss=win_loss,
        legacy_return_amount=return_amount,
    )


def _submit_command(order):
    return SimpleNamespace(
        instrument_id=order.instrument_id,
        strategy_id=order.strategy_id,
        order=order,
    )


class _Value:
    def __init__(self, value: float) -> None:
        self._value = value

    def as_double(self) -> float:
        return self._value


@pytest.mark.asyncio
async def test_generate_order_status_reports_without_filters_is_empty_startup_noop():
    cache = TestComponentStubs.cache()
    clock = TestComponentStubs.clock()
    venue_client = Mock()
    venue_client.get_bet_history = AsyncMock()
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
    command = GenerateOrderStatusReports(
        instrument_id=None,
        start=None,
        end=None,
        open_only=False,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )

    reports = await client.generate_order_status_reports(command)
    partial_reports = await client.generate_order_status_reports(
        GenerateOrderStatusReports(
            instrument_id=None,
            start=datetime.now(UTC),
            end=None,
            open_only=False,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        ),
    )

    assert reports == []
    assert partial_reports == []
    venue_client.get_bet_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_fill_and_position_reports_partial_range_are_empty_startup_noops():
    cache = TestComponentStubs.cache()
    clock = TestComponentStubs.clock()
    venue_client = Mock()
    venue_client.get_bet_history = AsyncMock()
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
    start = datetime.now(UTC)

    fill_reports = await client.generate_fill_reports(
        GenerateFillReports(
            instrument_id=None,
            venue_order_id=None,
            start=start,
            end=None,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        ),
    )
    position_reports = await client.generate_position_status_reports(
        GeneratePositionStatusReports(
            instrument_id=None,
            start=start,
            end=None,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        ),
    )

    assert fill_reports == []
    assert position_reports == []
    venue_client.get_bet_history.assert_not_awaited()


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


# -- M2 execution-completeness: fills, pending-acceptance reconciliation, cancel-terminal ----------


@pytest.mark.asyncio
async def test_accepted_bet_emits_single_fill_with_matched_qty_and_price():
    # (a) ACCEPTED response -> exactly one OrderFilled with correct qty/px.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.ACCEPTED, stake="1.25"),
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )

    await client._submit_order(_submit_command(order))

    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(1.25)
    assert fills[0].last_px == instrument.make_price(2.1)
    assert fills[0].venue_order_id == VenueOrderId("venue-ref")


@pytest.mark.asyncio
async def test_repeated_fill_emission_is_idempotent():
    # (b) repeated reconciliation / status polls -> idempotent, no double-fill.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.ACCEPTED, stake="1.25"),
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )

    await client._submit_order(_submit_command(order))
    # Simulate repeated status polls / reconciliation re-entering the fill path.
    client._emit_bet_fill(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("venue-ref"),
        bet_response=_bet_response(BetStatus.ACCEPTED, stake="1.25"),
    )
    client._emit_bet_fill(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("venue-ref"),
        bet_response=_bet_response(BetStatus.ACCEPTED, stake="1.25"),
    )

    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1


@pytest.mark.asyncio
async def test_pending_acceptance_reconciled_to_match_emits_fill_not_reject():
    # (c) PENDING_ACCEPTANCE then subsequently matched -> reconciled to a fill, NOT left as rejected.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
    )
    # Poll window keeps returning pending; only the authoritative reconciliation reads matched.
    venue_client.get_bet_status = AsyncMock(
        side_effect=[
            _bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
            _bet_response(BetStatus.ACCEPTED, stake="1.25"),
        ],
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(
            dry_run=False,
            accept_price_change="BETTER",
            pending_acceptance_poll_attempts=1,
            pending_acceptance_poll_interval_secs=0.0,
        ),
    )
    rejected: list[dict] = []
    client.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    await client._submit_order(_submit_command(order))

    # Poll (1) + reconciliation (1) = 2 status queries.
    assert venue_client.get_bet_status.await_count == 2
    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(1.25)
    assert rejected == []


@pytest.mark.asyncio
async def test_pending_acceptance_rejected_only_after_reconciliation_confirms():
    # (d) PENDING_ACCEPTANCE that is genuinely rejected -> rejected only AFTER reconciliation.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
    )
    venue_client.get_bet_status = AsyncMock(
        side_effect=[
            _bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
            _bet_response(BetStatus.REJECTED, stake="1.25"),
        ],
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(
            dry_run=False,
            accept_price_change="BETTER",
            pending_acceptance_poll_attempts=1,
            pending_acceptance_poll_interval_secs=0.0,
        ),
    )
    accepted: list[dict] = []
    rejected: list[dict] = []
    client.generate_order_accepted = lambda **kwargs: accepted.append(kwargs)
    client.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    await client._submit_order(_submit_command(order))

    assert venue_client.get_bet_status.await_count == 2
    assert [e for e in events if isinstance(e, OrderFilled)] == []
    assert accepted == []
    assert len(rejected) == 1
    assert rejected[0]["reason"] == BetStatus.REJECTED.value


@pytest.mark.asyncio
async def test_pending_acceptance_inconclusive_reconciliation_fails_toward_exposed():
    # PENDING that stays pending / cannot be confirmed -> accept (live), never reject: fail exposed.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
    )
    # Poll returns pending; reconciliation query errors (propagation delay) -> inconclusive.
    venue_client.get_bet_status = AsyncMock(
        side_effect=[
            _bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
            RuntimeError("reference not found yet"),
        ],
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(
            dry_run=False,
            accept_price_change="BETTER",
            pending_acceptance_poll_attempts=1,
            pending_acceptance_poll_interval_secs=0.0,
        ),
    )
    accepted: list[dict] = []
    rejected: list[dict] = []
    client.generate_order_accepted = lambda **kwargs: accepted.append(kwargs)
    client.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    await client._submit_order(_submit_command(order))

    # Accepted-as-live (exposure tracked), no fill (not matched), and crucially no reject.
    assert len(accepted) == 1
    assert rejected == []
    assert [e for e in events if isinstance(e, OrderFilled)] == []


@pytest.mark.asyncio
async def test_partial_match_fill_reflects_matched_stake_not_requested():
    # (e) partial matched stake -> filled_qty reflects the matched stake, not the full requested.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    # Accepted, but only 0.50 of the requested 1.25 stake matched.
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.ACCEPTED, stake="0.50"),
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )

    await client._submit_order(_submit_command(order))

    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(0.50)
    assert fills[0].last_qty != instrument.make_qty(1.25)


@pytest.mark.asyncio
async def test_cancel_matched_bet_resolves_to_fill_never_canceled():
    # (f) cancel of a matched CB bet -> resolves to terminal/filled and NEVER emits OrderCanceled.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.get_bet_status = AsyncMock(
        return_value=_bet_response(BetStatus.ACCEPTED, stake="1.25"),
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )

    command = SimpleNamespace(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("venue-ref"),
    )

    await client._cancel_order(command)

    fills = [e for e in events if isinstance(e, OrderFilled)]
    canceled = [e for e in events if isinstance(e, OrderCanceled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(1.25)
    assert canceled == []


# -- Symmetric submit-status handling: a matched/settled status on submit must fill, never reject ---


@pytest.mark.asyncio
async def test_submit_response_matched_status_emits_fill_not_reject():
    # A settled/matched status (in-play markets settle fast) surfaced on the submit response itself
    # must resolve to a fill, never OrderRejected while real money is live at Cloudbet.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    # In-play settlement: submit response already reports PARTIAL (matched 0.75 of 1.25).
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.PARTIAL, stake="0.75"),
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )
    rejected: list[dict] = []
    client.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    await client._submit_order(_submit_command(order))

    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(0.75)
    assert fills[0].venue_order_id == VenueOrderId("venue-ref")
    assert rejected == []


@pytest.mark.asyncio
async def test_pending_acceptance_reconciled_to_settled_status_emits_fill_not_reject():
    # The flagged naked-leg gap: place=PENDING, poll=PENDING, reconciliation re-query returns a
    # settled COMPLETED status -> exactly one OrderFilled (matched qty), and NEVER OrderRejected.
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
    )
    # Poll window keeps returning pending; the authoritative reconciliation reads a settled status.
    venue_client.get_bet_status = AsyncMock(
        side_effect=[
            _bet_response(BetStatus.PENDING_ACCEPTANCE, stake="1.25"),
            _bet_response(BetStatus.COMPLETED, stake="1.25"),
        ],
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(
            dry_run=False,
            accept_price_change="BETTER",
            pending_acceptance_poll_attempts=1,
            pending_acceptance_poll_interval_secs=0.0,
        ),
    )
    rejected: list[dict] = []
    client.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    await client._submit_order(_submit_command(order))

    # Poll (1) + reconciliation (1) = 2 status queries.
    assert venue_client.get_bet_status.await_count == 2
    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(1.25)
    assert rejected == []


@pytest.mark.asyncio
async def test_matched_submit_then_cancel_requery_does_not_double_fill():
    # Idempotency across the symmetric paths: a settled submit fills once; a later cancel resolution
    # re-query of the same settled status must not emit a second OrderFilled (nor an OrderCanceled).
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    venue_client = Mock()
    venue_client.connected = False
    venue_client.place_bets = AsyncMock(
        return_value=_bet_response(BetStatus.COMPLETED, stake="1.25"),
    )
    venue_client.get_bet_status = AsyncMock(
        return_value=_bet_response(BetStatus.COMPLETED, stake="1.25"),
    )
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )
    rejected: list[dict] = []
    client.generate_order_rejected = lambda **kwargs: rejected.append(kwargs)

    await client._submit_order(_submit_command(order))
    # The matched submit itself must fill (and not reject) — the fill originates here, not the cancel.
    assert len([e for e in events if isinstance(e, OrderFilled)]) == 1
    assert rejected == []

    # A subsequent cancel resolves the same matched reference; the fill guard must hold.
    await client._cancel_order(
        SimpleNamespace(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId("venue-ref"),
        ),
    )

    fills = [e for e in events if isinstance(e, OrderFilled)]
    canceled = [e for e in events if isinstance(e, OrderCanceled)]
    assert len(fills) == 1
    assert canceled == []


# -- Settlement publisher: graded Cloudbet bets -> BetSettlement on BET_SETTLEMENTS_TOPIC ----------
# Mirrors the SXBET settlement poll (#289): a graded-bet poll maps Cloudbet's terminal status
# vocabulary (WIN / LOSS / PUSH / HALF_WIN / HALF_LOSS) to the venue-neutral WON / LOST / VOID and
# publishes one BetSettlement per graded leg so the venue-agnostic strategy consumer books
# cross-venue arb P&L instead of leaving CB legs OPEN.


def _settlement_client(venue_client, cache, msgbus):
    client = _make_exec_client(
        venue_client,
        cache,
        msgbus,
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER"),
    )
    settlements: list[BetSettlement] = []
    msgbus.subscribe(topic=BET_SETTLEMENTS_TOPIC, handler=settlements.append)
    return client, settlements


def _tracked_order(cache):
    instrument = _betting_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(1.25),
    )
    cache.add_instrument(instrument)
    cache.add_order(order)
    return instrument, order


@pytest.mark.asyncio
async def test_settlement_poll_emits_won_once_and_refreshes_account():
    cache = TestComponentStubs.cache()
    instrument, order = _tracked_order(cache)
    venue_client = Mock()
    venue_client.connected = False
    # WIN: stake 1.25 at 2.1 -> net win of +1.375 (Cloudbet's signed winLoss).
    venue_client.get_bet_status = AsyncMock(
        return_value=_bet_response(BetStatus.WIN, stake="1.25", win_loss="1.375"),
    )
    client, settlements = _settlement_client(venue_client, cache, TestComponentStubs.msgbus())
    client.connection_account_state = AsyncMock()
    client.venue_order_id_to_client_order_id[VenueOrderId("venue-ref")] = order.client_order_id

    await client._reconcile_settlements()
    await client._reconcile_settlements()

    assert len(settlements) == 1
    settlement = settlements[0]
    assert settlement.result == SettlementResult.WON
    assert settlement.client_order_id == order.client_order_id.value
    assert settlement.instrument_id == str(instrument.id)
    assert settlement.settle_value == 1.375
    assert settlement.venue == CLOUDBET_VENUE.value
    # Grading pays out the wallet, so the account state refreshes once after emitting.
    assert client.connection_account_state.await_count == 1


@pytest.mark.asyncio
async def test_settlement_poll_maps_loss_to_negative_pnl():
    cache = TestComponentStubs.cache()
    _instrument, order = _tracked_order(cache)
    venue_client = Mock()
    venue_client.connected = False
    # LOSS: the full stake is forfeit -> winLoss -1.25.
    venue_client.get_bet_status = AsyncMock(
        return_value=_bet_response(BetStatus.LOSS, stake="1.25", win_loss="-1.25"),
    )
    client, settlements = _settlement_client(venue_client, cache, TestComponentStubs.msgbus())
    client.connection_account_state = AsyncMock()
    client.venue_order_id_to_client_order_id[VenueOrderId("venue-ref")] = order.client_order_id

    await client._reconcile_settlements()

    assert len(settlements) == 1
    assert settlements[0].result == SettlementResult.LOST
    assert settlements[0].settle_value == -1.25


@pytest.mark.asyncio
async def test_settlement_poll_maps_push_to_void_stake_refunded():
    cache = TestComponentStubs.cache()
    _instrument, order = _tracked_order(cache)
    venue_client = Mock()
    venue_client.connected = False
    # PUSH: market not applicable (e.g. draw on 2-way) -> stake refunded, zero P&L.
    venue_client.get_bet_status = AsyncMock(
        return_value=_bet_response(BetStatus.PUSH, stake="1.25", win_loss="0"),
    )
    client, settlements = _settlement_client(venue_client, cache, TestComponentStubs.msgbus())
    client.connection_account_state = AsyncMock()
    client.venue_order_id_to_client_order_id[VenueOrderId("venue-ref")] = order.client_order_id

    await client._reconcile_settlements()

    assert len(settlements) == 1
    assert settlements[0].result == SettlementResult.VOID
    assert settlements[0].settle_value == 0.0


@pytest.mark.asyncio
async def test_settlement_poll_maps_half_win_and_half_loss():
    # Quarter-ball Asian handicaps settle half the stake at odds and refund the other half; the
    # three-state venue-neutral model books the dominant side while the signed venue figure rides
    # on settle_value for diagnostics.
    for status, expected_result, win_loss, expected_value in (
        (BetStatus.HALF_WIN, SettlementResult.WON, "0.6875", 0.6875),
        (BetStatus.HALF_LOSS, SettlementResult.LOST, "-0.625", -0.625),
    ):
        cache = TestComponentStubs.cache()
        _instrument, order = _tracked_order(cache)
        venue_client = Mock()
        venue_client.connected = False
        venue_client.get_bet_status = AsyncMock(
            return_value=_bet_response(status, stake="1.25", win_loss=win_loss),
        )
        client, settlements = _settlement_client(venue_client, cache, TestComponentStubs.msgbus())
        client.connection_account_state = AsyncMock()
        client.venue_order_id_to_client_order_id[VenueOrderId("venue-ref")] = order.client_order_id

        await client._reconcile_settlements()

        assert len(settlements) == 1, status
        assert settlements[0].result == expected_result, status
        assert settlements[0].settle_value == expected_value, status


@pytest.mark.asyncio
async def test_settlement_poll_ignores_ungraded_and_is_idempotent_across_polls():
    cache = TestComponentStubs.cache()
    _instrument, order = _tracked_order(cache)
    venue_client = Mock()
    venue_client.connected = False
    # First two polls: still matched-but-ungraded (ACCEPTED) -> no settlement emitted. Then the bet
    # grades to WIN and every subsequent poll must not re-emit.
    venue_client.get_bet_status = AsyncMock(
        side_effect=[
            _bet_response(BetStatus.ACCEPTED, stake="1.25"),
            _bet_response(BetStatus.ACCEPTED, stake="1.25"),
            _bet_response(BetStatus.WIN, stake="1.25", win_loss="1.375"),
            _bet_response(BetStatus.WIN, stake="1.25", win_loss="1.375"),
        ],
    )
    client, settlements = _settlement_client(venue_client, cache, TestComponentStubs.msgbus())
    client.connection_account_state = AsyncMock()
    client.venue_order_id_to_client_order_id[VenueOrderId("venue-ref")] = order.client_order_id

    await client._reconcile_settlements()
    await client._reconcile_settlements()
    assert settlements == []

    await client._reconcile_settlements()
    await client._reconcile_settlements()

    # Graded exactly once; the settled reference is skipped on later polls (no re-query either).
    assert len(settlements) == 1
    assert settlements[0].result == SettlementResult.WON
    assert venue_client.get_bet_status.await_count == 3
    assert client.connection_account_state.await_count == 1


# -- Account-state loop + locked-funds modeling ----------------------------------------------------
# Periodic account-state refresh (mirrors the SXBET account-state poll) with locked = sum of stakes
# of open (matched but not yet settled) bets, computed from cached fill state — no per-tick venue
# re-queries of bet status. free = total - locked, floored at 0 (venue total is authoritative).


def _install_account_endpoints(venue_client, balances: dict[str, str]) -> None:
    venue_client.login = AsyncMock(
        return_value=GetAccountInfoResponse(
            uuid="acct-uuid-0001",
            email="test@example.com",
            nickname="tester",
        ),
    )
    venue_client.get_account_currencies = AsyncMock(
        return_value=GetAccountCurrencies(currencies=list(balances)),
    )
    venue_client.get_balances = AsyncMock(
        side_effect=lambda currency: GetAccountBalance(amount=balances[currency]),
    )


def _account_client(balances: dict[str, str], cache, **config_kwargs):
    venue_client = Mock()
    venue_client.connected = True
    _install_account_endpoints(venue_client, balances)
    client = _make_exec_client(
        venue_client,
        cache,
        TestComponentStubs.msgbus(),
        CloudbetExecClientConfig(dry_run=False, accept_price_change="BETTER", **config_kwargs),
    )
    states: list = []
    client._send_account_state = states.append
    return client, venue_client, states


def _fill_order(cache, client, *, client_order_id: str, stake: str, currency: str = "PLAY_EUR"):
    instrument = _betting_instrument()
    cache.add_instrument(instrument)
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.1),
        quantity=instrument.make_qty(float(stake)),
        client_order_id=ClientOrderId(client_order_id),
    )
    cache.add_order(order)
    venue_order_id = VenueOrderId(f"ref-{client_order_id}")
    client.venue_order_id_to_client_order_id[venue_order_id] = order.client_order_id
    client._emit_bet_fill(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=venue_order_id,
        bet_response=_bet_response(
            BetStatus.ACCEPTED,
            stake=stake,
            reference_id=venue_order_id.value,
            currency=currency,
        ),
    )
    return order


def _balances_by_code(state) -> dict[str, object]:
    return {balance.currency.code: balance for balance in state.balances}


@pytest.mark.asyncio
async def test_account_state_loop_ticks_at_interval_and_cancels_on_disconnect():
    # (a) the loop regenerates the account state at the configured interval and the task is
    # cancelled on disconnect (no leak).
    cache = TestComponentStubs.cache()
    client, venue_client, _states = _account_client(
        {"PLAY_EUR": "100"},
        cache,
        account_state_interval_secs=0.01,
    )
    client.connection_account_state = AsyncMock()
    venue_client.disconnect = AsyncMock()
    client.stream = SimpleNamespace(is_connected=True, disconnect=AsyncMock())

    await client._connect()
    task = client._account_state_task
    assert task is not None

    await asyncio.sleep(0.05)
    assert client.connection_account_state.await_count >= 2

    await client._disconnect()
    assert client._account_state_task is None
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_account_state_locked_sums_open_matched_stakes_and_releases_on_settlement():
    # (b) two open matched bets (5 + 7.5) -> locked=12.50, free=total-12.50; one settles ->
    # locked=5; all settled -> locked=0.
    play_eur = Currency.from_str("PLAY_EUR")
    cache = TestComponentStubs.cache()
    client, _venue_client, states = _account_client({"PLAY_EUR": "100"}, cache)
    order_a = _fill_order(cache, client, client_order_id="order-a", stake="5")
    order_b = _fill_order(cache, client, client_order_id="order-b", stake="7.5")

    await client.connection_account_state()
    balance = states[-1].balances[0]
    assert balance.total == Money(100, play_eur)
    assert balance.locked == Money(12.50, play_eur)
    assert balance.free == Money(87.50, play_eur)

    client._settled_client_order_ids.add(order_b.client_order_id)
    await client.connection_account_state()
    balance = states[-1].balances[0]
    assert balance.locked == Money(5, play_eur)
    assert balance.free == Money(95, play_eur)

    client._settled_client_order_ids.add(order_a.client_order_id)
    await client.connection_account_state()
    balance = states[-1].balances[0]
    assert balance.locked == Money(0, play_eur)
    assert balance.free == Money(100, play_eur)


@pytest.mark.asyncio
async def test_account_state_locked_exceeding_total_floors_free_at_zero():
    # (c) locked > venue-reported total -> the venue total is authoritative: free floors at 0 and
    # locked caps at total (never a negative free balance).
    play_eur = Currency.from_str("PLAY_EUR")
    cache = TestComponentStubs.cache()
    client, _venue_client, states = _account_client({"PLAY_EUR": "3"}, cache)
    _fill_order(cache, client, client_order_id="order-big", stake="5")

    await client.connection_account_state()

    balance = states[-1].balances[0]
    assert balance.total == Money(3, play_eur)
    assert balance.locked == Money(3, play_eur)
    assert balance.free == Money(0, play_eur)


@pytest.mark.asyncio
async def test_account_state_locked_buckets_mixed_currencies_separately():
    # (d) stakes lock in the currency of their own bet, never mixed across buckets.
    eur = Currency.from_str("EUR")
    btc = Currency.from_str("BTC")
    cache = TestComponentStubs.cache()
    client, _venue_client, states = _account_client({"EUR": "50", "BTC": "1.0"}, cache)
    _fill_order(cache, client, client_order_id="order-eur", stake="5", currency="EUR")
    _fill_order(cache, client, client_order_id="order-btc", stake="0.25", currency="BTC")

    await client.connection_account_state()

    balances = _balances_by_code(states[-1])
    assert balances["EUR"].locked == Money(5, eur)
    assert balances["EUR"].free == Money(45, eur)
    assert balances["BTC"].locked == Money(0.25, btc)
    assert balances["BTC"].free == Money(0.75, btc)


@pytest.mark.asyncio
async def test_account_state_loop_survives_venue_error_and_last_state_stands():
    # (e) a venue API failure mid-loop neither crashes the loop nor emits a partial state; the
    # last-known state stands until the next successful refresh.
    cache = TestComponentStubs.cache()
    client, venue_client, states = _account_client(
        {"PLAY_EUR": "100"},
        cache,
        account_state_interval_secs=0.01,
    )

    await client.connection_account_state()
    assert len(states) == 1

    venue_client.login = AsyncMock(side_effect=RuntimeError("venue down"))
    task = asyncio.create_task(client._account_state_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The loop kept ticking through failures and no partial/empty state replaced the last one.
    assert venue_client.login.await_count >= 2
    assert len(states) == 1


@pytest.mark.asyncio
async def test_settlement_and_account_refresh_interleave_without_double_count():
    # (f) the settlement poll's own refresh and the account loop's refresh agree: once a bet
    # settles its stake unlocks exactly once and stays unlocked on subsequent ticks.
    play_eur = Currency.from_str("PLAY_EUR")
    cache = TestComponentStubs.cache()
    client, venue_client, states = _account_client({"PLAY_EUR": "100"}, cache)
    _fill_order(cache, client, client_order_id="order-graded", stake="5")

    await client.connection_account_state()
    assert states[-1].balances[0].locked == Money(5, play_eur)

    venue_client.get_bet_status = AsyncMock(
        return_value=_bet_response(
            BetStatus.WIN,
            stake="5",
            reference_id="ref-order-graded",
            win_loss="5.5",
        ),
    )
    await client._reconcile_settlements()

    # The settlement path refreshed the account state itself: stake released.
    assert states[-1].balances[0].locked == Money(0, play_eur)
    assert states[-1].balances[0].free == Money(100, play_eur)

    # A subsequent account-loop tick reproduces the same state (no re-lock, no double release).
    await client.connection_account_state()
    assert states[-1].balances[0].locked == Money(0, play_eur)
    assert states[-1].balances[0].free == Money(100, play_eur)
