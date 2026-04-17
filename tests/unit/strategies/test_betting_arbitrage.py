# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for betting arbitrage strategy.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import TestDataStubs


class TestBettingArbitrageConfig:
    """
    Test configuration validation and parameters.
    """

    def test_default_config(self):
        """
        Test default configuration values.
        """
        config = BettingArbitrageConfig()

        assert config.min_profit_margin == Decimal("0.01")
        assert config.max_total_stake == Decimal(1000)
        assert config.enabled_venues == frozenset(["CLOUDBET", "SXBET", "10BET"])
        assert config.sport_filter is None
        assert config.market_timing_filter == "all"
        assert config.rollover_aware is True
        assert config.auto_execute is False

    def test_custom_venues(self):
        """
        Test custom venue configuration.
        """
        venues = frozenset(["10BET", "BLACKBET", "EASYBET"])
        config = BettingArbitrageConfig(enabled_venues=venues)

        assert config.enabled_venues == venues

    def test_sport_filter(self):
        """
        Test sport filter normalization.
        """
        config = BettingArbitrageConfig(sport_filter="SOCCER ")
        assert config.sport_filter == "soccer"

        config2 = BettingArbitrageConfig(sport_filter=None)
        assert config2.sport_filter is None

    def test_market_timing_filter_validation(self):
        """
        Test market timing filter validation.
        """
        # Valid filters
        for timing in ["all", "pre_market", "live"]:
            config = BettingArbitrageConfig(market_timing_filter=timing)
            assert config.market_timing_filter == timing

        # Invalid filter
        with pytest.raises(ValueError, match="Invalid market_timing_filter"):
            BettingArbitrageConfig(market_timing_filter="invalid")

    def test_exclude_live_flag(self):
        """
        Test exclude_live convenience flag.
        """
        config = BettingArbitrageConfig(exclude_live=True)
        assert config.market_timing_filter == "pre_market"

        # exclude_live overrides market_timing_filter
        config2 = BettingArbitrageConfig(
            market_timing_filter="live",
            exclude_live=True,
        )
        assert config2.market_timing_filter == "pre_market"

    def test_profit_margin_range(self):
        """
        Test various profit margin values.
        """
        # Small margin
        config1 = BettingArbitrageConfig(min_profit_margin=Decimal("0.005"))
        assert config1.min_profit_margin == Decimal("0.005")

        # Large margin
        config2 = BettingArbitrageConfig(min_profit_margin=Decimal("0.10"))
        assert config2.min_profit_margin == Decimal("0.10")

    def test_config_round_trips_via_parse(self):
        """
        Test config remains importable through JSON encoding/decoding.
        """
        config = BettingArbitrageConfig(
            min_profit_margin=Decimal("0.015"),
            max_total_stake=Decimal("2500"),
            enabled_venues=frozenset(["SXBET", "POLYMARKET"]),
            sport_filter=" Soccer ",
            market_timing_filter="live",
            auto_execute=True,
        )

        parsed = BettingArbitrageConfig.parse(config.json())

        assert parsed == config
        assert parsed.sport_filter == "soccer"
        assert parsed.enabled_venues == frozenset(["SXBET", "POLYMARKET"])


