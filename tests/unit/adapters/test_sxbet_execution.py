# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for SX.bet execution edge cases.
# -------------------------------------------------------------------------------------------------
# pylint: disable=duplicate-code

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.settlement import BET_SETTLEMENTS_TOPIC
from nautilus_trader.adapters.betting.common.settlement import BetSettlement
from nautilus_trader.adapters.betting.common.settlement import SettlementResult
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.execution import SXBET_USDC
from nautilus_trader.adapters.sxbet.execution import SXBetExecutionClient
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.common.component import Logger
from nautilus_trader.common.functions import get_event_loop
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs


def _fixed_expiry(hours: int = 24) -> int:
    return 1_700_000_000 + (hours - hours)


@pytest.mark.asyncio
async def test_submit_order_rejects_missing_order_hash(monkeypatch):
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.place_order = AsyncMock(return_value={"data": {}})
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    rejected: dict[str, str] = {}
    client._generate_order_submitted = Mock()
    client._generate_order_accepted = Mock()
    client._generate_order_rejected = lambda **kwargs: rejected.update({"reason": kwargs["reason"]})

    instrument = CryptoBettingInstrument(
        venue=SXBET_VENUE,
        event_id="0x" + "ab" * 32,
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        info={"outcome_one": True},
    )
    instrument_provider.find = Mock(return_value=instrument)

    order = SimpleNamespace(
        instrument_id=instrument.id,
        order_type=OrderType.LIMIT,
        price=Decimal("2.10"),
        quantity=Decimal("0.29"),
        client_order_id=ClientOrderId("order-1"),
    )
    command = SimpleNamespace(order=order)

    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.sign_eip712_order",
        lambda **_kwargs: "0xsig",
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.generate_salt",
        lambda: 12345,
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.get_expiry",
        _fixed_expiry,
    )

    await client._submit_order(command)

    client._generate_order_submitted.assert_called_once_with(order)
    client._generate_order_accepted.assert_not_called()
    assert rejected["reason"] == "SX.bet response missing a valid orderHash"
    assert client._orders == {}
    assert client._venue_order_ids == {}


@pytest.mark.asyncio
async def test_submit_order_rejects_signing_import_error(monkeypatch):
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.place_order = AsyncMock()
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    rejected: dict[str, str] = {}
    client._generate_order_submitted = Mock()
    client._generate_order_accepted = Mock()
    client._generate_order_rejected = lambda **kwargs: rejected.update({"reason": kwargs["reason"]})

    instrument = CryptoBettingInstrument(
        venue=SXBET_VENUE,
        event_id="0x" + "ab" * 32,
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        info={"outcome_one": True},
    )
    instrument_provider.find = Mock(return_value=instrument)

    order = SimpleNamespace(
        instrument_id=instrument.id,
        order_type=OrderType.LIMIT,
        price=Decimal("2.10"),
        quantity=Decimal("0.29"),
        client_order_id=ClientOrderId("order-import-error"),
    )
    command = SimpleNamespace(order=order)

    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.sign_eip712_order",
        lambda **_kwargs: (_ for _ in ()).throw(ImportError("eth_account unavailable")),
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.generate_salt",
        lambda: 12345,
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.get_expiry",
        _fixed_expiry,
    )

    await client._submit_order(command)

    client._generate_order_submitted.assert_not_called()
    client._generate_order_accepted.assert_not_called()
    http_client.place_order.assert_not_awaited()
    assert rejected["reason"] == "eth_account unavailable"
    assert client._orders == {}
    assert client._venue_order_ids == {}


