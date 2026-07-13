# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for Cloudbet execution safety guards.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.cloudbet.client.schema import AcceptPriceChange
from nautilus_trader.adapters.cloudbet.client.schema import BetStatus
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
    status: BetStatus, *, stake: str, price: float = 2.1, reference_id: str = "venue-ref"
):
    return GetBetResponse(
        legacy_status=status,
        legacy_price=price,
        legacy_side=CloudbetSelectionSide.BACK,
        stake=stake,
        currency="PLAY_EUR",
        reference_id=reference_id,
        create_time="2024-01-01T00:00:00Z",
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
