# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Comprehensive tests for Easybet adapter components.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.easybet.browser_client import EasybetBrowserClient
from nautilus_trader.adapters.easybet.constants import EASYBET_BASE_URL
from nautilus_trader.adapters.easybet.constants import EASYBET_DIRECT_URL
from nautilus_trader.adapters.easybet.constants import EASYBET_VENUE
from nautilus_trader.adapters.easybet.data import EasybetDataClient as Client
from nautilus_trader.adapters.easybet.execution import EasybetExecutionClient as ExecutionClient
from nautilus_trader.adapters.easybet.providers import EasybetInstrumentProvider
from nautilus_trader.adapters.easybet.providers import EasybetInstrumentProvider as Provider
from nautilus_trader.adapters.easybet.risk_engine import EasybetRiskEngine
from nautilus_trader.adapters.easybet.risk_engine import EasybetRiskEngine as Engine
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue


class TestEasybetRiskEngine:
    """
    Comprehensive tests for Easybet risk engine.
    """

    def test_venue_name_is_easybet(self):
        """
        Test venue is EASYBET (naming validation).
        """
        engine = EasybetRiskEngine()

        assert engine.venue_name == "EASYBET"
        assert "10BET" not in engine.venue_name
        assert "BLACKBET" not in engine.venue_name

    def test_initialization_defaults(self):
        """
        Test Easybet-specific defaults.
        """
        engine = EasybetRiskEngine()

        # Easybet has 8x rollover (not 5x like 10bet/BlackBet)
        assert engine._max_stake_zar == Decimal(2000)  # R2000 max
        assert engine._rollover_multiplier == Decimal(8)  # 8x
        assert engine._min_rollover_odds == Decimal("1.50")  # 1.50 min

    def test_custom_parameters(self):
        """
        Test custom initialization.
        """
        engine = EasybetRiskEngine(
            max_stake_zar=Decimal(3000),
            rollover_multiplier=Decimal(10),
            min_rollover_odds=Decimal("1.80"),
            bonus_amount=Decimal(500),
        )

        assert engine._max_stake_zar == Decimal(3000)
        assert engine._rollover_multiplier == Decimal(10)
        assert engine._min_rollover_odds == Decimal("1.80")
        assert engine._bonus_amount == Decimal(500)

    def test_r2000_stake_limit(self):
        """
        Test R2000 default stake limit.
        """
        engine = EasybetRiskEngine()

        # R1500 - approved
        result = engine.evaluate_order(
            stake=Decimal(1500),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )
        assert result.approved is True

        # R2500 - rejected (over R2000)
        result = engine.evaluate_order(
            stake=Decimal(2500),
            odds=Decimal("2.0"),
            market_type="match_odds",
        )
        assert result.approved is False

    def test_8x_rollover_requirement(self):
        """
        Test 8x rollover multiplier (Easybet-specific).
        """
        engine = EasybetRiskEngine(
            bonus_amount=Decimal(100),
            rollover_multiplier=Decimal(8),
        )

        progress = engine.get_rollover_progress()

        assert progress["rollover_multiplier"] == Decimal(8)
        assert progress["rollover_required"] == Decimal(800)  # 100 * 8

    def test_1_50_minimum_odds(self):
        """
        Test 1.50 minimum odds requirement.
        """
        engine = EasybetRiskEngine(
            bonus_amount=Decimal(100),
            min_rollover_odds=Decimal("1.50"),
        )

        # 1.60 odds - counts
        engine.update_rollover(
            stake=Decimal(100),
            odds=Decimal("1.60"),
            market_type="match_odds",
        )
        progress = engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(100)

        # Reset
        engine._rollover_completed = Decimal(0)

        # 1.45 odds - doesn't count
        engine.update_rollover(
            stake=Decimal(100),
            odds=Decimal("1.45"),
            market_type="match_odds",
        )
        progress = engine.get_rollover_progress()
        assert progress["rollover_completed"] == Decimal(0)

    def test_excluded_markets(self):
        """
        Test Easybet-specific market exclusions.
        """
        engine = EasybetRiskEngine(bonus_amount=Decimal(100))

        # Excluded markets
        for market in ["total_goals", "over_under", "handicap"]:
            engine._rollover_completed = Decimal(0)  # Reset

            engine.update_rollover(
                stake=Decimal(100),
                odds=Decimal("2.0"),
                market_type=market,
            )

            progress = engine.get_rollover_progress()
            assert progress["rollover_completed"] == Decimal(0), f"{market} should be excluded"


class TestEasybetInstrumentProvider:
    """
    Tests for Easybet instrument provider.
    """

    @pytest.fixture
    def mock_logger(self):
        return Mock()

    @pytest.fixture
    def provider(self, mock_logger):
        return EasybetInstrumentProvider(logger=mock_logger)

    def test_initialization(self, provider):
        """
        Test provider initializes correctly.
        """
        assert provider._instruments == {}

    @pytest.mark.asyncio
    async def test_load_all_async(self, provider, mock_logger):
        """
        Test load triggers logging.
        """
        await provider.load_all_async()
        assert mock_logger.info.called

    def test_list_all_empty(self, provider):
        """
        Test empty instrument list.
        """
        assert provider.list_all() == []

    def test_get_nonexistent(self, provider):
        """
        Test get returns None for missing instrument.
        """
        inst_id = InstrumentId(Symbol("MISSING"), Venue("EASYBET"))
        assert provider.get(inst_id) is None


class TestEasybetNamingConsistency:
    """
    Naming validation tests for Easybet adapter.
    """

    def test_risk_engine_class_name(self):
        """
        Ensure class is EasybetRiskEngine.
        """
        assert Engine.__name__ == "EasybetRiskEngine"
        assert "TenBet" not in Engine.__name__
        assert "BlackBet" not in Engine.__name__

    def test_provider_class_name(self):
        """
        Ensure provider has correct name.
        """
        assert Provider.__name__ == "EasybetInstrumentProvider"

    def test_data_client_class_name(self):
        """
        Ensure data client has correct name.
        """
        assert Client.__name__ == "EasybetDataClient"

    def test_execution_client_class_name(self):
        """
        Ensure execution client has correct name.
        """
        assert ExecutionClient.__name__ == "EasybetExecutionClient"

    def test_constants_correct(self):
        """
        Test constants are Easybet-specific.
        """
        assert EASYBET_VENUE == "EASYBET"
        assert "easybet" in EASYBET_BASE_URL.lower()
        assert "adv.bet" in EASYBET_DIRECT_URL  # Validates ADV.bet integration


class TestEasybetBrowserClientLifecycle:
    """
    Tests for browser lifecycle cleanup.
    """

    @pytest.mark.asyncio
    async def test_disconnect_clears_closed_state(self):
        browser_client = EasybetBrowserClient()
        page = AsyncMock()
        context = AsyncMock()
        browser = AsyncMock()
        playwright = AsyncMock()
        browser_client._page = page
        browser_client._context = context
        browser_client._browser = browser
        browser_client._playwright = playwright
        browser_client._is_logged_in = True
        browser_client._session_start = 123.0
        browser_client._request_count = 9

        await browser_client.disconnect()

        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()
        assert browser_client._page is None
        assert browser_client._context is None
        assert browser_client._browser is None
        assert browser_client._playwright is None
        assert browser_client._is_logged_in is False
        assert browser_client._session_start is None
        assert browser_client._request_count == 0

        with pytest.raises(RuntimeError, match="Browser not connected"):
            await browser_client.navigate_to("https://example.com")