@pytest.mark.asyncio
async def test_submit_order_dry_run_signs_but_does_not_place_order(monkeypatch):
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
        dry_run=True,
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.place_order = AsyncMock()
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    rejected: dict[str, str] = {}
    client._generate_order_submitted = Mock()
    client._generate_order_accepted = Mock()
    client._generate_order_rejected = lambda **kwargs: rejected.update({"reason": kwargs["reason"]})

    instrument = CryptoBettingInstrument(
        venue=SXBET_VENUE,
        event_id="fixture-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        market_id="0x" + "ab" * 32,
        info={"outcome_one": True},
    )
    instrument_provider.find = Mock(return_value=instrument)

    order = SimpleNamespace(
        instrument_id=instrument.id,
        order_type=OrderType.LIMIT,
        price=Decimal("2.10"),
        quantity=Decimal("0.29"),
        client_order_id=ClientOrderId("order-dry-run"),
    )
    command = SimpleNamespace(order=order)

    signed_payloads: list[dict] = []
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.sign_eip712_order",
        lambda **kwargs: signed_payloads.append(kwargs["order"]) or "0xsig",
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.generate_salt",
        lambda: 12345,
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.get_expiry",
        _fixed_expiry,
    )

    await client._submit_order(command)

    assert signed_payloads
    assert signed_payloads[0]["marketHash"] == instrument.market_id
    client._generate_order_submitted.assert_called_once_with(order)
    client._generate_order_accepted.assert_not_called()
    http_client.place_order.assert_not_awaited()
    assert rejected["reason"] == "dry_run_no_submit"


@pytest.mark.asyncio
async def test_submit_order_taker_fill_signs_and_calls_fill_endpoint(monkeypatch):
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
        dry_run=False,
        execution_mode="taker_fill",
        odds_slippage=7,
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.fill_order = AsyncMock(
        return_value={"data": {"fillHash": "0xfillhash"}},
    )
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )
    client._generate_order_submitted = Mock()
    client._generate_order_accepted = Mock()
    client._generate_order_rejected = Mock()

    instrument = CryptoBettingInstrument(
        venue=SXBET_VENUE,
        event_id="fixture-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        market_id="0x" + "ab" * 32,
        info={"outcome_one": True},
    )
    instrument_provider.find = Mock(return_value=instrument)

    order = SimpleNamespace(
        instrument_id=instrument.id,
        order_type=OrderType.LIMIT,
        price=Decimal("2.10"),
        quantity=Decimal("6.25"),
        client_order_id=ClientOrderId("order-taker-fill"),
    )
    command = SimpleNamespace(order=order)

    signed_payloads: list[dict] = []
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.sign_eip712_fill_order",
        lambda **kwargs: signed_payloads.append(kwargs["fill"]) or "0xfillsig",
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.generate_salt",
        lambda: 999,
    )

    await client._submit_order(command)

    assert signed_payloads
    assert signed_payloads[0]["market"] == instrument.market_id
    assert signed_payloads[0]["taker"] == config.wallet_address
    assert signed_payloads[0]["oddsSlippage"] == 7
    assert signed_payloads[0]["message"] == "Nautilus live arbitrage taker fill"
    http_client.fill_order.assert_awaited_once()
    call_kwargs = http_client.fill_order.await_args.kwargs
    assert call_kwargs["market"] == instrument.market_id
    assert call_kwargs["taker_sig"] == "0xfillsig"
    assert call_kwargs["message"] == "Nautilus live arbitrage taker fill"
    client._generate_order_submitted.assert_called_once_with(order)
    client._generate_order_accepted.assert_called_once()
    client._generate_order_rejected.assert_not_called()
    assert client._venue_order_ids[order.client_order_id] == VenueOrderId("0xfillhash")


