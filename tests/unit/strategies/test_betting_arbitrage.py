# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for betting arbitrage strategy.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# pylint: disable=protected-access

from decimal import Decimal
from typing import Any
from typing import cast
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


def ensure(condition: bool) -> None:
    if not condition:
        raise AssertionError


class TestBettingArbitrageConfig:
    """
    Test configuration validation and parameters.
    """

    def test_default_config(self):
        """
        Test default configuration values.
        """
        config = BettingArbitrageConfig()

        ensure(config.min_profit_margin == Decimal("0.01"))
        ensure(config.max_total_stake == Decimal(1000))
        ensure(config.enabled_venues == frozenset(["CLOUDBET", "SXBET", "10BET"]))
        ensure(config.sport_filter is None)
        ensure(config.market_timing_filter == "all")
        ensure(config.rollover_aware is True)
        ensure(config.auto_execute is False)
        ensure(config.arbitrage_quote_stale_threshold_secs == 30.0)
        ensure(config.arbitrage_summary_interval_secs == 60.0)
        ensure(config.opportunity_graph_enabled is True)
        ensure(config.opportunity_log_manual_instructions is True)
        ensure(config.graph_rebuild_on_new_instrument is True)

    def test_custom_venues(self):
        """
        Test custom venue configuration.
        """
        venues = frozenset(["10BET", "BLACKBET", "EASYBET"])
        config = BettingArbitrageConfig(enabled_venues=venues)

        ensure(config.enabled_venues == venues)

    def test_sport_filter(self):
        """
        Test sport filter normalization.
        """
        config = BettingArbitrageConfig(sport_filter="SOCCER ")
        ensure(config.sport_filter == "soccer")

        config2 = BettingArbitrageConfig(sport_filter=None)
        ensure(config2.sport_filter is None)

    def test_market_timing_filter_validation(self):
        """
        Test market timing filter validation.
        """
        # Valid filters
        for timing in ["all", "pre_market", "live"]:
            config = BettingArbitrageConfig(market_timing_filter=timing)
            ensure(config.market_timing_filter == timing)

        # Invalid filter
        with pytest.raises(ValueError, match="Invalid market_timing_filter"):
            BettingArbitrageConfig(market_timing_filter="invalid")

    def test_exclude_live_flag(self):
        """
        Test exclude_live convenience flag.
        """
        config = BettingArbitrageConfig(exclude_live=True)
        ensure(config.market_timing_filter == "pre_market")

        # exclude_live overrides market_timing_filter
        config2 = BettingArbitrageConfig(
            market_timing_filter="live",
            exclude_live=True,
        )
        ensure(config2.market_timing_filter == "pre_market")

    def test_profit_margin_range(self):
        """
        Test various profit margin values.
        """
        # Small margin
        config1 = BettingArbitrageConfig(min_profit_margin=Decimal("0.005"))
        ensure(config1.min_profit_margin == Decimal("0.005"))

        # Large margin
        config2 = BettingArbitrageConfig(min_profit_margin=Decimal("0.10"))
        ensure(config2.min_profit_margin == Decimal("0.10"))

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

        ensure(parsed == config)
        ensure(parsed.sport_filter == "soccer")
        ensure(parsed.enabled_venues == frozenset(["SXBET", "POLYMARKET"]))


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

        ensure(strategy._config == default_config)
        ensure(strategy._matcher is not None)
        ensure(len(strategy._subscribed_instruments) == 0)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._opportunities_executed == 0)
        ensure(strategy._raw_arbitrage_detections == 0)
        ensure(strategy._executable_candidates == 0)

    def test_get_stats(self, default_config):
        """
        Test get_stats returns correct structure.
        """
        strategy = BettingArbitrageStrategy(config=default_config)

        stats = strategy.get_stats()

        ensure("subscribed_instruments" in stats)
        ensure("opportunities_found" in stats)
        ensure("opportunities_executed" in stats)
        ensure("raw_arbitrage_detections" in stats)
        ensure("opportunity_graph_nodes" in stats)
        ensure("opportunity_graph_edges" in stats)
        ensure("opportunity_graph_quote_states" in stats)
        ensure("duplicate_opportunities_suppressed" in stats)
        ensure("stale_quote_suppressions" in stats)
        ensure("matcher_suspect_suppressions" in stats)
        ensure("liquidity_suppressions" in stats)
        ensure("manual_review_suppressions" in stats)
        ensure("executable_candidates" in stats)
        ensure("success_rate" in stats)
        ensure(stats["subscribed_instruments"] == 0)
        ensure(stats["opportunity_graph_nodes"] == 0)
        ensure(stats["opportunity_graph_edges"] == 0)
        ensure(stats["opportunity_graph_quote_states"] == 0)
        ensure(stats["liquidity_suppressions"] == 0)
        ensure(stats["manual_review_suppressions"] == 0)
        ensure(stats["success_rate"] == 0)

    def test_stats_success_rate_calculation(self, default_config):
        """
        Test success rate calculation in stats.
        """
        strategy = BettingArbitrageStrategy(config=default_config)

        # Simulate finding and executing opportunities
        strategy._opportunities_found = 10
        strategy._opportunities_executed = 7

        stats = strategy.get_stats()
        ensure(stats["success_rate"] == 0.7)

        # No opportunities found
        strategy._opportunities_found = 0
        strategy._opportunities_executed = 0
        stats = strategy.get_stats()
        ensure(stats["success_rate"] == 0)

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

        ensure(strategy._should_process_instrument(instrument) is True)

    def test_sport_filter_falls_back_to_legacy_sport_attribute(self, soccer_only_config):
        """
        Ensure sport filter remains compatible with legacy instrument mocks.
        """
        strategy = BettingArbitrageStrategy(config=soccer_only_config)

        instrument = Mock(spec=CryptoBettingInstrument)
        instrument.sport = "Soccer"

        ensure(strategy._should_process_instrument(instrument) is True)

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

        ensure(strategy._is_live_market(live_instrument) is True)
        ensure(strategy._is_live_market(stale_params_instrument) is False)

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
        ensure(opportunity.odds_a == Decimal("2.55"))
        ensure(opportunity.odds_b == Decimal("2.40"))
        ensure(strategy._opportunities_found == 1)
        ensure(strategy._opportunity_graph.connected_edge_count(str(instrument_b.id)) == 1)

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

        ensure(graph.node_count == 2)
        ensure(graph.edge_count == 1)
        ensure(graph.connected_edge_count(str(instrument_a.id)) == 1)
        node = graph.nodes_by_id[str(instrument_a.id)]
        ensure(node.instrument_id == str(instrument_a.id))
        ensure(node.venue == "SXBET")
        ensure(node.canonical_event_key)
        ensure(node.canonical_outcome_key.endswith("|over"))

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

        ensure(graph.connected_edge_count(str(unrelated.id)) == 0)
        ensure(graph.connected_edge_count(str(instrument_b.id)) == 1)
        ensure(len(candidates) == 1)
        ensure(candidates[0].updated_node_id == str(instrument_b.id))
        ensure(candidates[0].opportunity.profit_margin >= Decimal("0.02"))

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
        ensure(opportunity is not None)
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

        manual_plan = strategy._manual_execution_plan(diagnostics)

        ensure("Manual execution plan" in manual_plan)
        ensure("Instrument A" in manual_plan)
        ensure("Instrument B" in manual_plan)
        ensure("event='Team A vs Team B'" in manual_plan)
        ensure("selection='over'" in manual_plan)
        ensure("selection='under'" in manual_plan)
        ensure("bet=" in manual_plan)
        ensure("expected_profit=" in manual_plan)
        ensure("available_size=" in manual_plan)
        ensure("execution_enabled=False" in manual_plan)

    def test_quote_odds_falls_back_to_bid_for_one_sided_quote(self, default_config):
        """
        Ensure one-sided quotes remain usable when the ask side is absent.
        """
        strategy = BettingArbitrageStrategy(config=default_config)
        quote = TestDataStubs.quote_tick(
            bid_price=2.25,
            ask_price=0.0,
        )

        ensure(strategy._quote_odds(quote) == Decimal("2.25"))

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

        ensure(strategy._quote_odds(quote) == Decimal("2.25"))

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
        ensure(matching in strategy._subscribed_instruments)
        ensure(filtered not in strategy._subscribed_instruments)

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
        ensure(not strategy._subscribed_instruments)

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
        ensure(opportunity is not None)

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

        ensure(strategy._suppress_arbitrage_candidate(diagnostics) is False)
        strategy._seen_opportunity_pairs.add(diagnostics.canonical_pair_id)
        ensure(strategy._suppress_arbitrage_candidate(diagnostics) is True)

        ensure(strategy._duplicate_opportunities_suppressed == 1)

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
        ensure(opportunity is not None)

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

        ensure(diagnostics.stale is True)
        ensure(diagnostics.matcher_suspect is False)

    def test_strategy_lifecycle_and_filter_edge_cases(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                sport_filter="soccer",
                market_timing_filter="pre_market",
            ),
        )
        strategy._log_arbitrage_summary = Mock()

        strategy.on_stop()

        strategy._log_arbitrage_summary.assert_called_once_with(force=True)

        wrong_sport = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            params="line=2.5",
            sport_name="Basketball",
        )
        ensure(strategy._should_process_instrument(wrong_sport) is False)

        live_market = self._sxbet_instrument(
            event_id="market-2",
            outcome="under",
            params="line=2.5",
            live=True,
        )
        ensure(strategy._should_process_instrument(live_market) is False)

        live_only = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                market_timing_filter="live",
            ),
        )
        pre_market = self._sxbet_instrument(
            event_id="market-3",
            outcome="over",
            params="line=2.5",
            live=False,
        )
        ensure(live_only._should_process_instrument(pre_market) is False)
        ensure(BettingArbitrageStrategy._is_live_market(Mock(params="in_play=true")) is True)
        ensure(BettingArbitrageStrategy._is_live_market(object()) is False)
        ensure(strategy._quote_odds(None) is None)
        zero_quote = TestDataStubs.quote_tick(bid_price=0.0, ask_price=0.0)
        ensure(strategy._quote_odds(zero_quote) is None)

    def test_quote_tick_and_graph_branch_edges(self):
        instrument = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            params="line=2.5",
        )
        tick = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=2.10,
            ask_price=0.0,
        )

        search_strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                opportunity_graph_enabled=False,
            ),
        )
        cache = TestComponentStubs.cache()
        cache.add_instrument(instrument)
        search_strategy.register(
            trader_id=TraderId("TESTER-003"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        search_strategy._handle_search_quote_tick = Mock()

        search_strategy.on_quote_tick(tick)

        search_strategy._handle_search_quote_tick.assert_called_once_with(tick, instrument)

        missing_strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                graph_rebuild_on_new_instrument=False,
            ),
        )
        missing_strategy._handle_fast_opportunity_snapshots = Mock()
        missing_strategy.register(
            trader_id=TraderId("TESTER-004"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )

        missing_strategy._handle_graph_quote_tick(tick, instrument)

        missing_strategy._handle_fast_opportunity_snapshots.assert_not_called()

        fast_strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )
        fast_strategy._opportunity_graph.update_quote_and_scan_fast = Mock(
            return_value=(False, []),
        )
        ensure(
            fast_strategy._handle_graph_quote_tick_fast(
                tick,
                current_odds=Decimal("2.10"),
                now_ns=10,
            )
            is True
        )

    def test_remaining_lightweight_branch_edges(self):
        filtered_strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                sport_filter="soccer",
            ),
        )
        wrong_sport = self._sxbet_instrument(
            event_id="market-1",
            outcome="over",
            sport_name="Basketball",
        )
        ensure(filtered_strategy._maybe_subscribe_instrument(wrong_sport) is False)
        ensure(BettingArbitrageStrategy._is_live_market(Mock(params=123)) is False)

        graph_disabled = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(opportunity_graph_enabled=False),
        )
        graph_disabled._log_graph_topology_summary()

        instrument = self._sxbet_instrument(
            event_id="market-2",
            outcome="under",
            params="line=2.5",
        )
        tick = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=0.0,
            ask_price=0.0,
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )
        strategy.register(
            trader_id=TraderId("TESTER-006"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        strategy.on_quote_tick(tick)
        strategy._handle_graph_quote_tick(tick, instrument)

        strategy._opportunity_graph.update_quote_and_scan_fast = Mock(return_value=None)
        ensure(
            strategy._handle_graph_quote_tick_fast(
                tick,
                current_odds=Decimal("2.10"),
                now_ns=10,
            )
            is False
        )

        missing_snapshot = (
            "a|b",
            "missing-a",
            "missing-b",
            "same_market",
            1.0,
            2.45,
            2.30,
            0.05,
            10_000_000_000,
            10_000_000_000,
            "same_market",
            False,
        )
        ensure(
            strategy._handle_fast_opportunity_candidate(missing_snapshot, 11_000_000_000) is False
        )
        strategy._log_fast_arbitrage_snapshot(
            "missing-a",
            "missing-b",
            canonical_pair_id="a|b",
            match_type="same_market",
            hedge_type="same_market",
            hedge_confidence=1.0,
            odds_a_raw=2.45,
            odds_b_raw=2.30,
            profit_margin_raw=0.05,
            quote_ts_a=10,
            quote_ts_b=11,
            now_ns=12,
        )

        manual_off = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(opportunity_log_manual_instructions=False),
        )
        ensure(
            manual_off._fast_diagnostics_instrument_fields(
                instrument,
                instrument,
                2.0,
                2.0,
                0.0,
                0.0,
            )
            == ""
        )

    def test_fast_snapshot_materialized_edge_cases(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                opportunity_log_manual_instructions=True,
            ),
        )
        suspect_snapshot = (
            "a|b",
            "a",
            "b",
            "same_market",
            1.0,
            2.45,
            2.30,
            0.05,
            10_000_000_000,
            10_000_000_000,
            "same_market",
            True,
        )
        ensure(strategy._handle_fast_actionable_snapshot(suspect_snapshot, 11_000_000_000) is True)
        ensure(strategy._matcher_suspect_suppressions == 1)

        missing_snapshot = (
            "a|b",
            "missing-a",
            "missing-b",
            "same_market",
            1.0,
            2.45,
            2.30,
            0.05,
            10_000_000_000,
            10_000_000_000,
            "same_market",
            False,
        )
        ensure(strategy._handle_fast_actionable_snapshot(missing_snapshot, 11_000_000_000) is False)

        _, _, snapshot = self._fast_candidate_snapshot(strategy)
        unprofitable_snapshot = (
            snapshot[0],
            snapshot[1],
            snapshot[2],
            snapshot[3],
            snapshot[4],
            1.10,
            1.10,
            snapshot[7],
            snapshot[8],
            snapshot[9],
            snapshot[10],
            snapshot[11],
        )
        ensure(
            strategy._handle_fast_actionable_snapshot(
                unprofitable_snapshot,
                11_000_000_000,
            )
            is False
        )

    def test_fast_graph_candidate_matches_public_strategy_effects(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        _, _, snapshot = self._fast_candidate_snapshot(strategy)

        strategy._handle_fast_opportunity_candidate(snapshot, 11_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._opportunities_found == 1)
        ensure(strategy._executable_candidates == 1)
        strategy._handle_arbitrage_opportunity.assert_called_once()
        opportunity, diagnostics = strategy._handle_arbitrage_opportunity.call_args.args
        ensure(opportunity.odds_a == Decimal("2.45"))
        ensure(opportunity.odds_b == Decimal("2.30"))
        ensure(diagnostics.hedge_match_type == "same_market")
        ensure(diagnostics.hedge_confidence == 1.0)

    def test_fast_logging_and_suppression_formatters_cover_manual_context(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                opportunity_log_manual_instructions=True,
            ),
        )
        instrument_a, instrument_b, snapshot = self._fast_candidate_snapshot(strategy)

        strategy._log_fast_arbitrage_snapshot(
            snapshot[1],
            snapshot[2],
            canonical_pair_id=snapshot[0],
            match_type=snapshot[10],
            hedge_type=snapshot[3],
            hedge_confidence=snapshot[4],
            odds_a_raw=snapshot[5],
            odds_b_raw=snapshot[6],
            profit_margin_raw=snapshot[7],
            quote_ts_a=snapshot[8],
            quote_ts_b=snapshot[9],
            now_ns=11_000_000_000,
        )
        strategy._log_fast_stale_suppression(
            instrument_a,
            instrument_b,
            snapshot[5],
            snapshot[6],
            snapshot[0],
            snapshot[10],
            1.0,
            1.5,
            0.5,
        )
        strategy._log_fast_suspect_suppression(
            instrument_a,
            instrument_b,
            snapshot[5],
            snapshot[6],
            snapshot[0],
            snapshot[10],
            snapshot[3],
            snapshot[4],
            "event_mismatch",
            1.0,
            1.5,
        )

        ensure(
            "Instrument A"
            in strategy._fast_diagnostics_instrument_fields(
                instrument_a,
                instrument_b,
                snapshot[5],
                snapshot[6],
                1.0,
                1.5,
            )
        )

    def test_fast_graph_candidate_suppresses_duplicates_before_opportunity_construction(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        instrument_a, instrument_b, snapshot = self._fast_candidate_snapshot(strategy)
        strategy._seen_opportunity_pairs.add(
            strategy._canonical_pair_id(instrument_a, instrument_b),
        )

        strategy._handle_fast_opportunity_candidate(snapshot, 11_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._duplicate_opportunities_suppressed == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._executable_candidates == 0)
        strategy._handle_arbitrage_opportunity.assert_not_called()

    def test_fast_graph_candidate_suppresses_stale_quotes_before_opportunity_construction(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                arbitrage_quote_stale_threshold_secs=1.0,
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        _, _, snapshot = self._fast_candidate_snapshot(strategy)

        strategy._handle_fast_opportunity_candidate(snapshot, 20_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._stale_quote_suppressions == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._executable_candidates == 0)
        strategy._handle_arbitrage_opportunity.assert_not_called()

    def test_fast_graph_batch_suppresses_duplicates_from_snapshot_before_context(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
            ),
        )
        _, _, snapshot = self._fast_candidate_snapshot(strategy)
        strategy._seen_opportunity_pairs.add(snapshot[0])
        strategy._opportunity_graph.clear()
        strategy._latest_quotes.clear()

        strategy._handle_fast_opportunity_snapshots([snapshot], 11_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._duplicate_opportunities_suppressed == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._executable_candidates == 0)
        strategy._log_arbitrage_summary.assert_called_once()

    def test_fast_graph_batch_suppresses_stale_quotes_from_snapshot_before_context(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                arbitrage_quote_stale_threshold_secs=1.0,
            ),
        )
        _, _, snapshot = self._fast_candidate_snapshot(strategy)
        strategy._opportunity_graph.clear()
        strategy._latest_quotes.clear()

        strategy._handle_fast_opportunity_snapshots([snapshot], 20_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._stale_quote_suppressions == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._executable_candidates == 0)
        strategy._log_arbitrage_summary.assert_called_once()

    def test_fast_graph_batch_logs_accepted_snapshot_without_materializing(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                opportunity_log_manual_instructions=False,
            ),
        )
        _, _, snapshot = self._fast_candidate_snapshot(strategy)
        strategy._fast_arbitrage_opportunity = Mock(side_effect=AssertionError)
        strategy._log_fast_arbitrage_snapshot = Mock()

        strategy._handle_fast_opportunity_snapshots([snapshot], 11_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._opportunities_found == 1)
        ensure(strategy._executable_candidates == 1)
        strategy._log_fast_arbitrage_snapshot.assert_called_once()
        strategy._log_arbitrage_summary.assert_called_once()

    def test_fast_graph_candidate_preserves_auto_execute_behavior(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                auto_execute=True,
            ),
        )
        strategy._execute_arbitrage = Mock()
        _, _, snapshot = self._fast_candidate_snapshot(strategy)

        strategy._handle_fast_opportunity_candidate(snapshot, 11_000_000_000)

        strategy._execute_arbitrage.assert_called_once()
        opportunity = strategy._execute_arbitrage.call_args.args[0]
        ensure(opportunity.odds_a == Decimal("2.45"))
        ensure(opportunity.odds_b == Decimal("2.30"))

    def test_public_candidate_suppression_and_execution_branches(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                auto_execute=True,
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-005"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        strategy.submit_order = Mock()
        instrument_a, instrument_b, snapshot = self._fast_candidate_snapshot(strategy)
        opportunity = strategy._fast_arbitrage_opportunity(
            instrument_a,
            instrument_b,
            odds_a_raw=snapshot[5],
            odds_b_raw=snapshot[6],
            match_type="same_market",
        )
        diagnostics = strategy._fast_arbitrage_diagnostics(
            opportunity=opportunity,
            canonical_pair_id=snapshot[0],
            hedge_match_type=snapshot[3],
            hedge_confidence=snapshot[4],
            quote_ts_a=snapshot[8],
            quote_ts_b=snapshot[9],
            now_ns=11_000_000_000,
        )

        strategy._handle_arbitrage_opportunity(opportunity)
        strategy._handle_arbitrage_opportunity(opportunity, diagnostics)

        ensure(strategy.submit_order.call_count == 4)
        ensure(strategy._opportunities_executed == 2)

        strategy.on_order_filled(Mock())
        strategy.on_order_rejected(Mock())

    def test_diagnostics_suppression_and_matcher_reason_branches(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                opportunity_log_manual_instructions=True,
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
        opportunity = strategy._fast_arbitrage_opportunity(
            instrument_a,
            instrument_b,
            odds_a_raw=2.45,
            odds_b_raw=2.30,
            match_type="same_market",
        )
        stale = strategy._fast_arbitrage_diagnostics(
            opportunity=opportunity,
            canonical_pair_id=strategy._canonical_pair_id(instrument_a, instrument_b),
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_ts_a=1,
            quote_ts_b=2,
            now_ns=60_000_000_000,
        )
        ensure(strategy._suppress_arbitrage_candidate(stale) is True)

        mismatch = self._sxbet_instrument(
            event_id="market-2",
            outcome="under",
            params="line=2.5",
        )
        suspect_opportunity = strategy._fast_arbitrage_opportunity(
            instrument_a,
            mismatch,
            odds_a_raw=2.45,
            odds_b_raw=2.30,
            match_type="same_market",
        )
        suspect = strategy._build_arbitrage_diagnostics(
            opportunity=suspect_opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=TestDataStubs.quote_tick(instrument=instrument_a, ts_event=10),
            quote_b=TestDataStubs.quote_tick(instrument=mismatch, ts_event=11),
            now_ns=12,
        )
        ensure(strategy._suppress_arbitrage_candidate(suspect) is True)
        ensure("Instrument A" in strategy._diagnostics_instrument_fields(suspect))
        ensure(
            strategy._manual_execution_plan(
                suspect,
            )
        )

        manual_off = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(opportunity_log_manual_instructions=False),
        )
        ensure(manual_off._manual_execution_plan(suspect) == "")
        ensure(BettingArbitrageStrategy._quote_age_secs(10, Mock(ts_event=0)) == 0.0)

        other_event = self._sxbet_instrument(
            event_id="market-3",
            event_name="Other vs Team",
            home_name="Other",
            away_name="Team",
            outcome="away",
            params="line=2.5",
            venue="BLACKBET",
        )
        ensure(strategy._matcher_suspect_reason(instrument_a, other_event)[1] == "event_mismatch")
        param_mismatch = self._sxbet_instrument(
            event_id="market-1",
            outcome="under",
            params="line=3.5",
        )
        ensure(
            strategy._matcher_suspect_reason(instrument_a, param_mismatch)[1]
            == "same_market_params_mismatch"
        )

    def test_fast_graph_batch_preserves_auto_execute_behavior(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                auto_execute=True,
                opportunity_log_manual_instructions=False,
            ),
        )
        strategy._execute_arbitrage = Mock()
        _, _, snapshot = self._fast_candidate_snapshot(strategy)

        strategy._handle_fast_opportunity_snapshots([snapshot], 11_000_000_000)

        strategy._execute_arbitrage.assert_called_once()
        opportunity = strategy._execute_arbitrage.call_args.args[0]
        ensure(opportunity.odds_a == Decimal("2.45"))
        ensure(opportunity.odds_b == Decimal("2.30"))

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

        ensure(suspect is True)
        ensure(reason == "same_venue_event_id_mismatch")

    def test_arbitrage_diagnostics_allows_sxbet_two_way_match_odds_market_hash_drift(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )
        instrument_a = self._sxbet_instrument(
            event_id="market-a",
            outcome="home",
            market_name="match_odds",
            info={"is_two_way_market": True},
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-b",
            outcome="away",
            market_name="match_odds",
            info={"is_two_way_market": True},
        )

        suspect, reason = strategy._matcher_suspect_reason(instrument_a, instrument_b)

        assert suspect is False
        assert reason == "none"

    def test_arbitrage_diagnostics_flags_liquidity_insufficient_candidates(self):
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
        ensure(opportunity is not None)

        diagnostics = strategy._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.30,
                ask_price=0.0,
                bid_size=10,
                ask_size=0,
                ts_event=10_000_000_000,
            ),
            quote_b=TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.45,
                ask_price=0.0,
                bid_size=10,
                ask_size=0,
                ts_event=10_500_000_000,
            ),
            now_ns=11_000_000_000,
        )

        ensure(diagnostics.classification == "liquidity_insufficient")
        ensure(diagnostics.classification_reason == "top_of_book_size")
        ensure(strategy._suppress_arbitrage_candidate(diagnostics) is True)
        ensure(strategy.get_stats()["liquidity_suppressions"] == 1)

    def test_arbitrage_diagnostics_flags_cross_cycle_candidates_for_manual_review(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                max_total_stake=Decimal(100),
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
        ensure(opportunity is not None)

        diagnostics = strategy._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.30,
                ask_price=0.0,
                bid_size=500,
                ask_size=0,
                ts_event=10_000_000_000,
            ),
            quote_b=TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.45,
                ask_price=0.0,
                bid_size=500,
                ask_size=0,
                ts_event=13_500_000_000,
            ),
            now_ns=14_000_000_000,
        )

        ensure(diagnostics.same_quote_cycle is False)
        ensure(diagnostics.stale is False)
        ensure(diagnostics.classification == "needs_manual_review")
        ensure(diagnostics.classification_reason == "cross_cycle_quotes")
        ensure(strategy._suppress_arbitrage_candidate(diagnostics) is True)
        ensure(strategy.get_stats()["manual_review_suppressions"] == 1)

    def test_sxbet_two_sided_quotes_produce_valid_manual_candidate(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                max_total_stake=Decimal(100),
            ),
        )
        instrument_a = self._sxbet_instrument(
            event_id="market-a",
            outcome="home",
            market_name="match_odds",
            info={"is_two_way_market": True},
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-b",
            outcome="away",
            market_name="match_odds",
            info={"is_two_way_market": True},
        )
        opportunity = strategy._matcher.check_arbitrage(
            instrument_a,
            instrument_b,
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.30"),
        )
        assert opportunity is not None

        diagnostics = strategy._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.20,
                ask_price=0.0,
                bid_size=100,
                ask_size=0,
                ts_event=10_000_000_000,
            ),
            quote_b=TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.30,
                ask_price=0.0,
                bid_size=100,
                ask_size=0,
                ts_event=10_500_000_000,
            ),
            now_ns=11_000_000_000,
        )

        assert diagnostics.classification == "valid"
        assert diagnostics.quote_cycle_id_a == "10"
        assert diagnostics.quote_cycle_id_b == "10"
        assert "available_size=" in strategy._manual_execution_plan(opportunity, diagnostics)

    def test_sxbet_one_sided_quotes_fail_execution_readiness_gate(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                max_total_stake=Decimal(100),
            ),
        )
        instrument_a = self._sxbet_instrument(
            event_id="market-a",
            outcome="home",
            market_name="match_odds",
            info={"is_two_way_market": True},
        )
        instrument_b = self._sxbet_instrument(
            event_id="market-b",
            outcome="away",
            market_name="match_odds",
            info={"is_two_way_market": True},
        )
        opportunity = strategy._matcher.check_arbitrage(
            instrument_a,
            instrument_b,
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.30"),
        )
        assert opportunity is not None

        diagnostics = strategy._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type="same_market",
            hedge_confidence=1.0,
            quote_a=TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.20,
                ask_price=0.0,
                bid_size=2,
                ask_size=0,
                ts_event=10_000_000_000,
            ),
            quote_b=TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.30,
                ask_price=0.0,
                bid_size=100,
                ask_size=0,
                ts_event=10_500_000_000,
            ),
            now_ns=11_000_000_000,
        )

        assert diagnostics.classification == "liquidity_insufficient"
        assert diagnostics.classification_reason == "top_of_book_size"

    def _fast_candidate_snapshot(
        self,
        strategy: BettingArbitrageStrategy,
    ) -> tuple[CryptoBettingInstrument, CryptoBettingInstrument, tuple]:
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
        cast(Any, strategy)._log_arbitrage_summary = Mock()
        graph = strategy._opportunity_graph
        graph.build([instrument_a, instrument_b])
        graph.update_quote(tick_a, odds=Decimal("2.30"), received_ns=11_000_000_000)
        strategy._latest_quotes[str(instrument_a.id)] = tick_a
        strategy._latest_quotes[str(instrument_b.id)] = tick_b

        result = graph.update_quote_and_scan_fast(
            tick_b,
            odds=Decimal("2.45"),
            received_ns=11_000_000_000,
            min_profit_margin=strategy._config.min_profit_margin,
            now_ns=11_000_000_000,
        )
        if result is None:
            pytest.skip("Rust OpportunityGraphCore is unavailable")
        ensure(result is not None)
        quote_updated, snapshots = result
        ensure(quote_updated is True)
        ensure(len(snapshots) == 1)
        return instrument_a, instrument_b, snapshots[0]

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
        info: dict | None = None,
        sport_name: str = "Soccer",
        live: bool = False,
        venue: str = "SXBET",
    ) -> CryptoBettingInstrument:
        return CryptoBettingInstrument(
            venue=Venue(venue),
            event_id=event_id,
            event_name=event_name,
            home_name=home_name,
            away_name=away_name,
            sport_name=sport_name,
            competition_name="Test League",
            market_name=market_name,
            market_type=market_name,
            outcome=outcome,
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDC"),
            params=params,
            start_time=start_time,
            info=info or {},
            live=live,
        )


# Note: Full integration tests with actual instrument subscriptions and quote ticks
# would require more complex mocking of NautilusTrader components (cache, msgbus, etc.)
# These tests focus on configuration and core logic validation.
