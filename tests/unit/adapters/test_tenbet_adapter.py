# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Comprehensive tests for 10bet adapter components.
# -------------------------------------------------------------------------------------------------
# pylint: disable=duplicate-code

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
from unittest.mock import Mock

import msgspec
import pytest

from nautilus_trader.adapters.tenbet.config import TenBetExecClientConfig
from nautilus_trader.adapters.tenbet.config import TenBetInstrumentProviderConfig
from nautilus_trader.adapters.tenbet.execution import TenBetExecutionClient
from nautilus_trader.adapters.tenbet.browser_client import TenBetBrowserClient
from nautilus_trader.adapters.tenbet.constants import TENBET_BASE_URL
from nautilus_trader.adapters.tenbet.providers import TenBetInstrumentProvider
from nautilus_trader.adapters.tenbet.risk_engine import TenBetRiskEngine
from nautilus_trader.test_kit.stubs.commands import TestCommandStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue


class TestTenBetRiskEngine:
    """
    Comprehensive tests for 10bet risk engine.
    """

    def test_initialization(self):
        """
        Test risk engine initializes with correct venue.
        """
        engine = TenBetRiskEngine()

        assert engine.venue_name == "10BET"
        assert engine._max_stake_zar == Decimal(1000)
        assert engine._rollover_multiplier == Decimal(5)
        assert engine._min_rollover_odds == Decimal("1.60")

    def test_custom_parameters(self):
        """
        Test custom risk parameters.
        """
        engine = TenBetRiskEngine(
            max_stake_zar=Decimal(2000),
            rollover_multiplier=Decimal(10),
            min_rollover_odds=Decimal("2.0"),
            bonus_amount=Decimal(500),
        )

        assert engine._max_stake_zar == Decimal(2000)
        assert engine._rollover_multiplier == Decimal(10)
        assert engine._min_rollover_odds == Decimal("2.0")
        assert engine._bonus_amount == Decimal(500)

    def test_stake_limit_enforcement(self):
        """
        Test maximum stake limit is enforced.
        """
        engine = TenBetRiskEngine(max_stake_zar=Decimal(1000))

        # Under limit - approved
        result = engine.evaluate_order(
            stake=Decimal(500),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )
        assert result.approved is True

        # Over limit - rejected
        result = engine.evaluate_order(
            stake=Decimal(1500),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )
        assert result.approved is False
        assert any("stake" in v.lower() for v in result.violations)

    def test_rollover_tracking(self):
        """
        Test rollover progress tracking.
        """
        engine = TenBetRiskEngine(
            bonus_amount=Decimal(100),
            rollover_multiplier=Decimal(5),
        )

        # Initial rollover
        progress = engine.get_rollover_progress()
        assert progress["rollover_required"] == Decimal(500)
        assert progress["rollover_completed"] == Decimal(0)
        assert progress["rollover_remaining"] == Decimal(500)

        # Update rollover
        engine.update_rollover(
            stake=Decimal(100),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )

        progress = engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(100)
        assert progress["rollover_remaining"] == Decimal(400)

    def test_excluded_markets_dont_count_toward_rollover(self):
        """
        Test excluded markets don't contribute to rollover.
        """
        engine = TenBetRiskEngine(bonus_amount=Decimal(100))

        # over_under market is excluded
        engine.update_rollover(
            stake=Decimal(100),
            odds=Decimal("2.0"),
            market_type="over_under",
        )

        progress = engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(0)

    def test_low_odds_dont_count_toward_rollover(self):
        """
        Test odds below minimum don't contribute.
        """
        engine = TenBetRiskEngine(
            bonus_amount=Decimal(100),
            min_rollover_odds=Decimal("1.60"),
        )

        # Odds too low
        engine.update_rollover(
            stake=Decimal(100),
            odds=Decimal("1.50"),
            market_type="match_odds",
        )

        progress = engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(0)


