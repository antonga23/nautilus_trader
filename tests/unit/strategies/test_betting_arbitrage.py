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

import json
from dataclasses import replace
from decimal import Decimal
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import Mock

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.odds import calculate_arbitrage_stakes
from nautilus_trader.adapters.betting.fx import PortfolioCurrencyPolicy
from nautilus_trader.adapters.betting.fx_feeds import FxRateQuote
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.instruments import make_crypto_betting_instrument_id
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.runtime_cache import active_venue_instrument_index_key
from nautilus_trader.adapters.betting.runtime_cache import encode_active_venue_instrument_index
from nautilus_trader.adapters.betting.runtime_cache import encode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import PromotionStatus
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.adapters.cloudbet.client.schema import (
    SelectionSide as CloudbetSelectionSide,
)
from nautilus_trader.examples.strategies import betting_arbitrage as betting_arbitrage_module
from nautilus_trader.examples.strategies.betting_arbitrage import FX_REFRESH_TIMER_NAME
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.examples.strategies.betting_arbitrage import OpportunityPairState
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityEdge
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
        ensure(config.semantic_quote_subscription_limit_by_venue == {})
        ensure(config.semantic_unmatched_quote_probe_venues == frozenset({"POLYMARKET"}))
        ensure(config.semantic_unmatched_quote_probe_limit_per_venue == 20)
        ensure(config.quote_freshness_profile == "pre_match")
        ensure(config.quote_max_pair_skew_secs is None)
        ensure(config.quote_max_fetch_latency_secs is None)
        ensure(config.instrument_refresh_interval_secs is None)
        ensure(config.stale_quote_refresh_cooldown_secs == 60.0)
        ensure(
            config.venue_taker_fee_rates
            == {
                "CLOUDBET": Decimal(0),
                "POLYMARKET": Decimal("0.03"),
                "SXBET": Decimal(0),
            },
        )
        ensure(config.venue_maker_rebate_rates == {})
        # SX.bet's 4% net-winnings commission is modeled by default so unconfigured
        # cross-venue margins are not fee-adjusted with zero cost (#233).
        ensure(config.venue_winning_profit_fee_rates == {"SXBET": Decimal("0.04")})
        ensure(config.venue_basket_rebate_rates == {})
        ensure(config.venue_basket_boost_rates == {})
        ensure(config.devig_enabled is True)
        ensure(config.devig_method == "auto")
        ensure(config.devig_reference_venues is None)
        ensure(config.value_diagnostics_enabled is True)
        ensure(config.value_execution_enabled is False)
        ensure(config.min_value_edge == Decimal("0.015"))
        ensure(config.live_execution_armed is False)
        ensure(config.max_leg_stake == Decimal(15))
        ensure(config.max_daily_notional == Decimal(100))
        ensure(config.max_daily_loss == Decimal(25))
        ensure(config.allow_same_venue_live_execution is True)
        ensure(config.allow_cross_currency_live_execution is False)
        ensure(config.execution_venue_mode == "all")
        ensure(config.portfolio_base_currency == "USD")
        ensure(config.stablecoin_currencies == frozenset({"USD", "USDC", "USDT"}))
        ensure(config.stablecoin_haircut_bps == 10)
        ensure(config.max_resolution_horizon_hours is None)
        ensure(config.execution_price_change_policy == "better")
        ensure(config.unwind_filled_leg_enabled is False)
        ensure(config.unwind_max_slippage_bps == 50)
        # execution_max_retry_count / execution_retry_slippage_bps were removed: they were
        # declared and validated but never read by any execution path (no retry existed).
        ensure(not hasattr(config, "execution_max_retry_count"))
        ensure(not hasattr(config, "execution_retry_slippage_bps"))

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

    def test_semantic_quote_subscription_limit_validation(self):  # skipcq
        config = BettingArbitrageConfig(
            semantic_quote_subscription_limit_by_venue={" cloudbet ": 80, "sxbet": 120},
        )

        ensure(
            config.semantic_quote_subscription_limit_by_venue == {"CLOUDBET": 80, "SXBET": 120},
        )

        with pytest.raises(ValueError, match="semantic_quote_subscription_limit_by_venue"):
            BettingArbitrageConfig(
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": -1},
            )

    def test_instrument_refresh_interval_validation(self):  # skipcq
        config = BettingArbitrageConfig(instrument_refresh_interval_secs=300.0)
        ensure(config.instrument_refresh_interval_secs == 300.0)

        with pytest.raises(ValueError, match="instrument_refresh_interval_secs"):
            BettingArbitrageConfig(instrument_refresh_interval_secs=0.0)

    def test_stale_quote_refresh_cooldown_validation(self):  # skipcq
        config = BettingArbitrageConfig(stale_quote_refresh_cooldown_secs=None)
        ensure(config.stale_quote_refresh_cooldown_secs is None)

        with pytest.raises(ValueError, match="stale_quote_refresh_cooldown_secs"):
            BettingArbitrageConfig(stale_quote_refresh_cooldown_secs=0.0)

    def test_unwind_config_validation(self):  # skipcq
        config = BettingArbitrageConfig(
            unwind_filled_leg_enabled=True,
            unwind_max_slippage_bps=0,
        )
        ensure(config.unwind_filled_leg_enabled is True)
        ensure(config.unwind_max_slippage_bps == 0)

        with pytest.raises(ValueError, match="unwind_max_slippage_bps"):
            BettingArbitrageConfig(unwind_max_slippage_bps=-1)

    def test_venue_fee_rate_validation(self):  # skipcq
        config = BettingArbitrageConfig(
            venue_taker_fee_rates={" polymarket ": "0.02", "sxbet": Decimal("0.01")},
            venue_maker_rebate_rates={"polymarket": "0.0075"},
            venue_winning_profit_fee_rates={"cloudbet": "0.005"},
            venue_basket_rebate_rates={"sxbet": "0.02"},
            venue_basket_boost_rates={"cloudbet": "0.015"},
        )

        ensure(
            config.venue_taker_fee_rates
            == {
                "CLOUDBET": Decimal(0),
                "POLYMARKET": Decimal("0.02"),
                "SXBET": Decimal("0.01"),
            },
        )
        ensure(config.venue_maker_rebate_rates == {"POLYMARKET": Decimal("0.0075")})
        ensure(
            config.venue_winning_profit_fee_rates
            == {"CLOUDBET": Decimal("0.005"), "SXBET": Decimal("0.04")},
        )
        ensure(config.venue_basket_rebate_rates == {"SXBET": Decimal("0.02")})
        ensure(config.venue_basket_boost_rates == {"CLOUDBET": Decimal("0.015")})

        with pytest.raises(ValueError, match="less than 1"):
            BettingArbitrageConfig(venue_taker_fee_rates={"POLYMARKET": "1.0"})
        with pytest.raises(ValueError, match="non-negative"):
            BettingArbitrageConfig(venue_maker_rebate_rates={"POLYMARKET": "-0.01"})

    def test_devig_and_value_diagnostics_validation(self):  # skipcq
        config = BettingArbitrageConfig(
            devig_method="SHIN",
            devig_reference_venues=frozenset({" polymarket ", "sxbet"}),
            min_value_edge=Decimal("0.02"),
        )

        ensure(config.devig_method == "shin")
        ensure(config.devig_reference_venues == frozenset({"POLYMARKET", "SXBET"}))
        ensure(config.min_value_edge == Decimal("0.02"))

        with pytest.raises(ValueError, match="Invalid devig_method"):
            BettingArbitrageConfig(devig_method="bad")
        with pytest.raises(ValueError, match="min_value_edge"):
            BettingArbitrageConfig(min_value_edge=Decimal("-0.01"))
        with pytest.raises(ValueError, match="value_execution_enabled"):
            BettingArbitrageConfig(
                value_diagnostics_enabled=False,
                value_execution_enabled=True,
            )

    def test_live_execution_risk_config_validation(self):  # skipcq
        config = BettingArbitrageConfig(
            live_execution_armed=True,
            max_leg_stake=Decimal(10),
            max_daily_notional=Decimal(50),
            max_daily_loss=Decimal(5),
            execution_price_change_policy="BETTER",
        )

        ensure(config.live_execution_armed is True)
        ensure(config.max_leg_stake == Decimal(10))
        ensure(config.max_daily_notional == Decimal(50))
        ensure(config.max_daily_loss == Decimal(5))
        ensure(config.execution_price_change_policy == "better")

        with pytest.raises(ValueError, match="execution_price_change_policy"):
            BettingArbitrageConfig(execution_price_change_policy="worse")
        with pytest.raises(ValueError, match="max_leg_stake"):
            BettingArbitrageConfig(max_leg_stake=Decimal(0))
        with pytest.raises(ValueError, match="max_daily_notional"):
            BettingArbitrageConfig(max_daily_notional=Decimal(0))
        with pytest.raises(ValueError, match="max_daily_loss"):
            BettingArbitrageConfig(max_daily_loss=Decimal(-1))

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
        event_name: str = "Team A vs Team B",
        home_name: str = "Team A",
        away_name: str = "Team B",
        start_time: str = "2026-03-13T18:00:00Z",
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
                "condition_id": f"0xpm-test-{symbol_value}",
                "sports_market": {
                    "sport": "basketball",
                    "market_name": "basketball.moneyline",
                    "market_type": "basketball.moneyline",
                    "selection_role": selection_role,
                    "event_name": event_name,
                    "home_name": home_name,
                    "away_name": away_name,
                    "competition_name": "NBA",
                    "start_time": start_time,
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

    def test_fee_adjusted_opportunity_preserves_raw_margin_and_applies_venue_fees(self):  # skipcq
        """
        Fee-aware decisions should use net margin while preserving raw vig diagnostics.
        """
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["POLYMARKET", "SXBET"])),
        )
        polymarket = self._sxbet_instrument(
            event_id="event-1",
            venue="POLYMARKET",
            outcome="over",
        )
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        opportunity = MarketMatcher().check_arbitrage(
            polymarket,
            sxbet,
            odds_a=Decimal("2.02"),
            odds_b=Decimal("2.02"),
        )

        ensure(opportunity is not None)
        adjusted = strategy.fee_adjusted_opportunity(opportunity)

        ensure(adjusted.fee_adjusted is True)
        ensure(adjusted.raw_profit_margin == Decimal("0.01"))
        ensure(adjusted.profit_margin < adjusted.raw_profit_margin)
        ensure(adjusted.fee_drag > 0)
        ensure(adjusted.taker_fee_rate_a == Decimal("0.03"))
        ensure(adjusted.taker_fee_rate_b == Decimal(0))
        ensure(adjusted.maker_rebate_rate_a == Decimal(0))
        ensure(adjusted.maker_rebate_rate_b == Decimal(0))

    def test_fee_adjusted_opportunity_uses_market_fee_metadata_over_venue_defaults(self):  # skipcq
        """
        Polymarket-like markets can provide per-market taker/rebate parameters.
        """
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET", "SXBET"]),
                venue_taker_fee_rates={"POLYMARKET": "0.03"},
                venue_maker_rebate_rates={"POLYMARKET": "0.001"},
            ),
        )
        polymarket = self._sxbet_instrument(
            event_id="event-1",
            venue="POLYMARKET",
            outcome="over",
            info={
                "taker_fee_rate": "0.01",
                "maker_rebate_rate": "0.004",
            },
        )
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        opportunity = MarketMatcher().check_arbitrage(
            polymarket,
            sxbet,
            odds_a=Decimal("2.02"),
            odds_b=Decimal("2.02"),
        )

        ensure(opportunity is not None)
        adjusted = strategy.fee_adjusted_opportunity(opportunity)

        ensure(adjusted.taker_fee_rate_a == Decimal("0.01"))
        ensure(adjusted.maker_rebate_rate_a == Decimal("0.004"))
        ensure(adjusted.profit_margin < adjusted.raw_profit_margin)
        ensure(adjusted.fee_drag > 0)

    def test_fee_adjusted_opportunity_accepts_basis_point_market_metadata(self):  # skipcq
        """
        Venue feeds may expose fee and incentive parameters in basis points.
        """
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET", "SXBET"]),
                venue_taker_fee_rates={"POLYMARKET": "0.03"},
                venue_maker_rebate_rates={"POLYMARKET": "0.001"},
            ),
        )
        polymarket = self._sxbet_instrument(
            event_id="event-1",
            venue="POLYMARKET",
            outcome="over",
            info={
                "feeRateBps": "75",
                "maker_rebate_rate_bps": "25",
                "basket_rebate_rate_bps": "50",
            },
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
            info={"basket_boost_rate_bps": "100"},
        )
        opportunity = MarketMatcher().check_arbitrage(
            polymarket,
            sxbet,
            odds_a=Decimal("2.02"),
            odds_b=Decimal("2.02"),
        )

        ensure(opportunity is not None)
        adjusted = strategy.fee_adjusted_opportunity(opportunity)

        ensure(adjusted.taker_fee_rate_a == Decimal("0.0075"))
        ensure(adjusted.maker_rebate_rate_a == Decimal("0.0025"))
        ensure(adjusted.basket_rebate_rate == Decimal("0.005"))
        ensure(adjusted.basket_boost_rate == Decimal("0.01"))

    def test_fee_adjusted_opportunity_applies_pair_basket_incentives(self):  # skipcq
        """
        Basket-level rewards can improve a covered pair without changing safety tiers.
        """
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                venue_basket_rebate_rates={"SXBET": "0.01"},
                venue_basket_boost_rates={"SXBET": "0.02"},
            ),
        )
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="over")
        cloudbet = self._sxbet_instrument(event_id="event-1", venue="CLOUDBET", outcome="under")
        opportunity = MarketMatcher().check_arbitrage(
            sxbet,
            cloudbet,
            odds_a=Decimal("2.02"),
            odds_b=Decimal("2.02"),
        )

        ensure(opportunity is not None)
        adjusted = strategy.fee_adjusted_opportunity(opportunity)

        ensure(adjusted.basket_rebate_rate == Decimal("0.01"))
        ensure(adjusted.basket_boost_rate == Decimal("0.02"))
        ensure(adjusted.profit_margin > adjusted.raw_profit_margin)
        ensure(adjusted.fee_drag < 0)

    def test_fee_adjusted_coverage_basket_applies_three_leg_incentives(self):  # skipcq
        """
        Hyperedge/full-book diagnostics should use the same fee and rebate model.
        """
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                venue_basket_rebate_rates={"SXBET": "0.015"},
                venue_basket_boost_rates={"SXBET": "0.01"},
                venue_winning_profit_fee_rates={"CLOUDBET": "0.005"},
            ),
        )
        home = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="home")
        draw = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="draw",
            info={"basket_rebate_rate_bps": "200"},
        )
        away = self._sxbet_instrument(event_id="event-1", venue="CLOUDBET", outcome="away")

        adjusted = strategy.fee_adjusted_coverage_basket(
            (home, draw, away),
            (Decimal("3.05"), Decimal("3.10"), Decimal("3.15")),
        )

        ensure(adjusted.leg_count == 3)
        ensure(adjusted.basket.basket_rebate_rate == Decimal("0.02"))
        ensure(adjusted.basket.basket_boost_rate == Decimal("0.01"))
        ensure(adjusted.legs[2].winning_profit_fee_rate == Decimal("0.005"))
        ensure(
            adjusted.overround
            == sum((Decimal(1) / leg.raw_odds for leg in adjusted.legs), Decimal(0)),
        )
        ensure(adjusted.vig == adjusted.overround - Decimal(1))
        ensure(abs(sum(adjusted.no_vig_probabilities, Decimal(0)) - Decimal(1)) < Decimal("1e-12"))
        ensure(adjusted.devig_method == "proportional")
        ensure(adjusted.devig_convergence_status == "not_required")
        ensure(adjusted.basket.effective_profit_margin > adjusted.basket.raw_profit_margin)

    def test_devigged_book_uses_strategy_devig_method_without_changing_execution(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(devig_method="shin"),
        )

        devigged = strategy.devigged_book((Decimal("1.75"), Decimal("2.20")))

        ensure(devigged is not None)
        ensure(devigged.method == "shin")
        ensure(strategy.value_execution_enabled is False)

    def test_fee_adjusted_coverage_basket_rejects_shape_mismatch(self):  # skipcq
        """
        Coverage fee diagnostics must not silently drop legs from a hyperedge.
        """
        strategy = BettingArbitrageStrategy(config=BettingArbitrageConfig())
        instrument = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="home")

        with pytest.raises(ValueError, match="lengths must match"):
            strategy.fee_adjusted_coverage_basket((instrument,), (Decimal("2.0"), Decimal("2.1")))

    def test_fee_adjusted_margin_blocks_raw_only_opportunity_candidate(self):  # skipcq
        """
        Raw overround edge is not enough when configured fees erase the margin.
        """
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.005"),
                enabled_venues=frozenset(["POLYMARKET", "SXBET"]),
            ),
        )
        strategy._handle_arbitrage_opportunity = Mock()
        polymarket = self._sxbet_instrument(
            event_id="event-1",
            venue="POLYMARKET",
            outcome="over",
        )
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        opportunity = MarketMatcher().check_arbitrage(
            polymarket,
            sxbet,
            odds_a=Decimal("2.02"),
            odds_b=Decimal("2.02"),
        )
        ensure(opportunity is not None)
        candidate = SimpleNamespace(
            opportunity=opportunity,
            edge=SimpleNamespace(hedge_type="same_market", confidence=1.0),
            quote_a=None,
            quote_b=None,
        )

        strategy._handle_opportunity_candidate(candidate, 10_000_000_000)

        ensure(strategy._raw_arbitrage_detections == 1)
        ensure(strategy._opportunities_found == 0)
        ensure(strategy._executable_candidates == 0)
        strategy._handle_arbitrage_opportunity.assert_not_called()

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
        ensure("unique_opportunity_pairs" in stats)
        ensure("active_opportunity_pairs" in stats)
        ensure("duplicate_suppression_cooldown_secs" in stats)
        ensure("opportunity_graph_nodes" in stats)
        ensure("opportunity_graph_edges" in stats)
        ensure("opportunity_graph_quote_states" in stats)
        ensure("opportunity_graph_rust_enabled" in stats)
        ensure("opportunity_graph_topology_source" in stats)
        ensure("opportunity_graph_semantic_template_count" in stats)
        ensure("opportunity_graph_coverage_summary" in stats)
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
        ensure("instrument_refresh_reconciles" in stats)
        ensure("instrument_refresh_graph_rebuilds" in stats)
        ensure("instrument_refresh_stale_triggers" in stats)
        ensure("quote_unsubscribe_requests" in stats)
        ensure("instrument_cache_miss" in stats)
        ensure("quote_odds_rejected" in stats)
        ensure("instrument_cache_miss_by_venue" in stats)
        ensure("quote_odds_rejected_by_venue" in stats)
        ensure("quote_subscription_counts_by_venue" in stats)
        ensure("semantic_quote_subscription_limit_by_venue" in stats)
        ensure("semantic_quote_subscription_limit_exceeded_by_venue" in stats)
        ensure(
            stats["venue_taker_fee_rates"] == {"CLOUDBET": "0", "POLYMARKET": "0.03", "SXBET": "0"},
        )
        ensure(stats["venue_maker_rebate_rates"] == {})
        ensure(stats["venue_winning_profit_fee_rates"] == {"SXBET": "0.04"})
        ensure(stats["venue_basket_rebate_rates"] == {})
        ensure(stats["venue_basket_boost_rates"] == {})
        ensure("instrument_refresh_by_venue" in stats)
        ensure("provider_quote_poll_stats" in stats)
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
        ensure(stats["instrument_refresh_reconciles"] == 0)
        ensure(stats["instrument_refresh_graph_rebuilds"] == 0)
        ensure(stats["instrument_refresh_stale_triggers"] == 0)
        ensure(stats["quote_unsubscribe_requests"] == 0)
        ensure(stats["instrument_cache_miss"] == 0)
        ensure(stats["quote_odds_rejected"] == 0)
        ensure(stats["instrument_cache_miss_by_venue"] == {})
        ensure(stats["quote_odds_rejected_by_venue"] == {})
        ensure(stats["quote_subscription_counts_by_venue"] == {})
        ensure(stats["semantic_quote_subscription_limit_by_venue"] == {})
        ensure(stats["semantic_quote_subscription_limit_exceeded_by_venue"] == {})
        ensure(stats["instrument_refresh_by_venue"] == {})
        ensure(stats["provider_quote_poll_stats"] == {})
        ensure(stats["success_rate"] == 0)

    def test_get_stats_reads_provider_quote_poll_stats(self):  # skipcq
        cache = TestComponentStubs.cache()
        cache.add(
            venue_quote_poll_stats_key("SXBET"),
            encode_venue_quote_poll_stats(
                venue="SXBET",
                updated_at_ns=123,
                cycle_id=4,
                source="rest_order_book_poll",
                subscribed_instrument_count=12,
                market_count=6,
                quote_count=9,
                request_count=5,
                event_request_count=4,
                line_request_count=1,
                pruned_subscription_count=2,
                refilled_subscription_count=1,
                order_count=20,
                empty_market_count=1,
                one_sided_market_count=2,
                two_sided_market_count=3,
                concurrency=4,
                backlog_count=2,
                cycle_elapsed_secs=1.25,
                max_fetch_latency_secs=0.4,
                fetch_latency_p50_secs=0.12,
                fetch_latency_p95_secs=0.32,
                fetch_latency_p99_secs=0.4,
                poll_interval_secs=3.0,
                poll_target_cycle_secs=4.0,
                next_poll_sleep_secs=1.0,
                min_concurrency=2,
                max_concurrency=16,
                adaptive_concurrency=True,
                quote_event_timestamp_source="request_started",
                quote_init_timestamp_source="response_received",
                failure_count=2,
                rate_limit_count=1,
                backoff_secs=1.5,
                last_error="429 rate limit",
            ),
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"SXBET"})),
        )
        strategy.register(
            trader_id=TraderId("TESTER-POLL-STATS"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=cache,
            clock=TestComponentStubs.clock(),
        )

        stats = strategy.get_stats()["provider_quote_poll_stats"]

        ensure(stats["SXBET"]["cycle_id"] == 4)
        ensure(stats["SXBET"]["source"] == "rest_order_book_poll")
        ensure(stats["SXBET"]["market_count"] == 6)
        ensure(stats["SXBET"]["quote_count"] == 9)
        ensure(stats["SXBET"]["request_count"] == 5)
        ensure(stats["SXBET"]["event_request_count"] == 4)
        ensure(stats["SXBET"]["line_request_count"] == 1)
        ensure(stats["SXBET"]["pruned_subscription_count"] == 2)
        ensure(stats["SXBET"]["refilled_subscription_count"] == 1)
        ensure(stats["SXBET"]["backlog_count"] == 2)
        ensure(stats["SXBET"]["max_fetch_latency_secs"] == 0.4)
        ensure(stats["SXBET"]["fetch_latency_p50_secs"] == 0.12)
        ensure(stats["SXBET"]["fetch_latency_p95_secs"] == 0.32)
        ensure(stats["SXBET"]["fetch_latency_p99_secs"] == 0.4)
        ensure(stats["SXBET"]["poll_target_cycle_secs"] == 4.0)
        ensure(stats["SXBET"]["next_poll_sleep_secs"] == 1.0)
        ensure(stats["SXBET"]["min_concurrency"] == 2)
        ensure(stats["SXBET"]["max_concurrency"] == 16)
        ensure(stats["SXBET"]["adaptive_concurrency"] is True)
        ensure(stats["SXBET"]["quote_event_timestamp_source"] == "request_started")
        ensure(stats["SXBET"]["quote_init_timestamp_source"] == "response_received")
        ensure(stats["SXBET"]["failure_count"] == 2)
        ensure(stats["SXBET"]["rate_limit_count"] == 1)
        ensure(stats["SXBET"]["backoff_secs"] == 1.5)
        ensure(stats["SXBET"]["last_error"] == "429 rate limit")

    def test_get_stats_reports_quote_latency_by_venue(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"CLOUDBET", "SXBET"})),
        )

        strategy._record_quote_receive_latency(
            cast(
                Any,
                SimpleNamespace(
                    instrument_id=InstrumentId.from_str("NBA.CLOUDBET"),
                    ts_event=1_000_000_000,
                    ts_init=1_200_000_000,
                ),
            ),
            1_250_000_000,
        )
        strategy._record_quote_receive_latency(
            cast(
                Any,
                SimpleNamespace(
                    instrument_id=InstrumentId.from_str("NBA.SXBET"),
                    ts_event=2_000_000_000,
                    ts_init=2_040_000_000,
                ),
            ),
            2_090_000_000,
        )

        by_venue = strategy.get_stats()["latency_diagnostics"]["by_venue"]

        ensure(by_venue["CLOUDBET"]["quote_event_to_strategy"]["p95_ms"] == 250.0)
        ensure(by_venue["CLOUDBET"]["quote_fetch_latency"]["p95_ms"] == 200.0)
        ensure(by_venue["SXBET"]["quote_event_to_strategy"]["p95_ms"] == 90.0)
        ensure(by_venue["SXBET"]["quote_fetch_latency"]["p95_ms"] == 40.0)

    def test_get_stats_reports_instrument_refresh_by_venue(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"CLOUDBET", "SXBET"})),
        )
        strategy.register(
            trader_id=TraderId("TESTER-REFRESH-STATS"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        strategy._instrument_refresh_requests_by_venue["SXBET"] = 2
        strategy._instrument_refresh_failures_by_venue["SXBET"] = 1
        strategy._instrument_refresh_added_by_venue["SXBET"] = 3
        strategy._instrument_refresh_removed_by_venue["SXBET"] = 1
        strategy._instrument_refresh_delisted_removed_by_venue["SXBET"] = 1
        strategy._instrument_refresh_reconciles_by_venue["SXBET"] = 2
        strategy._instrument_refresh_graph_rebuilds_by_venue["SXBET"] = 2
        strategy._instrument_refresh_stale_triggers_by_venue["SXBET"] = 1
        strategy._quote_unsubscribe_requests_by_venue["SXBET"] = 1

        payload = strategy.get_stats()["instrument_refresh_by_venue"]

        ensure(payload["SXBET"]["requests"] == 2)
        ensure(payload["SXBET"]["failures"] == 1)
        ensure(payload["SXBET"]["added"] == 3)
        ensure(payload["SXBET"]["removed"] == 1)
        ensure(payload["SXBET"]["delisted_removed"] == 1)
        ensure(payload["SXBET"]["reconciles"] == 2)
        ensure(payload["SXBET"]["graph_rebuilds"] == 2)
        ensure(payload["SXBET"]["stale_triggers"] == 1)
        ensure(payload["SXBET"]["quote_unsubscribe_requests"] == 1)

    def test_stale_quote_refresh_triggers_bounded_provider_refresh(self, monkeypatch):  # skipcq
        requested: list[tuple[str, dict[str, object]]] = []

        def fake_request_instruments(
            self: object,
            *,
            venue: Venue,
            params: dict[str, Any] | None = None,
            client_id: object | None = None,
        ) -> None:
            requested.append((venue.value, dict(params or {})))

        monkeypatch.setattr(
            BettingArbitrageStrategy,
            "request_instruments",
            fake_request_instruments,
        )
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({"SXBET", "CLOUDBET"}),
                stale_quote_refresh_cooldown_secs=60.0,
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-STALE-REFRESH"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        sxbet = CryptoBettingInstrument(
            venue=Venue("SXBET"),
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
        )
        cloudbet = CryptoBettingInstrument(
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
        )
        now_ns = strategy.clock.timestamp_ns()

        strategy._maybe_trigger_stale_quote_refresh(
            sxbet,
            cloudbet,
            reason="stale_quote",
            now_ns=now_ns,
        )
        strategy._maybe_trigger_stale_quote_refresh(
            sxbet,
            cloudbet,
            reason="stale_quote",
            now_ns=now_ns + 1,
        )

        ensure(
            requested
            == [
                (
                    "CLOUDBET",
                    {"semantic_refresh": True, "only_last": True, "trigger": "stale_quote"},
                ),
                (
                    "SXBET",
                    {"semantic_refresh": True, "only_last": True, "trigger": "stale_quote"},
                ),
            ],
        )
        stats = strategy.get_stats()
        ensure(stats["instrument_refresh_stale_triggers"] == 2)
        ensure(stats["instrument_refresh_requests"] == 2)

    def test_instrument_refresh_requests_all_enabled_venues(self, monkeypatch):  # skipcq
        requested: list[tuple[str, dict[str, bool]]] = []

        def fake_request_instruments(
            self: object,
            *,
            venue: Venue,
            params: dict[str, Any] | None = None,
            client_id: object | None = None,
        ) -> None:
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
        ensure(stats["instrument_refresh_reconciles"] == 1)
        ensure(stats["quote_unsubscribe_requests"] == 1)

    def test_refresh_schedules_delayed_reconcile_alerts(self, monkeypatch):  # skipcq
        requested: list[tuple[str, dict[str, bool]]] = []

        def fake_request_instruments(
            self: object,
            *,
            venue: Venue,
            params: dict[str, Any] | None = None,
            client_id: object | None = None,
        ) -> None:
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

    def test_quote_odds_converts_polymarket_probability_price(self, default_config):  # skipcq
        strategy = BettingArbitrageStrategy(config=default_config)
        instrument = self._sxbet_instrument(
            event_id="pm-evt-1",
            venue="POLYMARKET",
            outcome="home",
            market_name="basketball.moneyline",
        )
        quote = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=0.42,
            ask_price=0.43,
            bid_size=500,
            ask_size=400,
        )

        ensure(strategy._quote_odds(quote) == Decimal(1) / Decimal("0.43"))
        ensure(strategy._quote_available_size(quote) == Decimal("172.0"))

    def test_order_price_converts_polymarket_decimal_odds_back_to_probability(self):  # skipcq
        polymarket = self._sxbet_instrument(
            event_id="pm-evt-1",
            venue="POLYMARKET",
            outcome="home",
            market_name="basketball.moneyline",
        )
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="home")

        ensure(
            BettingArbitrageStrategy._order_price_for_instrument(
                polymarket,
                Decimal("2.50"),
            )
            == Decimal("0.4"),
        )
        ensure(
            BettingArbitrageStrategy._order_price_for_instrument(sxbet, Decimal("2.50"))
            == Decimal("2.50"),
        )

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
    ) -> None:  # skipcq
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
    ) -> None:  # skipcq
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

    def test_semantic_unmatched_probe_prioritizes_near_term_common_fixture(
        self,
        tmp_path: Path,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET", "SXBET"]),
                opportunity_graph_engine="python",
                semantic_unmatched_quote_probe_venues=frozenset({"POLYMARKET"}),
                semantic_unmatched_quote_probe_limit_per_venue=1,
                max_resolution_horizon_hours=48.0,
            ),
        )
        strategy._matcher.set_rule_store(RuleStore(FileRuleCache(tmp_path / "rules")))
        strategy.subscribe_quote_ticks = Mock()
        now = datetime.now(tz=UTC)
        live_start = (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        stale_start = (now - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        stale_polymarket = self._polymarket_sports_binary_option(
            symbol_value="aaa-stale",
            event_name="Old Team A vs Old Team B",
            home_name="Old Team A",
            away_name="Old Team B",
            start_time=stale_start,
        )
        live_polymarket = self._polymarket_sports_binary_option(
            symbol_value="zzz-live",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            start_time=live_start,
        )
        transformed_stale = strategy._coerce_betting_instrument(stale_polymarket)
        transformed_live = strategy._coerce_betting_instrument(live_polymarket)
        sxbet = self._sxbet_instrument(
            event_id="sxbet-live",
            outcome="home",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            market_name="match_odds",
            start_time=live_start,
            sport_name="basketball",
        )
        assert transformed_stale is not None
        assert transformed_live is not None

        strategy._subscribed_instruments.update({transformed_stale, transformed_live, sxbet})
        strategy._opportunity_graph.build([transformed_stale, transformed_live, sxbet])
        strategy._opportunity_graph.edge_ids_by_node_id = {
            node_id: set() for node_id in strategy._opportunity_graph.nodes_by_id
        }

        subscribed_count = strategy._subscribe_semantic_unmatched_quote_probe_ticks()

        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}
        ensure(subscribed_count == 1)
        ensure(quoted_ids == {live_polymarket.id})

    def test_resolution_horizon_allows_polymarket_date_only_same_day_fixture(
        self,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET"]),
                max_resolution_horizon_hours=48.0,
            ),
        )
        today = datetime.now(tz=UTC).date().isoformat()
        option = self._polymarket_sports_binary_option(start_time=today)
        transformed = strategy._coerce_betting_instrument(option)

        assert transformed is not None
        ensure(strategy._instrument_resolution_horizon_priority(transformed) < 2)
        ensure(strategy._should_process_instrument(transformed) is True)

    def test_semantic_unmatched_probe_precomputes_fixture_aliases(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET", "SXBET"]),
                opportunity_graph_engine="python",
                semantic_unmatched_quote_probe_venues=frozenset({"POLYMARKET"}),
                semantic_unmatched_quote_probe_limit_per_venue=2,
            ),
        )
        strategy._matcher.set_rule_store(RuleStore(FileRuleCache(tmp_path / "rules")))
        strategy.subscribe_quote_ticks = Mock()
        instruments: list[CryptoBettingInstrument] = []
        aliases_by_id: dict[str, set[str]] = {}
        for idx in range(8):
            home = f"Team {idx} A"
            away = f"Team {idx} B"
            polymarket = self._polymarket_sports_binary_option(
                symbol_value=f"pm-{idx}",
                event_name=f"{home} vs {away}",
                home_name=home,
                away_name=away,
            )
            transformed = strategy._coerce_betting_instrument(polymarket)
            sxbet = self._sxbet_instrument(
                event_id=f"sxbet-{idx}",
                outcome="home",
                event_name=f"{home} vs {away}",
                home_name=home,
                away_name=away,
                market_name="match_odds",
                sport_name="basketball",
            )
            assert transformed is not None
            instruments.extend([transformed, sxbet])
            aliases_by_id[str(transformed.id)] = {f"fixture-{idx}"}
            aliases_by_id[str(sxbet.id)] = {f"fixture-{idx}"}

        strategy._subscribed_instruments.update(instruments)
        strategy._opportunity_graph.build(instruments)
        strategy._opportunity_graph.edge_ids_by_node_id = {
            node_id: set() for node_id in strategy._opportunity_graph.nodes_by_id
        }
        alias_probe = Mock(side_effect=lambda instrument: aliases_by_id[str(instrument.id)])
        monkeypatch.setattr(strategy, "_instrument_event_alias_keys", alias_probe)

        subscribed_count = strategy._subscribe_semantic_unmatched_quote_probe_ticks()

        ensure(subscribed_count == 2)
        ensure(alias_probe.call_count == len(instruments))

    def test_semantic_unmatched_probe_limit_ignores_existing_subscriptions(
        self,
        tmp_path: Path,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET"]),
                opportunity_graph_engine="python",
                semantic_unmatched_quote_probe_venues=frozenset({"POLYMARKET"}),
                semantic_unmatched_quote_probe_limit_per_venue=1,
            ),
        )
        strategy._matcher.set_rule_store(RuleStore(FileRuleCache(tmp_path / "rules")))
        strategy.subscribe_quote_ticks = Mock()
        already_quoted = self._polymarket_sports_binary_option(
            symbol_value="aaa-already-quoted",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
        )
        candidate = self._polymarket_sports_binary_option(
            symbol_value="zzz-candidate",
            event_name="Team C vs Team D",
            home_name="Team C",
            away_name="Team D",
        )
        transformed_already = strategy._coerce_betting_instrument(already_quoted)
        transformed_candidate = strategy._coerce_betting_instrument(candidate)
        assert transformed_already is not None
        assert transformed_candidate is not None
        strategy._subscribed_instruments.update({transformed_already, transformed_candidate})
        strategy._opportunity_graph.build([transformed_already, transformed_candidate])
        strategy._opportunity_graph.edge_ids_by_node_id = {
            node_id: set() for node_id in strategy._opportunity_graph.nodes_by_id
        }
        strategy._quote_subscribed_instrument_ids.add(str(already_quoted.id))

        subscribed_count = strategy._subscribe_semantic_unmatched_quote_probe_ticks()

        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}
        ensure(subscribed_count == 1)
        ensure(quoted_ids == {candidate.id})

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

    def test_semantic_connected_quote_subscriptions_respect_venue_limit(
        self,
        tmp_path: Path,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET"]),
                opportunity_graph_engine="python",
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 1},
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

        strategy.subscribe_instruments([over, under])

        ensure(strategy.subscribe_quote_ticks.call_count == 1)
        ensure(strategy.get_stats()["quote_subscribed_instruments"] == 1)

    def test_semantic_connected_quote_subscriptions_prioritize_cross_venue_edges(
        self,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "POLYMARKET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 1, "POLYMARKET": 1},
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        cloudbet_same = self._sxbet_instrument(
            event_id="same-1",
            venue="CLOUDBET",
            outcome="over",
        )
        cloudbet_same_pair = self._sxbet_instrument(
            event_id="same-1",
            venue="CLOUDBET",
            outcome="under",
        )
        cloudbet_cross = self._sxbet_instrument(
            event_id="cross-1",
            venue="CLOUDBET",
            outcome="home",
            market_name="match_odds",
        )
        polymarket_cross = self._sxbet_instrument(
            event_id="cross-1",
            venue="POLYMARKET",
            outcome="away",
            market_name="match_odds",
        )
        graph = cast(Any, strategy._opportunity_graph)
        graph.nodes_by_id = {
            "cloudbet-same": SimpleNamespace(instrument=cloudbet_same),
            "cloudbet-same-pair": SimpleNamespace(instrument=cloudbet_same_pair),
            "cloudbet-cross": SimpleNamespace(instrument=cloudbet_cross),
            "polymarket-cross": SimpleNamespace(instrument=polymarket_cross),
        }
        graph.edges_by_id = {
            "same-edge": SimpleNamespace(
                source_node_id="cloudbet-same",
                target_node_id="cloudbet-same-pair",
                execution_safe=True,
                same_venue_execution_eligible=False,
            ),
            "cross-edge": SimpleNamespace(
                source_node_id="cloudbet-cross",
                target_node_id="polymarket-cross",
                execution_safe=True,
                same_venue_execution_eligible=False,
            ),
        }
        graph.edge_ids_by_node_id = {
            "cloudbet-same": {"same-edge"},
            "cloudbet-same-pair": {"same-edge"},
            "cloudbet-cross": {"cross-edge"},
            "polymarket-cross": {"cross-edge"},
        }

        subscribed = strategy._subscribe_semantic_connected_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        ensure(subscribed == 2)
        ensure(quoted_ids == {cloudbet_cross.id, polymarket_cross.id})

    def test_cross_venue_common_fixture_subscriptions_reserve_quote_slots(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 1, "SXBET": 1},
                semantic_unmatched_quote_probe_limit_per_venue=2,
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        cloudbet_common = self._sxbet_instrument(
            event_id="cb-common",
            venue="CLOUDBET",
            outcome="home",
            event_name="Cleveland Cavaliers v Detroit Pistons",
            home_name="Cleveland Cavaliers",
            away_name="Detroit Pistons",
            market_name="match_odds",
            sport_name="Basketball",
        )
        sxbet_common = self._sxbet_instrument(
            event_id="sx-common",
            venue="SXBET",
            outcome="away",
            event_name="Cleveland vs Detroit",
            home_name="Cleveland",
            away_name="Detroit",
            market_name="match_odds",
            sport_name="Basketball",
        )
        cloudbet_filler = self._sxbet_instrument(
            event_id="cb-filler",
            venue="CLOUDBET",
            outcome="over",
            event_name="Only Cloudbet A v Only Cloudbet B",
            home_name="Only Cloudbet A",
            away_name="Only Cloudbet B",
        )
        sxbet_filler = self._sxbet_instrument(
            event_id="sx-filler",
            venue="SXBET",
            outcome="under",
            event_name="Only SXBET A v Only SXBET B",
            home_name="Only SXBET A",
            away_name="Only SXBET B",
        )
        instruments = {cloudbet_common, sxbet_common, cloudbet_filler, sxbet_filler}
        strategy._subscribed_instruments.update(instruments)
        common_ids = {str(cloudbet_common.id), str(sxbet_common.id)}
        monkeypatch.setattr(
            strategy,
            "_instrument_event_alias_keys",
            lambda instrument: (
                {"basketball:cleveland cavaliers:detroit pistons"}
                if str(instrument.id) in common_ids
                else {str(instrument.id)}
            ),
        )

        subscribed = strategy._subscribe_cross_venue_common_fixture_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        ensure(subscribed == 2)
        ensure(quoted_ids == {cloudbet_common.id, sxbet_common.id})

    def _liquidity_alias_setup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        strategy: BettingArbitrageStrategy,
        alias_by_event_id: dict[str, str],
    ) -> None:
        monkeypatch.setattr(
            strategy,
            "_instrument_event_alias_keys",
            lambda instrument: {alias_by_event_id.get(instrument.event_id, str(instrument.id))},
        )

    def test_cross_venue_common_fixture_quote_prefers_both_deep_fixture(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        # One SX.bet quote slot, two co-listed fixtures competing for it. The deep-on-
        # both fixture must win it over the fixture that is deep on SX.bet but shallow
        # on the co-listed Cloudbet leg -- only the former can actually be arbed.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 2, "SXBET": 1},
                cross_venue_liquidity_priority_enabled=True,
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        sxbet_deep = self._sxbet_instrument(
            event_id="sx-deep",
            venue="SXBET",
            outcome="home",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        sxbet_shallow = self._sxbet_instrument(
            event_id="sx-shallow",
            venue="SXBET",
            outcome="home",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        cloudbet_deep = self._sxbet_instrument(
            event_id="cb-deep",
            venue="CLOUDBET",
            outcome="away",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        cloudbet_shallow = self._sxbet_instrument(
            event_id="cb-shallow",
            venue="CLOUDBET",
            outcome="away",
            market_name="match_odds",
            info={"liquidity_depth": 1.0},
        )
        strategy._subscribed_instruments.update(
            {sxbet_deep, sxbet_shallow, cloudbet_deep, cloudbet_shallow},
        )
        self._liquidity_alias_setup(
            monkeypatch,
            strategy,
            {
                "sx-deep": "fx-deep",
                "cb-deep": "fx-deep",
                "sx-shallow": "fx-shallow",
                "cb-shallow": "fx-shallow",
            },
        )

        strategy._subscribe_cross_venue_common_fixture_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        ensure(sxbet_deep.id in quoted_ids)
        ensure(sxbet_shallow.id not in quoted_ids)

    def test_cross_venue_common_fixture_quote_rotates_with_depth_shift(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        # Same topology, but the co-listed depth has rotated onto the other fixture
        # (e.g. a new match-day slate). The single SX.bet slot must follow it.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 2, "SXBET": 1},
                cross_venue_liquidity_priority_enabled=True,
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        sxbet_a = self._sxbet_instrument(
            event_id="sx-a",
            venue="SXBET",
            outcome="home",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        sxbet_b = self._sxbet_instrument(
            event_id="sx-b",
            venue="SXBET",
            outcome="home",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        cloudbet_a = self._sxbet_instrument(
            event_id="cb-a",
            venue="CLOUDBET",
            outcome="away",
            market_name="match_odds",
            info={"liquidity_depth": 2.0},
        )
        cloudbet_b = self._sxbet_instrument(
            event_id="cb-b",
            venue="CLOUDBET",
            outcome="away",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        strategy._subscribed_instruments.update({sxbet_a, sxbet_b, cloudbet_a, cloudbet_b})
        self._liquidity_alias_setup(
            monkeypatch,
            strategy,
            {"sx-a": "fx-a", "cb-a": "fx-a", "sx-b": "fx-b", "cb-b": "fx-b"},
        )

        strategy._subscribe_cross_venue_common_fixture_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        # fx-b is the deep-on-both fixture now, so its SX.bet leg takes the slot.
        ensure(sxbet_b.id in quoted_ids)
        ensure(sxbet_a.id not in quoted_ids)

    def test_cross_venue_liquidity_priority_disabled_is_depth_neutral(self) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
            ),
        )
        instrument = self._sxbet_instrument(
            event_id="sx-1",
            venue="SXBET",
            outcome="home",
            info={"liquidity_depth": 500.0},
        )
        alias_keys = {str(instrument.id): {"fx"}}
        alias_venues = {"fx": {"SXBET", "CLOUDBET"}}
        alias_depth = strategy._cross_venue_alias_venue_depth(
            [instrument],
            alias_keys_by_instrument_id=alias_keys,
        )

        priority = strategy._cross_venue_common_fixture_quote_priority(
            instrument,
            alias_keys_by_instrument_id=alias_keys,
            alias_venues_by_key=alias_venues,
            alias_venue_depth=alias_depth,
        )

        # Depth feature off: the alias->venue depth index is empty and the depth term
        # in the priority tuple is a constant, so ordering is unchanged from before.
        ensure(alias_depth == {})
        ensure(priority[1] == 0.0)

    def test_cross_venue_common_fixture_quote_gates_shallow_venue_leg(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        # Per-venue min-depth floor drops a co-listed but shallow SX.bet leg, and an
        # instrument with no depth metadata at all is treated as zero depth (no crash).
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 3, "SXBET": 3},
                min_quote_depth_by_venue={"SXBET": 50.0},
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        sxbet_deep = self._sxbet_instrument(
            event_id="sx-deep",
            venue="SXBET",
            outcome="home",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        sxbet_shallow = self._sxbet_instrument(
            event_id="sx-shallow",
            venue="SXBET",
            outcome="home",
            market_name="match_odds",
            info={"liquidity_depth": 10.0},
        )
        sxbet_unknown = self._sxbet_instrument(
            event_id="sx-unknown",
            venue="SXBET",
            outcome="home",
            market_name="match_odds",
        )
        cloudbet_common = self._sxbet_instrument(
            event_id="cb-common",
            venue="CLOUDBET",
            outcome="away",
            market_name="match_odds",
            info={"liquidity_depth": 100.0},
        )
        strategy._subscribed_instruments.update(
            {sxbet_deep, sxbet_shallow, sxbet_unknown, cloudbet_common},
        )
        self._liquidity_alias_setup(
            monkeypatch,
            strategy,
            {
                "sx-deep": "fx",
                "sx-shallow": "fx",
                "sx-unknown": "fx",
                "cb-common": "fx",
            },
        )

        strategy._subscribe_cross_venue_common_fixture_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        ensure(sxbet_deep.id in quoted_ids)
        ensure(sxbet_shallow.id not in quoted_ids)
        ensure(sxbet_unknown.id not in quoted_ids)

    def test_refreshed_active_instruments_ordered_by_depth_when_enabled(self) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                cross_venue_liquidity_priority_enabled=True,
            ),
        )
        shallow = self._sxbet_instrument(
            event_id="sx-shallow",
            venue="SXBET",
            outcome="home",
            info={"liquidity_depth": 5.0},
        )
        deep = self._sxbet_instrument(
            event_id="sx-deep",
            venue="SXBET",
            outcome="home",
            info={"liquidity_depth": 500.0},
        )
        unknown = self._sxbet_instrument(
            event_id="sx-unknown",
            venue="SXBET",
            outcome="home",
        )

        added = strategy._add_refreshed_active_instruments([shallow, deep, unknown])

        # Rotation feeds the deepest instruments into the subscription passes first;
        # the depth-less instrument reads as zero depth and sorts last, no crash.
        ensure(
            [instrument.event_id for instrument in added]
            == ["sx-deep", "sx-shallow", "sx-unknown"],
        )

    def test_refreshed_active_instruments_preserve_order_when_disabled(self) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )
        shallow = self._sxbet_instrument(
            event_id="sx-shallow",
            venue="SXBET",
            outcome="home",
            info={"liquidity_depth": 5.0},
        )
        deep = self._sxbet_instrument(
            event_id="sx-deep",
            venue="SXBET",
            outcome="home",
            info={"liquidity_depth": 500.0},
        )

        added = strategy._add_refreshed_active_instruments([shallow, deep])

        # Feature off: arrival order preserved (no depth reordering).
        ensure([instrument.event_id for instrument in added] == ["sx-shallow", "sx-deep"])

    def test_cross_venue_common_fixture_quote_slots_prefer_compatible_families(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 1, "SXBET": 1},
                semantic_unmatched_quote_probe_limit_per_venue=2,
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        cloudbet_scope_mismatch = self._sxbet_instrument(
            event_id="cb-scope",
            venue="CLOUDBET",
            outcome="no",
            market_name="basketball.team_to_lead_by_points",
            params="point=22&team=away",
            sport_name="Basketball",
        )
        cloudbet_match_odds = self._sxbet_instrument(
            event_id="cb-match",
            venue="CLOUDBET",
            outcome="home",
            market_name="match_odds",
            sport_name="Basketball",
        )
        sxbet_match_odds = self._sxbet_instrument(
            event_id="sx-match",
            venue="SXBET",
            outcome="away",
            market_name="match_odds",
            sport_name="Basketball",
        )
        instruments = {cloudbet_scope_mismatch, cloudbet_match_odds, sxbet_match_odds}
        strategy._subscribed_instruments.update(instruments)
        monkeypatch.setattr(
            strategy,
            "_instrument_event_alias_keys",
            lambda instrument: {"basketball:cleveland cavaliers:detroit pistons"},
        )

        subscribed = strategy._subscribe_cross_venue_common_fixture_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        ensure(subscribed == 2)
        ensure(cloudbet_match_odds.id in quoted_ids)
        ensure(sxbet_match_odds.id in quoted_ids)
        ensure(cloudbet_scope_mismatch.id not in quoted_ids)

    def test_cross_venue_edge_legs_subscribed_without_fixture_alias_match(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        # The graph's rule-matcher formed a cross-venue edge, but the alias index sees
        # no cross-venue fixture overlap (different alias normalization). Both edge
        # legs must still be reserved ahead of the same-venue bucket.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 2, "SXBET": 2},
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        cloudbet_leg = self._sxbet_instrument(
            event_id="cb-edge",
            venue="CLOUDBET",
            outcome="home",
            event_name="CLE Cavaliers v DET Pistons",
            home_name="CLE Cavaliers",
            away_name="DET Pistons",
            market_name="match_odds",
            sport_name="Basketball",
        )
        sxbet_leg = self._sxbet_instrument(
            event_id="sx-edge",
            venue="SXBET",
            outcome="away",
            event_name="Cleveland Cavaliers vs Detroit Pistons",
            home_name="Cleveland Cavaliers",
            away_name="Detroit Pistons",
            market_name="match_odds",
            sport_name="Basketball",
        )
        strategy._subscribed_instruments.update({cloudbet_leg, sxbet_leg})
        graph = strategy._opportunity_graph
        for instrument in (cloudbet_leg, sxbet_leg):
            node = graph._node_from_instrument(instrument)
            graph.nodes_by_id[node.node_id] = node
            graph.edge_ids_by_node_id[node.node_id] = set()
        edge_id = graph._edge_id(str(cloudbet_leg.id), str(sxbet_leg.id))
        graph.edges_by_id[edge_id] = OpportunityEdge(
            edge_id=edge_id,
            source_node_id=str(cloudbet_leg.id),
            target_node_id=str(sxbet_leg.id),
            hedge_type="same_market",
            confidence=1.0,
            same_venue=False,
            market_relationship_type="same_market",
            push_capable=False,
            execution_safe=False,
            safety_tier="TOPOLOGY_SAFE",
            caveats=("cross_venue_topology_only",),
        )
        graph.edge_ids_by_node_id[str(cloudbet_leg.id)].add(edge_id)
        graph.edge_ids_by_node_id[str(sxbet_leg.id)].add(edge_id)
        monkeypatch.setattr(
            strategy,
            "_instrument_event_alias_keys",
            lambda instrument: {str(instrument.id)},
        )

        subscribed = strategy._subscribe_cross_venue_common_fixture_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        ensure(subscribed == 2)
        ensure(quoted_ids == {cloudbet_leg.id, sxbet_leg.id})

    def _common_fixture_reserve_strategy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        reserve_limit: int | None,
    ) -> BettingArbitrageStrategy:
        # Two common fixtures per venue with a tiny probe limit: the reserve must be
        # sized independently of semantic_unmatched_quote_probe_limit_per_venue (#227).
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 3, "SXBET": 3},
                semantic_unmatched_quote_probe_limit_per_venue=1,
                cross_venue_common_fixture_quote_reserve_limit_per_venue=reserve_limit,
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        alias_by_instrument_id: dict[str, set[str]] = {}
        for fixture, (home, away) in {
            "cavs-pistons": ("Cleveland Cavaliers", "Detroit Pistons"),
            "lakers-celtics": ("Los Angeles Lakers", "Boston Celtics"),
        }.items():
            for venue in ("CLOUDBET", "SXBET"):
                instrument = self._sxbet_instrument(
                    event_id=f"{venue.lower()}-{fixture}",
                    venue=venue,
                    outcome="home" if venue == "CLOUDBET" else "away",
                    event_name=f"{home} v {away}",
                    home_name=home,
                    away_name=away,
                    market_name="match_odds",
                    sport_name="Basketball",
                )
                strategy._subscribed_instruments.add(instrument)
                alias_by_instrument_id[str(instrument.id)] = {f"basketball:{fixture}"}
        monkeypatch.setattr(
            strategy,
            "_instrument_event_alias_keys",
            lambda instrument: alias_by_instrument_id[str(instrument.id)],
        )
        return strategy

    def test_cross_venue_common_fixture_reserve_not_capped_by_probe_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        strategy = self._common_fixture_reserve_strategy(monkeypatch, reserve_limit=None)

        subscribed = strategy._subscribe_cross_venue_common_fixture_quote_ticks()

        ensure(subscribed == 4)

    def test_cross_venue_common_fixture_reserve_explicit_limit_caps_per_venue(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        strategy = self._common_fixture_reserve_strategy(monkeypatch, reserve_limit=1)

        subscribed = strategy._subscribe_cross_venue_common_fixture_quote_ticks()

        ensure(subscribed == 2)

    def test_cross_venue_common_fixture_reserve_zero_disables_pass(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # skipcq
        strategy = self._common_fixture_reserve_strategy(monkeypatch, reserve_limit=0)

        subscribed = strategy._subscribe_cross_venue_common_fixture_quote_ticks()

        ensure(subscribed == 0)
        ensure(strategy.subscribe_quote_ticks.call_args_list == [])

    def test_cross_venue_common_fixture_reserve_limit_rejects_negative(self) -> None:  # skipcq
        with pytest.raises(
            ValueError,
            match="cross_venue_common_fixture_quote_reserve_limit_per_venue",
        ):
            BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                cross_venue_common_fixture_quote_reserve_limit_per_venue=-1,
            )

    def test_resolution_horizon_priority_demotes_stale_past_fixtures(self) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(max_resolution_horizon_hours=48.0),
        )
        now = datetime.now(tz=UTC)

        def node_for(start_time):
            return SimpleNamespace(
                instrument=SimpleNamespace(parsed_start_time=lambda: start_time),
            )

        ensure(strategy._resolution_horizon_priority(node_for(now + timedelta(hours=2))) == -1)
        ensure(strategy._resolution_horizon_priority(node_for(now - timedelta(hours=1))) == 0)
        ensure(strategy._resolution_horizon_priority(node_for(None)) == 1)
        ensure(strategy._resolution_horizon_priority(node_for(now + timedelta(days=5))) == 2)
        ensure(strategy._resolution_horizon_priority(node_for(now - timedelta(days=2))) == 3)

    def test_should_process_instrument_filters_stale_and_far_horizon(self) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(max_resolution_horizon_hours=48.0),
        )
        now = datetime.now(tz=UTC)

        fresh = self._sxbet_instrument(
            event_id="fresh",
            outcome="home",
            start_time=(now + timedelta(hours=2)).isoformat(),
        )
        recent_past = self._sxbet_instrument(
            event_id="recent",
            outcome="home",
            start_time=(now - timedelta(hours=1)).isoformat(),
        )
        stale = self._sxbet_instrument(
            event_id="stale",
            outcome="home",
            start_time=(now - timedelta(days=2)).isoformat(),
        )
        far = self._sxbet_instrument(
            event_id="far",
            outcome="home",
            start_time=(now + timedelta(days=5)).isoformat(),
        )

        ensure(strategy._should_process_instrument(fresh) is True)
        ensure(strategy._should_process_instrument(recent_past) is True)
        ensure(strategy._should_process_instrument(stale) is False)
        ensure(strategy._should_process_instrument(far) is False)

    def test_semantic_quote_subscriptions_skip_out_of_horizon_fixtures(self) -> None:  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET", "SXBET"]),
                max_resolution_horizon_hours=48.0,
            ),
        )
        strategy.subscribe_quote_ticks = Mock()
        now = datetime.now(tz=UTC)
        fresh_start = (now + timedelta(hours=2)).isoformat()
        stale_start = (now - timedelta(days=2)).isoformat()
        far_start = (now + timedelta(days=5)).isoformat()

        fresh_pm = self._sxbet_instrument(
            event_id="fresh",
            venue="POLYMARKET",
            outcome="home",
            market_name="match_odds",
            start_time=fresh_start,
        )
        fresh_sx = self._sxbet_instrument(
            event_id="fresh",
            venue="SXBET",
            outcome="away",
            market_name="match_odds",
            start_time=fresh_start,
        )
        stale_pm = self._sxbet_instrument(
            event_id="stale",
            venue="POLYMARKET",
            outcome="home",
            market_name="match_odds",
            start_time=stale_start,
        )
        stale_sx = self._sxbet_instrument(
            event_id="stale",
            venue="SXBET",
            outcome="away",
            market_name="match_odds",
            start_time=stale_start,
        )
        far_pm = self._sxbet_instrument(
            event_id="far",
            venue="POLYMARKET",
            outcome="home",
            market_name="match_odds",
            start_time=far_start,
        )
        far_sx = self._sxbet_instrument(
            event_id="far",
            venue="SXBET",
            outcome="away",
            market_name="match_odds",
            start_time=far_start,
        )
        graph = cast(Any, strategy._opportunity_graph)
        graph.nodes_by_id = {
            "fresh-pm": SimpleNamespace(instrument=fresh_pm),
            "fresh-sx": SimpleNamespace(instrument=fresh_sx),
            "stale-pm": SimpleNamespace(instrument=stale_pm),
            "stale-sx": SimpleNamespace(instrument=stale_sx),
            "far-pm": SimpleNamespace(instrument=far_pm),
            "far-sx": SimpleNamespace(instrument=far_sx),
        }
        graph.edges_by_id = {
            "fresh-edge": SimpleNamespace(
                source_node_id="fresh-pm",
                target_node_id="fresh-sx",
                execution_safe=True,
                same_venue_execution_eligible=False,
            ),
            "stale-edge": SimpleNamespace(
                source_node_id="stale-pm",
                target_node_id="stale-sx",
                execution_safe=True,
                same_venue_execution_eligible=False,
            ),
            "far-edge": SimpleNamespace(
                source_node_id="far-pm",
                target_node_id="far-sx",
                execution_safe=True,
                same_venue_execution_eligible=False,
            ),
        }
        graph.edge_ids_by_node_id = {
            "fresh-pm": {"fresh-edge"},
            "fresh-sx": {"fresh-edge"},
            "stale-pm": {"stale-edge"},
            "stale-sx": {"stale-edge"},
            "far-pm": {"far-edge"},
            "far-sx": {"far-edge"},
        }

        subscribed = strategy._subscribe_semantic_connected_quote_ticks()
        quoted_ids = {call.args[0] for call in strategy.subscribe_quote_ticks.call_args_list}

        ensure(subscribed == 2)
        ensure(quoted_ids == {fresh_pm.id, fresh_sx.id})

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
        ensure(live_strategy.live_quote_age_slo_secs == 5.0)

    def test_live_quote_age_slo_must_be_positive(self):  # skipcq
        with pytest.raises(ValueError, match="live_quote_age_slo_secs must be positive"):
            BettingArbitrageConfig(live_quote_age_slo_secs=0.0)

    def test_latency_summary_reports_percentiles(self):  # skipcq
        samples: list[int] = []
        BettingArbitrageStrategy._record_latency_sample(samples, 1_000_000)
        BettingArbitrageStrategy._record_latency_sample(samples, 2_000_000)
        BettingArbitrageStrategy._record_latency_sample(samples, 3_000_000)

        summary = BettingArbitrageStrategy._latency_summary(samples)

        ensure(summary["count"] == 3)
        ensure(summary["p50_ms"] == 2.0)
        ensure(summary["p95_ms"] == 2.0)
        ensure(summary["max_ms"] == 3.0)

    def test_quote_receive_latency_records_event_and_publish_stages(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )

        strategy._record_quote_receive_latency(
            Mock(ts_event=1_000_000_000, ts_init=1_200_000_000),
            1_500_000_000,
        )

        stats = strategy.get_stats()
        ensure(stats["latency_diagnostics"]["quote_event_to_strategy"]["p50_ms"] == 500.0)
        ensure(stats["latency_diagnostics"]["quote_publish_to_strategy"]["p50_ms"] == 300.0)

    def test_polymarket_provider_timestamp_does_not_count_as_fetch_latency(self):  # skipcq
        polymarket_quote = Mock(
            instrument_id=InstrumentId(Symbol("market-token"), Venue("POLYMARKET")),
            ts_event=1_000_000_000,
            ts_init=11_000_000_000,
        )
        sxbet_quote = Mock(
            instrument_id=InstrumentId(Symbol("market-token"), Venue("SXBET")),
            ts_event=1_000_000_000,
            ts_init=11_000_000_000,
        )

        ensure(BettingArbitrageStrategy.quote_fetch_latency_secs(polymarket_quote) == 0.0)
        ensure(BettingArbitrageStrategy.quote_fetch_latency_secs(sxbet_quote) == 10.0)

    def test_polymarket_decision_freshness_uses_receive_timestamp(self):  # skipcq
        polymarket_quote = Mock(
            instrument_id=InstrumentId(Symbol("market-token"), Venue("POLYMARKET")),
            ts_event=1_000_000_000,
            ts_init=11_000_000_000,
        )
        sxbet_quote = Mock(
            instrument_id=InstrumentId(Symbol("market-token"), Venue("SXBET")),
            ts_event=10_500_000_000,
            ts_init=10_700_000_000,
        )

        ensure(BettingArbitrageStrategy.quote_age_secs(12_000_000_000, polymarket_quote) == 1.0)
        ensure(BettingArbitrageStrategy.quote_age_secs(12_000_000_000, sxbet_quote) == 1.5)
        ensure(
            BettingArbitrageStrategy._quote_pair_skew_secs(polymarket_quote, sxbet_quote) == 0.5,
        )

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

    def test_prune_inactive_pairs_survives_concurrent_mutation(self):  # skipcq
        """
        Prune must iterate a snapshot so a concurrent add during iteration cannot
        raise RuntimeError: dictionary changed size during iteration.
        """
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                duplicate_suppression_cooldown_secs=1.0,
            ),
        )

        pairs: dict[str, object] = {}

        class _MutatingState:
            last_opportunity_id = "pair-old"
            last_accepted_ns = 0

            @property
            def last_seen_ns(self) -> int:  # skipcq
                # Reading a field mid-iteration emulates the quote path inserting a
                # new pair while prune walks the mapping. Over a live dict view this
                # raises RuntimeError; over a list() snapshot it is safe.
                pairs.setdefault(
                    f"pair-injected-{len(pairs)}",
                    OpportunityPairState(
                        last_opportunity_id="pair-injected",
                        last_accepted_ns=0,
                        last_seen_ns=0,
                    ),
                )
                return 0

        pairs["pair-old"] = _MutatingState()
        strategy._active_opportunity_pairs = pairs

        strategy._prune_inactive_opportunity_pairs(5_000_000_000)

        ensure("pair-old" not in strategy._active_opportunity_pairs)

    def test_changed_price_pair_can_reenter_after_cooldown_while_active(self):  # skipcq
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
                "pair-a|same_market|2.2:2.3",
                1_500_000_000,
            )
            is True,
        )
        ensure(
            strategy._is_duplicate_opportunity_pair(
                "pair-a",
                "pair-a|same_market|2.2:2.3",
                2_200_000_000,
            )
            is False,
        )
        ensure("pair-a" in strategy._active_opportunity_pairs)

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
                # A fee-less pair is the case that must not materialize; zero out the
                # SXBET net-winnings default so the fast path stays fee-free (#233).
                venue_winning_profit_fee_rates={"SXBET": "0"},
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
                execution_approval_mode="auto",
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
                execution_approval_mode="auto",
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
        strategy._live_execution_block_reasons_for = Mock(return_value=[])
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

    def test_live_execution_rejection_halts_further_submission(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
            ),
        )
        rejected_event = SimpleNamespace(
            instrument_id=SimpleNamespace(venue=Venue("SXBET")),
        )

        strategy.on_order_rejected(rejected_event)

        stats = strategy.get_stats()["live_execution"]
        ensure(stats["halt_reason"] == "order_rejected")
        ensure(stats["unhedged_exposures"] == 1)
        ensure(stats["block_reasons"]["order_rejected"] == 1)

    def test_live_execution_blocks_without_manifest_and_env_arming(self):  # skipcq
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
            ),
        )
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            venue="CLOUDBET",
            outcome="over",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure("manifest_not_live_armed" in reasons)
        ensure("env_not_live_armed" in reasons)
        ensure("no_semantic_edge" in reasons)

    def test_live_execution_allows_strict_cross_venue_when_armed(self, monkeypatch):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
            ),
        )
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            venue="CLOUDBET",
            outcome="over",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
        )
        edge_id = strategy._canonical_pair_id(cloudbet, sxbet)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=True,
            same_venue_execution_eligible=False,
            caveats=(),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure(reasons == [])

    def test_live_execution_blocks_cross_currency_without_explicit_policy(
        self,
        monkeypatch,
    ):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
            ),
        )
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            venue="CLOUDBET",
            outcome="over",
            currency="PLAY_EUR",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
            currency="USDC",
        )
        edge_id = strategy._canonical_pair_id(cloudbet, sxbet)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=True,
            same_venue_execution_eligible=False,
            caveats=(),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure("sandbox_currency_not_live_settlement" in reasons)

    def test_live_execution_allows_stablecoin_cross_currency_with_usd_policy(
        self,
        monkeypatch,
    ):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["POLYMARKET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                execution_venue_mode="cross_venue",
            ),
        )
        polymarket = self._sxbet_instrument(
            event_id="event-1",
            venue="POLYMARKET",
            outcome="over",
            currency="USDT",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
            currency="USDC",
        )
        edge_id = strategy._canonical_pair_id(polymarket, sxbet)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=True,
            same_venue_execution_eligible=False,
            caveats=(),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=polymarket,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure(reasons == [])

    def test_live_execution_allows_eur_when_configured_fx_rate_exists(
        self,
        monkeypatch,
    ):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                execution_venue_mode="cross_venue",
                configured_fx_rates={"EUR/USD": Decimal("1.09")},
            ),
        )
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            venue="CLOUDBET",
            outcome="over",
            currency="EUR",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
            currency="USDC",
        )
        edge_id = strategy._canonical_pair_id(cloudbet, sxbet)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=True,
            same_venue_execution_eligible=False,
            caveats=(),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure("cross_currency_live_execution_blocked" not in reasons)
        ensure("missing_fx_rate" not in reasons)

    def test_phantom_cross_currency_edge_reported_non_positive_after_fx(self):  # skipcq
        # Single-currency arb math shows +0.8% on 2.016/2.016, but the legs settle in EUR and
        # USDC. Net of a realistic FX round-trip (50 bps effective spread) the edge inverts to
        # negative, so the fee-adjusted opportunity must report a non-positive post-FX margin.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                execution_venue_mode="cross_venue",
                configured_fx_rates={"EUR/USD": Decimal("1.10")},
                stablecoin_haircut_bps=50,
                # Zero the SXBET winning-profit fee so this asserts the FX effect in isolation.
                venue_winning_profit_fee_rates={"SXBET": Decimal(0)},
                min_profit_margin=Decimal("0.001"),
            ),
        )
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            venue="CLOUDBET",
            outcome="over",
            currency="EUR",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
            currency="USDC",
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal(1) / Decimal("2.016"),
            probability_b=Decimal(1) / Decimal("2.016"),
            total_probability=Decimal(2) / Decimal("2.016"),
            profit_margin=Decimal("0.008"),
            odds_a=Decimal("2.016"),
            odds_b=Decimal("2.016"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        adjusted = strategy.fee_adjusted_opportunity(opportunity)

        # Single-currency margin clears the +0.1% floor, so the naive gate would accept it.
        ensure(adjusted.raw_profit_margin == Decimal("0.008"))
        ensure(adjusted.raw_profit_margin > strategy._config.min_profit_margin)
        # Post-FX the edge is inverted and the pair is rejected.
        ensure(adjusted.profit_margin < 0)
        ensure(adjusted.profit_margin.quantize(Decimal("0.000001")) == Decimal("-0.002030"))
        ensure(adjusted.profit_margin < strategy._config.min_profit_margin)

    def test_cross_currency_missing_rate_blocks_even_with_flag(self):  # skipcq
        # allow_cross_currency_live_execution must NOT bypass the convertibility gate: a
        # missing EUR/USD rate blocks the pair rather than falling back to a raw 1:1 split.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                execution_venue_mode="cross_venue",
                allow_cross_currency_live_execution=True,
            ),
        )
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            venue="CLOUDBET",
            outcome="over",
            currency="EUR",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
            currency="USDC",
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_currency_block_reasons(opportunity)

        ensure(reasons == ["cross_currency_live_execution_blocked"])

    def _stablecoin_gate_opportunity(
        self,
        *,
        currency_a: str,
        currency_b: str,
        is_same_venue: bool,
        venue_a: str = "CLOUDBET",
        venue_b: str = "SXBET",
    ) -> ArbitrageOpportunity:
        instrument_a = self._sxbet_instrument(
            event_id="event-1",
            venue=venue_a,
            outcome="over",
            currency=currency_a,
        )
        instrument_b = self._sxbet_instrument(
            event_id="event-1",
            venue=venue_b,
            outcome="under",
            currency=currency_b,
        )
        return ArbitrageOpportunity(
            instrument_a=instrument_a,
            instrument_b=instrument_b,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=is_same_venue,
            match_type="same_venue" if is_same_venue else "cross_venue",
        )

    def test_require_same_stablecoin_blocks_cross_venue_fiat_even_with_fx_rate(self):  # skipcq
        # (f) require_same_stablecoin_settlement must reject a cross-venue USDC<->EUR pair even
        # when a configured FX rate would otherwise make it convertible, because the hard gate
        # is enforced BEFORE the FX-rate allowance.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                execution_venue_mode="cross_venue",
                configured_fx_rates={"EUR/USD": Decimal("1.09")},
                require_same_stablecoin_settlement=True,
            ),
        )
        opportunity = self._stablecoin_gate_opportunity(
            currency_a="EUR",
            currency_b="USDC",
            is_same_venue=False,
        )

        reasons = strategy._live_execution_currency_block_reasons(opportunity)

        ensure(reasons == ["cross_venue_requires_same_stablecoin"])

    def test_require_same_stablecoin_allows_cross_venue_same_stablecoin(self):  # skipcq
        # (g) both legs in the SAME stablecoin (USDC<->USDC) cross-venue is the one allowed shape.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                execution_venue_mode="cross_venue",
                require_same_stablecoin_settlement=True,
            ),
        )
        opportunity = self._stablecoin_gate_opportunity(
            currency_a="USDC",
            currency_b="USDC",
            is_same_venue=False,
        )

        ensure(strategy._live_execution_currency_block_reasons(opportunity) == [])

    def test_require_same_stablecoin_leaves_same_venue_unaffected(self):  # skipcq
        # (h) same-venue pairs carry no cross-currency exposure between the legs, so the gate
        # never applies; a same-venue same-currency pair stays allowed.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                require_same_stablecoin_settlement=True,
            ),
        )
        opportunity = self._stablecoin_gate_opportunity(
            currency_a="USDC",
            currency_b="USDC",
            is_same_venue=True,
            venue_a="SXBET",
            venue_b="SXBET",
        )

        ensure(strategy._live_execution_currency_block_reasons(opportunity) == [])

    def test_require_same_stablecoin_default_off_preserves_fx_allowance(self):  # skipcq
        # (i) with the flag off (default) the cross-venue USDC<->EUR pair keeps the existing
        # convertible-with-FX behavior and is NOT blocked by the new reason.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                execution_venue_mode="cross_venue",
                configured_fx_rates={"EUR/USD": Decimal("1.09")},
            ),
        )
        opportunity = self._stablecoin_gate_opportunity(
            currency_a="EUR",
            currency_b="USDC",
            is_same_venue=False,
        )

        reasons = strategy._live_execution_currency_block_reasons(opportunity)

        ensure("cross_venue_requires_same_stablecoin" not in reasons)
        ensure(reasons == [])

    def test_require_same_stablecoin_blocks_two_different_stablecoins(self):  # skipcq
        # (j) USDT<->USDC are both stablecoins but NOT the same one, so a cross-venue pair is
        # still blocked: the gate demands identical settlement stablecoins on both legs.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                execution_venue_mode="cross_venue",
                require_same_stablecoin_settlement=True,
            ),
        )
        opportunity = self._stablecoin_gate_opportunity(
            currency_a="USDT",
            currency_b="USDC",
            is_same_venue=False,
        )

        ensure(
            strategy._live_execution_currency_block_reasons(opportunity)
            == ["cross_venue_requires_same_stablecoin"],
        )

    def test_same_currency_pair_unchanged_by_fx_adjustment(self):  # skipcq
        # USDC/USDC cross-venue is the stablecoin-first path: no FX exposure between the legs,
        # so the FX-net recomputation is skipped and sizing matches the single-currency solver.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                execution_venue_mode="cross_venue",
            ),
        )
        cloudbet = self._sxbet_instrument(
            event_id="event-1",
            venue="CLOUDBET",
            outcome="over",
            currency="USDC",
        )
        sxbet = self._sxbet_instrument(
            event_id="event-1",
            venue="SXBET",
            outcome="under",
            currency="USDC",
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal(1) / Decimal("2.10"),
            probability_b=Decimal(1) / Decimal("2.10"),
            total_probability=Decimal(2) / Decimal("2.10"),
            profit_margin=Decimal("0.05"),
            odds_a=Decimal("2.10"),
            odds_b=Decimal("2.10"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        # No FX exposure -> FX-net recomputation is a no-op.
        ensure(
            strategy._fx_net_profit_margin(opportunity, Decimal("2.10"), Decimal("2.10")) is None,
        )
        # Sizing matches the single-currency solver exactly (no cross-currency skew).
        sized = strategy._sized_arbitrage_stakes(opportunity, total_stake=Decimal(1000))
        baseline = calculate_arbitrage_stakes(
            odds_a=Decimal("2.10"),
            odds_b=Decimal("2.10"),
            total_stake=Decimal(1000),
        )
        ensure(sized == baseline)
        ensure(sized == (Decimal("500.00"), Decimal("500.00"), Decimal("50.00")))

    def test_cross_venue_execution_mode_blocks_same_venue_candidates(self, monkeypatch):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                execution_venue_mode="cross_venue",
            ),
        )
        over = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="over")
        under = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        edge_id = strategy._canonical_pair_id(over, under)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=False,
            same_venue_execution_eligible=True,
            caveats=(),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=over,
            instrument_b=under,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=True,
            match_type="same_market",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure("cross_venue_execution_only" in reasons)

    def test_same_venue_execution_mode_blocks_cross_venue_candidates(self, monkeypatch):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                execution_venue_mode="same_venue",
                allow_same_venue_live_execution=True,
            ),
        )
        cloudbet = self._sxbet_instrument(event_id="event-1", venue="CLOUDBET", outcome="over")
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        edge_id = strategy._canonical_pair_id(cloudbet, sxbet)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=True,
            same_venue_execution_eligible=False,
            caveats=(),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure("same_venue_execution_only" in reasons)

    def test_live_execution_allows_same_venue_only_with_policy_edge(self, monkeypatch):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                allow_same_venue_live_execution=True,
            ),
        )
        over = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="over")
        under = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        edge_id = strategy._canonical_pair_id(over, under)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=False,
            same_venue_execution_eligible=True,
            caveats=("same_venue_risk_engine_elevation_required",),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=over,
            instrument_b=under,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=True,
            match_type="same_market",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure(reasons == [])

    def test_live_execution_blocks_same_venue_fixture_identity_mismatch(
        self,
        monkeypatch,
    ):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                allow_same_venue_live_execution=True,
            ),
        )
        over = self._sxbet_instrument(event_id="fixture-a", venue="SXBET", outcome="over")
        under = self._sxbet_instrument(event_id="fixture-b", venue="SXBET", outcome="under")
        edge_id = strategy._canonical_pair_id(over, under)
        strategy.opportunity_graph.edges_by_id[edge_id] = SimpleNamespace(
            execution_safe=False,
            same_venue_execution_eligible=True,
            caveats=("same_venue_risk_engine_elevation_required",),
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=over,
            instrument_b=under,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=True,
            match_type="same_market",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(5),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure(reasons == ["semantic_execution_policy_blocked"])

    def test_live_execution_refreshes_final_quote_odds_before_submit(self, monkeypatch):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                quote_freshness_profile="live",
                max_total_stake=Decimal(25),
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-000"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        cloudbet = self._sxbet_instrument(event_id="event-1", venue="CLOUDBET", outcome="over")
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        now_ns = strategy.clock.timestamp_ns()
        strategy._latest_quotes[str(cloudbet.id)] = TestDataStubs.quote_tick(
            instrument=cloudbet,
            bid_price=0.0,
            ask_price=3.0,
            ask_size=100.0,
            ts_event=now_ns,
            ts_init=now_ns,
        )
        strategy._latest_quotes[str(sxbet.id)] = TestDataStubs.quote_tick(
            instrument=sxbet,
            bid_price=3.2,
            ask_price=0.0,
            bid_size=100.0,
            ts_event=now_ns,
            ts_init=now_ns,
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.50"),
            probability_b=Decimal("0.50"),
            total_probability=Decimal("1.00"),
            profit_margin=Decimal("0.00"),
            odds_a=Decimal(2),
            odds_b=Decimal(2),
            is_same_venue=False,
            match_type="cross_venue",
        )

        refreshed, reasons = strategy._live_execution_refresh_opportunity(opportunity)

        ensure(reasons == [])
        ensure(refreshed.odds_a == Decimal(3))
        ensure(refreshed.odds_b == Decimal("3.2"))
        ensure(refreshed.profit_margin > Decimal("0.5"))

    def test_live_execution_final_quote_check_blocks_stale_quotes(self, monkeypatch):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                quote_freshness_profile="live",
                max_total_stake=Decimal(25),
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-000"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        cloudbet = self._sxbet_instrument(event_id="event-1", venue="CLOUDBET", outcome="over")
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        now_ns = strategy.clock.timestamp_ns()
        old_ns = now_ns - 10 * 1_000_000_000
        strategy._latest_quotes[str(cloudbet.id)] = TestDataStubs.quote_tick(
            instrument=cloudbet,
            bid_price=0.0,
            ask_price=3.0,
            ask_size=100.0,
            ts_event=old_ns,
            ts_init=old_ns,
        )
        strategy._latest_quotes[str(sxbet.id)] = TestDataStubs.quote_tick(
            instrument=sxbet,
            bid_price=3.2,
            ask_price=0.0,
            bid_size=100.0,
            ts_event=old_ns,
            ts_init=old_ns,
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.50"),
            probability_b=Decimal("0.50"),
            total_probability=Decimal("1.00"),
            profit_margin=Decimal("0.00"),
            odds_a=Decimal(2),
            odds_b=Decimal(2),
            is_same_venue=False,
            match_type="cross_venue",
        )

        _refreshed, reasons = strategy._live_execution_refresh_opportunity(opportunity)

        ensure("final_quote_stale" in reasons)

    def test_live_execution_final_quote_check_blocks_undated_quotes(self, monkeypatch):  # skipcq
        # An undated quote (ts_event=0) has a deceptive 0.0 age/skew; the final live
        # gate must fail closed rather than fast-track an arbitrarily stale quote.
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                quote_freshness_profile="live",
                max_total_stake=Decimal(25),
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-000"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        cloudbet = self._sxbet_instrument(event_id="event-1", venue="CLOUDBET", outcome="over")
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        strategy._latest_quotes[str(cloudbet.id)] = TestDataStubs.quote_tick(
            instrument=cloudbet,
            bid_price=0.0,
            ask_price=3.0,
            ask_size=100.0,
            ts_event=0,
            ts_init=0,
        )
        strategy._latest_quotes[str(sxbet.id)] = TestDataStubs.quote_tick(
            instrument=sxbet,
            bid_price=3.2,
            ask_price=0.0,
            bid_size=100.0,
            ts_event=0,
            ts_init=0,
        )
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.50"),
            probability_b=Decimal("0.50"),
            total_probability=Decimal("1.00"),
            profit_margin=Decimal("0.00"),
            odds_a=Decimal(2),
            odds_b=Decimal(2),
            is_same_venue=False,
            match_type="cross_venue",
        )

        _refreshed, reasons = strategy._live_execution_refresh_opportunity(opportunity)

        ensure("final_quote_missing_timestamp" in reasons)

    def test_live_execution_blocks_tiny_pilot_cap_breach(self, monkeypatch):  # skipcq
        monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                auto_execute=True,
                live_execution_armed=True,
                max_leg_stake=Decimal(15),
            ),
        )
        cloudbet = self._sxbet_instrument(event_id="event-1", venue="CLOUDBET", outcome="over")
        sxbet = self._sxbet_instrument(event_id="event-1", venue="SXBET", outcome="under")
        strategy.opportunity_graph.edges_by_id[
            str(strategy._canonical_pair_id(cloudbet, sxbet))
        ] = SimpleNamespace(execution_safe=True, same_venue_execution_eligible=False, caveats=())
        opportunity = ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

        reasons = strategy._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=Decimal(16),
            stake_b=Decimal(5),
            diagnostics=None,
        )

        ensure("max_leg_stake_exceeded" in reasons)

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
        ensure(
            strategy._matcher_suspect_reason(instrument_a, other_event)[1]
            == "participant_mismatch",
        )
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
                execution_approval_mode="auto",
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

    def _manual_approval_strategy(self, **config_overrides):
        config_kwargs: dict[str, Any] = {
            "enabled_venues": frozenset(["CLOUDBET", "SXBET"]),
            "auto_execute": True,
            "max_total_stake": Decimal(25),
        }
        config_kwargs.update(config_overrides)
        strategy = BettingArbitrageStrategy(config=BettingArbitrageConfig(**config_kwargs))
        strategy.register(
            trader_id=TraderId("TESTER-000"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        strategy.submit_order = Mock()
        strategy._live_execution_block_reasons_for = Mock(return_value=[])
        return strategy

    def _cross_venue_opportunity(self, event_id: str = "event-1") -> ArbitrageOpportunity:
        cloudbet = self._sxbet_instrument(event_id=event_id, venue="CLOUDBET", outcome="over")
        sxbet = self._sxbet_instrument(event_id=event_id, venue="SXBET", outcome="under")
        return ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

    def test_manual_mode_stages_instead_of_submitting(self):  # skipcq
        strategy = self._manual_approval_strategy()

        strategy._handle_arbitrage_opportunity(self._cross_venue_opportunity())

        strategy.submit_order.assert_not_called()
        ensure(strategy._opportunities_executed == 0)
        ensure(len(strategy._pending_approvals) == 1)
        approvals = strategy.get_stats()["execution_approvals"]
        ensure(approvals["mode"] == "manual")
        ensure(approvals["staged"] == 1)
        record = approvals["pending"][0]
        ensure(record["venue_a"] == "CLOUDBET")
        ensure(record["venue_b"] == "SXBET")
        ensure(record["odds_a"] == "2.20")
        ensure(Decimal(record["stake_a"]) + Decimal(record["stake_b"]) <= Decimal(25))
        ensure(Decimal(record["expected_profit"]) > 0)
        ensure(record["expires_at"] > record["created_at"])

    def test_manual_stage_blocked_when_gates_fail(self):  # skipcq
        strategy = self._manual_approval_strategy()
        strategy._live_execution_block_reasons_for = Mock(return_value=["kill_switch_active"])

        strategy._handle_arbitrage_opportunity(self._cross_venue_opportunity())

        ensure(len(strategy._pending_approvals) == 0)
        ensure(strategy.get_stats()["execution_approvals"]["staged"] == 0)

    def test_manual_approve_reruns_gates_and_executes(self):  # skipcq
        strategy = self._manual_approval_strategy()
        strategy._handle_arbitrage_opportunity(self._cross_venue_opportunity())
        approval_id = next(iter(strategy._pending_approvals))
        gate_calls_after_stage = strategy._live_execution_block_reasons_for.call_count

        decision = strategy.handle_execution_approval_command(
            {"id": "cmd-1", "command": "approve_arb", "approval_id": approval_id},
        )

        ensure(decision["result"] == "executed")
        ensure(strategy.submit_order.call_count == 2)
        ensure(strategy._live_execution_block_reasons_for.call_count > gate_calls_after_stage)
        ensure(len(strategy._pending_approvals) == 0)
        approvals = strategy.get_stats()["execution_approvals"]
        ensure(approvals["approved_executed"] == 1)
        ensure(approvals["commands_processed"] == 1)
        ensure(approvals["recent_decisions"][-1]["command_id"] == "cmd-1")

    def test_manual_approve_blocked_when_gates_now_fail(self):  # skipcq
        strategy = self._manual_approval_strategy()
        strategy._handle_arbitrage_opportunity(self._cross_venue_opportunity())
        approval_id = next(iter(strategy._pending_approvals))
        strategy._live_execution_block_reasons_for = Mock(return_value=["kill_switch_active"])

        decision = strategy.handle_execution_approval_command(
            {"command": "approve_arb", "approval_id": approval_id},
        )

        ensure(decision["result"] == "blocked")
        ensure("kill_switch_active" in decision["reasons"])
        strategy.submit_order.assert_not_called()
        ensure(len(strategy._pending_approvals) == 0)
        ensure(strategy.get_stats()["execution_approvals"]["approved_blocked"] == 1)

    def test_manual_approve_expired_approval_blocked(self):  # skipcq
        strategy = self._manual_approval_strategy()
        strategy._handle_arbitrage_opportunity(self._cross_venue_opportunity())
        approval_id = next(iter(strategy._pending_approvals))
        record = strategy._pending_approvals[approval_id]
        record.expires_ts_ns = strategy.clock.timestamp_ns() - 1

        decision = strategy.handle_execution_approval_command(
            {"command": "approve_arb", "approval_id": approval_id},
        )

        ensure(decision["result"] == "unknown_approval_id")
        strategy.submit_order.assert_not_called()
        approvals = strategy.get_stats()["execution_approvals"]
        ensure(approvals["expired"] == 1)
        expiries = [entry for entry in approvals["recent_decisions"] if entry["action"] == "expire"]
        ensure(expiries[-1]["reasons"] == ["approval_ttl_elapsed"])

    def test_manual_reject_discards_without_submitting(self):  # skipcq
        strategy = self._manual_approval_strategy()
        strategy._handle_arbitrage_opportunity(self._cross_venue_opportunity())
        approval_id = next(iter(strategy._pending_approvals))

        decision = strategy.handle_execution_approval_command(
            {"command": "reject_arb", "approval_id": approval_id},
        )

        ensure(decision["result"] == "discarded")
        strategy.submit_order.assert_not_called()
        ensure(len(strategy._pending_approvals) == 0)
        ensure(strategy.get_stats()["execution_approvals"]["rejected"] == 1)

    def test_manual_restage_refreshes_existing_pair_record(self):  # skipcq
        strategy = self._manual_approval_strategy()
        opportunity = self._cross_venue_opportunity()

        strategy._handle_arbitrage_opportunity(opportunity)
        strategy._handle_arbitrage_opportunity(replace(opportunity, odds_a=Decimal("2.40")))

        ensure(len(strategy._pending_approvals) == 1)
        approvals = strategy.get_stats()["execution_approvals"]
        ensure(approvals["staged"] == 1)
        ensure(approvals["pending"][0]["odds_a"] == "2.40")

    def test_manual_pending_capacity_evicts_oldest(self):  # skipcq
        strategy = self._manual_approval_strategy(execution_approval_max_pending=2)

        for index in range(3):
            strategy._handle_arbitrage_opportunity(
                self._cross_venue_opportunity(event_id=f"event-{index}"),
            )

        ensure(len(strategy._pending_approvals) == 2)
        approvals = strategy.get_stats()["execution_approvals"]
        ensure(approvals["staged"] == 3)
        ensure(approvals["evicted"] == 1)
        evictions = [entry for entry in approvals["recent_decisions"] if entry["action"] == "evict"]
        ensure(evictions[-1]["reasons"] == ["pending_capacity_exceeded"])

    def test_invalid_approval_command_counted(self):  # skipcq
        strategy = self._manual_approval_strategy()

        decision = strategy.handle_execution_approval_command({"command": "detonate"})

        ensure(decision["result"] == "invalid_command")
        unknown = strategy.handle_execution_approval_command(
            {"command": "approve_arb", "approval_id": "does-not-exist"},
        )
        ensure(unknown["result"] == "unknown_approval_id")
        approvals = strategy.get_stats()["execution_approvals"]
        ensure(approvals["commands_invalid"] == 1)
        ensure(approvals["commands_processed"] == 1)

    def test_approval_command_files_processed_and_removed(self, tmp_path):  # skipcq
        command_dir = tmp_path / "commands"
        command_dir.mkdir()
        strategy = self._manual_approval_strategy(
            execution_approval_command_dir=str(command_dir),
        )
        strategy._handle_arbitrage_opportunity(self._cross_venue_opportunity())
        approval_id = next(iter(strategy._pending_approvals))
        (command_dir / "001-reject.json").write_text(
            json.dumps({"id": "cmd-9", "command": "reject_arb", "approval_id": approval_id}),
            encoding="utf8",
        )
        (command_dir / "002-junk.json").write_text("{not json", encoding="utf8")
        (command_dir / "ignored.tmp").write_text("{}", encoding="utf8")

        strategy._process_approval_command_files()

        ensure(len(strategy._pending_approvals) == 0)
        ensure(not (command_dir / "001-reject.json").exists())
        ensure(not (command_dir / "002-junk.json").exists())
        ensure((command_dir / "ignored.tmp").exists())
        approvals = strategy.get_stats()["execution_approvals"]
        ensure(approvals["rejected"] == 1)
        ensure(approvals["commands_processed"] == 1)
        ensure(approvals["commands_invalid"] == 1)

    def test_approval_command_timer_only_in_manual_mode_with_dir(self, tmp_path):  # skipcq
        manual = self._manual_approval_strategy(
            execution_approval_command_dir=str(tmp_path),
        )
        ensure(manual._approval_command_polling_enabled() is True)

        no_dir = self._manual_approval_strategy()
        ensure(no_dir._approval_command_polling_enabled() is False)

        auto = self._manual_approval_strategy(
            execution_approval_mode="auto",
            execution_approval_command_dir=str(tmp_path),
        )
        ensure(auto._approval_command_polling_enabled() is False)

    def test_execution_approval_config_validation(self):  # skipcq
        with pytest.raises(ValueError, match="Invalid execution_approval_mode"):
            BettingArbitrageConfig(execution_approval_mode="ask-nicely")
        with pytest.raises(ValueError, match="execution_approval_ttl_secs"):
            BettingArbitrageConfig(execution_approval_ttl_secs=0)
        with pytest.raises(ValueError, match="execution_approval_max_pending"):
            BettingArbitrageConfig(execution_approval_max_pending=0)
        normalized = BettingArbitrageConfig(execution_approval_mode=" AUTO ")
        ensure(normalized.execution_approval_mode == "auto")

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

    def test_arbitrage_diagnostics_uses_fixture_proof_for_cross_venue_alias_drift(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["CLOUDBET", "SXBET"])),
        )
        cloudbet = self._sxbet_instrument(
            venue="CLOUDBET",
            event_id="cloudbet-market-1",
            event_name="MIN Timberwolves v SA Spurs",
            home_name="MIN Timberwolves",
            away_name="SA Spurs",
            outcome="home",
            market_name="draw_no_bet",
            start_time="",
        )
        sxbet = self._sxbet_instrument(
            venue="SXBET",
            event_id="sxbet-market-1",
            event_name="Minnesota Timberwolves v San Antonio Spurs",
            home_name="Minnesota Timberwolves",
            away_name="San Antonio Spurs",
            outcome="away",
            market_name="draw_no_bet",
            start_time="",
        )

        suspect, reason = strategy._matcher_suspect_reason(cloudbet, sxbet)

        ensure(suspect is False)
        ensure(reason == "none")

    def test_arbitrage_diagnostics_same_venue_event_id_is_authoritative_for_name_drift(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )
        instrument_a = self._sxbet_instrument(
            event_id="fixture-1",
            event_name="Cleveland v Minnesota",
            home_name="Cleveland",
            away_name="Minnesota",
            outcome="home",
            market_name="match_odds",
            start_time="",
        )
        instrument_b = self._sxbet_instrument(
            event_id="fixture-1",
            event_name="Cleveland Bears v Minnesota Wolves",
            home_name="Cleveland Bears",
            away_name="Minnesota Wolves",
            outcome="away",
            market_name="match_odds",
            start_time="",
        )

        suspect, reason = strategy._semantic_fixture_suspect_reason(instrument_a, instrument_b)

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

    def test_instrument_id_canonicalizes_numeric_params(self):  # skipcq
        canonical = make_crypto_betting_instrument_id(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            market_name="total_goals",
            outcome="over",
            params="line=2.5",
        )
        ensure(str(canonical.symbol) == "event-1:total_goals:over:line_2_5")
        for raw in ("line=2.50", "line=2.500"):
            jittered = make_crypto_betting_instrument_id(
                venue=Venue("CLOUDBET"),
                event_id="event-1",
                market_name="total_goals",
                outcome="over",
                params=raw,
            )
            ensure(jittered == canonical)

        negative = make_crypto_betting_instrument_id(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            market_name="handicap",
            outcome="home",
            params="line=-1.5",
        )
        ensure(str(negative.symbol) == "event-1:handicap:home:line_-1_5")
        ensure(
            make_crypto_betting_instrument_id(
                venue=Venue("CLOUDBET"),
                event_id="event-1",
                market_name="handicap",
                outcome="home",
                params="line=-1.50",
            )
            == negative,
        )

        non_numeric = make_crypto_betting_instrument_id(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            market_name="totals",
            outcome="over",
            params="period=full_time",
        )
        ensure(str(non_numeric.symbol) == "event-1:totals:over:period_full_time")

    def test_instruments_with_jittered_numeric_params_share_one_id(self):  # skipcq
        ids = {
            str(self._sxbet_instrument(event_id="market-1", outcome="over", params=params).id)
            for params in ("line=2.5", "line=2.50", "line=2.500")
        }
        ensure(len(ids) == 1)

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
        currency: str = "USDC",
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
            currency=Currency.from_str(currency),
            params=params,
            start_time=start_time,
            info=info or {},
            live=live,
        )