@pytest.mark.asyncio
async def test_submit_order_taker_fill_dry_run_does_not_call_fill_endpoint(monkeypatch):
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
        dry_run=True,
        execution_mode="taker_fill",
        odds_slippage=5,
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.fill_order = AsyncMock()
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )
    rejected: dict[str, str] = {}
    client._generate_order_submitted = Mock()
    client._generate_order_accepted = Mock()
    client._generate_order_rejected = lambda **kwargs: rejected.update({"reason": kwargs["reason"]})
    instrument = CryptoBettingInstrument(
        venue=SXBET_VENUE,
        event_id="fixture-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        market_id="0x" + "ab" * 32,
        info={"outcome_one": True},
    )
    instrument_provider.find = Mock(return_value=instrument)
    order = SimpleNamespace(
        instrument_id=instrument.id,
        order_type=OrderType.LIMIT,
        price=Decimal("2.10"),
        quantity=Decimal("6.25"),
        client_order_id=ClientOrderId("order-taker-fill-dry-run"),
    )
    command = SimpleNamespace(order=order)
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.sign_eip712_fill_order",
        lambda **_kwargs: "0xfillsig",
    )
    monkeypatch.setattr(
        "nautilus_trader.adapters.sxbet.execution.generate_salt",
        lambda: 999,
    )

    await client._submit_order(command)

    client._generate_order_submitted.assert_called_once_with(order)
    client._generate_order_accepted.assert_not_called()
    http_client.fill_order.assert_not_awaited()
    assert rejected["reason"] == "dry_run_no_submit"


@pytest.mark.asyncio
async def test_generate_order_status_report_uses_command_and_cached_venue_id():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.get_user_orders = AsyncMock(
        return_value={
            "data": {
                "orders": [
                    {
                        "orderHash": "0xorderhash",
                        "status": "ACTIVE",
                    },
                ],
            },
        },
    )
    instrument = CryptoBettingInstrument(
        venue=SXBET_VENUE,
        event_id="0x" + "ab" * 32,
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        info={"outcome_one": True},
    )
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )
    client_order_id = ClientOrderId("order-2")
    client._venue_order_ids[client_order_id] = VenueOrderId("0xorderhash")
    command = SimpleNamespace(
        instrument_id=instrument.id,
        client_order_id=client_order_id,
        venue_order_id=None,
    )

    report = await client.generate_order_status_report(command)

    http_client.get_user_orders.assert_awaited_once_with(config.wallet_address)
    assert report is not None
    assert report.client_order_id == client_order_id
    assert str(report.venue_order_id) == "0xorderhash"
    assert report.instrument_id == instrument.id
    assert report.order_side == OrderSide.BUY


@pytest.mark.asyncio
async def test_generate_order_status_reports_without_filters_is_empty_startup_noop():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.get_user_orders = AsyncMock()
    clock = TestComponentStubs.clock()
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=clock,
        logger=Logger(name="test-sxbet-execution"),
        config=config,
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

    assert reports == []
    http_client.get_user_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_fill_and_position_reports_are_empty_startup_noops():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    clock = TestComponentStubs.clock()
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=clock,
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    fill_reports = await client.generate_fill_reports(
        GenerateFillReports(
            instrument_id=None,
            venue_order_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        ),
    )
    position_reports = await client.generate_position_status_reports(
        GeneratePositionStatusReports(
            instrument_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        ),
    )

    assert fill_reports == []
    assert position_reports == []


@pytest.mark.asyncio
async def test_generate_mass_status_registers_account_for_reconciliation_conversion():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.get_user_orders = AsyncMock()
    clock = TestComponentStubs.clock()
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=clock,
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    mass_status = await client.generate_mass_status(lookback_mins=60)

    assert mass_status is not None
    assert str(mass_status.account_id).startswith("SXBET-")
    assert mass_status.to_dict()["account_id"] == mass_status.account_id.value
    mass_status.to_pyo3()
    http_client.get_user_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_orders_cancels_tracked_sxbet_maker_orders():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.cancel_order = AsyncMock()
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )
    client_order_id = ClientOrderId("order-cancel-all")
    client._orders[client_order_id] = {"order_hash": "0xorderhash"}
    client._venue_order_ids[client_order_id] = VenueOrderId("0xorderhash")

    await client._cancel_all_orders(SimpleNamespace())

    http_client.cancel_order.assert_awaited_once_with("0xorderhash")


