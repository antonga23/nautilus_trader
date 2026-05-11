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
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.runtime_cache import active_venue_instrument_index_key
from nautilus_trader.adapters.betting.runtime_cache import encode_active_venue_instrument_index
from nautilus_trader.adapters.betting.runtime_cache import encode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key
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
        ensure(config.semantic_quote_subscription_limit_by_venue == {})
        ensure(config.semantic_unmatched_quote_probe_venues == frozenset({"POLYMARKET"}))
        ensure(config.semantic_unmatched_quote_probe_limit_per_venue == 20)
        ensure(config.quote_freshness_profile == "pre_match")
        ensure(config.quote_max_pair_skew_secs is None)
        ensure(config.quote_max_fetch_latency_secs is None)
        ensure(config.instrument_refresh_interval_secs is None)
        ensure(config.stale_quote_refresh_cooldown_secs == 60.0)
        ensure(config.venue_taker_fee_rates == {"POLYMARKET": Decimal("0.03")})
        ensure(config.venue_maker_rebate_rates == {})
        ensure(config.venue_winning_profit_fee_rates == {})
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
        ensure(config.execution_max_retry_count == 1)
        ensure(config.execution_retry_slippage_bps == 25)

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
            == {"POLYMARKET": Decimal("0.02"), "SXBET": Decimal("0.01")},
        )
        ensure(config.venue_maker_rebate_rates == {"POLYMARKET": Decimal("0.0075")})
        ensure(config.venue_winning_profit_fee_rates == {"CLOUDBET": Decimal("0.005")})
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
        ensure("quote_subscription_counts_by_venue" in stats)
        ensure("semantic_quote_subscription_limit_by_venue" in stats)
        ensure("semantic_quote_subscription_limit_exceeded_by_venue" in stats)
        ensure(stats["venue_taker_fee_rates"] == {"POLYMARKET": "0.03"})
        ensure(stats["venue_maker_rebate_rates"] == {})
        ensure(stats["venue_winning_profit_fee_rates"] == {})
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


# Note: Full integration tests with actual instrument subscriptions and quote ticks
# would require more complex mocking of NautilusTrader components (cache, msgbus, etc.)
# These tests focus on configuration and core logic validation.