class TestBettingArbitrageStrategy:
    """
    Test arbitrage strategy logic.
    """

    @pytest.fixture
    def default_config(self):
        """
        Create default config for testing.
        """
        return BettingArbitrageConfig(
            min_profit_margin=Decimal("0.02"),
            max_total_stake=Decimal(5000),
            enabled_venues=frozenset(["10BET", "BLACKBET"]),
            auto_execute=False,
        )

    @pytest.fixture
    def soccer_only_config(self):
        """
        Create soccer-only config.
        """
        return BettingArbitrageConfig(
            sport_filter="soccer",
            enabled_venues=frozenset(["10BET", "EASYBET"]),
        )

    @pytest.fixture
    def pre_market_only_config(self):
        """
        Create pre-market only config.
        """
        return BettingArbitrageConfig(
            market_timing_filter="pre_market",
            enabled_venues=frozenset(["BLACKBET", "EASYBET"]),
        )

    def test_strategy_initialization(self, default_config):
        """
        Test strategy initializes correctly.
        """
        strategy = BettingArbitrageStrategy(config=default_config)

        assert strategy._config == default_config
        assert strategy._matcher is not None
        assert len(strategy._subscribed_instruments) == 0
        assert strategy._opportunities_found == 0
        assert strategy._opportunities_executed == 0

    def test_get_stats(self, default_config):
        """
        Test get_stats returns correct structure.
        """
        strategy = BettingArbitrageStrategy(config=default_config)

        stats = strategy.get_stats()

        assert "subscribed_instruments" in stats
        assert "opportunities_found" in stats
        assert "opportunities_executed" in stats
        assert "success_rate" in stats
        assert stats["subscribed_instruments"] == 0
        assert stats["success_rate"] == 0

    def test_stats_success_rate_calculation(self, default_config):
        """
        Test success rate calculation in stats.
        """
        strategy = BettingArbitrageStrategy(config=default_config)

        # Simulate finding and executing opportunities
        strategy._opportunities_found = 10
        strategy._opportunities_executed = 7

        stats = strategy.get_stats()
        assert stats["success_rate"] == 0.7

        # No opportunities found
        strategy._opportunities_found = 0
        strategy._opportunities_executed = 0
        stats = strategy.get_stats()
        assert stats["success_rate"] == 0

    def test_sport_filter_uses_sport_name(self, soccer_only_config):
        """
        Ensure sport filter checks instrument sport_name.
        """
        strategy = BettingArbitrageStrategy(config=soccer_only_config)

        instrument = CryptoBettingInstrument(
            venue=Venue("10BET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("ZAR"),
            params="",
        )

        assert strategy._should_process_instrument(instrument) is True

    def test_sport_filter_falls_back_to_legacy_sport_attribute(self, soccer_only_config):
        """
        Ensure sport filter remains compatible with legacy instrument mocks.
        """
        strategy = BettingArbitrageStrategy(config=soccer_only_config)

        instrument = Mock(spec=CryptoBettingInstrument)
        instrument.sport = "Soccer"

        assert strategy._should_process_instrument(instrument) is True

    def test_is_live_market_prefers_explicit_live_flag(self, pre_market_only_config):
        """
        Ensure explicit instrument.live wins over params heuristics.
        """
        strategy = BettingArbitrageStrategy(config=pre_market_only_config)

        live_instrument = CryptoBettingInstrument(
            venue=Venue("BLACKBET"),
            event_id="event-live",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("ZAR"),
            live=True,
            params="",
        )
        stale_params_instrument = CryptoBettingInstrument(
            venue=Venue("BLACKBET"),
            event_id="event-pre",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("ZAR"),
            live=False,
            params="in-play",
        )

        assert strategy._is_live_market(live_instrument) is True
        assert strategy._is_live_market(stale_params_instrument) is False

    def test_on_quote_tick_uses_latest_live_quotes_for_arbitrage(self, default_config):
        """
        Ensure arbitrage checks use latest quote odds rather than instrument snapshots.
        """
        strategy = BettingArbitrageStrategy(config=default_config)
        strategy._handle_arbitrage_opportunity = Mock()
        cache = TestComponentStubs.cache()
        strategy.register(
            trader_id=TraderId("TESTER-000"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )

        instrument_a = CryptoBettingInstrument(
            venue=Venue("10BET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Total Goals",
            market_type="total_goals",
            outcome="over",
            side=SelectionSide.BACK,
            price=1.80,
            currency=Currency.from_str("ZAR"),
            params="",
            start_time="2026-03-13T18:00:00Z",
        )
        instrument_b = CryptoBettingInstrument(
            venue=Venue("BLACKBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Total Goals",
            market_type="total_goals",
            outcome="under",
            side=SelectionSide.LAY,
            price=1.80,
            currency=Currency.from_str("ZAR"),
            params="",
            start_time="2026-03-13T18:00:00Z",
        )

        strategy._subscribed_instruments = {instrument_a, instrument_b}
        cache.add_instrument(instrument_a)
        cache.add_instrument(instrument_b)

        tick_a = TestDataStubs.quote_tick(
            instrument=instrument_a,
            bid_price=2.30,
            ask_price=2.40,
        )
        tick_b = TestDataStubs.quote_tick(
            instrument=instrument_b,
            bid_price=2.45,
            ask_price=2.55,
        )

        strategy.on_quote_tick(tick_a)
        strategy._handle_arbitrage_opportunity.assert_not_called()

        strategy.on_quote_tick(tick_b)

        strategy._handle_arbitrage_opportunity.assert_called_once()
        opportunity = strategy._handle_arbitrage_opportunity.call_args.args[0]
        assert opportunity.odds_a == Decimal("2.55")
        assert opportunity.odds_b == Decimal("2.40")
        assert strategy._opportunities_found == 1

    def test_quote_odds_falls_back_to_bid_for_one_sided_quote(self, default_config):
        """
        Ensure one-sided quotes remain usable when the ask side is absent.
        """
        strategy = BettingArbitrageStrategy(config=default_config)
        quote = TestDataStubs.quote_tick(
            bid_price=2.25,
            ask_price=0.0,
        )

        assert strategy._quote_odds(quote) == Decimal("2.25")

    def test_quote_odds_prefers_bid_for_sxbet_quote(self, default_config):
        strategy = BettingArbitrageStrategy(config=default_config)
        instrument = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="evt-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.20,
            currency=Currency.from_str("USDT"),
            params="",
        )
        quote = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=2.25,
            ask_price=4.0,
        )

        assert strategy._quote_odds(quote) == Decimal("2.25")


# Note: Full integration tests with actual instrument subscriptions and quote ticks
# would require more complex mocking of NautilusTrader components (cache, msgbus, etc.)
# These tests focus on configuration and core logic validation.