@pytest.mark.asyncio
async def test_connect_uses_usdc_token_address():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.connect = AsyncMock()
    http_client.disconnect = AsyncMock()
    http_client.get_balance = AsyncMock(return_value={"balance": "1"})
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    await client._connect()

    http_client.connect.assert_awaited_once()
    http_client.get_balance.assert_awaited_once_with(
        config.wallet_address,
        SXBET_TOKENS["USDC"],
    )

    await client._disconnect()


def _fill_instrument() -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=SXBET_VENUE,
        event_id="0x" + "ab" * 32,
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        market_id="0x" + "ab" * 32,
        info={"outcome_one": True},
    )


def _make_fill_client(http_client, instrument, msgbus, cache, *, execution_mode="maker_post"):
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
        execution_mode=execution_mode,
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    instrument_provider.find = Mock(return_value=instrument)
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=msgbus,
        cache=cache,
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )
    return client


# Odds 2.0 as SX.bet percentage odds (implied probability * 10^20).
_ODDS_2X = "50000000000000000000"


@pytest.mark.asyncio
async def test_reconcile_emits_fill_and_status_report_reports_filled_qty():
    instrument = _fill_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    http_client = Mock()
    http_client.get_user_orders = AsyncMock(
        return_value={
            "data": {
                "orders": [
                    {
                        "orderHash": "0xorderhash",
                        "orderStatus": "ACTIVE",
                        "totalBetSize": "10000000",  # 10 USDC (6 decimals)
                        "fillAmount": "4000000",  # 4 USDC matched
                        "percentageOdds": _ODDS_2X,
                    },
                ],
            },
        },
    )
    client = _make_fill_client(http_client, instrument, msgbus, cache)
    client._venue_order_ids[order.client_order_id] = VenueOrderId("0xorderhash")

    await client._reconcile_order_fills()

    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(4)
    assert fills[0].last_px == instrument.make_price(2.0)
    assert fills[0].liquidity_side == LiquiditySide.MAKER
    assert fills[0].currency == SXBET_USDC

    report = await client.generate_order_status_report(
        GenerateOrderStatusReport(
            instrument_id=instrument.id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId("0xorderhash"),
            command_id=UUID4(),
            ts_init=client._clock.timestamp_ns(),
        ),
    )
    assert report is not None
    assert report.filled_qty == instrument.make_qty(4)
    assert report.quantity == instrument.make_qty(10)


@pytest.mark.asyncio
async def test_reconcile_order_fills_is_idempotent_and_emits_deltas():
    instrument = _fill_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)

    def order_payload(fill_amount: str) -> dict:
        return {
            "data": {
                "orders": [
                    {
                        "orderHash": "0xorderhash",
                        "orderStatus": "ACTIVE",
                        "totalBetSize": "10000000",
                        "fillAmount": fill_amount,
                        "percentageOdds": _ODDS_2X,
                    },
                ],
            },
        }

    http_client = Mock()
    http_client.get_user_orders = AsyncMock(return_value=order_payload("4000000"))
    client = _make_fill_client(http_client, instrument, msgbus, cache)
    client._venue_order_ids[order.client_order_id] = VenueOrderId("0xorderhash")

    # Same matched size polled twice -> exactly one fill.
    await client._reconcile_order_fills()
    await client._reconcile_order_fills()
    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 1
    assert fills[0].last_qty == instrument.make_qty(4)

    # A larger cumulative matched size -> one further fill for the delta only.
    http_client.get_user_orders.return_value = order_payload("7000000")
    await client._reconcile_order_fills()
    fills = [e for e in events if isinstance(e, OrderFilled)]
    assert len(fills) == 2
    assert fills[1].last_qty == instrument.make_qty(3)
    assert client._last_matched_wei[order.client_order_id] == 7_000_000


