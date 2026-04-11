# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Integration tests for betting arbitrage strategy.
# -------------------------------------------------------------------------------------------------

from unittest.mock import MagicMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue


@pytest.mark.asyncio
class TestBettingArbitrageIntegration:
    """
    Integration tests for betting arbitrage strategy.
    """

    @pytest.fixture
    def mock_instrument_soccer_tenbet(self):
        """
        Create mock soccer instrument for 10bet.
        """
        inst = Mock(spec=CryptoBettingInstrument)
        inst.id = InstrumentId(Symbol("SOCCER_EPL_MATCH123"), Venue("10BET"))
        inst.sport = "Soccer"
        inst.params = "pre_market"
        return inst

    @pytest.fixture
    def mock_instrument_basketball_blackbet(self):
        """
        Create mock basketball instrument for BlackBet.
        """
        inst = Mock(spec=CryptoBettingInstrument)
        inst.id = InstrumentId(Symbol("BASKETBALL_NBA_MATCH456"), Venue("BLACKBET"))
        inst.sport = "Basketball"
        inst.params = "pre_market"
        return inst

    @pytest.fixture
    def mock_instrument_soccer_live_easybet(self):
        """
        Create mock live soccer instrument for Easybet.
        """
        inst = Mock(spec=CryptoBettingInstrument)
        inst.id = InstrumentId(Symbol("SOCCER_LALIGA_MATCH789"), Venue("EASYBET"))
        inst.sport = "Soccer"
        inst.params = "live_in_play"
        return inst

    def test_sport_filter_soccer_only(
        self,
        mock_instrument_soccer_tenbet,
        mock_instrument_basketball_blackbet,
    ):
        """
        Test sport filter only allows soccer instruments.
        """
        config = BettingArbitrageConfig(
            sport_filter="soccer",
            enabled_venues=frozenset(["10BET", "BLACKBET"]),
        )
        strategy = BettingArbitrageStrategy(config=config)

        # Soccer should pass
        assert strategy._should_process_instrument(mock_instrument_soccer_tenbet)

        # Basketball should be filtered out
        assert not strategy._should_process_instrument(mock_instrument_basketball_blackbet)

    def test_market_timing_filter_pre_market_only(
        self,
        mock_instrument_soccer_tenbet,
        mock_instrument_soccer_live_easybet,
    ):
        """
        Test market timing filter excludes live markets.
        """
        config = BettingArbitrageConfig(
            market_timing_filter="pre_market",
            enabled_venues=frozenset(["10BET", "EASYBET"]),
        )
        strategy = BettingArbitrageStrategy(config=config)

        # Pre-market should pass
        assert strategy._should_process_instrument(mock_instrument_soccer_tenbet)

        # Live market should be filtered out
        assert not strategy._should_process_instrument(mock_instrument_soccer_live_easybet)

    def test_combined_filters_soccer_pre_market(
        self,
        mock_instrument_soccer_tenbet,
        mock_instrument_basketball_blackbet,
        mock_instrument_soccer_live_easybet,
    ):
        """
        Test combined sport and market timing filters.
        """
        config = BettingArbitrageConfig(
            sport_filter="soccer",
            market_timing_filter="pre_market",
            enabled_venues=frozenset(["10BET", "BLACKBET", "EASYBET"]),
        )
        strategy = BettingArbitrageStrategy(config=config)

        # Pre-market soccer should pass
        assert strategy._should_process_instrument(mock_instrument_soccer_tenbet)

        # Basketball filtered by sport
        assert not strategy._should_process_instrument(mock_instrument_basketball_blackbet)

        # Live soccer filtered by timing
        assert not strategy._should_process_instrument(mock_instrument_soccer_live_easybet)

    def test_is_live_market_detection(self):
        """
        Test live market detection logic.
        """
        config = BettingArbitrageConfig()
        strategy = BettingArbitrageStrategy(config=config)

        # Pre-market indicators
        for params in ["pre_market", "prematch", "upcoming"]:
            inst = Mock(spec=CryptoBettingInstrument)
            inst.params = params
            assert not strategy._is_live_market(inst)

        # Live indicators
        for params in ["live", "in_play", "in-play", "live_match"]:
            inst = Mock(spec=CryptoBettingInstrument)
            inst.params = params
            assert strategy._is_live_market(inst)

    def test_subscribe_instruments_with_venue_filter(
        self,
        mock_instrument_soccer_tenbet,
        mock_instrument_basketball_blackbet,
    ):
        """
        Test subscribe_instruments respects venue filter.
        """
        config = BettingArbitrageConfig(
            enabled_venues=frozenset(["10BET"]),  # Only 10BET
        )
        strategy = BettingArbitrageStrategy(config=config)

        # Mock the subscribe_quote_ticks method
        strategy.subscribe_quote_ticks = MagicMock()

        instruments = [
            mock_instrument_soccer_tenbet,
            mock_instrument_basketball_blackbet,
        ]

        strategy.subscribe_instruments(instruments)

        # Should only subscribe to 10BET instrument
        assert len(strategy._subscribed_instruments) == 1
        assert mock_instrument_soccer_tenbet in strategy._subscribed_instruments
        assert mock_instrument_basketball_blackbet not in strategy._subscribed_instruments


# Note: Full end-to-end integration tests would require:
# - Actual NautilusTrader environment (TradingNode, Cache, MessageBus)
# - Live or simulated data feeds
# - Mock order submission and execution
# These tests focus on filter logic and subscription management.
