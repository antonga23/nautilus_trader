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
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.execution import SXBetExecutionClient
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.common.component import Logger
from nautilus_trader.common.functions import get_event_loop
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


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