@pytest.mark.asyncio
async def test_connect_emits_single_account_state_with_wallet_balance():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    http_client = Mock()
    http_client.connect = AsyncMock()
    http_client.disconnect = AsyncMock()
    http_client.get_balance = AsyncMock(return_value={"balance": "1234560"})  # 1.23456 USDC
    http_client.get_user_orders = AsyncMock(return_value={"data": {"orders": []}})
    msgbus = TestComponentStubs.msgbus()
    account_states: list[object] = []
    msgbus.register("Portfolio.update_account", account_states.append)
    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=instrument_provider,
        msgbus=msgbus,
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    await client._connect()

    states = [e for e in account_states if isinstance(e, AccountState)]
    assert len(states) == 1
    assert states[0].account_type == AccountType.BETTING
    balance = states[0].balances[0]
    assert balance.total.as_double() == pytest.approx(1.23456)
    assert balance.currency == SXBET_USDC
    assert balance.locked.as_double() == 0.0

    await client._disconnect()


@pytest.mark.asyncio
async def test_generate_fill_reports_builds_from_user_trades():
    instrument = _fill_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    http_client = Mock()
    http_client.get_user_trades = AsyncMock(
        return_value={
            "data": {
                "trades": [
                    {
                        "orderHash": "0xorderhash",
                        "stake": "5000000",  # 5 USDC
                        "odds": _ODDS_2X,
                        "fillHash": "0xfillhash",
                        "maker": True,
                    },
                ],
            },
        },
    )
    client = _make_fill_client(http_client, instrument, TestComponentStubs.msgbus(), cache)
    client._venue_order_ids[order.client_order_id] = VenueOrderId("0xorderhash")

    reports = await client.generate_fill_reports(
        GenerateFillReports(
            instrument_id=None,
            venue_order_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=client._clock.timestamp_ns(),
        ),
    )

    assert len(reports) == 1
    assert reports[0].venue_order_id == VenueOrderId("0xorderhash")
    assert str(reports[0].trade_id) == "0xfillhash"
    assert reports[0].last_qty == instrument.make_qty(5)
    assert reports[0].last_px == instrument.make_price(2.0)
    assert reports[0].liquidity_side == LiquiditySide.MAKER


def test_init_rejects_non_usdc_base_currency():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="0x" + "12" * 20,
        private_key="0x" + "34" * 32,
        base_currency="UNKNOWN",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )

    with pytest.raises(
        ValueError,
        match="supports only USDC base_currency",
    ):
        SXBetExecutionClient(
            loop=get_event_loop(),
            http_client=Mock(),
            instrument_provider=instrument_provider,
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
            logger=Logger(name="test-sxbet-execution"),
            config=config,
        )


def test_init_accepts_unprefixed_hex_wallet_credentials():
    config = SimpleNamespace(
        api_key="api-key",
        wallet_address="12" * 20,
        private_key="34" * 32,
        base_currency="USDC",
    )
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )

    client = SXBetExecutionClient(
        loop=get_event_loop(),
        http_client=Mock(),
        instrument_provider=instrument_provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-execution"),
        config=config,
    )

    assert client._wallet_address == "0x" + "12" * 20
    assert client._private_key == "0x" + "34" * 32


@pytest.mark.parametrize(
    ("field_name", "field_value", "message"),
    [
        ("api_key", "", "api_key"),
        ("wallet_address", "", "wallet_address"),
        ("private_key", "", "private_key"),
        ("wallet_address", "0x1234", "wallet_address"),
        ("private_key", "0x1234", "private_key"),
    ],
)
def test_init_rejects_missing_or_malformed_required_credentials(
    field_name,
    field_value,
    message,
):
    config_values = {
        "api_key": "api-key",
        "wallet_address": "0x" + "12" * 20,
        "private_key": "0x" + "34" * 32,
        "base_currency": "USDC",
    }
    config_values[field_name] = field_value
    config = SimpleNamespace(**config_values)
    instrument_provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )

    with pytest.raises(
        ValueError,
        match=message,
    ):
        SXBetExecutionClient(
            loop=get_event_loop(),
            http_client=Mock(),
            instrument_provider=instrument_provider,
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
            logger=Logger(name="test-sxbet-execution"),
            config=config,
        )


