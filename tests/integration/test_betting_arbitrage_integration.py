# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Integration tests for betting arbitrage strategy.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module,protected-access
"""
Integration coverage for the betting arbitrage strategy on a trading node.
"""

from decimal import Decimal
from unittest.mock import MagicMock
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.adapters.betting.semantics import PolymarketSportsTransformer
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.live.node import TradingNode
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import build_trading_node_config
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import (
    BettingArbitrageNodeManifest,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import BettingVenueManifest
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.test_kit.functions import ensure_all_tasks_completed
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import TestDataStubs


def ensure(condition: bool) -> None:  # skipcq
    """
    Raise an assertion error when a boolean expectation is not met.
    """
    if not condition:
        raise AssertionError


@pytest.mark.asyncio
class TestBettingArbitrageIntegration:  # skipcq
    """
    Integration tests for betting arbitrage strategy.
    """

    @staticmethod
    def teardown_method():
        ensure_all_tasks_completed()

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
        ensure(strategy._should_process_instrument(mock_instrument_soccer_tenbet))

        # Basketball should be filtered out
        ensure(not strategy._should_process_instrument(mock_instrument_basketball_blackbet))

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
        ensure(strategy._should_process_instrument(mock_instrument_soccer_tenbet))

        # Live market should be filtered out
        ensure(not strategy._should_process_instrument(mock_instrument_soccer_live_easybet))

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
        ensure(strategy._should_process_instrument(mock_instrument_soccer_tenbet))

        # Basketball filtered by sport
        ensure(not strategy._should_process_instrument(mock_instrument_basketball_blackbet))

        # Live soccer filtered by timing
        ensure(not strategy._should_process_instrument(mock_instrument_soccer_live_easybet))

    def test_is_live_market_detection(self):  # skipcq
        """
        Test live market detection logic.
        """
        config = BettingArbitrageConfig()
        strategy = BettingArbitrageStrategy(config=config)

        # Pre-market indicators
        for params in ["pre_market", "prematch", "upcoming"]:
            inst = Mock(spec=CryptoBettingInstrument)
            inst.params = params
            ensure(not strategy._is_live_market(inst))

        # Live indicators
        for params in ["live", "in_play", "in-play", "live_match"]:
            inst = Mock(spec=CryptoBettingInstrument)
            inst.params = params
            ensure(strategy._is_live_market(inst))

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
        ensure(len(strategy._subscribed_instruments) == 1)
        ensure(mock_instrument_soccer_tenbet in strategy._subscribed_instruments)
        ensure(mock_instrument_basketball_blackbet not in strategy._subscribed_instruments)

    def test_strategy_consumes_promoted_semantic_template_from_file_cache(self, tmp_path):
        cache_dir = tmp_path / "semantic-cache"
        home = self._instrument(venue="SXBET", market_type="match_odds", outcome="home")
        away_draw = self._instrument(
            venue="SXBET",
            market_type="double_chance",
            outcome="away_draw",
        )
        self._seed_promoted_template(
            cache_dir=cache_dir,
            instrument_a=home,
            instrument_b=away_draw,
            support=TemplateSupportStats(
                template_id="strict-support",
                observed_count=10,
                event_count=10,
                provider_count=1,
                providers=("SXBET",),
                sports=("soccer",),
                confidence=1.0,
            ),
        )

        strategy = self._registered_strategy(cache_dir=cache_dir, instruments=[home, away_draw])
        strategy._handle_arbitrage_opportunity = Mock()
        strategy.on_start()

        strategy.on_quote_tick(
            TestDataStubs.quote_tick(instrument=home, bid_price=2.40, ask_price=0.0),
        )
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(instrument=away_draw, bid_price=2.45, ask_price=0.0),
        )

        strategy._handle_arbitrage_opportunity.assert_called_once()
        edge = next(iter(strategy._opportunity_graph.edges_by_id.values()))
        assert edge.execution_safe is True
        assert edge.safety_tier == SafetyTier.EXECUTION_SAFE.value
        assert strategy._opportunities_found == 1

    def test_strategy_keeps_same_venue_eligible_template_non_executable(self, tmp_path):
        cache_dir = tmp_path / "semantic-cache"
        dnb_home = self._instrument(venue="SXBET", market_type="draw_no_bet", outcome="home")
        ah_home = self._instrument(
            venue="SXBET",
            market_type="asian_handicap",
            market_name="asian_handicap",
            outcome="home",
            params="line=0",
            handicap=0.0,
        )
        self._seed_promoted_template(
            cache_dir=cache_dir,
            instrument_a=dnb_home,
            instrument_b=ah_home,
            support=TemplateSupportStats(
                template_id="venue-support",
                observed_count=3,
                event_count=3,
                provider_count=1,
                providers=("SXBET",),
                sports=("soccer",),
                confidence=0.99,
            ),
        )

        strategy = self._registered_strategy(cache_dir=cache_dir, instruments=[dnb_home, ah_home])
        strategy._handle_arbitrage_opportunity = Mock()
        strategy.on_start()

        strategy.on_quote_tick(
            TestDataStubs.quote_tick(instrument=dnb_home, bid_price=2.40, ask_price=0.0),
        )
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(instrument=ah_home, bid_price=2.45, ask_price=0.0),
        )

        edge = next(iter(strategy._opportunity_graph.edges_by_id.values()))
        assert edge.execution_safe is False
        assert edge.same_venue_execution_eligible is True
        assert edge.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value
        assert strategy._opportunities_found == 0
        strategy._handle_arbitrage_opportunity.assert_not_called()

    def test_trading_node_strategy_consumes_seeded_semantic_cache(self, tmp_path):
        cache_dir = tmp_path / "semantic-cache"
        home = self._instrument(venue="SXBET", market_type="match_odds", outcome="home")
        away_draw = self._instrument(
            venue="SXBET",
            market_type="double_chance",
            outcome="away_draw",
        )
        self._seed_promoted_template(
            cache_dir=cache_dir,
            instrument_a=home,
            instrument_b=away_draw,
            support=TemplateSupportStats(
                template_id="node-support",
                observed_count=10,
                event_count=10,
                provider_count=1,
                providers=("SXBET",),
                sports=("soccer",),
                confidence=1.0,
            ),
        )

        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-node",
            trader_id="BETARB-NODE-001",
            validation_mode=True,
            allow_dummy_credentials=True,
            semantic_rule_cache_dir=str(cache_dir),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                    execution_enabled=False,
                ),
            ],
        )
        node = TradingNode(config=build_trading_node_config(manifest))
        try:
            strategy = node.trader.strategies()[0]
            assert isinstance(strategy, BettingArbitrageStrategy)
            node.cache.add_instrument(home)
            node.cache.add_instrument(away_draw)
            strategy.subscribe_quote_ticks = Mock()

            strategy.on_start()

            stats = strategy.get_stats()
            assert stats["subscribed_instruments"] == 2
            assert stats["opportunity_graph_edges"] >= 1
            assert stats["opportunity_graph_connected_nodes"] >= 2
        finally:
            node.dispose()

    def test_cloudbet_polymarket_sports_binary_builds_dry_run_semantic_edge(self, tmp_path):
        cache_dir = tmp_path / "semantic-cache"
        cloudbet_home = self._instrument(
            venue="CLOUDBET",
            market_type="basketball.moneyline",
            outcome="home",
            sport_name="basketball",
            competition_name="NBA",
        )
        polymarket_away_binary = self._polymarket_sports_binary_option(selection_role="away")
        polymarket_away = PolymarketSportsTransformer.to_crypto_betting_instrument(
            polymarket_away_binary,
        )
        assert polymarket_away is not None
        self._seed_promoted_template(
            cache_dir=cache_dir,
            instrument_a=cloudbet_home,
            instrument_b=polymarket_away,
            support=TemplateSupportStats(
                template_id="cloudbet-polymarket-support",
                observed_count=10,
                event_count=10,
                provider_count=2,
                providers=("CLOUDBET", "POLYMARKET"),
                sports=("basketball",),
                confidence=1.0,
            ),
        )

        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["CLOUDBET", "POLYMARKET"]),
                auto_execute=False,
                semantic_rule_cache_dir=str(cache_dir),
                opportunity_graph_engine="python",
            ),
        )
        cache = TestComponentStubs.cache()
        cache.add_instrument(cloudbet_home)
        cache.add_instrument(polymarket_away_binary)
        strategy.register(
            trader_id=TraderId("TESTER-POLY-001"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        strategy.subscribe_quote_ticks = Mock()
        strategy._handle_arbitrage_opportunity = Mock()

        strategy.on_start()

        stats = strategy.get_stats()
        assert stats["subscribed_instruments"] == 2
        assert stats["opportunity_graph_edges"] >= 1
        assert stats["opportunity_graph_connected_nodes"] == 2
        subscribed_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}
        assert polymarket_away_binary.id in subscribed_ids
        assert polymarket_away.id not in subscribed_ids

        strategy.on_quote_tick(
            TestDataStubs.quote_tick(
                instrument=cloudbet_home,
                bid_price=0.0,
                ask_price=2.40,
            ),
        )
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(
                instrument=polymarket_away_binary,
                bid_price=0.0,
                ask_price=2.45,
            ),
        )

        assert strategy._handle_arbitrage_opportunity.call_count == 0
        assert strategy._opportunities_executed == 0

    @staticmethod
    def _seed_promoted_template(
        *,
        cache_dir,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        support: TemplateSupportStats,
    ) -> None:
        store = RuleStore(FileRuleCache(cache_dir))
        for provider in support.providers:
            store.save_manifest(
                RuleCorpusManifest(
                    manifest_id=f"manifest-{provider.lower()}",
                    provider=provider,
                    fetched_at="2026-04-27T00:00:00Z",
                    endpoint_version="test",
                    sport_count=1,
                    event_count=support.event_count,
                    selection_count=support.observed_count * 2,
                    market_taxonomy_hash="test",
                    source_refs=(),
                ),
            )
        rule = RuleClassifier().classify(instrument_a, instrument_b)
        assert rule is not None
        template = SemanticRuleTemplate.from_rule(rule, support=support)
        promoted = RulePromotionPolicy().promote_template(store, template)
        assert promoted is not None

    @staticmethod
    def _registered_strategy(
        *,
        cache_dir,
        instruments: list[CryptoBettingInstrument],
    ) -> BettingArbitrageStrategy:
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET"]),
                auto_execute=False,
                semantic_rule_cache_dir=str(cache_dir),
            ),
        )
        cache = TestComponentStubs.cache()
        for instrument in instruments:
            cache.add_instrument(instrument)
        strategy.register(
            trader_id=TraderId("TESTER-SEM-001"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )
        strategy.subscribe_quote_ticks = Mock()
        return strategy

    @staticmethod
    def _instrument(
        *,
        venue: str,
        market_type: str,
        outcome: str,
        market_name: str | None = None,
        params: str = "",
        handicap: float | None = None,
        sport_name: str = "soccer",
        competition_name: str = "Test League",
    ) -> CryptoBettingInstrument:
        return CryptoBettingInstrument(
            venue=Venue(venue),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name=sport_name,
            competition_name=competition_name,
            market_name=market_name or market_type,
            market_type=market_type,
            outcome=outcome,
            side=SelectionSide.BACK,
            price=2.1,
            currency=Currency.from_str("USDC"),
            params=params,
            handicap=handicap,
            start_time="2026-03-13T18:00:00Z",
        )

    @staticmethod
    def _polymarket_sports_binary_option(*, selection_role: str) -> BinaryOption:
        symbol = f"0xpm-{selection_role}"
        return BinaryOption(
            instrument_id=InstrumentId(Symbol(symbol), Venue("POLYMARKET")),
            raw_symbol=Symbol(symbol),
            outcome="Yes",
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

    def test_trading_node_processes_betting_arbitrage_graph_quotes(
        self,
        event_loop_for_setup,
    ):
        """
        Run the strategy registered on a real trading node with realistic quote ticks.
        """
        node = TradingNode(
            config=TradingNodeConfig(logging=LoggingConfig(bypass_logging=True)),
            loop=event_loop_for_setup,
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET", "BLACKBET"]),
                auto_execute=False,
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        instrument_a = self._total_goals_instrument(
            venue="SXBET",
            event_id="arsenal-chelsea-20260503",
            outcome="over",
            price=2.30,
        )
        instrument_b = self._total_goals_instrument(
            venue="BLACKBET",
            event_id="arsenal-chelsea-20260503",
            outcome="under",
            price=2.45,
        )

        node.cache.add_instrument(instrument_a)
        node.cache.add_instrument(instrument_b)
        node.trader.add_strategy(strategy)
        node.build()

        strategy.on_start()

        now_ns = strategy.clock.timestamp_ns()
        tick_a = TestDataStubs.quote_tick(
            instrument=instrument_a,
            bid_price=2.30,
            ask_price=2.40,
            ts_event=now_ns - 250_000_000,
        )
        tick_b = TestDataStubs.quote_tick(
            instrument=instrument_b,
            bid_price=2.35,
            ask_price=2.45,
            ts_event=now_ns,
        )

        strategy.on_quote_tick(tick_a)
        strategy._handle_arbitrage_opportunity.assert_not_called()

        strategy.on_quote_tick(tick_b)

        strategy._handle_arbitrage_opportunity.assert_called_once()
        opportunity, diagnostics = strategy._handle_arbitrage_opportunity.call_args.args
        ensure(opportunity.odds_a == Decimal("2.45"))
        ensure(opportunity.odds_b == Decimal("2.30"))
        ensure(diagnostics.venue_a == "BLACKBET")
        ensure(diagnostics.venue_b == "SXBET")
        ensure(diagnostics.match_type == "same_market")
        ensure(diagnostics.canonical_pair_id in strategy._seen_opportunity_pairs)
        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._opportunities_found == 1)
        ensure(strategy._executable_candidates == 1)
        ensure(strategy._opportunity_graph.node_count == 2)
        ensure(strategy._opportunity_graph.connected_edge_count(str(instrument_b.id)) == 1)

    def test_trading_node_suppresses_stale_arbitrage_candidates(
        self,
        event_loop_for_setup,
    ):
        node = TradingNode(
            config=TradingNodeConfig(logging=LoggingConfig(bypass_logging=True)),
            loop=event_loop_for_setup,
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET", "BLACKBET"]),
                auto_execute=False,
                arbitrage_quote_stale_threshold_secs=0.25,
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        instrument_a = self._total_goals_instrument(
            venue="SXBET",
            event_id="arsenal-chelsea-20260503",
            outcome="over",
            price=2.30,
        )
        instrument_b = self._total_goals_instrument(
            venue="BLACKBET",
            event_id="arsenal-chelsea-20260503",
            outcome="under",
            price=2.45,
        )

        node.cache.add_instrument(instrument_a)
        node.cache.add_instrument(instrument_b)
        node.trader.add_strategy(strategy)
        node.build()
        strategy.on_start()

        now_ns = strategy.clock.timestamp_ns()
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.30,
                ask_price=2.40,
                ts_event=now_ns - 5_000_000_000,
            ),
        )
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.35,
                ask_price=2.45,
                ts_event=now_ns,
            ),
        )

        strategy._handle_arbitrage_opportunity.assert_not_called()
        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._stale_quote_suppressions == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._seen_opportunity_pairs == set())

    def test_trading_node_suppresses_duplicate_graph_opportunities(
        self,
        event_loop_for_setup,
    ):
        node = TradingNode(
            config=TradingNodeConfig(logging=LoggingConfig(bypass_logging=True)),
            loop=event_loop_for_setup,
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                enabled_venues=frozenset(["SXBET", "BLACKBET"]),
                auto_execute=False,
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        instrument_a = self._total_goals_instrument(
            venue="SXBET",
            event_id="arsenal-chelsea-20260503",
            outcome="over",
            price=2.30,
        )
        instrument_b = self._total_goals_instrument(
            venue="BLACKBET",
            event_id="arsenal-chelsea-20260503",
            outcome="under",
            price=2.45,
        )

        node.cache.add_instrument(instrument_a)
        node.cache.add_instrument(instrument_b)
        node.trader.add_strategy(strategy)
        node.build()
        strategy.on_start()

        now_ns = strategy.clock.timestamp_ns()
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.30,
                ask_price=2.40,
                ts_event=now_ns - 250_000_000,
            ),
        )
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(
                instrument=instrument_b,
                bid_price=2.35,
                ask_price=2.45,
                ts_event=now_ns,
            ),
        )
        strategy.on_quote_tick(
            TestDataStubs.quote_tick(
                instrument=instrument_a,
                bid_price=2.30,
                ask_price=2.40,
                ts_event=now_ns + 250_000_000,
            ),
        )

        strategy._handle_arbitrage_opportunity.assert_called_once()
        ensure(strategy._raw_arbitrage_detections == 2)
        ensure(strategy._opportunities_found == 1)
        ensure(strategy._duplicate_opportunities_suppressed == 1)
        ensure(len(strategy._seen_opportunity_pairs) == 1)

    @staticmethod
    def _total_goals_instrument(
        *,
        venue: str,
        event_id: str,
        outcome: str,
        price: float,
    ) -> CryptoBettingInstrument:
        return CryptoBettingInstrument(
            venue=Venue(venue),
            event_id=event_id,
            event_name="Arsenal vs Chelsea",
            home_name="Arsenal",
            away_name="Chelsea",
            sport_name="Soccer",
            competition_name="English Premier League",
            market_name="Total Goals",
            market_type="total_goals",
            outcome=outcome,
            side=SelectionSide.BACK,
            price=price,
            currency=Currency.from_str("USDC"),
            params="line=2.5",
            start_time="2026-05-03T16:30:00Z",
            live=False,
        )


# External venue connectivity and live order submission remain covered by adapter tests.
