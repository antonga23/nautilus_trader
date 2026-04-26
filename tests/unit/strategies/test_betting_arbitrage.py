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
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
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
        assert config.arbitrage_quote_stale_threshold_secs == 30.0
        assert config.arbitrage_summary_interval_secs == 60.0
        assert config.opportunity_graph_enabled is True
        assert config.opportunity_log_manual_instructions is True
        assert config.graph_rebuild_on_new_instrument is True

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
            max_total_stake=Decimal(2500),
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
        assert strategy._raw_arbitrage_detections == 0
        assert strategy._executable_candidates == 0

    def test_get_stats(self, default_config):
        """
        Test get_stats returns correct structure.
        """
        strategy = BettingArbitrageStrategy(config=default_config)

        stats = strategy.get_stats()

        assert "subscribed_instruments" in stats
        assert "opportunities_found" in stats
        assert "opportunities_executed" in stats
        assert "raw_arbitrage_detections" in stats
        assert "opportunity_graph_nodes" in stats
        assert "opportunity_graph_edges" in stats
        assert "opportunity_graph_quote_states" in stats
        assert "duplicate_opportunities_suppressed" in stats
        assert "stale_quote_suppressions" in stats
        assert "matcher_suspect_suppressions" in stats
        assert "executable_candidates" in stats
        assert "success_rate" in stats
        assert stats["subscribed_instruments"] == 0
        assert stats["opportunity_graph_nodes"] == 0
        assert stats["opportunity_graph_edges"] == 0
        assert stats["opportunity_graph_quote_states"] == 0
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
        assert strategy._opportunity_graph.connected_edge_count(str(instrument_b.id)) == 1

    def test_opportunity_graph_builds_nodes_and_matching_edges(self):
        matcher = MarketMatcher()
        graph = OpportunityGraph(matcher)
        instrument_a = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            params="line=2.5",
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-1",
            outcome="under",
            params="line=2.5",
        )

        graph.build([instrument_a, instrument_b])

        assert graph.node_count == 2
        assert graph.edge_count == 1
        assert graph.connected_edge_count(str(instrument_a.id)) == 1
        node = graph.nodes_by_id[str(instrument_a.id)]
        assert node.instrument_id == str(instrument_a.id)
        assert node.venue == "SXBET"
        assert node.canonical_event_key
        assert node.canonical_outcome_key.endswith("|over")

    def test_opportunity_graph_quote_update_evaluates_only_connected_edges(self):
        matcher = MarketMatcher()
        graph = OpportunityGraph(matcher)
        instrument_a = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            params="line=2.5",
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-1",
            outcome="under",
            params="line=2.5",
        )
        unrelated = self._sxbet_instrument(
            event_id="market-2",
            event_name="Team C vs Team D",
            home_name="Team C",
            away_name="Team D",
            outcome="over",
            params="line=2.5",
            start_time="2026-03-14T18:00:00Z",
        )
        graph.build([instrument_a, instrument_b, unrelated])

        tick_a = TestDataStubs.quote_tick(
            instrument=instrument_a,
            bid_price=2.30,
            ask_price=0.0,
            ts_event=10_000_000_000,
        )
        tick_b = TestDataStubs.quote_tick(
            instrument=instrument_b,
            bid_price=2.45,
            ask_price=0.0,
            ts_event=10_500_000_000,
        )
        graph.update_quote(tick_a, odds=Decimal("2.30"), received_ns=11_000_000_000)
        graph.update_quote(tick_b, odds=Decimal("2.45"), received_ns=11_000_000_000)

        candidates = graph.evaluate_updated_node(
            str(instrument_b.id),
            min_profit_margin=Decimal("0.02"),
            now_ns=11_000_000_000,
        )

        assert graph.connected_edge_count(str(unrelated.id)) == 0
        assert len(candidates) == 1
        assert candidates[0].updated_node_id == str(instrument_b.id)
        assert candidates[0].opportunity.profit_margin >= Decimal("0.02")

    def test_manual_execution_plan_includes_instrument_context(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                max_total_stake=Decimal(100),
            ),
        )
        instrument_a = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            params="line=2.5",
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-1",
            outcome="under",
            params="line=2.5",
        )
        opportunity = strategy._matcher.check_arbitrage(
            instrument_a,
            instrument_b,
            odds_a=Decimal("2.30"),
            odds_b=Decimal("2.45"),
        )
        assert opportunity is not None
        diagnostics = strategy._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.30,
                ask_price=0.0,
                ts_event=10_000_000_000,
            ),
            quote_b=TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.45,
                ask_price=0.0,
                ts_event=10_500_000_000,
            ),
            now_ns=11_000_000_000,
        )

        manual_plan = strategy._manual_execution_plan(opportunity, diagnostics)

        assert "Manual execution plan" in manual_plan
        assert "Instrument A" in manual_plan
        assert "Instrument B" in manual_plan
        assert "event='Team A vs Team B'" in manual_plan
        assert "selection='over'" in manual_plan
        assert "selection='under'" in manual_plan
        assert "bet=" in manual_plan
        assert "expected_profit=" in manual_plan
        assert "execution_enabled=False" in manual_plan

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

    def test_on_start_subscribes_cached_matching_instruments(self, default_config):
        strategy = BettingArbitrageStrategy(config=default_config)
        cache = TestComponentStubs.cache()
        strategy.register(
            trader_id=TraderId("TESTER-001"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        strategy.subscribe_quote_ticks = Mock()

        matching = CryptoBettingInstrument(
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
        filtered = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team C vs Team D",
            home_name="Team C",
            away_name="Team D",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDC"),
            params="",
        )
        cache.add_instrument(matching)
        cache.add_instrument(filtered)

        strategy.on_start()

        strategy.subscribe_quote_ticks.assert_called_once_with(matching.id)
        assert matching in strategy._subscribed_instruments
        assert filtered not in strategy._subscribed_instruments

    def test_on_instrument_subscribes_new_matching_instrument_once(self, default_config):
        strategy = BettingArbitrageStrategy(config=default_config)
        strategy.subscribe_quote_ticks = Mock()

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

        strategy.on_instrument(instrument)
        strategy.on_instrument(instrument)

        strategy.subscribe_quote_ticks.assert_called_once_with(instrument.id)

    def test_on_start_skips_subscription_when_cache_is_empty(self, default_config):
        strategy = BettingArbitrageStrategy(config=default_config)
        strategy.register(
            trader_id=TraderId("TESTER-002"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        strategy.subscribe_quote_ticks = Mock()

        strategy.on_start()

        strategy.subscribe_quote_ticks.assert_not_called()
        assert not strategy._subscribed_instruments

    def test_arbitrage_diagnostics_suppresses_inverse_duplicate_opportunities(self):
        config = BettingArbitrageConfig(
            min_profit_margin=Decimal("0.02"),
            enabled_venues=frozenset(["SXBET"]),
            auto_execute=False,
        )
        strategy = BettingArbitrageStrategy(config=config)
        instrument_a = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            params="line=2.5",
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-1",
            outcome="under",
            params="line=2.5",
        )
        opportunity = strategy._matcher.check_arbitrage(
            instrument_a,
            instrument_b,
            odds_a=Decimal("2.30"),
            odds_b=Decimal("2.45"),
        )
        assert opportunity is not None

        tick_a = TestDataStubs.quote_tick(
            instrument=instrument_a,
            bid_price=2.30,
            ask_price=0.0,
            ts_event=10_000_000_000,
        )
        tick_b = TestDataStubs.quote_tick(
            instrument=instrument_b,
            bid_price=2.45,
            ask_price=0.0,
            ts_event=10_500_000_000,
        )
        diagnostics = strategy._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=tick_a,
            quote_b=tick_b,
            now_ns=11_000_000_000,
        )

        assert strategy._suppress_arbitrage_candidate(diagnostics) is False
        strategy._seen_opportunity_pairs.add(diagnostics.canonical_pair_id)
        assert strategy._suppress_arbitrage_candidate(diagnostics) is True

        assert strategy._duplicate_opportunities_suppressed == 1

    def test_arbitrage_diagnostics_flags_stale_quotes(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                arbitrage_quote_stale_threshold_secs=30.0,
            ),
        )
        instrument_a = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            params="line=2.5",
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-1",
            outcome="under",
            params="line=2.5",
        )
        opportunity = strategy._matcher.check_arbitrage(
            instrument_a,
            instrument_b,
            odds_a=Decimal("2.30"),
            odds_b=Decimal("2.45"),
        )
        assert opportunity is not None

        diagnostics = strategy._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.30,
                ask_price=0.0,
                ts_event=1_000_000_000,
            ),
            quote_b=TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.45,
                ask_price=0.0,
                ts_event=2_000_000_000,
            ),
            now_ns=40_000_000_000,
        )

        assert diagnostics.stale is True
        assert diagnostics.matcher_suspect is False

    def test_arbitrage_diagnostics_flags_same_venue_event_mismatch(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )
        instrument_a = self._sxbet_instrument(
            event_id="market-a",
            outcome="home",
            market_name="match_odds",
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-b",
            outcome="away",
            market_name="match_odds",
        )

        suspect, reason = strategy._matcher_suspect_reason(instrument_a, instrument_b)

        assert suspect is True
        assert reason == "same_venue_event_id_mismatch"

    @staticmethod
    def _sxbet_instrument(
        *,
        event_id: str,
        outcome: str,
        event_name: str = "Team A vs Team B",
        home_name: str = "Team A",
        away_name: str = "Team B",
        market_name: str = "total_goals",
        params: str = "",
        start_time: str = "2026-03-13T18:00:00Z",
    ) -> CryptoBettingInstrument:
        return CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id=event_id,
            event_name=event_name,
            home_name=home_name,
            away_name=away_name,
            sport_name="Soccer",
            competition_name="Test League",
            market_name=market_name,
            market_type=market_name,
            outcome=outcome,
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDC"),
            params=params,
            start_time=start_time,
        )


# Note: Full integration tests with actual instrument subscriptions and quote ticks
# would require more complex mocking of NautilusTrader components (cache, msgbus, etc.)
# These tests focus on configuration and core logic validation.