class TestTenBetInstrumentProvider:
    """
    Tests for 10bet instrument provider.
    """

    @pytest.fixture
    def mock_logger(self):
        """
        Create mock logger.
        """
        return Mock()

    @pytest.fixture
    def mock_browser_client(self):
        """
        Mock browser client.
        """
        browser_client = Mock()
        browser_client.is_connected = False
        browser_client.connect = AsyncMock()
        browser_client.get_markets_for_sport = AsyncMock(return_value=[])
        return browser_client

    @pytest.fixture
    def mock_config(self):
        """
        Mock config.
        """
        return TenBetInstrumentProviderConfig(sports=frozenset(["soccer"]))

    @pytest.fixture
    def provider(self, mock_logger, mock_browser_client, mock_config):
        """
        Create instrument provider instance.
        """
        return TenBetInstrumentProvider(
            browser_client=mock_browser_client,
            config=mock_config,
            logger=mock_logger,
        )

    def test_initialization(self, provider):
        """
        Test provider initializes correctly.
        """
        assert provider._instruments == {}

    @pytest.mark.asyncio
    async def test_load_all_async(self, provider, mock_logger, mock_browser_client):
        """
        Test loading instruments.
        """
        await provider.load_all_async()

        # Should log that it's loading (placeholder implementation)
        mock_browser_client.connect.assert_awaited_once()
        assert mock_logger.info.called

    def test_list_all_empty(self, provider):
        """
        Test listing with no instruments.
        """
        instruments = provider.list_all()
        assert instruments == []

    def test_get_nonexistent_instrument(self, provider):
        """
        Test getting non-existent instrument returns None.
        """
        inst_id = InstrumentId(Symbol("TEST"), Venue("10BET"))
        result = provider.find(inst_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_load_all_async_creates_real_betting_instruments(self, mock_logger):
        browser_client = Mock()
        browser_client.is_connected = False
        browser_client.connect = AsyncMock(side_effect=lambda: setattr(browser_client, "is_connected", True))
        browser_client.get_markets_for_sport = AsyncMock(
            return_value=[
                {
                    "marketHash": "market-1",
                    "teamOneName": "Team A",
                    "teamTwoName": "Team B",
                    "sportId": 1,
                    "leagueName": "Premier League",
                    "type": 0,
                    "orders": [
                        {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                        {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
                    ],
                },
            ],
        )

        provider = TenBetInstrumentProvider(
            browser_client=browser_client,
            config=TenBetInstrumentProviderConfig(sports=frozenset(["soccer"])),
            logger=mock_logger,
        )

        await provider.load_all_async()

        instruments = provider.list_all()
        assert len(instruments) == 2
        assert all(instrument.venue == Venue("10BET") for instrument in instruments)
        assert {instrument.outcome for instrument in instruments} == {"home", "away"}
        assert browser_client.get_markets_for_sport.await_count == 1


@pytest.mark.asyncio
async def test_tenbet_browser_client_disconnect_clears_connection_state():
    client = TenBetBrowserClient(base_url=TENBET_BASE_URL)
    client._page = AsyncMock()
    client._context = AsyncMock()
    client._browser = AsyncMock()
    client._playwright = AsyncMock()
    client._is_logged_in = True
    client._session_start_time = 123.0

    await client.disconnect()

    assert client.is_connected is False
    assert client._page is None
    assert client._context is None
    assert client._browser is None
    assert client._playwright is None
    assert client._is_logged_in is False
    assert client._session_start_time is None


@pytest.mark.asyncio
async def test_tenbet_browser_client_uses_synthetic_auth_without_secrets():
    client = TenBetBrowserClient(
        base_url=TENBET_BASE_URL,
        allow_synthetic_auth=True,
    )

    assert await client.login_placeholder() is True
    assert client.is_logged_in is True
    assert client.auth_mode == "synthetic"


@pytest.mark.asyncio
async def test_tenbet_exec_config_round_trip_supports_synthetic_validation_mode():
    raw = msgspec.json.encode(
        {
            "base_url": TENBET_BASE_URL,
            "email": "validator@example.com",
            "password": "dummy-password",
            "allow_synthetic_auth": True,
            "allow_synthetic_execution": True,
            "max_stake_zar": "1000",
            "instrument_provider": {
                "sports": ["soccer"],
            },
        },
    )

    config = msgspec.json.decode(raw, type=TenBetExecClientConfig)

    assert isinstance(config, TenBetExecClientConfig)
    assert config.allow_synthetic_auth is True
    assert config.allow_synthetic_execution is True
    assert config.instrument_provider is not None
    assert config.instrument_provider.sports == frozenset({"soccer"})


@pytest.mark.asyncio
async def test_tenbet_execution_client_accepts_synthetic_orders():
    browser_client = Mock()
    browser_client.connect = AsyncMock()
    browser_client.disconnect = AsyncMock()
    browser_client.login_placeholder = AsyncMock(return_value=True)
    browser_client.is_logged_in = True
    browser_client.auth_mode = "synthetic"

    market_browser_client = Mock()
    market_browser_client.is_connected = True

    provider = TenBetInstrumentProvider(
        browser_client=market_browser_client,
        config=TenBetInstrumentProviderConfig(sports=frozenset(["soccer"])),
    )

    await provider._process_market(
        {
            "marketHash": "market-1",
            "teamOneName": "Team A",
            "teamTwoName": "Team B",
            "sportId": 1,
            "leagueName": "Premier League",
            "type": 0,
            "orders": [
                {"isMakerBettingOutcomeOne": False, "percentageOdds": 5000},
                {"isMakerBettingOutcomeOne": True, "percentageOdds": 4500},
            ],
        },
    )

    instrument = provider.list_all()[0]
    order = TestExecStubs.limit_order(
        instrument=instrument,
        price=instrument.make_price(2.0),
        quantity=instrument.make_qty(10),
    )
    command = TestCommandStubs.submit_order_command(order)

    msgbus = TestComponentStubs.msgbus()
    events: list[object] = []
    msgbus.register("ExecEngine.process", events.append)
    cache = TestComponentStubs.cache()
    clock = TestComponentStubs.clock()
    logger = TestComponentStubs.logger()
    loop = asyncio.get_running_loop()

    client = TenBetExecutionClient(
        loop=loop,
        browser_client=browser_client,
        instrument_provider=provider,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        logger=logger,
        config=TenBetExecClientConfig(
            allow_synthetic_auth=True,
            allow_synthetic_execution=True,
            email=None,
            password=None,
        ),
    )

    await client._submit_order(command)

    assert len(events) >= 2
    assert any(event.__class__.__name__ == "OrderAccepted" for event in events)
    assert command.order.client_order_id in client._orders


# Note: Data client and execution client tests would require more complex
# mocking of Playwright and NautilusTrader infrastructure. These provide
# baseline coverage for core risk engine and provider logic.