# Field names mirror the SX.bet GET /trades schema (settled, outcome, bettingOutcomeOne).
def _settled_trade(
    *,
    order_hash="0xorderhash",
    fill_hash="0xfillhash",
    outcome=1,
    betting_outcome_one=True,
    settled=True,
    trade_status="SUCCESS",
    settle_value=1,
) -> dict:
    return {
        "orderHash": order_hash,
        "fillHash": fill_hash,
        "marketHash": "0xmarkethash",
        "stake": "5000000",
        "odds": _ODDS_2X,
        "maker": True,
        "bettingOutcomeOne": betting_outcome_one,
        "settled": settled,
        "outcome": outcome,
        "settleValue": settle_value,
        "tradeStatus": trade_status,
    }


def _trades_payload(*trades: dict) -> dict:
    return {"data": {"trades": list(trades)}}


def _make_settlement_client(http_client, instrument, msgbus, cache):
    client = _make_fill_client(http_client, instrument, msgbus, cache)
    settlements: list[BetSettlement] = []
    msgbus.subscribe(topic=BET_SETTLEMENTS_TOPIC, handler=settlements.append)
    return client, settlements


@pytest.mark.asyncio
async def test_settlement_poll_emits_won_once_and_refreshes_account():
    instrument = _fill_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    http_client = Mock()
    http_client.get_user_trades = AsyncMock(
        return_value=_trades_payload(_settled_trade(outcome=1, betting_outcome_one=True)),
    )
    http_client.get_balance = AsyncMock(return_value={"balance": "1000000"})
    client, settlements = _make_settlement_client(
        http_client,
        instrument,
        TestComponentStubs.msgbus(),
        cache,
    )
    client._venue_order_ids[order.client_order_id] = VenueOrderId("0xorderhash")

    await client._reconcile_settlements()
    await client._reconcile_settlements()

    assert len(settlements) == 1
    settlement = settlements[0]
    assert settlement.result == SettlementResult.WON
    assert settlement.client_order_id == order.client_order_id.value
    assert settlement.instrument_id == str(instrument.id)
    assert settlement.settle_value == 1.0
    assert settlement.venue == SXBET_VENUE.value
    http_client.get_user_trades.assert_awaited_once_with(
        client._wallet_address,
        settled=True,
    )
    # Grading changes the wallet balance, so the account state refreshes immediately.
    assert http_client.get_balance.await_count == 1


@pytest.mark.asyncio
async def test_settlement_poll_maps_lost_and_matches_taker_fill_hash():
    instrument = _fill_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    http_client = Mock()
    # Taker fills track the fillHash as the venue order id; the maker's orderHash in the
    # trade row is not ours.
    http_client.get_user_trades = AsyncMock(
        return_value=_trades_payload(
            _settled_trade(
                order_hash="0xmakerorder",
                fill_hash="0xtakerfill",
                outcome=2,
                betting_outcome_one=True,
            ),
        ),
    )
    http_client.get_balance = AsyncMock(return_value={"balance": "1000000"})
    client, settlements = _make_settlement_client(
        http_client,
        instrument,
        TestComponentStubs.msgbus(),
        cache,
    )
    client._venue_order_ids[order.client_order_id] = VenueOrderId("0xtakerfill")

    await client._reconcile_settlements()

    assert len(settlements) == 1
    assert settlements[0].result == SettlementResult.LOST