def _build_semantic_cache(
    cache_dir: Path,
    source: CryptoBettingInstrument,
    target: CryptoBettingInstrument,
    *,
    scope: str,
) -> str:
    """
    Write a self-consistent, ready semantic cache (manifest + one promoted template +
    compatibility stamp) into ``cache_dir`` and return the template id.
    """
    from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
        stamp_semantic_cache_compatibility,
    )

    store = RuleStore(FileRuleCache(cache_dir))
    store.save_manifest(
        RuleCorpusManifest(
            manifest_id="manifest-1",
            provider="SXBET",
            fetched_at="2026-01-01T00:00:00Z",
            endpoint_version="v1",
            sport_count=1,
            event_count=1,
            selection_count=2,
            market_taxonomy_hash="hash",
        ),
    )
    rule = RuleClassifier().classify(source, target)
    if rule is None:
        raise AssertionError("Expected classifier to produce a semantic rule")
    template = SemanticRuleTemplate.from_rule(
        rule,
        support=TemplateSupportStats(
            template_id=SemanticRuleTemplate.from_rule(rule).template_id,
            observed_count=10,
            event_count=3,
            provider_count=1,
            providers=("SXBET",),
            sports=(source.sport_name.lower(),),
            confidence=1.0,
        ),
        provider_scope=("SXBET", "CLOUDBET"),
        promotion_status=PromotionStatus.PROMOTED.value,
        safety_tier=SafetyTier.EXECUTION_SAFE.value,
    )
    store.save_promoted_template(template)
    stamp_semantic_cache_compatibility(cache_dir, scope=scope)
    return template.template_id


