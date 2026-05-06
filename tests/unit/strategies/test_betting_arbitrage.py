# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for betting arbitrage strategy.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-C0302, PYL-R0904, PYL-R0913, PYL-W0212
# pylint: disable=duplicate-code,missing-function-docstring,no-name-in-module,protected-access,too-many-arguments,too-many-lines,too-many-public-methods
"""
Strategy regression tests for the betting arbitrage fast-path integration.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import cast
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.runtime_cache import active_venue_instrument_index_key
from nautilus_trader.adapters.betting.runtime_cache import encode_active_venue_instrument_index
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.cloudbet.client.schema import (
    SelectionSide as CloudbetSelectionSide,
)
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.instruments.crypto_betting import (
    CryptoBettingInstrument as LegacyCryptoBettingInstrument,
)
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import TestDataStubs


def ensure(condition: bool) -> None:  # skipcq
    """
    Raise an assertion error when a boolean expectation is not met.
    """
    if not condition:
        raise AssertionError


class TestBettingArbitrageConfig:  # skipcq
    """
    Test configuration validation and parameters.
    """

    def test_default_config(self):  # skipcq
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
        ensure(config.opportunity_graph_engine == "auto")
        ensure(config.semantic_unmatched_quote_probe_venues == frozenset({"POLYMARKET"}))
        ensure(config.semantic_unmatched_quote_probe_limit_per_venue == 20)
        ensure(config.quote_freshness_profile == "pre_match")
        ensure(config.quote_max_pair_skew_secs is None)
        ensure(config.quote_max_fetch_latency_secs is None)
        ensure(config.instrument_refresh_interval_secs is None)

    def test_custom_venues(self):  # skipcq
        """
        Test custom venue configuration.
        """
        venues = frozenset(["10BET", "BLACKBET", "EASYBET"])
        config = BettingArbitrageConfig(enabled_venues=venues)

        ensure(config.enabled_venues == venues)

    def test_sport_filter(self):  # skipcq
        """
        Test sport filter normalization.
        """
        config = BettingArbitrageConfig(sport_filter="SOCCER ")
        ensure(config.sport_filter == "soccer")

        config2 = BettingArbitrageConfig(sport_filter=None)
        ensure(config2.sport_filter is None)

    def test_market_timing_filter_validation(self):  # skipcq
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

    def test_opportunity_graph_engine_validation(self):  # skipcq
        for engine in ["auto", "python", "rust", "semantic_rust"]:
            config = BettingArbitrageConfig(opportunity_graph_engine=engine.upper())
            ensure(config.opportunity_graph_engine == engine)

        with pytest.raises(ValueError, match="Invalid opportunity_graph_engine"):
            BettingArbitrageConfig(opportunity_graph_engine="invalid")

    def test_semantic_unmatched_quote_probe_validation(self):  # skipcq
        config = BettingArbitrageConfig(
            semantic_unmatched_quote_probe_venues=frozenset({" polymarket ", "sxbet"}),
            semantic_unmatched_quote_probe_limit_per_venue=3,
        )

        ensure(config.semantic_unmatched_quote_probe_venues == frozenset({"POLYMARKET", "SXBET"}))
        ensure(config.semantic_unmatched_quote_probe_limit_per_venue == 3)

        with pytest.raises(ValueError, match="semantic_unmatched_quote_probe_limit"):
            BettingArbitrageConfig(semantic_unmatched_quote_probe_limit_per_venue=-1)

    def test_instrument_refresh_interval_validation(self):  # skipcq
        config = BettingArbitrageConfig(instrument_refresh_interval_secs=300.0)
        ensure(config.instrument_refresh_interval_secs == 300.0)

        with pytest.raises(ValueError, match="instrument_refresh_interval_secs"):
            BettingArbitrageConfig(instrument_refresh_interval_secs=0.0)

    def test_quote_freshness_profile_validation(self):  # skipcq
        for profile in ["pre_match", "live", "custom"]:
            config = BettingArbitrageConfig(quote_freshness_profile=profile.upper())
            ensure(config.quote_freshness_profile == profile)

        with pytest.raises(ValueError, match="Invalid quote_freshness_profile"):
            BettingArbitrageConfig(quote_freshness_profile="invalid")

    def test_exclude_live_flag(self):  # skipcq
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

    def test_profit_margin_range(self):  # skipcq
        """
        Test various profit margin values.
        """
        # Small margin
        config1 = BettingArbitrageConfig(min_profit_margin=Decimal("0.005"))
        ensure(config1.min_profit_margin == Decimal("0.005"))

        # Large margin
        config2 = BettingArbitrageConfig(min_profit_margin=Decimal("0.10"))
        ensure(config2.min_profit_margin == Decimal("0.10"))

    def test_config_round_trips_via_parse(self):  # skipcq
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


class TestBettingArbitrageStrategy:  # skipcq
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

    @staticmethod
    def _polymarket_sports_binary_option(
        *,
        symbol_value: str = "cond-home",
        outcome: str = "Yes",
        selection_role: str = "home",
    ) -> BinaryOption:
        return BinaryOption(
            instrument_id=InstrumentId(Symbol(symbol_value), Venue("POLYMARKET")),
            raw_symbol=Symbol(symbol_value),
            outcome=outcome,
            description="Will Team A beat Team B?",
            asset_class=AssetClass.ALTERNATIVE,
            currency=USDC_POS,
            price_increment=Price.from_str("0.001"),
            price_precision=3,
            size_increment=Quantity.from_str("0.000001"),
            size_precision=6,
            activation_ns=0,
            expiration_ns=1,
            max_quantity=None,
            min_quantity=Quantity.from_int(5),
            maker_fee=Decimal(0),
            taker_fee=Decimal(0),
            ts_event=0,
            ts_init=0,
            info={
                "condition_id": "0xpm-test",
                "sports_market": {
                    "sport": "basketball",
                    "market_name": "basketball.moneyline",
                    "market_type": "basketball.moneyline",
                    "selection_role": selection_role,
                    "event_name": "Team A vs Team B",
                    "home_name": "Team A",
                    "away_name": "Team B",
                    "competition_name": "NBA",
                    "price": 0.43,
                    "resolution_policy": {"tie_or_unknown": "50_50"},
                },
            },
        )

    def test_strategy_initialization(self, default_config):  # skipcq
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

    def test_get_stats(self, default_config):  # skipcq
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
        ensure("opportunity_graph_rust_enabled" in stats)
        ensure("opportunity_graph_topology_source" in stats)
        ensure("opportunity_graph_semantic_template_count" in stats)
        ensure("duplicate_opportunities_suppressed" in stats)
        ensure("stale_quote_suppressions" in stats)
        ensure("matcher_suspect_suppressions" in stats)
        ensure("liquidity_suppressions" in stats)
        ensure("manual_review_suppressions" in stats)
        ensure("executable_candidates" in stats)
        ensure("instrument_refresh_requests" in stats)
        ensure("instrument_refresh_failures" in stats)
        ensure("instrument_refresh_added" in stats)
        ensure("instrument_refresh_removed" in stats)
        ensure("instrument_refresh_delisted_removed" in stats)
        ensure("instrument_refresh_graph_rebuilds" in stats)
        ensure("quote_unsubscribe_requests" in stats)
        ensure("success_rate" in stats)
        ensure(stats["subscribed_instruments"] == 0)
        ensure(stats["opportunity_graph_nodes"] == 0)
        ensure(stats["opportunity_graph_edges"] == 0)
        ensure(stats["opportunity_graph_quote_states"] == 0)
        ensure(stats["liquidity_suppressions"] == 0)
        ensure(stats["manual_review_suppressions"] == 0)
        ensure(stats["instrument_refresh_requests"] == 0)
        ensure(stats["instrument_refresh_failures"] == 0)
        ensure(stats["instrument_refresh_added"] == 0)
        ensure(stats["instrument_refresh_removed"] == 0)
        ensure(stats["instrument_refresh_delisted_removed"] == 0)
        ensure(stats["instrument_refresh_graph_rebuilds"] == 0)
        ensure(stats["quote_unsubscribe_requests"] == 0)
        ensure(stats["success_rate"] == 0)

    def test_instrument_refresh_requests_all_enabled_venues(self, monkeypatch):  # skipcq
        requested: list[tuple[str, dict[str, bool]]] = []

        def fake_request_instruments(self, *, venue, params=None, client_id=None) -> None:
            requested.append((venue.value, dict(params or {})))

        monkeypatch.setattr(
            BettingArbitrageStrategy,
            "request_instruments",
            fake_request_instruments,
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({"SXBET", "CLOUDBET"}),
                instrument_refresh_interval_secs=300.0,
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-REFRESH-REQUESTS"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )

        strategy._refresh_enabled_venue_instruments()

        ensure(
            requested
            == [
                ("CLOUDBET", {"semantic_refresh": True, "only_last": True}),
                ("SXBET", {"semantic_refresh": True, "only_last": True}),
            ],
        )
        ensure(strategy.get_stats()["instrument_refresh_requests"] == 2)
        ensure(strategy.get_stats()["instrument_refresh_failures"] == 0)

    def test_refresh_reconciles_cached_instruments_and_removes_closed_markets(self):  # skipcq
        cache = TestComponentStubs.cache()
        active = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
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
            currency=Currency.from_str("USDC"),
            params="",
            trading_status="ACTIVE",
        )
        closed = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
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
            currency=Currency.from_str("USDC"),
            params="",
            trading_status="CLOSED",
        )
        cache.add_instrument(active)
        cache.add_instrument(closed)

        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"CLOUDBET"})),
        )
        strategy.register(
            trader_id=TraderId("TESTER-REFRESH"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        strategy.unsubscribe_quote_ticks = Mock()
        strategy._subscribed_instruments.add(closed)
        strategy._quote_subscribed_instrument_ids.add(str(closed.id))

        strategy._reconcile_cached_venue_instruments("CLOUDBET")

        ensure(active in strategy._subscribed_instruments)
        ensure(closed not in strategy._subscribed_instruments)
        strategy.unsubscribe_quote_ticks.assert_called_once_with(closed.id)
        stats = strategy.get_stats()
        ensure(stats["instrument_refresh_added"] == 1)
        ensure(stats["instrument_refresh_removed"] == 1)
        ensure(stats["instrument_refresh_delisted_removed"] == 1)
        ensure(stats["quote_unsubscribe_requests"] == 1)

    def test_refresh_schedules_delayed_reconcile_alerts(self, monkeypatch):  # skipcq
        requested: list[tuple[str, dict[str, bool]]] = []

        def fake_request_instruments(self, *, venue, params=None, client_id=None) -> None:
            requested.append((venue.value, dict(params or {})))

        monkeypatch.setattr(
            BettingArbitrageStrategy,
            "request_instruments",
            fake_request_instruments,
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({"SXBET", "CLOUDBET"}),
                instrument_refresh_interval_secs=300.0,
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-REFRESH-TIMERS"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        strategy._schedule_instrument_reconcile = Mock()

        strategy._refresh_enabled_venue_instruments()

        ensure(len(requested) == 2)
        calls = strategy._schedule_instrument_reconcile.call_args_list
        ensure(len(calls) == 2)
        scheduled = {call.args[0] for call in calls}
        ensure(scheduled == {"CLOUDBET", "SXBET"})

    def test_schedule_instrument_reconcile_replaces_existing_timer_safely(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"CLOUDBET"})),
        )
        clock = TestComponentStubs.clock()
        strategy.register(
            trader_id=TraderId("TESTER-REFRESH-RESCHEDULE"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=clock,
        )

        timer_name = strategy._instrument_reconcile_timer_name("CLOUDBET")
        strategy._schedule_instrument_reconcile("CLOUDBET")
        ensure(timer_name in clock.timer_names)

        strategy._schedule_instrument_reconcile("CLOUDBET")

        ensure(clock.timer_names.count(timer_name) == 1)

    def test_active_cached_venue_instruments_prefers_current_refresh_index(self):  # skipcq
        cache = TestComponentStubs.cache()
        active = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
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
            currency=Currency.from_str("USDC"),
            params="",
            trading_status="ACTIVE",
        )
        stale = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
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
            trading_status="ACTIVE",
        )
        cache.add_instrument(active)
        cache.add_instrument(stale)
        cache.add(
            active_venue_instrument_index_key("CLOUDBET"),
            encode_active_venue_instrument_index(
                venue="CLOUDBET",
                instrument_ids=[str(active.id)],
                updated_at_ns=2,
            ),
        )

        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"CLOUDBET"})),
        )
        strategy.register(
            trader_id=TraderId("TESTER-REFRESH-INDEX"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        strategy._last_refresh_request_at_ns["CLOUDBET"] = 1

        active_cached = strategy._active_cached_venue_instruments("CLOUDBET")

        ensure(active_cached == [active])

    def test_active_cached_venue_instruments_ignores_outdated_refresh_index(self):  # skipcq
        cache = TestComponentStubs.cache()
        active = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
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
            currency=Currency.from_str("USDC"),
            params="",
            trading_status="ACTIVE",
        )
        stale = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
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
            trading_status="ACTIVE",
        )
        cache.add_instrument(active)
        cache.add_instrument(stale)
        cache.add(
            active_venue_instrument_index_key("CLOUDBET"),
            encode_active_venue_instrument_index(
                venue="CLOUDBET",
                instrument_ids=[str(active.id)],
                updated_at_ns=1,
            ),
        )

        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"CLOUDBET"})),
        )
        strategy.register(
            trader_id=TraderId("TESTER-REFRESH-STALE"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        strategy._last_refresh_request_at_ns["CLOUDBET"] = 2

        active_cached = strategy._active_cached_venue_instruments("CLOUDBET")

        ensure(set(active_cached) == {active, stale})

    def test_stats_success_rate_calculation(self, default_config):  # skipcq
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

    def test_sport_filter_uses_sport_name(self, soccer_only_config):  # skipcq
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

    def test_sport_filter_falls_back_to_legacy_sport_attribute(self, soccer_only_config):  # skipcq
        """
        Ensure sport filter remains compatible with legacy instrument mocks.
        """
        strategy = BettingArbitrageStrategy(config=soccer_only_config)

        instrument = Mock(spec=CryptoBettingInstrument)
        instrument.sport = "Soccer"

        ensure(strategy._should_process_instrument(instrument) is True)

    def test_is_live_market_prefers_explicit_live_flag(self, pre_market_only_config):  # skipcq
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

    def test_on_quote_tick_uses_latest_live_quotes_for_arbitrage(self, default_config):  # skipcq
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

    def test_opportunity_graph_builds_nodes_and_matching_edges(self):  # skipcq
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

    def test_opportunity_graph_quote_update_evaluates_only_connected_edges(self):  # skipcq
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

    def test_manual_execution_plan_includes_instrument_context(self):  # skipcq
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

    def test_quote_odds_falls_back_to_bid_for_one_sided_quote(self, default_config):  # skipcq
        """
        Ensure one-sided quotes remain usable when the ask side is absent.
        """
        strategy = BettingArbitrageStrategy(config=default_config)
        quote = TestDataStubs.quote_tick(
            bid_price=2.25,
            ask_price=0.0,
        )

        ensure(strategy._quote_odds(quote) == Decimal("2.25"))

    def test_quote_odds_prefers_bid_for_sxbet_quote(self, default_config):  # skipcq
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

    def test_on_start_subscribes_cached_matching_instruments(self, default_config):  # skipcq
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

    def test_on_instrument_subscribes_new_matching_instrument_once(self, default_config):  # skipcq
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

    def test_on_instrument_transforms_polymarket_sports_binary_option(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["POLYMARKET"])),
        )
        strategy.subscribe_quote_ticks = Mock()
        binary_option = self._polymarket_sports_binary_option()

        strategy.on_instrument(binary_option)

        subscribed = tuple(strategy._subscribed_instruments)
        ensure(len(subscribed) == 1)
        transformed = subscribed[0]
        ensure(transformed.id.venue == Venue("POLYMARKET"))
        ensure(transformed.market_type == "basketball.moneyline")
        ensure(transformed.outcome == "home")
        strategy.subscribe_quote_ticks.assert_called_once_with(binary_option.id)

    def test_semantic_batch_transforms_polymarket_sports_binary_options(
        self,
        tmp_path: Path,
    ):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET"]),
                opportunity_graph_engine="python",
            ),
        )
        strategy._matcher.set_rule_store(RuleStore(FileRuleCache(tmp_path / "rules")))
        strategy.subscribe_quote_ticks = Mock()
        home = self._polymarket_sports_binary_option(
            symbol_value="cond-home",
            selection_role="home",
        )
        away = self._polymarket_sports_binary_option(
            symbol_value="cond-away",
            selection_role="away",
        )

        strategy.subscribe_instruments([home, away])

        subscribed = tuple(strategy._subscribed_instruments)
        ensure(len(subscribed) == 2)
        ensure({instrument.outcome for instrument in subscribed} == {"home", "away"})
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}
        ensure(quoted_ids == {home.id, away.id})
        ensure(all(str(instrument.id) != str(home.id) for instrument in subscribed))
        ensure(all(str(instrument.id) != str(away.id) for instrument in subscribed))

    def test_semantic_mode_probes_unmatched_polymarket_quotes(
        self,
        tmp_path: Path,
    ):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET"]),
                opportunity_graph_engine="python",
                semantic_unmatched_quote_probe_limit_per_venue=1,
            ),
        )
        strategy._matcher.set_rule_store(RuleStore(FileRuleCache(tmp_path / "rules")))
        strategy.subscribe_quote_ticks = Mock()
        home = self._polymarket_sports_binary_option(
            symbol_value="cond-home",
            selection_role="home",
        )
        away = self._polymarket_sports_binary_option(
            symbol_value="cond-away",
            selection_role="away",
        )
        transformed_home = strategy._coerce_betting_instrument(home)
        transformed_away = strategy._coerce_betting_instrument(away)
        assert transformed_home is not None
        assert transformed_away is not None

        strategy._subscribed_instruments.update({transformed_home, transformed_away})
        strategy._opportunity_graph.build([transformed_home, transformed_away])
        strategy._opportunity_graph.edge_ids_by_node_id = {
            node_id: set() for node_id in strategy._opportunity_graph.nodes_by_id
        }

        subscribed_count = strategy._subscribe_semantic_unmatched_quote_probe_ticks()

        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}
        ensure(subscribed_count == 1)
        ensure(len(quoted_ids) == 1)
        ensure(quoted_ids <= {home.id, away.id})
        ensure(strategy.get_stats()["quote_subscribed_instruments"] == 1)

    def test_on_quote_tick_remaps_polymarket_binary_option_to_betting_instrument(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["POLYMARKET"])),
        )
        cache = TestComponentStubs.cache()
        strategy.register(
            trader_id=TraderId("TESTER-003"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        strategy._handle_graph_quote_tick = Mock()
        binary_option = self._polymarket_sports_binary_option()
        cache.add_instrument(binary_option)
        source_tick = TestDataStubs.quote_tick(
            instrument=binary_option,
            bid_price=0.42,
            ask_price=0.43,
        )

        strategy.on_quote_tick(source_tick)

        strategy._handle_graph_quote_tick.assert_called_once()
        remapped_tick, transformed = strategy._handle_graph_quote_tick.call_args.args
        ensure(transformed.id.venue == Venue("POLYMARKET"))
        ensure(str(remapped_tick.instrument_id) == str(transformed.id))
        ensure(str(remapped_tick.instrument_id) != str(binary_option.id))

    def test_on_instrument_subscribes_legacy_cloudbet_instrument(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["CLOUDBET"])),
        )
        strategy.subscribe_quote_ticks = Mock()

        instrument = LegacyCryptoBettingInstrument(
            home_name="Team A",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            price=2.0,
            currency=Currency.from_str("USDT"),
            event_name="Team A vs Team B",
            market_name="soccer.match_odds",
            venue=Venue("CLOUDBET"),
            live=False,
            enabled=True,
            outcome="home",
            side=CloudbetSelectionSide.BACK,
            params="",
            market_type="soccer.match_odds",
            event_id=1,
        )

        strategy.on_instrument(instrument)

        strategy.subscribe_quote_ticks.assert_called_once_with(instrument.id)
        ensure(instrument in strategy._subscribed_instruments)

    def test_legacy_cloudbet_instruments_support_market_matching(self):  # skipcq
        over = LegacyCryptoBettingInstrument(
            home_name="Team A",
            away_name="Team B",
            sport_name="Basketball",
            competition_name="Test League",
            price=2.0,
            currency=Currency.from_str("USDT"),
            event_name="Team A vs Team B",
            market_name="basketball.total_points",
            venue=Venue("CLOUDBET"),
            live=False,
            enabled=True,
            outcome="over",
            side=CloudbetSelectionSide.BACK,
            params="line=2.5",
            market_type="basketball.total_points",
            start_time="2026-03-13T18:00:00Z",
            event_id=1,
        )
        under = LegacyCryptoBettingInstrument(
            home_name="Team A",
            away_name="Team B",
            sport_name="Basketball",
            competition_name="Test League",
            price=2.0,
            currency=Currency.from_str("USDT"),
            event_name="Team A vs Team B",
            market_name="basketball.total_points",
            venue=Venue("CLOUDBET"),
            live=False,
            enabled=True,
            outcome="under",
            side=CloudbetSelectionSide.BACK,
            params="line=2.5",
            market_type="basketball.total_points",
            start_time="2026-03-13T18:00:00Z",
            event_id=1,
        )

        ensure(over.matches_event(under))
        ensure(over.is_opposite_outcome(under))
        ensure(MarketMatcher._is_same_market_hedge(over, under))

    def test_semantic_rule_store_quotes_connected_instruments_first(
        self,
        tmp_path: Path,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET"]),
                opportunity_graph_engine="python",
            ),
        )
        strategy._matcher.set_rule_store(RuleStore(FileRuleCache(tmp_path / "rules")))
        strategy.subscribe_quote_ticks = Mock()

        over = LegacyCryptoBettingInstrument(
            home_name="Team A",
            away_name="Team B",
            sport_name="Basketball",
            competition_name="Test League",
            price=2.0,
            currency=Currency.from_str("USDT"),
            event_name="Team A vs Team B",
            market_name="basketball.total_points",
            venue=Venue("CLOUDBET"),
            live=False,
            enabled=True,
            outcome="over",
            side=CloudbetSelectionSide.BACK,
            params="line=2.5",
            market_type="basketball.total_points",
            start_time="2026-03-13T18:00:00Z",
            event_id=1,
        )
        under = LegacyCryptoBettingInstrument(
            home_name="Team A",
            away_name="Team B",
            sport_name="Basketball",
            competition_name="Test League",
            price=2.0,
            currency=Currency.from_str("USDT"),
            event_name="Team A vs Team B",
            market_name="basketball.total_points",
            venue=Venue("CLOUDBET"),
            live=False,
            enabled=True,
            outcome="under",
            side=CloudbetSelectionSide.BACK,
            params="line=2.5",
            market_type="basketball.total_points",
            start_time="2026-03-13T18:00:00Z",
            event_id=1,
        )

        strategy.subscribe_instruments([over])
        strategy.subscribe_quote_ticks.assert_not_called()

        strategy.subscribe_instruments([under])

        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}
        ensure(quoted_ids == {over.id, under.id})
        ensure(strategy.get_stats()["quote_subscribed_instruments"] == 2)

    def test_on_start_skips_subscription_when_cache_is_empty(self, default_config):  # skipcq
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

    def test_arbitrage_diagnostics_suppresses_inverse_duplicate_opportunities(self):  # skipcq
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
        strategy._record_opportunity_pair(
            diagnostics.canonical_pair_id,
            diagnostics.opportunity_id,
            11_000_000_000,
        )
        ensure(strategy._suppress_arbitrage_candidate(diagnostics) is True)

        ensure(strategy._duplicate_opportunities_suppressed == 1)

    def test_arbitrage_diagnostics_flags_stale_quotes(self):  # skipcq
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

    def test_quote_freshness_threshold_profiles(self):  # skipcq
        pre_match_strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET", "CLOUDBET"]),
                quote_freshness_profile="pre_match",
            ),
        )
        sxbet = self._sxbet_instrument(event_id="event-1", outcome="home")
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            outcome="away",
            venue="CLOUDBET",
        )

        pre_match = pre_match_strategy._quote_freshness_thresholds(sxbet, cloudbet)

        ensure(pre_match.profile == "pre_match")
        ensure(pre_match.max_quote_age_secs == 30.0)
        ensure(pre_match.max_pair_skew_secs == 5.0)
        ensure(pre_match.max_fetch_latency_secs == 10.0)

        live_strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET", "CLOUDBET"]),
                quote_freshness_profile="live",
            ),
        )

        live = live_strategy._quote_freshness_thresholds(sxbet, cloudbet)

        ensure(live.profile == "live")
        ensure(live.max_quote_age_secs == 3.0)
        ensure(live.max_pair_skew_secs == 1.0)
        ensure(live.max_fetch_latency_secs == 2.0)

    def test_arbitrage_diagnostics_flags_fetch_latency(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                quote_freshness_profile="custom",
                arbitrage_quote_stale_threshold_secs=10.0,
                quote_max_pair_skew_secs=5.0,
                quote_max_fetch_latency_secs=2.0,
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
                ts_init=4_500_000_000,
            ),
            quote_b=TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.45,
                ask_price=0.0,
                ts_event=1_500_000_000,
                ts_init=2_000_000_000,
            ),
            now_ns=5_000_000_000,
        )

        ensure(diagnostics.stale is False)
        ensure(diagnostics.fetch_latency_stale is True)
        ensure(diagnostics.classification == "fetch_latency")
        ensure(diagnostics.classification_reason == "rest_fetch_latency")
        ensure(strategy._suppress_arbitrage_candidate(diagnostics) is True)
        ensure(strategy.get_stats()["stale_quote_suppressions"] == 1)

    def test_strategy_lifecycle_and_filter_edge_cases(self):  # skipcq
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

    def test_quote_tick_and_graph_branch_edges(self):  # skipcq
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
            is True,
        )

    def test_remaining_lightweight_branch_edges(self):  # skipcq
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
            is False,
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
            strategy._handle_fast_opportunity_candidate(missing_snapshot, 11_000_000_000) is False,
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
            == "",
        )

    def test_fast_snapshot_materialized_edge_cases(self):  # skipcq
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
            is False,
        )

    def test_fast_graph_candidate_matches_public_strategy_effects(self):  # skipcq
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

    def test_fast_logging_and_suppression_formatters_cover_manual_context(self):  # skipcq
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
            ),
        )

    def test_fast_graph_candidate_suppresses_duplicates_before_opportunity_construction(
        self,
    ):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        instrument_a, instrument_b, snapshot = self._fast_candidate_snapshot(strategy)
        strategy._record_opportunity_pair(
            strategy._canonical_pair_id(instrument_a, instrument_b),
            strategy._fast_opportunity_id(snapshot[0], snapshot[10], snapshot[5], snapshot[6]),
            10_000_000_000,
        )

        strategy._handle_fast_opportunity_candidate(snapshot, 11_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._duplicate_opportunities_suppressed == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._executable_candidates == 0)
        strategy._handle_arbitrage_opportunity.assert_not_called()

    def test_fast_graph_candidate_suppresses_stale_quotes_before_opportunity_construction(
        self,
    ):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                quote_freshness_profile="custom",
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

    def test_fast_graph_batch_suppresses_duplicates_from_snapshot_before_context(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
            ),
        )
        _, _, snapshot = self._fast_candidate_snapshot(strategy)
        strategy._record_opportunity_pair(
            snapshot[0],
            strategy._fast_opportunity_id(snapshot[0], snapshot[10], snapshot[5], snapshot[6]),
            10_000_000_000,
        )
        strategy._opportunity_graph.clear()
        strategy._latest_quotes.clear()

        strategy._handle_fast_opportunity_snapshots([snapshot], 11_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._duplicate_opportunities_suppressed == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._executable_candidates == 0)
        strategy._log_arbitrage_summary.assert_called_once()

    def test_duplicate_pair_state_expires_after_cooldown(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                duplicate_suppression_cooldown_secs=1.0,
            ),
        )

        strategy._record_opportunity_pair("pair-a", "pair-a|same_market|2.1:2.2", 1_000_000_000)

        ensure(
            strategy._is_duplicate_opportunity_pair(
                "pair-a",
                "pair-a|same_market|2.1:2.2",
                1_500_000_000,
            )
            is True,
        )
        ensure(
            strategy._is_duplicate_opportunity_pair(
                "pair-a",
                "pair-a|same_market|2.1:2.2",
                3_000_000_000,
            )
            is False,
        )
        ensure("pair-a" in strategy._seen_opportunity_pairs)
        ensure("pair-a" not in strategy._active_opportunity_pairs)

    def test_fast_graph_batch_suppresses_stale_quotes_from_snapshot_before_context(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                quote_freshness_profile="custom",
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

    def test_fast_graph_batch_logs_accepted_snapshot_without_materializing(self):  # skipcq
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

    def test_fast_graph_candidate_preserves_auto_execute_behavior(self):  # skipcq
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

    def test_public_candidate_suppression_and_execution_branches(self):  # skipcq
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

    def test_diagnostics_suppression_and_matcher_reason_branches(self):  # skipcq
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
            ),
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
            == "same_market_params_mismatch",
        )
        ensure(
            strategy._semantic_fixture_suspect_reason(instrument_a, param_mismatch)
            == (False, "none"),
        )

    def test_fast_graph_batch_preserves_auto_execute_behavior(self):  # skipcq
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

    def test_arbitrage_diagnostics_flags_same_venue_event_mismatch(self):  # skipcq
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

        ensure(suspect is False)
        ensure(reason == "none")

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

    def test_arbitrage_diagnostics_flags_cross_cycle_candidates_for_manual_review(self):  # skipcq
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
                ts_event=16_000_000_000,
            ),
            now_ns=17_000_000_000,
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
        quote_updated, snapshots = cast(tuple[bool, list[tuple[Any, ...]]], result)
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