@pytest.mark.asyncio
async def test_settlement_poll_maps_void_outcome_zero():
    instrument = _fill_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    http_client = Mock()
    http_client.get_user_trades = AsyncMock(
        return_value=_trades_payload(_settled_trade(outcome=0, settle_value=None)),
    )
    http_client.get_balance = AsyncMock(return_value={"balance": "1000000"})
    client, settlements = _make_settlement_client(
        http_client,
        instrument,
        TestComponentStubs.msgbus(),
        cache,
    )
    client._venue_order_ids[order.client_order_id] = VenueOrderId("0xorderhash")

    await client._reconcile_settlements()

    assert len(settlements) == 1
    assert settlements[0].result == SettlementResult.VOID
    assert settlements[0].settle_value is None


@pytest.mark.asyncio
async def test_settlement_poll_handles_legs_grading_across_polls():
    instrument = _fill_instrument()
    order_a = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
        client_order_id=ClientOrderId("O-LEG-A"),
    )
    order_b = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
        client_order_id=ClientOrderId("O-LEG-B"),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order_a)
    cache.add_order(order_b)
    http_client = Mock()
    trade_a = _settled_trade(order_hash="0xlega", fill_hash="0xfilla", outcome=1)
    trade_b = _settled_trade(
        order_hash="0xlegb",
        fill_hash="0xfillb",
        outcome=1,
        betting_outcome_one=False,
    )
    http_client.get_user_trades = AsyncMock(return_value=_trades_payload(trade_a))
    http_client.get_balance = AsyncMock(return_value={"balance": "1000000"})
    client, settlements = _make_settlement_client(
        http_client,
        instrument,
        TestComponentStubs.msgbus(),
        cache,
    )
    client._venue_order_ids[order_a.client_order_id] = VenueOrderId("0xlega")
    client._venue_order_ids[order_b.client_order_id] = VenueOrderId("0xlegb")

    await client._reconcile_settlements()
    # The second leg grades on a later poll; the first row is re-served by the venue.
    http_client.get_user_trades.return_value = _trades_payload(trade_a, trade_b)
    await client._reconcile_settlements()

    assert [s.client_order_id for s in settlements] == [
        order_a.client_order_id.value,
        order_b.client_order_id.value,
    ]
    assert [s.result for s in settlements] == [SettlementResult.WON, SettlementResult.LOST]
    # Both legs settled -> nothing left to poll.
    await client._reconcile_settlements()
    assert http_client.get_user_trades.await_count == 2


@pytest.mark.asyncio
async def test_settlement_poll_without_tracked_orders_skips_api():
    instrument = _fill_instrument()
    http_client = Mock()
    http_client.get_user_trades = AsyncMock()
    client, settlements = _make_settlement_client(
        http_client,
        instrument,
        TestComponentStubs.msgbus(),
        TestComponentStubs.cache(),
    )

    await client._reconcile_settlements()

    http_client.get_user_trades.assert_not_awaited()
    assert settlements == []


@pytest.mark.asyncio
async def test_settlement_poll_ignores_ungraded_failed_and_unknown_rows():
    instrument = _fill_instrument()
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    cache = TestComponentStubs.cache()
    cache.add_instrument(instrument)
    cache.add_order(order)
    http_client = Mock()
    http_client.get_user_trades = AsyncMock(
        return_value=_trades_payload(
            _settled_trade(settled=False, outcome=None),
            _settled_trade(trade_status="FAILED"),
            _settled_trade(outcome=None),
            _settled_trade(order_hash="0xother", fill_hash="0xotherfill"),
        ),
    )
    http_client.get_balance = AsyncMock(return_value={"balance": "1000000"})
    client, settlements = _make_settlement_client(
        http_client,
        instrument,
        TestComponentStubs.msgbus(),
        cache,
    )
    client._venue_order_ids[order.client_order_id] = VenueOrderId("0xorderhash")

    await client._reconcile_settlements()

    assert settlements == []
    assert http_client.get_balance.await_count == 0
    # The order is still pending, so the next poll queries the venue again.
    await client._reconcile_settlements()
    assert http_client.get_user_trades.await_count == 2
