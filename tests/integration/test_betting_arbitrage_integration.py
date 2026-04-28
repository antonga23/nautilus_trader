# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Integration tests for betting arbitrage strategy.
# -------------------------------------------------------------------------------------------------

from unittest.mock import MagicMock
from unittest.mock import Mock

import pytest

from decimal import Decimal

from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import TestDataStubs


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

    @staticmethod
    def _seed_promoted_template(
        *,
        cache_dir,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        support: TemplateSupportStats,
    ) -> None:
        store = RuleStore(FileRuleCache(cache_dir))
        store.save_manifest(
            RuleCorpusManifest(
                manifest_id="manifest-sxbet",
                provider="SXBET",
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
    ) -> CryptoBettingInstrument:
        return CryptoBettingInstrument(
            venue=Venue(venue),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
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


# Note: Full end-to-end integration tests would require:
# - Actual NautilusTrader environment (TradingNode, Cache, MessageBus)
# - Live or simulated data feeds
# - Mock order submission and execution
# These tests focus on filter logic and subscription management.
