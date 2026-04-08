# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Comprehensive tests for BlackBet adapter components.
# -------------------------------------------------------------------------------------------------
# pylint: disable=duplicate-code

from decimal import Decimal
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.blackbet.browser_client import BlackBetBrowserClient
from nautilus_trader.adapters.blackbet.constants import BLACKBET_BASE_URL
from nautilus_trader.adapters.blackbet.constants import BLACKBET_VENUE
from nautilus_trader.adapters.blackbet.providers import BlackBetInstrumentProvider
from nautilus_trader.adapters.blackbet.providers import BlackBetInstrumentProvider as Provider
from nautilus_trader.adapters.blackbet.risk_engine import BlackBetRiskEngine
from nautilus_trader.adapters.blackbet.risk_engine import BlackBetRiskEngine as Engine
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue


class TestBlackBetRiskEngine:
    """Comprehensive tests for BlackBet risk engine - ensures correct naming."""

    def test_venue_name_is_blackbet(self):
        """CRITICAL: Test venue is BLACKBET not 10BET (catches naming bug)."""
        engine = BlackBetRiskEngine()

        # This test would have caught the copy-paste bug!
        assert engine.venue_name == "BLACKBET"
        assert engine.venue_name != "10BET"  # Explicit check

    def test_initialization_defaults(self):
        """
        Test default initialization parameters.
        """
        engine = BlackBetRiskEngine()

        assert engine._max_stake_zar == Decimal(1000)
        assert engine._rollover_multiplier == Decimal(5)
        assert engine._min_rollover_odds == Decimal("1.60")
        assert engine._bonus_amount == Decimal(0)

    def test_custom_parameters(self):
        """
        Test custom risk parameters.
        """
        engine = BlackBetRiskEngine(
            max_stake_zar=Decimal(5000),
            rollover_multiplier=Decimal(3),
            min_rollover_odds=Decimal("1.80"),
            bonus_amount=Decimal(1000),
        )

        assert engine._max_stake_zar == Decimal(5000)
        assert engine._rollover_multiplier == Decimal(3)
        assert engine._min_rollover_odds == Decimal("1.80")
        assert engine._bonus_amount == Decimal(1000)

    def test_stake_violation(self):
        """
        Test stake over limit is rejected.
        """
        engine = BlackBetRiskEngine(max_stake_zar=Decimal(1000))

        result = engine.evaluate_order(
            stake=Decimal(2000),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )

        assert result.approved is False
        assert len(result.violations) > 0

    def test_stake_approved(self):
        """
        Test stake under limit is approved.
        """
        engine = BlackBetRiskEngine(max_stake_zar=Decimal(1000))

        result = engine.evaluate_order(
            stake=Decimal(500),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )

        assert result.approved is True

    def test_rollover_calculation(self):
        """
        Test rollover requirement calculation.
        """
        engine = BlackBetRiskEngine(
            bonus_amount=Decimal(200),
            rollover_multiplier=Decimal(5),
        )

        progress = engine.get_rollover_progress()

        assert progress["bonus_amount"] == Decimal(200)
        assert progress["rollover_multiplier"] == Decimal(5)
        assert progress["rollover_required"] == Decimal(1000)
        assert progress["rollover_completed"] == Decimal(0)
        assert progress["rollover_remaining"] == Decimal(1000)

    def test_rollover_update_valid_bet(self):
        """
        Test valid bet updates rollover.
        """
        engine = BlackBetRiskEngine(
            bonus_amount=Decimal(100),
            rollover_multiplier=Decimal(5),
            min_rollover_odds=Decimal("1.60"),
        )

        # Valid bet - counts toward rollover
        engine.update_rollover(
            stake=Decimal(100),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )

        progress = engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(100)

    def test_rollover_excluded_market(self):
        """
        Test excluded markets don't update rollover.
        """
        engine = BlackBetRiskEngine(
            bonus_amount=Decimal(100),
            rollover_multiplier=Decimal(5),
        )

        # Over/under is excluded
        engine.update_rollover(
            stake=Decimal(100),
            odds=Decimal("2.0"),
            market_type="over_under",
        )

        progress = engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(0)

    def test_rollover_low_odds(self):
        """
        Test low odds don't count toward rollover.
        """
        engine = BlackBetRiskEngine(
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


class TestBlackBetInstrumentProvider:
    """Tests for BlackBet instrument provider - validates naming."""

    @pytest.fixture
    def mock_logger(self):
        return Mock()

    @pytest.fixture
    def mock_browser_client(self):
        browser_client = Mock()
        browser_client.is_connected = False
        browser_client.connect = AsyncMock()
        browser_client.get_markets_for_sport = AsyncMock(return_value=[])
        return browser_client

    @pytest.fixture
    def mock_config(self):
        config = Mock()
        config.sports = frozenset(["soccer"])
        return config

    @pytest.fixture
    def provider(self, mock_logger, mock_browser_client, mock_config):
        return BlackBetInstrumentProvider(
            browser_client=mock_browser_client,
            config=mock_config,
            logger=mock_logger,
        )

    def test_initialization(self, provider):
        """
        Test provider initializes with empty instruments.
        """
        assert provider._instruments == {}

    @pytest.mark.asyncio
    async def test_load_all_async(self, provider, mock_logger, mock_browser_client):
        """
        Test load triggers logging.
        """
        await provider.load_all_async()
        mock_browser_client.connect.assert_awaited_once()
        assert mock_logger.info.called

    def test_list_all_empty(self, provider):
        """
        Test list returns empty list initially.
        """
        assert provider.list_all() == []

    def test_get_returns_none_for_missing(self, provider):
        """
        Test get returns None for non-existent instrument.
        """
        inst_id = InstrumentId(Symbol("MISSING"), Venue("BLACKBET"))
        assert provider.find(inst_id) is None


class TestBlackBetNamingConsistency:
    """
    Meta-tests to ensure naming consistency across BlackBet adapter.
    """

    def test_risk_engine_class_name(self):
        """
        Ensure class is named BlackBetRiskEngine not TenBetRiskEngine.
        """
        assert Engine.__name__ == "BlackBetRiskEngine"
        assert "TenBet" not in Engine.__name__

    def test_provider_class_name(self):
        """
        Ensure provider class has correct name.
        """
        assert Provider.__name__ == "BlackBetInstrumentProvider"
        assert "TenBet" not in Provider.__name__

    def test_venue_constant(self):
        """
        Test BLACKBET_VENUE constant is correct.
        """
        assert BLACKBET_VENUE.value == "BLACKBET"
        assert BLACKBET_VENUE.value != "10BET"


@pytest.mark.asyncio
async def test_blackbet_browser_client_disconnect_clears_connection_state():
    client = BlackBetBrowserClient(base_url=BLACKBET_BASE_URL)
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