def _cache_bytes_snapshot(cache_dir: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(cache_dir)): path.read_bytes()
        for path in sorted(cache_dir.rglob("*"))
        if path.is_file()
    }


class TestSemanticCacheHotSwap:  # skipcq
    """
    In-node reload_semantic_cache admin command: swap-safe, rollback-clean hot swap of
    the semantic template store into a RUNNING node with no restart (PR-C).
    """

    @staticmethod
    def ensure(condition: bool) -> None:
        if not condition:
            raise AssertionError

    def _hot_swap_strategy(
        self,
        cache_dir: Path,
        *,
        execution_approval_mode: str = "auto",
        execution_approval_command_dir: str | None = None,
    ):  # untyped: mock method-assignment below is intentional
        config_kwargs: dict[str, Any] = {
            "enabled_venues": frozenset(["CLOUDBET", "SXBET"]),
            "semantic_rule_cache_dir": str(cache_dir),
            "opportunity_graph_engine": "semantic_rust",
            "execution_approval_mode": execution_approval_mode,
        }
        if execution_approval_command_dir is not None:
            config_kwargs["execution_approval_command_dir"] = execution_approval_command_dir
        strategy = BettingArbitrageStrategy(config=BettingArbitrageConfig(**config_kwargs))
        strategy.register(
            trader_id=TraderId("TESTER-SWAP"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        # Isolate the graph rebuild from the quote-subscription side effects; the
        # semantic-priority branch is exercised via these mocks.
        strategy._subscribe_cross_venue_common_fixture_quote_ticks = Mock()  # type: ignore[method-assign]
        strategy._subscribe_semantic_connected_quote_ticks = Mock()  # type: ignore[method-assign]
        strategy._subscribe_semantic_unmatched_quote_probe_ticks = Mock()  # type: ignore[method-assign]
        return strategy

    def _baseball_and_soccer_legs(self):
        make = TestBettingArbitrageStrategy._sxbet_instrument
        line = "line=2.5"
        bb_over = make(
            event_id="evt-bb",
            venue="CLOUDBET",
            outcome="over",
            sport_name="Baseball",
            params=line,
        )
        bb_under = make(
            event_id="evt-bb",
            venue="SXBET",
            outcome="under",
            sport_name="Baseball",
            params=line,
        )
        soc_over = make(
            event_id="evt-soc",
            venue="CLOUDBET",
            outcome="over",
            sport_name="Soccer",
            params=line,
        )
        soc_under = make(
            event_id="evt-soc",
            venue="SXBET",
            outcome="under",
            sport_name="Soccer",
            params=line,
        )
        return bb_over, bb_under, soc_over, soc_under

    def _prime_live_graph(
        self,
        strategy: BettingArbitrageStrategy,
        cache_dir: Path,
        subscribed,
    ) -> None:
        strategy._subscribed_instruments.update(subscribed)
        strategy._matcher.set_rule_store(RuleStore(FileRuleCache(cache_dir)))
        strategy._rebuild_opportunity_graph_and_resubscribe(list(subscribed))

    def test_swap_adopts_new_templates_with_identical_count(self, tmp_path: Path):  # skipcq
        # Live cache carries a soccer template that does NOT connect the subscribed
        # baseball legs; staging carries a baseball template that DOES — same promoted
        # AND semantic template COUNT (1), different content. A correct swap goes through
        # build() (full build_semantic replace), so the graph adopts the new templates;
        # the count-keyed add_instrument fast path would have skipped them.
        live = tmp_path / "cache"
        staging = tmp_path / "staging"
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        soc_id = _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        bb_id = _build_semantic_cache(staging, bb_over, bb_under, scope="miner-scope")
        self.ensure(soc_id != bb_id)

        strategy = self._hot_swap_strategy(live)
        self._prime_live_graph(strategy, live, {bb_over, bb_under})
        self.ensure(strategy._opportunity_graph.edge_count == 0)
        template_count_before = strategy._opportunity_graph.semantic_template_count
        self.ensure(template_count_before == 1)

        decision = strategy._reload_semantic_cache(str(staging), command_id="miner-1")

        self.ensure(decision["result"] == "reloaded")
        self.ensure(decision["details"]["promoted_template_count"] == 1)
        # Same count, but the graph now forms the baseball edge: new content adopted.
        self.ensure(strategy._opportunity_graph.semantic_template_count == template_count_before)
        self.ensure(strategy._opportunity_graph.edge_count >= 1)
        payload_ids = {
            payload["template_id"]
            for payload in strategy._opportunity_graph._semantic_template_payloads()
        }
        self.ensure(payload_ids == {bb_id})
        # Node scope is re-stamped over the miner's scope after publish.
        from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
            read_semantic_cache_scope,
        )

        self.ensure(read_semantic_cache_scope(live) == "node-scope")
        self.ensure(not staging.exists())
        prev_dirs = list(live.parent.glob(f"{live.name}.prev-*"))
        self.ensure(len(prev_dirs) == 1)
        stats = strategy.get_stats()["execution_approvals"]
        self.ensure(stats["semantic_cache_reloads_succeeded"] == 1)
        # Quote subscriptions re-established via the semantic-priority path.
        strategy._subscribe_semantic_connected_quote_ticks.assert_called()
        strategy._subscribe_cross_venue_common_fixture_quote_ticks.assert_called()

    def test_invalid_not_ready_staging_rejected_live_untouched(self, tmp_path: Path):  # skipcq
        live = tmp_path / "cache"
        staging = tmp_path / "staging"
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        staging.mkdir()  # empty: manifest_count == 0 → not ready

        strategy = self._hot_swap_strategy(live)
        self._prime_live_graph(strategy, live, {bb_over, bb_under})
        before = _cache_bytes_snapshot(live)

        decision = strategy._reload_semantic_cache(str(staging), command_id="miner-2")

        self.ensure(decision["result"] == "rejected")
        self.ensure("staging_cache_not_ready" in decision["reasons"])
        self.ensure(_cache_bytes_snapshot(live) == before)
        stats = strategy.get_stats()["execution_approvals"]
        self.ensure(stats["semantic_cache_reloads_rejected"] == 1)
        self.ensure(stats["recent_decisions"][-1]["action"] == "reload_semantic_cache")

    def test_incompatible_version_staging_rejected_live_untouched(self, tmp_path: Path):  # skipcq
        live = tmp_path / "cache"
        staging = tmp_path / "staging"
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        _build_semantic_cache(staging, bb_over, bb_under, scope="miner-scope")
        (staging / ".semantic-cache-version").write_text(
            json.dumps({"version": "semantic-rule-cache:OLD", "scope": "miner-scope"}),
            encoding="utf-8",
        )

        strategy = self._hot_swap_strategy(live)
        self._prime_live_graph(strategy, live, {bb_over, bb_under})
        before = _cache_bytes_snapshot(live)

        decision = strategy._reload_semantic_cache(str(staging), command_id="miner-3")

        self.ensure(decision["result"] == "rejected")
        self.ensure("compatibility_version_mismatch" in decision["reasons"])
        self.ensure(_cache_bytes_snapshot(live) == before)
        self.ensure(strategy._opportunity_graph.edge_count == 0)

    def test_rebuild_failure_restores_previous_cache(self, tmp_path: Path, monkeypatch):  # skipcq
        live = tmp_path / "cache"
        staging = tmp_path / "staging"
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        _build_semantic_cache(staging, bb_over, bb_under, scope="miner-scope")

        strategy = self._hot_swap_strategy(live)
        self._prime_live_graph(strategy, live, {bb_over, bb_under})
        before = _cache_bytes_snapshot(live)

        # The semantic hot-swap builds a FRESH OpportunityGraph and swaps it in, so inject
        # the failure at the class level (the fresh graph's build, not the live instance).
        real_build = OpportunityGraph.build
        calls = {"count": 0}

        def _flaky_build(graph_self, instruments):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("injected rebuild failure")
            return real_build(graph_self, instruments)

        monkeypatch.setattr(OpportunityGraph, "build", _flaky_build)

        decision = strategy._reload_semantic_cache(str(staging), command_id="miner-4")

        self.ensure(decision["result"] == "failed")
        self.ensure(any("rebuild_failed" in reason for reason in decision["reasons"]))
        # Live cache restored byte-for-byte from the retained .prev generation.
        self.ensure(_cache_bytes_snapshot(live) == before)
        # Graph rebuilt against the restored old cache: soccer template, no baseball edge.
        self.ensure(strategy._opportunity_graph.edge_count == 0)
        self.ensure(strategy._opportunity_graph.semantic_template_count == 1)
        stats = strategy.get_stats()["execution_approvals"]
        self.ensure(stats["semantic_cache_reloads_failed"] == 1)

    def test_reload_works_in_manual_mode(self, tmp_path: Path) -> None:  # skipcq
        live = tmp_path / "cache"
        staging = tmp_path / "staging"
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        _build_semantic_cache(staging, bb_over, bb_under, scope="miner-scope")

        strategy = self._hot_swap_strategy(live, execution_approval_mode="manual")
        self._prime_live_graph(strategy, live, {bb_over, bb_under})

        decision = strategy.handle_execution_approval_command(
            {"command": "reload_semantic_cache", "id": "miner-5", "staging_dir": str(staging)},
        )

        self.ensure(decision["result"] == "reloaded")
        self.ensure(strategy._opportunity_graph.edge_count >= 1)

    def test_approve_reject_gated_to_manual_mode(self, tmp_path: Path) -> None:  # skipcq
        live = tmp_path / "cache"
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        strategy = self._hot_swap_strategy(live, execution_approval_mode="auto")

        approve = strategy.handle_execution_approval_command(
            {"command": "approve_arb", "approval_id": "abc"},
        )
        reject = strategy.handle_execution_approval_command(
            {"command": "reject_arb", "approval_id": "abc"},
        )

        self.ensure(approve["result"] == "approval_mode_disabled")
        self.ensure(reject["result"] == "approval_mode_disabled")
        stats = strategy.get_stats()["execution_approvals"]
        self.ensure(stats["commands_invalid"] == 2)
        self.ensure(stats["commands_processed"] == 0)

    def test_command_polling_starts_timer_in_auto_mode(self, tmp_path: Path) -> None:  # skipcq
        strategy = self._hot_swap_strategy(
            tmp_path / "cache",
            execution_approval_mode="auto",
            execution_approval_command_dir=str(tmp_path / "commands"),
        )
        self.ensure(strategy._command_polling_enabled() is True)
        self.ensure(strategy._approval_command_polling_enabled() is False)

        strategy._start_approval_command_timer()

        from nautilus_trader.examples.strategies.betting_arbitrage import (
            APPROVAL_COMMAND_TIMER_NAME,
        )

        self.ensure(APPROVAL_COMMAND_TIMER_NAME in strategy.clock.timer_names)

    def test_reload_command_file_processed_and_removed(self, tmp_path: Path) -> None:  # skipcq
        # End-to-end through the miner's shared schema and the read→unlink→dispatch path.
        live = tmp_path / "cache"
        staging = tmp_path / "staging"
        command_dir = tmp_path / "commands"
        command_dir.mkdir()
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        _build_semantic_cache(staging, bb_over, bb_under, scope="miner-scope")

        strategy = self._hot_swap_strategy(
            live,
            execution_approval_mode="auto",
            execution_approval_command_dir=str(command_dir),
        )
        self._prime_live_graph(strategy, live, {bb_over, bb_under})
        command_file = command_dir / "miner-20260101T000000000000Z-reload_semantic_cache.json"
        command_file.write_text(
            json.dumps(
                {
                    "command": "reload_semantic_cache",
                    "id": "miner-6",
                    "staging_dir": str(staging),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        strategy._process_approval_command_files()

        self.ensure(not command_file.exists())
        self.ensure(strategy._opportunity_graph.edge_count >= 1)
        stats = strategy.get_stats()["execution_approvals"]
        self.ensure(stats["semantic_cache_reloads_succeeded"] == 1)

    def test_reload_prunes_old_prev_generations(self, tmp_path: Path) -> None:  # skipcq
        live = tmp_path / "cache"
        bb_over, bb_under, soc_over, soc_under = self._baseball_and_soccer_legs()
        _build_semantic_cache(live, soc_over, soc_under, scope="node-scope")
        strategy = self._hot_swap_strategy(live)
        self._prime_live_graph(strategy, live, {bb_over, bb_under})

        for index in range(4):
            staging = tmp_path / f"staging-{index}"
            _build_semantic_cache(staging, bb_over, bb_under, scope="miner-scope")
            decision = strategy._reload_semantic_cache(str(staging), command_id=f"miner-{index}")
            self.ensure(decision["result"] == "reloaded")

        prev_dirs = list(live.parent.glob(f"{live.name}.prev-*"))
        self.ensure(len(prev_dirs) <= 2)


class TestFxRefresh:  # skipcq
    """
    Live FX refresh loop wired into the portfolio currency policy, plus the block-not-
    sum notional fallback fix.
    """

    @staticmethod
    def _registered_strategy(**config_kwargs) -> BettingArbitrageStrategy:
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset(["CLOUDBET", "SXBET"]),
                **config_kwargs,
            ),
        )
        strategy.register(
            trader_id=TraderId("TESTER-FX"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=TestComponentStubs.cache(),
            clock=TestComponentStubs.clock(),
        )
        return strategy

    @staticmethod
    def _cross_currency_opportunity() -> ArbitrageOpportunity:
        cloudbet = TestBettingArbitrageStrategy._sxbet_instrument(
            event_id="event-fx",
            venue="CLOUDBET",
            outcome="over",
            currency="EUR",
        )
        sxbet = TestBettingArbitrageStrategy._sxbet_instrument(
            event_id="event-fx",
            venue="SXBET",
            outcome="under",
            currency="USDC",
        )
        return ArbitrageOpportunity(
            instrument_a=cloudbet,
            instrument_b=sxbet,
            probability_a=Decimal("0.45"),
            probability_b=Decimal("0.45"),
            total_probability=Decimal("0.90"),
            profit_margin=Decimal("0.11"),
            odds_a=Decimal("2.20"),
            odds_b=Decimal("2.20"),
            is_same_venue=False,
            match_type="cross_venue",
        )

    def test_config_rejects_non_positive_refresh_interval(self):  # skipcq
        with pytest.raises(ValueError, match="fx_refresh_interval_secs"):
            BettingArbitrageConfig(fx_refresh_interval_secs=0.0)

    def test_config_rejects_unsupported_refresh_pair(self):  # skipcq
        with pytest.raises(ValueError, match="fx_refresh_pairs"):
            BettingArbitrageConfig(fx_refresh_pairs=["EUR/GBP"])

    def test_config_normalizes_and_dedupes_refresh_pairs(self):  # skipcq
        config = BettingArbitrageConfig(fx_refresh_pairs=[" btc/usd ", "BTC/USD", "eur/usd"])
        ensure(config.fx_refresh_pairs == ["BTC/USD", "EUR/USD"])
        ensure(BettingArbitrageConfig().fx_refresh_interval_secs is None)
        ensure(BettingArbitrageConfig().fx_refresh_pairs is None)

    def test_refresh_timer_fetch_lands_quote_and_policy_uses_live_rate(
        self,
        monkeypatch,
    ):  # skipcq
        # (a)+(c) timer registered, fetch invoked on the timer event, the fetched quote
        # lands, and the policy converts on the LIVE rate over the static baseline.
        strategy = self._registered_strategy(
            fx_refresh_interval_secs=300.0,
            configured_fx_rates={"EUR/USD": Decimal("1.00")},
        )
        now_ns = strategy.clock.timestamp_ns()
        fetched: list[str] = []

        def fake_fetch(pair, *, timeout_secs):
            fetched.append(pair)
            return FxRateQuote(pair, Decimal("1.10"), "hyperliquid", now_ns, now_ns)

        monkeypatch.setattr(betting_arbitrage_module, "fetch_fx_rate", fake_fetch)
        strategy._start_fx_refresh_timer()
        ensure(FX_REFRESH_TIMER_NAME in strategy.clock.timer_names)

        strategy.on_time_event(SimpleNamespace(name=FX_REFRESH_TIMER_NAME))

        ensure(fetched == ["EUR/USD"])
        conversion = strategy._portfolio_currency_policy().convert(Decimal(100), "EUR")
        # 100 EUR * 1.10 live * 1.001 notional haircut = 110.11, not 100.10 static.
        ensure(conversion.converted_amount == Decimal("110.11"))
        ensure(conversion.source == "hyperliquid")
        ensure(strategy.get_stats()["fx_refresh_fetches"] == 1)

    def test_stale_live_quote_blocks_conversion_at_policy_age_gate(self):  # skipcq
        # (b) The stored FxRateQuote's own age_secs is the FETCH latency (~0), so a
        # naive pass-through would never look stale; the policy snapshot must recompute
        # the age from the fetch event so a 61s-old rate trips the 30s gate.
        strategy = self._registered_strategy(fx_refresh_interval_secs=300.0)
        stale_ns = strategy.clock.timestamp_ns() - 61 * 1_000_000_000
        strategy._live_fx_quotes["EUR/USD"] = FxRateQuote(
            "EUR/USD",
            Decimal("1.10"),
            "hyperliquid",
            stale_ns,
            stale_ns,
        )

        conversion = strategy._portfolio_currency_policy().convert(Decimal(100), "EUR")

        ensure(conversion.converted_amount is None)
        ensure(conversion.blocker_reason == "stale_fx_rate")

    def test_refresh_disabled_is_byte_identical_policy_and_no_timer(self):  # skipcq
        # (d) fx_refresh_interval_secs=None preserves current behavior exactly.
        strategy = self._registered_strategy(configured_fx_rates={"EUR/USD": Decimal("1.09")})

        strategy._start_fx_refresh_timer()

        ensure(FX_REFRESH_TIMER_NAME not in strategy.clock.timer_names)
        expected = PortfolioCurrencyPolicy(
            base_currency="USD",
            stablecoin_currencies=strategy._config.stablecoin_currencies,
            stablecoin_haircut_bps=10,
            fx_quote_max_age_secs=30.0,
            static_fx_rates={"EUR/USD": Decimal("1.09")},
        )
        ensure(strategy._portfolio_currency_policy() == expected)

    def test_refresh_fetches_configured_crypto_pair_for_conversion(
        self,
        monkeypatch,
    ):  # skipcq
        strategy = self._registered_strategy(
            fx_refresh_interval_secs=300.0,
            fx_refresh_pairs=["EUR/USD", "BTC/USD"],
        )
        now_ns = strategy.clock.timestamp_ns()
        rates = {"EUR/USD": Decimal("1.10"), "BTC/USD": Decimal(97_000)}

        monkeypatch.setattr(
            betting_arbitrage_module,
            "fetch_fx_rate",
            lambda pair, *, timeout_secs: FxRateQuote(
                pair,
                rates[pair],
                "hyperliquid",
                now_ns,
                now_ns,
            ),
        )
        strategy._refresh_fx_rates()

        conversion = strategy._portfolio_currency_policy().convert(Decimal("0.001"), "BTC")
        # 0.001 BTC * 97000 * 1.001 = 97.097 USD — never the raw 0.001 counted 1:1.
        ensure(conversion.converted_amount == Decimal("97.097"))

    def test_refresh_fetch_failure_is_counted_and_stores_no_quote(
        self,
        monkeypatch,
    ):  # skipcq
        strategy = self._registered_strategy(fx_refresh_interval_secs=300.0)
        monkeypatch.setattr(
            betting_arbitrage_module,
            "fetch_fx_rate",
            Mock(side_effect=RuntimeError("all sources down")),
        )

        strategy._refresh_fx_rates()

        ensure(strategy._fx_refresh_failures == 1)
        ensure(strategy._live_fx_quotes == {})
        ensure(strategy._portfolio_currency_policy().fx_quotes is None)

    def test_usd_equivalent_notional_blocks_when_rate_missing(self):  # skipcq
        # (e) THE BUG: the old fallback returned stake_a + stake_b 1:1 (20) exactly when
        # a rate was missing, under-stating notional against the caps.
        strategy = self._registered_strategy(execution_venue_mode="cross_venue")
        opportunity = self._cross_currency_opportunity()

        notional = strategy._usd_equivalent_notional(opportunity, Decimal(10), Decimal(10))

        ensure(notional is None)
        reasons = strategy._live_execution_cap_block_reasons(
            opportunity,
            Decimal(10),
            Decimal(10),
        )
        ensure("missing_fx_rate" in reasons)

    def test_usd_equivalent_notional_converts_when_rate_available(self):  # skipcq
        strategy = self._registered_strategy(
            execution_venue_mode="cross_venue",
            configured_fx_rates={"EUR/USD": Decimal("1.10")},
        )
        opportunity = self._cross_currency_opportunity()

        notional = strategy._usd_equivalent_notional(opportunity, Decimal(10), Decimal(10))

        # EUR leg 10 * 1.10 * 1.001 = 11.011; USDC leg 10 * 1.001 = 10.01.
        ensure(notional == Decimal("21.021"))

    def test_simultaneous_submit_fails_closed_when_notional_unavailable(self):  # skipcq
        # A missing conversion at submit time must block the pair outright: no orders,
        # no attempt, no understated notional accrual.
        strategy = self._registered_strategy(execution_venue_mode="cross_venue")
        strategy.submit_order = Mock()
        opportunity = self._cross_currency_opportunity()

        reasons = strategy._submit_arbitrage_simultaneous(
            order_a=cast("Any", SimpleNamespace()),
            order_b=cast("Any", SimpleNamespace()),
            opportunity=opportunity,
            stake_a=Decimal(10),
            stake_b=Decimal(10),
        )

        ensure(reasons == ["usd_notional_unavailable"])
        ensure(strategy.submit_order.call_count == 0)
        ensure(strategy._live_execution_attempts == 0)
        ensure(strategy._live_execution_notional_used == Decimal(0))
        ensure(strategy._arb_leg_siblings == {})


class TestIncrementalGraphRefresh:  # skipcq
    """
    Instrument-refresh deltas apply incrementally; full rebuild stays for template swaps
    and oversized deltas.
    """

    @staticmethod
    def ensure(condition: bool) -> None:
        if not condition:
            raise AssertionError

    def _strategy(self) -> BettingArbitrageStrategy:
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset({"SXBET", "BLACKBET"})),
        )
        strategy._subscribe_cross_venue_common_fixture_quote_ticks = Mock()  # type: ignore[method-assign]
        strategy._subscribe_semantic_connected_quote_ticks = Mock()  # type: ignore[method-assign]
        strategy._subscribe_semantic_unmatched_quote_probe_ticks = Mock()  # type: ignore[method-assign]
        strategy._subscribe_quote_ticks_for_instrument = Mock()  # type: ignore[method-assign]
        return strategy

    def _paired_instruments(self, event_count: int) -> list[CryptoBettingInstrument]:
        make = TestBettingArbitrageStrategy._sxbet_instrument
        return [
            make(
                event_id=f"evt-{index}",
                event_name=f"Team {index}A vs Team {index}B",
                home_name=f"Team {index}A",
                away_name=f"Team {index}B",
                outcome=outcome,
                params="line=2.5",
            )
            for index in range(event_count)
            for outcome in ("over", "under")
        ]

    def test_small_delta_applies_incrementally_without_full_build(self):  # skipcq
        strategy = self._strategy()
        instruments = self._paired_instruments(6)
        strategy._subscribed_instruments.update(instruments)
        strategy._rebuild_opportunity_graph_and_resubscribe(instruments)
        graph = strategy._opportunity_graph
        if graph._active_rust_core() is None:
            pytest.skip("Rust OpportunityGraphCore is unavailable")
        nodes_before = graph.node_count
        full_syncs_before = graph.edge_sync_full_runs

        added = TestBettingArbitrageStrategy._sxbet_instrument(
            event_id="evt-0",
            outcome="over",
            venue="BLACKBET",
            params="line=2.5",
        )
        removed = instruments[-1]
        strategy._subscribed_instruments.add(added)
        strategy._subscribed_instruments.discard(removed)

        strategy._rebuild_after_instrument_refresh("SXBET", [added], [removed])

        self.ensure(strategy._instrument_refresh_graph_incremental_updates == 1)
        self.ensure(strategy._instrument_refresh_graph_rebuilds == 0)
        # No full edge re-sync ran; the delta paths maintained the same node count.
        self.ensure(graph.edge_sync_full_runs == full_syncs_before)
        self.ensure(graph.edge_sync_delta_runs >= 1)
        self.ensure(graph.node_count == nodes_before)
        self.ensure(str(added.id) in graph.nodes_by_id)
        self.ensure(str(removed.id) not in graph.nodes_by_id)
        stats = strategy.get_stats()
        self.ensure(stats["instrument_refresh_graph_incremental_updates"] == 1)
        self.ensure("graph_rebuild" in stats["latency_diagnostics"])
        self.ensure("edge_sync" in stats["latency_diagnostics"])
        self.ensure(stats["latency_diagnostics"]["edge_sync"]["count"] >= 1)

    def test_oversized_delta_falls_back_to_full_build(self):  # skipcq
        strategy = self._strategy()
        instruments = self._paired_instruments(2)
        strategy._subscribed_instruments.update(instruments)
        strategy._rebuild_opportunity_graph_and_resubscribe(instruments)
        graph = strategy._opportunity_graph
        if graph._active_rust_core() is None:
            pytest.skip("Rust OpportunityGraphCore is unavailable")

        added = [
            TestBettingArbitrageStrategy._sxbet_instrument(
                event_id=f"evt-{index}",
                event_name=f"Team {index}A vs Team {index}B",
                home_name=f"Team {index}A",
                away_name=f"Team {index}B",
                outcome=outcome,
                venue="BLACKBET",
                params="line=2.5",
            )
            for index in range(2)
            for outcome in ("over", "under")
        ]
        strategy._subscribed_instruments.update(added)

        strategy._rebuild_after_instrument_refresh("BLACKBET", added, [])

        self.ensure(strategy._instrument_refresh_graph_rebuilds == 1)
        self.ensure(strategy._instrument_refresh_graph_incremental_updates == 0)
        self.ensure(strategy.get_stats()["latency_diagnostics"]["graph_rebuild"]["count"] >= 1)

    def test_offloop_full_build_adopts_a_fresh_graph_object(self):  # skipcq
        # SCALE-3: a full rebuild is prepared off the event loop and adopted by swapping
        # in a freshly built graph object (the old one keeps serving until the swap),
        # rather than mutating the live graph in place. run_in_executor is inline in
        # tests, so the prepare+adopt complete synchronously here.
        strategy = self._strategy()
        instruments = self._paired_instruments(4)
        strategy._subscribed_instruments.update(instruments)
        strategy._rebuild_opportunity_graph_and_resubscribe(instruments)
        first_graph = strategy._opportunity_graph
        if first_graph._active_rust_core() is None:
            pytest.skip("Rust OpportunityGraphCore is unavailable")
        builds_before = strategy._offloop_graph_builds

        added = [
            TestBettingArbitrageStrategy._sxbet_instrument(
                event_id=f"evt-x{index}",
                event_name=f"Team {index}A vs Team {index}B",
                home_name=f"Team {index}A",
                away_name=f"Team {index}B",
                outcome=outcome,
                venue="BLACKBET",
                params="line=2.5",
            )
            for index in range(3)
            for outcome in ("over", "under")
        ]
        strategy._subscribed_instruments.update(added)
        strategy._rebuild_after_instrument_refresh("BLACKBET", added, [])

        self.ensure(strategy._opportunity_graph is not first_graph)
        self.ensure(strategy._offloop_graph_builds == builds_before + 1)
        self.ensure(strategy._graph_build_in_flight is False)
        self.ensure(strategy._prepared_graph is None)
        for instrument in added:
            self.ensure(str(instrument.id) in strategy._opportunity_graph.nodes_by_id)

    def test_offloop_build_failure_retains_current_graph_and_releases_latch(
        self,
        monkeypatch,
    ):  # skipcq
        # A build error raised inside the off-loop prepare must never publish a partial
        # graph: the current graph keeps serving quotes and the in-flight latch is
        # released so a later refresh can retry.
        strategy = self._strategy()
        instruments = self._paired_instruments(4)
        strategy._subscribed_instruments.update(instruments)
        strategy._rebuild_opportunity_graph_and_resubscribe(instruments)
        current_graph = strategy._opportunity_graph
        if current_graph._active_rust_core() is None:
            pytest.skip("Rust OpportunityGraphCore is unavailable")
        failures_before = strategy._offloop_graph_build_failures

        def _boom(self, instruments):
            raise RuntimeError("injected off-loop build failure")

        monkeypatch.setattr(OpportunityGraph, "build", _boom)

        added = [
            TestBettingArbitrageStrategy._sxbet_instrument(
                event_id=f"evt-boom{index}",
                event_name=f"Team {index}A vs Team {index}B",
                home_name=f"Team {index}A",
                away_name=f"Team {index}B",
                outcome=outcome,
                venue="BLACKBET",
                params="line=2.5",
            )
            for index in range(3)
            for outcome in ("over", "under")
        ]
        strategy._subscribed_instruments.update(added)
        strategy._rebuild_after_instrument_refresh("BLACKBET", added, [])

        self.ensure(strategy._opportunity_graph is current_graph)
        self.ensure(strategy._prepared_graph is None)
        self.ensure(strategy._graph_build_in_flight is False)
        self.ensure(strategy._offloop_graph_build_failures == failures_before + 1)

    def test_stale_semantic_templates_force_full_build(self, tmp_path: Path):  # skipcq
        strategy = self._strategy()
        instruments = self._paired_instruments(6)
        strategy._subscribed_instruments.update(instruments)
        strategy._matcher.set_rule_store(
            RuleStore(FileRuleCache(tmp_path / "refresh-rules")),
        )
        strategy._rebuild_opportunity_graph_and_resubscribe(instruments)
        graph = strategy._opportunity_graph
        self.ensure(graph.semantic_templates_stale() is False)

        # A store swap (the hot-swap precedent) must route through build().
        strategy._matcher.set_rule_store(
            RuleStore(FileRuleCache(tmp_path / "swapped-rules")),
        )
        self.ensure(graph.semantic_templates_stale() is True)

        added = TestBettingArbitrageStrategy._sxbet_instrument(
            event_id="evt-0",
            outcome="over",
            venue="BLACKBET",
            params="line=2.5",
        )
        strategy._subscribed_instruments.add(added)
        strategy._rebuild_after_instrument_refresh("BLACKBET", [added], [])

        self.ensure(strategy._instrument_refresh_graph_rebuilds == 1)
        self.ensure(strategy._instrument_refresh_graph_incremental_updates == 0)


# Note: Full integration tests with actual instrument subscriptions and quote ticks
# would require more complex mocking of NautilusTrader components (cache, msgbus, etc.)
# These tests focus on configuration and core logic validation.
