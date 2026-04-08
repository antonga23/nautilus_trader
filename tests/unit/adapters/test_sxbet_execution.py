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
