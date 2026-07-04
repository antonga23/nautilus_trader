# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
# skipcq: PYL-C0302, PYL-E0611, PYL-R0902, PYL-R0911, PYL-R0913, PYL-R0914, PYL-R0917
# pylint: disable=no-name-in-module,too-many-arguments,too-many-instance-attributes,too-many-lines,too-many-locals,too-many-positional-arguments,too-many-return-statements
"""
Cross-venue arbitrage strategy for sports betting.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
import os
import time
from typing import Any

import msgspec

from nautilus_trader.adapters.betting.common.fees import DEFAULT_TAKER_FEE_RATES
from nautilus_trader.adapters.betting.common.fees import FeeAdjustedCoverageBasket
from nautilus_trader.adapters.betting.common.fees import fee_adjusted_basket_margin
from nautilus_trader.adapters.betting.common.fees import fee_adjusted_coverage_basket
from nautilus_trader.adapters.betting.common.fees import fee_adjusted_odds
from nautilus_trader.adapters.betting.common.fees import normalize_venue_fee_rates
from nautilus_trader.adapters.betting.common.odds import DeviggedBook
from nautilus_trader.adapters.betting.common.odds import calculate_arbitrage_stakes
from nautilus_trader.adapters.betting.common.odds import devig_probabilities
from nautilus_trader.adapters.betting.fixture_identity import DEFAULT_FIXTURE_IDENTITY_RESOLVER
from nautilus_trader.adapters.betting.fx import FxConversion
from nautilus_trader.adapters.betting.fx import PortfolioCurrencyPolicy
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.runtime_cache import active_venue_instrument_index_key
from nautilus_trader.adapters.betting.runtime_cache import decode_active_venue_instrument_index
from nautilus_trader.adapters.betting.runtime_cache import decode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import PolymarketSportsTransformer
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.message import Event
from nautilus_trader.common.events import TimeEvent
from nautilus_trader.examples.strategies.opportunity_graph import FastCandidateSnapshot
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityCandidate
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.instruments.base import Instrument
from nautilus_trader.model.instruments.crypto_betting import (
    CryptoBettingInstrument as LegacyCryptoBettingInstrument,
)
from nautilus_trader.trading.strategy import Strategy


VALID_MARKET_TIMINGS = frozenset({"all", "pre_market", "live"})
VALID_QUOTE_FRESHNESS_PROFILES = frozenset({"pre_match", "live", "custom"})
VALID_DEVIG_METHODS = frozenset({"auto", "proportional", "shin", "logarithmic"})
VALID_EXECUTION_PRICE_CHANGE_POLICIES = frozenset({"none", "better", "all"})
VALID_EXECUTION_VENUE_MODES = frozenset({"all", "cross_venue", "same_venue"})
DEFAULT_ENABLED_VENUES = frozenset({"CLOUDBET", "SXBET", "10BET"})
NANOSECONDS_PER_SECOND = 1_000_000_000
INSTRUMENT_REFRESH_TIMER_NAME = "betting-arbitrage-instrument-refresh"
INSTRUMENT_RECONCILE_TIMER_PREFIX = "betting-arbitrage-instrument-reconcile"
INSTRUMENT_RECONCILE_DELAY_SECS = 5.0
RESOLUTION_HORIZON_STALE_GRACE_HOURS = 6.0
LATENCY_SAMPLE_LIMIT = 2_000
BettingInstrument = CryptoBettingInstrument | LegacyCryptoBettingInstrument
BETTING_INSTRUMENT_TYPES = (CryptoBettingInstrument, LegacyCryptoBettingInstrument)


@dataclass(frozen=True)
class QuoteFreshnessThresholds:
    profile: str
    max_quote_age_secs: float
    max_pair_skew_secs: float
    max_fetch_latency_secs: float


@dataclass(frozen=True)
# skipcq: PYL-R0902
class ArbitrageDiagnostics:  # skipcq
    """
    Structured diagnostics captured for one arbitrage evaluation.
    """

    opportunity_id: str
    canonical_pair_id: str
    match_type: str
    hedge_match_type: str
    hedge_confidence: float
    instrument_a: BettingInstrument
    instrument_b: BettingInstrument
    event_id_a: str
    event_id_b: str
    instrument_id_a: str
    instrument_id_b: str
    event_name_a: str
    event_name_b: str
    canonical_event_key_a: str
    canonical_event_key_b: str
    market_id_a: str
    market_id_b: str
    market_name_a: str
    market_name_b: str
    params_a: str
    params_b: str
    outcome_a: str
    outcome_b: str
    venue_a: str
    venue_b: str
    odds_a: Decimal
    odds_b: Decimal
    quote_ts_a: int
    quote_ts_b: int
    quote_cycle_id_a: str
    quote_cycle_id_b: str
    quote_age_a_secs: float
    quote_age_b_secs: float
    quote_delta_secs: float
    fetch_latency_a_secs: float
    fetch_latency_b_secs: float
    freshness_profile: str
    max_quote_age_secs: float
    max_pair_skew_secs: float
    max_fetch_latency_secs: float
    same_quote_cycle: bool
    stale: bool
    fetch_latency_stale: bool
    matcher_suspect: bool
    suspect_reason: str
    suggested_stake_a: Decimal
    suggested_stake_b: Decimal
    expected_profit: Decimal
    raw_profit_margin: Decimal
    fee_adjusted_profit_margin: Decimal
    fee_drag: Decimal
    raw_total_probability: Decimal
    fee_adjusted_total_probability: Decimal
    taker_fee_rate_a: Decimal
    taker_fee_rate_b: Decimal
    maker_rebate_rate_a: Decimal
    maker_rebate_rate_b: Decimal
    winning_profit_fee_rate_a: Decimal
    winning_profit_fee_rate_b: Decimal
    basket_rebate_rate: Decimal
    basket_boost_rate: Decimal
    available_size_a: Decimal
    available_size_b: Decimal
    classification: str
    classification_reason: str


@dataclass(frozen=True)
class OpportunityPairState:
    """
    Active duplicate-suppression state for a continuously visible pair.
    """

    last_opportunity_id: str
    last_accepted_ns: int
    last_seen_ns: int


class BettingArbitrageConfig(StrategyConfig, frozen=True):
    """
    Configuration for betting arbitrage strategy.

    Parameters
    ----------
    min_profit_margin : Decimal
        Minimum profit margin required (e.g., 0.01 for 1%).
    max_total_stake : Decimal
        Maximum total stake across all venues.
    enabled_venues : frozenset[str]
        Venues to include in arbitrage (e.g., {"CLOUDBET", "SXBET"}).
    sport_filter : str | None
        Filter for specific sport (e.g., "soccer", "basketball").
        If None, all sports are included.
    market_timing_filter : str
        Filter by market timing: "all", "pre_market", "live".
        Default is "all".
    exclude_live : bool
        If True, exclude live/in-play markets (convenience flag).
        Overrides market_timing_filter if set to True.
    rollover_aware : bool, default True
        Consider rollover requirements in stake sizing.
    auto_execute : bool, default False
        Automatically execute arbitrage when found.
    arbitrage_quote_stale_threshold_secs : float, default 30.0
        Maximum quote age before an arbitrage candidate is treated as stale.
    duplicate_suppression_cooldown_secs : float, default 60.0
        Candidate gap after which the same pair may be logged as a fresh opportunity.
    arbitrage_summary_interval_secs : float, default 60.0
        Minimum interval between arbitrage quality summary log lines.
    opportunity_graph_enabled : bool, default True
        Use the persistent opportunity graph instead of quote-time hedge discovery.
    opportunity_log_manual_instructions : bool, default True
        Include manual execution fields in arbitrage logs.
    graph_rebuild_on_new_instrument : bool, default True
        Add newly observed instruments to the opportunity graph incrementally.
    opportunity_graph_engine : str, default "auto"
        Opportunity graph engine: "auto", "python", "rust", or "semantic_rust".
    semantic_rule_cache_dir : str | None, default None
        Optional file-backed semantic rule cache directory for trading-node runtime.
    semantic_quote_subscription_limit_by_venue : dict[str, int], default {}
        Optional venue-level cap for semantic-connected quote subscriptions. Trading-node
        manifests derive this from provider ``quote_subscription_limit`` so semantic graph
        mode cannot silently subscribe every connected instrument.
    semantic_unmatched_quote_probe_venues : frozenset[str], default {"POLYMARKET"}
        Venues whose unmatched instruments should still receive bounded quote
        subscriptions in semantic mode for audit/runtime diagnostics.
    semantic_unmatched_quote_probe_limit_per_venue : int, default 20
        Maximum unmatched quote probes per venue when semantic quote priority is active.
    quote_freshness_profile : str, default "pre_match"
        Quote timing policy: "pre_match", "live", or "custom".
    quote_max_pair_skew_secs : float | None, default None
        Custom maximum timestamp skew between two quotes.
    quote_max_fetch_latency_secs : float | None, default None
        Custom maximum REST fetch latency encoded as ``QuoteTick.ts_init - ts_event``.
    live_quote_age_slo_secs : float, default 5.0
        Maximum live quote age at strategy decision time for diagnostics.
    instrument_refresh_interval_secs : float | None, default None
        Optional timer interval for requesting refreshed venue instrument catalogs.
    stale_quote_refresh_cooldown_secs : float | None, default 60.0
        Minimum interval between stale-quote-triggered catalog refresh requests per venue.
    venue_taker_fee_rates : dict[str, Decimal], default {"POLYMARKET": Decimal("0.03")}
        Venue-level taker fee-rate parameters. Prediction-market venues use the
        protocol formula ``shares * rate * price * (1 - price)``.
    venue_maker_rebate_rates : dict[str, Decimal], default {}
        Venue-level maker rebate-rate parameters. These use the same
        prediction-market curve as taker fees, but reduce effective stake cost
        for passive fills.
    venue_winning_profit_fee_rates : dict[str, Decimal], default {}
        Venue-level fee rates applied only to winning profit.
    venue_basket_rebate_rates : dict[str, Decimal], default {}
        Venue-level basket reward/cashback rate applied to the covered set,
        useful for parlay-style or temporary promotion accounting.
    venue_basket_boost_rates : dict[str, Decimal], default {}
        Venue-level return boost rate applied to the covered set.
    devig_enabled : bool, default True
        Enable no-vig fair-probability diagnostics for complete books and quoted semantic pairs.
    devig_method : str, default "auto"
        Devig method: "auto", "proportional", "shin", or "logarithmic".
    devig_reference_venues : frozenset[str] | None, default None
        Optional venue allowlist for value-reference diagnostics. Empty means every complete
        sportsbook/exchange book can contribute labelled fair-value diagnostics.
    value_diagnostics_enabled : bool, default True
        Emit value-edge diagnostics based on devigged fair probabilities.
    value_execution_enabled : bool, default False
        Reserved execution gate for future value-betting strategies. It must remain false for
        validation deployments unless explicitly approved.
    min_value_edge : Decimal, default Decimal("0.015")
        Minimum net value edge used for dry-run value diagnostics.
    live_execution_armed : bool, default False
        Manifest-level live execution arming flag. The environment gate
        ``BETTING_LIVE_EXECUTION_ARMED`` must also be truthy before order submission.
    max_leg_stake : Decimal, default Decimal("15")
        Maximum stake for any single live execution leg.
    max_daily_notional : Decimal, default Decimal("100")
        Maximum live execution notional allowed for the process lifetime.
    max_daily_loss : Decimal, default Decimal("25")
        Daily realized loss guardrail reported in runtime status. This slice
        blocks new live execution when externally updated realized loss reaches the cap.
    allow_same_venue_live_execution : bool, default True
        Permit same-venue execution only after strict same-venue risk checks pass.
    allow_cross_currency_live_execution : bool, default False
        Permit cross-venue execution when settlement currencies differ. Disabled
        by default because locked payout math is not currency-neutral without an
        explicit FX/currency-risk policy.
    execution_price_change_policy : str, default "better"
        Venue price-change policy for live execution.
    live_execution_kill_switch_path : str | None, default None
        Optional file path which halts live execution when present.

    """

    min_profit_margin: Decimal = Decimal("0.01")
    max_total_stake: Decimal = Decimal(1000)
    enabled_venues: frozenset[str] = DEFAULT_ENABLED_VENUES
    sport_filter: str | None = None
    market_timing_filter: str = "all"
    exclude_live: bool = False
    rollover_aware: bool = True
    auto_execute: bool = False
    arbitrage_quote_stale_threshold_secs: float = 30.0
    duplicate_suppression_cooldown_secs: float = 60.0
    arbitrage_summary_interval_secs: float = 60.0
    opportunity_graph_enabled: bool = True
    opportunity_log_manual_instructions: bool = True
    graph_rebuild_on_new_instrument: bool = True
    opportunity_graph_engine: str = "auto"
    semantic_rule_cache_dir: str | None = None
    semantic_quote_subscription_limit_by_venue: dict[str, int] = {}
    semantic_unmatched_quote_probe_venues: frozenset[str] = frozenset({"POLYMARKET"})
    semantic_unmatched_quote_probe_limit_per_venue: int = 20
    quote_freshness_profile: str = "pre_match"
    quote_max_pair_skew_secs: float | None = None
    quote_max_fetch_latency_secs: float | None = None
    live_quote_age_slo_secs: float = 5.0
    max_resolution_horizon_hours: float | None = None
    instrument_refresh_interval_secs: float | None = None
    stale_quote_refresh_cooldown_secs: float | None = 60.0
    venue_taker_fee_rates: dict[str, Decimal] = {}
    venue_maker_rebate_rates: dict[str, Decimal] = {}
    venue_winning_profit_fee_rates: dict[str, Decimal] = {}
    venue_basket_rebate_rates: dict[str, Decimal] = {}
    venue_basket_boost_rates: dict[str, Decimal] = {}
    devig_enabled: bool = True
    devig_method: str = "auto"
    devig_reference_venues: frozenset[str] | None = None
    value_diagnostics_enabled: bool = True
    value_execution_enabled: bool = False
    min_value_edge: Decimal = Decimal("0.015")
    live_execution_armed: bool = False
    max_leg_stake: Decimal = Decimal(15)
    max_daily_notional: Decimal = Decimal(100)
    max_daily_loss: Decimal = Decimal(25)
    allow_same_venue_live_execution: bool = True
    allow_cross_currency_live_execution: bool = False
    execution_venue_mode: str = "all"
    portfolio_base_currency: str = "USD"
    stablecoin_currencies: frozenset[str] = frozenset({"USD", "USDC", "USDT"})
    stablecoin_haircut_bps: int = 10
    fx_quote_max_age_secs: float = 30.0
    configured_fx_rates: dict[str, Decimal] = {}
    execution_price_change_policy: str = "better"
    live_execution_kill_switch_path: str | None = None

    def __post_init__(self) -> None:
        """
        Normalize configured venues and market-timing filters.
        """
        enabled_venues = frozenset(self.enabled_venues or DEFAULT_ENABLED_VENUES)
        normalized_sport_filter = self.sport_filter.strip().lower() if self.sport_filter else None
        market_timing_filter = self.market_timing_filter if not self.exclude_live else "pre_market"
        semantic_rule_cache_dir = (
            self.semantic_rule_cache_dir.strip() if self.semantic_rule_cache_dir else None
        )
        live_execution_kill_switch_path = (
            self.live_execution_kill_switch_path.strip()
            if self.live_execution_kill_switch_path
            else None
        )
        semantic_unmatched_quote_probe_venues = frozenset(
            str(venue).strip().upper()
            for venue in self.semantic_unmatched_quote_probe_venues
            if str(venue).strip()
        )
        semantic_quote_subscription_limit_by_venue = (
            self._normalize_semantic_quote_subscription_limits(
                self.semantic_quote_subscription_limit_by_venue,
            )
        )
        venue_taker_fee_rates = normalize_venue_fee_rates(
            self.venue_taker_fee_rates,
            defaults=DEFAULT_TAKER_FEE_RATES,
        )
        venue_maker_rebate_rates = normalize_venue_fee_rates(
            self.venue_maker_rebate_rates,
        )
        venue_winning_profit_fee_rates = normalize_venue_fee_rates(
            self.venue_winning_profit_fee_rates,
        )
        venue_basket_rebate_rates = normalize_venue_fee_rates(
            self.venue_basket_rebate_rates,
        )
        venue_basket_boost_rates = normalize_venue_fee_rates(
            self.venue_basket_boost_rates,
        )
        opportunity_graph_engine = self.opportunity_graph_engine.strip().lower()
        quote_freshness_profile = self.quote_freshness_profile.strip().lower()
        devig_method, devig_reference_venues = self._normalize_devig_config()
        execution_venue_mode = self.execution_venue_mode.strip().lower()
        execution_price_change_policy = self.execution_price_change_policy.strip().lower()
        portfolio_base_currency = self.portfolio_base_currency.strip().upper() or "USD"
        stablecoin_currencies = frozenset(
            str(currency).strip().upper()
            for currency in self.stablecoin_currencies
            if str(currency).strip()
        )
        configured_fx_rates = {
            str(pair).strip().upper(): Decimal(str(rate))
            for pair, rate in self.configured_fx_rates.items()
            if str(pair).strip()
        }

        self._validate_filter_config(
            market_timing_filter=market_timing_filter,
            quote_freshness_profile=quote_freshness_profile,
            opportunity_graph_engine=opportunity_graph_engine,
        )
        self._validate_live_execution_config(
            execution_venue_mode=execution_venue_mode,
            execution_price_change_policy=execution_price_change_policy,
            stablecoin_currencies=stablecoin_currencies,
        )
        self._validate_refresh_config()

        msgspec.structs.force_setattr(self, "enabled_venues", enabled_venues)
        msgspec.structs.force_setattr(self, "sport_filter", normalized_sport_filter)
        msgspec.structs.force_setattr(self, "market_timing_filter", market_timing_filter)
        msgspec.structs.force_setattr(self, "opportunity_graph_engine", opportunity_graph_engine)
        msgspec.structs.force_setattr(self, "semantic_rule_cache_dir", semantic_rule_cache_dir)
        msgspec.structs.force_setattr(
            self,
            "live_execution_kill_switch_path",
            live_execution_kill_switch_path,
        )
        msgspec.structs.force_setattr(
            self,
            "semantic_unmatched_quote_probe_venues",
            semantic_unmatched_quote_probe_venues,
        )
        msgspec.structs.force_setattr(
            self,
            "semantic_quote_subscription_limit_by_venue",
            semantic_quote_subscription_limit_by_venue,
        )
        msgspec.structs.force_setattr(self, "venue_taker_fee_rates", venue_taker_fee_rates)
        msgspec.structs.force_setattr(
            self,
            "venue_maker_rebate_rates",
            venue_maker_rebate_rates,
        )
        msgspec.structs.force_setattr(
            self,
            "venue_winning_profit_fee_rates",
            venue_winning_profit_fee_rates,
        )
        msgspec.structs.force_setattr(
            self,
            "venue_basket_rebate_rates",
            venue_basket_rebate_rates,
        )
        msgspec.structs.force_setattr(
            self,
            "venue_basket_boost_rates",
            venue_basket_boost_rates,
        )
        msgspec.structs.force_setattr(self, "quote_freshness_profile", quote_freshness_profile)
        msgspec.structs.force_setattr(self, "devig_method", devig_method)
        msgspec.structs.force_setattr(self, "devig_reference_venues", devig_reference_venues)
        msgspec.structs.force_setattr(
            self,
            "execution_price_change_policy",
            execution_price_change_policy,
        )
        msgspec.structs.force_setattr(self, "execution_venue_mode", execution_venue_mode)
        msgspec.structs.force_setattr(self, "portfolio_base_currency", portfolio_base_currency)
        msgspec.structs.force_setattr(self, "stablecoin_currencies", stablecoin_currencies)
        msgspec.structs.force_setattr(self, "configured_fx_rates", configured_fx_rates)
        msgspec.structs.force_setattr(
            self,
            "live_quote_age_slo_secs",
            float(self.live_quote_age_slo_secs),
        )
        if self.max_resolution_horizon_hours is not None:
            msgspec.structs.force_setattr(
                self,
                "max_resolution_horizon_hours",
                float(self.max_resolution_horizon_hours),
            )
        msgspec.structs.force_setattr(
            self,
            "fx_quote_max_age_secs",
            float(self.fx_quote_max_age_secs),
        )
        if self.stale_quote_refresh_cooldown_secs is not None:
            msgspec.structs.force_setattr(
                self,
                "stale_quote_refresh_cooldown_secs",
                float(self.stale_quote_refresh_cooldown_secs),
            )

    def _validate_filter_config(
        self,
        *,
        market_timing_filter: str,
        quote_freshness_profile: str,
        opportunity_graph_engine: str,
    ) -> None:
        if market_timing_filter not in VALID_MARKET_TIMINGS:
            msg = (
                f"Invalid market_timing_filter: {market_timing_filter}. "
                f"Must be one of {VALID_MARKET_TIMINGS}"
            )
            raise ValueError(msg)
        if quote_freshness_profile not in VALID_QUOTE_FRESHNESS_PROFILES:
            msg = (
                f"Invalid quote_freshness_profile: {quote_freshness_profile}. "
                f"Must be one of {VALID_QUOTE_FRESHNESS_PROFILES}"
            )
            raise ValueError(msg)
        if opportunity_graph_engine not in {"auto", "python", "rust", "semantic_rust"}:
            msg = (
                f"Invalid opportunity_graph_engine: {opportunity_graph_engine}. "
                "Must be one of {'auto', 'python', 'rust', 'semantic_rust'}"
            )
            raise ValueError(msg)
        if self.duplicate_suppression_cooldown_secs < 0:
            msg = "duplicate_suppression_cooldown_secs must be non-negative"
            raise ValueError(msg)
        if self.live_quote_age_slo_secs <= 0:
            msg = "live_quote_age_slo_secs must be positive"
            raise ValueError(msg)
        if self.max_resolution_horizon_hours is not None and self.max_resolution_horizon_hours <= 0:
            msg = "max_resolution_horizon_hours must be positive when set"
            raise ValueError(msg)
        if self.semantic_unmatched_quote_probe_limit_per_venue < 0:
            msg = "semantic_unmatched_quote_probe_limit_per_venue must be non-negative"
            raise ValueError(msg)

    def _validate_live_execution_config(
        self,
        *,
        execution_venue_mode: str,
        execution_price_change_policy: str,
        stablecoin_currencies: frozenset[str],
    ) -> None:
        if execution_venue_mode not in VALID_EXECUTION_VENUE_MODES:
            msg = (
                f"Invalid execution_venue_mode: {execution_venue_mode}. "
                f"Must be one of {VALID_EXECUTION_VENUE_MODES}"
            )
            raise ValueError(msg)
        if execution_price_change_policy not in VALID_EXECUTION_PRICE_CHANGE_POLICIES:
            msg = (
                f"Invalid execution_price_change_policy: {execution_price_change_policy}. "
                f"Must be one of {VALID_EXECUTION_PRICE_CHANGE_POLICIES}"
            )
            raise ValueError(msg)
        if self.max_leg_stake <= 0:
            msg = "max_leg_stake must be positive"
            raise ValueError(msg)
        if self.max_daily_notional <= 0:
            msg = "max_daily_notional must be positive"
            raise ValueError(msg)
        if self.max_daily_loss < 0:
            msg = "max_daily_loss must be non-negative"
            raise ValueError(msg)
        self._validate_portfolio_currency_config(stablecoin_currencies)

    def _validate_portfolio_currency_config(
        self,
        stablecoin_currencies: frozenset[str],
    ) -> None:
        if self.stablecoin_haircut_bps < 0:
            msg = "stablecoin_haircut_bps must be non-negative"
            raise ValueError(msg)
        if self.fx_quote_max_age_secs <= 0:
            msg = "fx_quote_max_age_secs must be positive"
            raise ValueError(msg)
        if not stablecoin_currencies:
            msg = "stablecoin_currencies must not be empty"
            raise ValueError(msg)

    def _validate_refresh_config(self) -> None:
        if (
            self.instrument_refresh_interval_secs is not None
            and self.instrument_refresh_interval_secs <= 0
        ):
            msg = "instrument_refresh_interval_secs must be positive when set"
            raise ValueError(msg)
        if (
            self.stale_quote_refresh_cooldown_secs is not None
            and self.stale_quote_refresh_cooldown_secs <= 0
        ):
            msg = "stale_quote_refresh_cooldown_secs must be positive when set"
            raise ValueError(msg)

    def _normalize_devig_config(self) -> tuple[str, frozenset[str] | None]:
        devig_method = self.devig_method.strip().lower()
        if devig_method not in VALID_DEVIG_METHODS:
            msg = f"Invalid devig_method: {devig_method}. Must be one of {VALID_DEVIG_METHODS}"
            raise ValueError(msg)
        if self.min_value_edge < 0:
            msg = "min_value_edge must be non-negative"
            raise ValueError(msg)
        if self.value_execution_enabled and not self.value_diagnostics_enabled:
            msg = "value_execution_enabled requires value_diagnostics_enabled"
            raise ValueError(msg)
        reference_venues = (
            frozenset(
                str(venue).strip().upper()
                for venue in self.devig_reference_venues
                if str(venue).strip()
            )
            if self.devig_reference_venues is not None
            else None
        )
        return devig_method, reference_venues

    @staticmethod
    def _normalize_semantic_quote_subscription_limits(
        limits: dict[str, int],
    ) -> dict[str, int]:
        normalized = {
            str(venue).strip().upper(): int(limit)
            for venue, limit in limits.items()
            if str(venue).strip() and limit is not None
        }
        invalid = {venue: limit for venue, limit in normalized.items() if limit < 0}
        if invalid:
            msg = (
                f"semantic_quote_subscription_limit_by_venue values must be non-negative: {invalid}"
            )
            raise ValueError(msg)
        return normalized


# skipcq: PYL-R0902
class BettingArbitrageStrategy(Strategy):  # skipcq
    """
    Cross-venue sports betting arbitrage strategy.

    Finds and executes arbitrage opportunities across multiple betting venues:
    1. Monitors quote ticks from all subscribed instruments
    2. Uses MarketMatcher to find hedge opportunities
    3. Calculates optimal stake allocation
    4. Validates with venue-specific risk engines
    5. Submits simultaneous orders to both venues

    Parameters
    ----------
    config : BettingArbitrageConfig
        Strategy configuration.

    """

    def __init__(
        self,
        config: BettingArbitrageConfig,
    ):
        """
        Initialize the strategy state, matcher, and opportunity graph.
        """
        super().__init__(config)
        self._config = config

        # Market matcher for finding arbitrage
        self._matcher = MarketMatcher()
        self._opportunity_graph = OpportunityGraph(
            self._matcher,
            engine=config.opportunity_graph_engine,
        )

        # Tracking
        self._subscribed_instruments: set[BettingInstrument] = set()
        self._quote_subscribed_instrument_ids: set[str] = set()
        self._betting_instruments_by_source_id: dict[str, CryptoBettingInstrument] = {}
        self._source_ids_by_betting_instrument_id: dict[str, InstrumentId] = {}
        self._latest_quotes: dict[str, QuoteTick] = {}
        self._opportunities_found = 0
        self._opportunities_executed = 0
        self._raw_arbitrage_detections = 0
        self._duplicate_opportunities_suppressed = 0
        self._stale_quote_suppressions = 0
        self._matcher_suspect_suppressions = 0
        self._liquidity_suppressions = 0
        self._manual_review_suppressions = 0
        self._executable_candidates = 0
        self._live_execution_attempts = 0
        self._live_execution_blocks = 0
        self._live_execution_submissions = 0
        self._live_execution_unhedged_exposures = 0
        self._live_execution_halt_reason: str | None = None
        self._live_execution_notional_used = Decimal(0)
        self._live_execution_realized_loss = Decimal(0)
        self._live_execution_block_reasons: Counter[str] = Counter()
        self._live_execution_submissions_by_venue: Counter[str] = Counter()
        self._order_lifecycle_counts_by_venue: dict[str, Counter[str]] = {}
        self._instrument_refresh_requests = 0
        self._instrument_refresh_failures = 0
        self._instrument_refresh_added = 0
        self._instrument_refresh_removed = 0
        self._instrument_refresh_delisted_removed = 0
        self._instrument_refresh_reconciles = 0
        self._instrument_refresh_graph_rebuilds = 0
        self._instrument_refresh_stale_triggers = 0
        self._quote_unsubscribe_requests = 0
        self._instrument_refresh_requests_by_venue: Counter[str] = Counter()
        self._instrument_refresh_failures_by_venue: Counter[str] = Counter()
        self._instrument_refresh_added_by_venue: Counter[str] = Counter()
        self._instrument_refresh_removed_by_venue: Counter[str] = Counter()
        self._instrument_refresh_delisted_removed_by_venue: Counter[str] = Counter()
        self._instrument_refresh_reconciles_by_venue: Counter[str] = Counter()
        self._instrument_refresh_graph_rebuilds_by_venue: Counter[str] = Counter()
        self._instrument_refresh_stale_triggers_by_venue: Counter[str] = Counter()
        self._quote_unsubscribe_requests_by_venue: Counter[str] = Counter()
        self._pending_refresh_reconcile_venues: set[str] = set()
        self._last_refresh_request_at_ns: dict[str, int] = {}
        self._last_stale_refresh_trigger_at_ns: dict[str, int] = {}
        self._seen_opportunity_pairs: set[str] = set()
        self._active_opportunity_pairs: dict[str, OpportunityPairState] = {}
        self._graph_scan_latency_ns: list[int] = []
        self._candidate_decision_latency_ns: list[int] = []
        self._order_construction_latency_ns: list[int] = []
        self._order_submit_latency_ns: list[int] = []
        self._quote_event_to_strategy_latency_ns: list[int] = []
        self._quote_publish_to_strategy_latency_ns: list[int] = []
        self._quote_fetch_latency_ns: list[int] = []
        self._quote_event_to_strategy_latency_ns_by_venue: dict[str, list[int]] = {}
        self._quote_publish_to_strategy_latency_ns_by_venue: dict[str, list[int]] = {}
        self._quote_fetch_latency_ns_by_venue: dict[str, list[int]] = {}
        self._instrument_refresh_reconcile_latency_ns: list[int] = []
        self._last_arbitrage_summary_at_ns = 0

    @property
    def market_matcher(self) -> MarketMatcher:
        """
        Matcher used by runtime diagnostics and node probes.
        """
        return self._matcher

    @property
    def opportunity_graph(self) -> OpportunityGraph:
        """
        Opportunity graph used by runtime diagnostics and node probes.
        """
        return self._opportunity_graph

    def on_start(self) -> None:
        """
        Run strategy startup subscriptions and diagnostics logging.
        """
        self.log.info("BettingArbitrageStrategy starting...")
        rule_store = self._semantic_rule_store()
        if rule_store is not None:
            self._matcher.set_rule_store(rule_store)
        msg = f"Min profit margin: {self._config.min_profit_margin}"
        self.log.info(msg)
        msg = f"Max total stake: {self._config.max_total_stake}"
        self.log.info(msg)
        msg = f"Enabled venues: {self._config.enabled_venues}"
        self.log.info(msg)
        msg = f"Sport filter: {self._config.sport_filter or 'all'}"
        self.log.info(msg)
        msg = f"Market timing filter: {self._config.market_timing_filter}"
        self.log.info(msg)
        msg = f"Auto execute: {self._config.auto_execute}"
        self.log.info(msg)
        msg = (
            "Arbitrage diagnostics: "
            f"quote_stale_threshold_secs={self._config.arbitrage_quote_stale_threshold_secs} "
            f"quote_freshness_profile={self._config.quote_freshness_profile} "
            f"quote_max_pair_skew_secs={self._config.quote_max_pair_skew_secs} "
            f"quote_max_fetch_latency_secs={self._config.quote_max_fetch_latency_secs} "
            f"summary_interval_secs={self._config.arbitrage_summary_interval_secs} "
            f"opportunity_graph_enabled={self._config.opportunity_graph_enabled} "
            f"opportunity_graph_engine={self._config.opportunity_graph_engine} "
            f"devig_enabled={self._config.devig_enabled} "
            f"devig_method={self._config.devig_method} "
            f"value_diagnostics_enabled={self._config.value_diagnostics_enabled} "
            f"value_execution_enabled={self._config.value_execution_enabled} "
            f"min_value_edge={self._config.min_value_edge} "
            f"max_resolution_horizon_hours={self._config.max_resolution_horizon_hours} "
            f"live_execution_armed={self._config.live_execution_armed} "
            f"live_execution_env_armed={self._live_execution_env_armed()} "
            f"execution_venue_mode={self._config.execution_venue_mode} "
            f"portfolio_base_currency={self._config.portfolio_base_currency} "
            f"stablecoin_currencies={sorted(self._config.stablecoin_currencies)} "
            f"max_leg_stake={self._config.max_leg_stake} "
            f"max_daily_notional={self._config.max_daily_notional} "
            f"max_daily_loss={self._config.max_daily_loss} "
            f"allow_same_venue_live_execution={self._config.allow_same_venue_live_execution} "
            "allow_cross_currency_live_execution="
            f"{self._config.allow_cross_currency_live_execution} "
            "semantic_unmatched_quote_probe_venues="
            f"{sorted(self._config.semantic_unmatched_quote_probe_venues)} "
            "semantic_unmatched_quote_probe_limit_per_venue="
            f"{self._config.semantic_unmatched_quote_probe_limit_per_venue} "
            f"instrument_refresh_interval_secs={self._config.instrument_refresh_interval_secs} "
            "stale_quote_refresh_cooldown_secs="
            f"{self._config.stale_quote_refresh_cooldown_secs} "
            f"manual_instructions={self._config.opportunity_log_manual_instructions}"
        )
        self.log.info(msg)
        self._subscribe_enabled_venue_instrument_updates()
        self._subscribe_cached_instruments()
        self._start_instrument_refresh_timer()

    def _semantic_rule_store(self) -> RuleStore | None:
        if self._config.semantic_rule_cache_dir:
            rule_store = RuleStore(FileRuleCache(self._config.semantic_rule_cache_dir))
            return rule_store if self._has_semantic_rules(rule_store) else None

        rule_store = RuleStore(self.cache)
        return rule_store if self._has_semantic_rules(rule_store) else None

    @staticmethod
    def _has_semantic_rules(rule_store: RuleStore) -> bool:
        return bool(rule_store.list_manifest_ids() or rule_store.list_promoted_template_ids())

    def on_stop(self) -> None:
        """
        Run strategy shutdown logging and final summary emission.
        """
        self.log.info("BettingArbitrageStrategy stopping...")
        self._stop_instrument_refresh_timer()
        self._cancel_instrument_reconcile_timers()
        msg = f"Opportunities found: {self._opportunities_found}"
        self.log.info(msg)
        msg = f"Opportunities executed: {self._opportunities_executed}"
        self.log.info(msg)
        self._log_arbitrage_summary(force=True)

    def on_time_event(self, event: TimeEvent) -> None:
        """
        Refresh venue instrument catalogs on the configured runtime timer.
        """
        if event.name == INSTRUMENT_REFRESH_TIMER_NAME:
            self._refresh_enabled_venue_instruments()
            return
        if event.name.startswith(f"{INSTRUMENT_RECONCILE_TIMER_PREFIX}:"):
            venue_value = event.name.split(":", maxsplit=1)[-1].upper()
            self._pending_refresh_reconcile_venues.discard(venue_value)
            self._reconcile_cached_venue_instruments(venue_value)

    def subscribe_instruments(self, instruments: list[Instrument]) -> None:
        """
        Subscribe to instruments for arbitrage monitoring.

        Applies filtering by:
        - Enabled venues
        - Sport (if sport_filter specified)

        Parameters
        ----------
        instruments : list[Instrument]
            Instruments to monitor.

        """
        subscribed_before = len(self._subscribed_instruments)
        if self._semantic_batch_subscription_enabled():
            self._subscribe_instruments_semantic_batch(instruments)
            return

        for instrument in instruments:
            if not self._maybe_subscribe_instrument(instrument):
                continue
        if len(self._subscribed_instruments) != subscribed_before:
            self._log_graph_topology_summary()

    def on_instrument(self, instrument: Instrument) -> None:
        """
        Subscribe a newly seen betting instrument when it passes strategy filters.
        """
        self._maybe_subscribe_instrument(instrument)

    def _subscribe_cached_instruments(self) -> None:
        cached_instruments = [
            betting_instrument
            for instrument in self.cache.instruments()
            if (betting_instrument := self._coerce_betting_instrument(instrument)) is not None
        ]
        if not cached_instruments:
            self.log.warning("No cached betting instruments available at strategy start")
            return
        self.subscribe_instruments(cached_instruments)

    def _subscribe_enabled_venue_instrument_updates(self) -> None:
        for venue_value in sorted(self._config.enabled_venues):
            try:
                Strategy.subscribe_instruments(self, Venue(venue_value))
            except Exception as exc:
                self.log.warning(
                    "Unable to subscribe to venue instrument updates: "
                    f"venue={venue_value} error={exc}",
                )

    def _start_instrument_refresh_timer(self) -> None:
        interval_secs = self._config.instrument_refresh_interval_secs
        if interval_secs is None:
            return
        self.clock.set_timer(
            name=INSTRUMENT_REFRESH_TIMER_NAME,
            interval=timedelta(seconds=float(interval_secs)),
            callback=self.on_time_event,
        )
        self.log.info(
            f"Started betting instrument refresh timer: interval_secs={float(interval_secs):.3f}",
        )

    def _stop_instrument_refresh_timer(self) -> None:
        if self._config.instrument_refresh_interval_secs is None:
            return
        try:
            self.clock.cancel_timer(INSTRUMENT_REFRESH_TIMER_NAME)
        except Exception as exc:
            self.log.warning(f"Unable to cancel instrument refresh timer: error={exc}")

    def _cancel_instrument_reconcile_timers(self) -> None:
        for venue_value in list(self._pending_refresh_reconcile_venues):
            try:
                self.clock.cancel_timer(self._instrument_reconcile_timer_name(venue_value))
            except Exception as exc:
                self.log.warning(
                    f"Unable to cancel instrument reconcile timer: venue={venue_value} error={exc}",
                )
        self._pending_refresh_reconcile_venues.clear()

    @staticmethod
    def _instrument_reconcile_timer_name(venue_value: str) -> str:
        return f"{INSTRUMENT_RECONCILE_TIMER_PREFIX}:{venue_value.upper()}"

    def _schedule_instrument_reconcile(self, venue_value: str) -> None:
        timer_name = self._instrument_reconcile_timer_name(venue_value)
        if timer_name in self.clock.timer_names:
            self.clock.cancel_timer(timer_name)
        self._pending_refresh_reconcile_venues.add(venue_value)
        self.clock.set_time_alert(
            timer_name,
            self.clock.utc_now() + timedelta(seconds=INSTRUMENT_RECONCILE_DELAY_SECS),
            self.on_time_event,
        )

    def _refresh_enabled_venue_instruments(self) -> None:
        for venue_value in sorted(self._config.enabled_venues):
            try:
                requested_at_ns = self.clock.timestamp_ns()
                self._last_refresh_request_at_ns[venue_value] = requested_at_ns
                self.request_instruments(
                    venue=Venue(venue_value),
                    params={"semantic_refresh": True, "only_last": True},
                )
                self._instrument_refresh_requests += 1
                self._instrument_refresh_requests_by_venue[venue_value] += 1
                self._schedule_instrument_reconcile(venue_value)
            except Exception as exc:
                self._instrument_refresh_failures += 1
                self._instrument_refresh_failures_by_venue[venue_value] += 1
                self.log.warning(
                    "Unable to request refreshed betting instruments: "
                    f"venue={venue_value} error={exc}",
                )

    def _maybe_trigger_stale_quote_refresh(
        self,
        instrument_a: BettingInstrument,
        instrument_b: BettingInstrument,
        *,
        reason: str,
        now_ns: int,
    ) -> None:
        cooldown_secs = self._config.stale_quote_refresh_cooldown_secs
        if cooldown_secs is None:
            return
        for venue_value in sorted(
            {
                str(instrument_a.id.venue).upper(),
                str(instrument_b.id.venue).upper(),
            },
        ):
            last_triggered_ns = self._last_stale_refresh_trigger_at_ns.get(venue_value, 0)
            if last_triggered_ns > 0 and (
                now_ns - last_triggered_ns < int(cooldown_secs * NANOSECONDS_PER_SECOND)
            ):
                continue
            try:
                self._last_stale_refresh_trigger_at_ns[venue_value] = now_ns
                self._last_refresh_request_at_ns[venue_value] = now_ns
                self.request_instruments(
                    venue=Venue(venue_value),
                    params={
                        "semantic_refresh": True,
                        "only_last": True,
                        "trigger": reason,
                    },
                )
                self._instrument_refresh_requests += 1
                self._instrument_refresh_stale_triggers += 1
                self._instrument_refresh_requests_by_venue[venue_value] += 1
                self._instrument_refresh_stale_triggers_by_venue[venue_value] += 1
                self._schedule_instrument_reconcile(venue_value)
                self.log.info(
                    "Requested stale-quote-driven betting instrument refresh: "
                    f"venue={venue_value} reason={reason} cooldown_secs={cooldown_secs:.3f}",
                )
            except Exception as exc:
                self._instrument_refresh_failures += 1
                self._instrument_refresh_failures_by_venue[venue_value] += 1
                self.log.warning(
                    "Unable to request stale-quote-driven betting instrument refresh: "
                    f"venue={venue_value} reason={reason} error={exc}",
                )

    def _reconcile_cached_venue_instruments(self, venue_value: str) -> None:
        now_ns = self.clock.timestamp_ns()
        requested_at_ns = self._last_refresh_request_at_ns.get(venue_value, 0)
        if requested_at_ns > 0:
            self._record_latency_sample(
                self._instrument_refresh_reconcile_latency_ns,
                max(0, now_ns - requested_at_ns),
            )
        self._instrument_refresh_reconciles += 1
        self._instrument_refresh_reconciles_by_venue[venue_value] += 1
        active_cached = self._active_cached_venue_instruments(venue_value)
        if active_cached is None:
            # Cache read failed: abort reconcile for this venue rather than treating the
            # venue as having zero active instruments, which would mass-remove every
            # subscribed instrument and collapse cross-venue topology.
            return
        active_ids = {str(instrument.id) for instrument in active_cached}
        added_instruments = self._add_refreshed_active_instruments(active_cached)
        removed = self._remove_inactive_or_delisted_instruments(
            venue_value=venue_value,
            active_instrument_ids=active_ids,
        )
        added = len(added_instruments)
        if added <= 0 and removed <= 0:
            return

        self._instrument_refresh_added += added
        self._instrument_refresh_removed += removed
        self._instrument_refresh_added_by_venue[venue_value] += added
        self._instrument_refresh_removed_by_venue[venue_value] += removed
        self._rebuild_after_instrument_refresh(venue_value, added_instruments)
        self._log_graph_topology_summary()
        self.log.info(
            "Reconciled refreshed betting instruments: "
            f"venue={venue_value} added={added} removed={removed} "
            f"subscribed={len(self._subscribed_instruments)}",
        )

    def _active_cached_venue_instruments(
        self,
        venue_value: str,
    ) -> list[BettingInstrument] | None:
        try:
            cached_instruments = list(self.cache.instruments())
        except Exception as exc:
            # Return None (not []) so the caller distinguishes "read failed" from
            # "genuinely no active instruments" and skips removal on failure.
            self.log.warning(f"Instrument cache read failed for venue={venue_value}: {exc!r}")
            return None

        current_active_ids = self._current_active_refresh_ids(venue_value)
        active_cached: list[BettingInstrument] = []
        for instrument in cached_instruments:
            betting_instrument = self._coerce_betting_instrument(instrument)
            if betting_instrument is None:
                continue
            if not self._instrument_available_for_refresh(betting_instrument, venue_value):
                continue
            if (
                current_active_ids is not None
                and str(betting_instrument.id) not in current_active_ids
            ):
                continue
            active_cached.append(betting_instrument)
        return active_cached

    def _current_active_refresh_ids(self, venue_value: str) -> set[str] | None:
        try:
            raw = self.cache.get(active_venue_instrument_index_key(venue_value))
        except Exception:
            return None
        payload = decode_active_venue_instrument_index(raw)
        if payload is None or payload.venue != venue_value.upper():
            return None
        requested_at_ns = self._last_refresh_request_at_ns.get(venue_value, 0)
        if requested_at_ns > 0 and payload.updated_at_ns < requested_at_ns:
            return None
        return set(payload.instrument_ids)

    def _add_refreshed_active_instruments(
        self,
        instruments: list[BettingInstrument],
    ) -> list[BettingInstrument]:
        existing_ids = {str(instrument.id) for instrument in self._subscribed_instruments}
        added: list[BettingInstrument] = []
        for instrument in instruments:
            if str(instrument.id) in existing_ids:
                continue
            self._subscribed_instruments.add(instrument)
            existing_ids.add(str(instrument.id))
            added.append(instrument)
        return added

    def _rebuild_after_instrument_refresh(
        self,
        venue_value: str,
        added_instruments: list[BettingInstrument],
    ) -> None:
        if not (
            self._config.opportunity_graph_enabled and self._config.graph_rebuild_on_new_instrument
        ):
            return
        self._opportunity_graph.build(list(self._subscribed_instruments))
        self._instrument_refresh_graph_rebuilds += 1
        self._instrument_refresh_graph_rebuilds_by_venue[venue_value] += 1
        if self._semantic_quote_priority_enabled():
            self._subscribe_cross_venue_common_fixture_quote_ticks()
            self._subscribe_semantic_connected_quote_ticks()
            self._subscribe_semantic_unmatched_quote_probe_ticks()
            return
        for instrument in added_instruments:
            self._subscribe_quote_ticks_for_instrument(instrument)

    def _instrument_available_for_refresh(
        self,
        instrument: BettingInstrument | None,
        venue_value: str,
    ) -> bool:
        return (
            instrument is not None
            and instrument.id.venue.value == venue_value
            and self._should_process_instrument(instrument)
            and self._instrument_is_active_for_refresh(instrument)
        )

    def _remove_inactive_or_delisted_instruments(
        self,
        *,
        venue_value: str,
        active_instrument_ids: set[str],
    ) -> int:
        subscribed_snapshot = tuple(self._subscribed_instruments)
        to_remove = [
            instrument
            for instrument in subscribed_snapshot
            if instrument.id.venue.value == venue_value
            and (
                str(instrument.id) not in active_instrument_ids
                or not self._instrument_is_active_for_refresh(instrument)
                or not self._should_process_instrument(instrument)
            )
        ]
        for instrument in to_remove:
            self._remove_subscribed_instrument(instrument)
        self._instrument_refresh_delisted_removed += len(to_remove)
        self._instrument_refresh_delisted_removed_by_venue[venue_value] += len(to_remove)
        return len(to_remove)

    def _remove_subscribed_instrument(self, instrument: BettingInstrument) -> None:
        self._subscribed_instruments.discard(instrument)
        quote_instrument_id = self._quote_subscription_instrument_id(instrument)
        for instrument_id in (instrument.id, quote_instrument_id):
            self._latest_quotes.pop(str(instrument_id), None)
        if str(quote_instrument_id) in self._quote_subscribed_instrument_ids:
            self._quote_subscribed_instrument_ids.discard(str(quote_instrument_id))
            self._quote_unsubscribe_requests += 1
            self._quote_unsubscribe_requests_by_venue[instrument.id.venue.value.upper()] += 1
            try:
                self.unsubscribe_quote_ticks(quote_instrument_id)
            except Exception as exc:
                self.log.warning(
                    "Unable to unsubscribe delisted betting quote stream: "
                    f"instrument_id={quote_instrument_id} error={exc}",
                )

    @staticmethod
    def _instrument_is_active_for_refresh(instrument: BettingInstrument) -> bool:
        if not bool(getattr(instrument, "enabled", True)):
            return False
        status = getattr(instrument, "trading_status", None)
        if status is None:
            return True
        normal_status = str(status).strip().upper()
        inactive_statuses = {
            "CANCELED",
            "CANCELLED",
            "CLOSED",
            "ENDED",
            "EXPIRED",
            "FINISHED",
            "HALTED",
            "INACTIVE",
            "RESULTED",
            "SETTLED",
            "SUSPENDED",
            "VOID",
        }
        return normal_status not in inactive_statuses

    def _maybe_subscribe_instrument(self, instrument: Instrument) -> bool:
        betting_instrument = self._coerce_betting_instrument(instrument)
        if betting_instrument is None:
            return False

        # Venue filter
        if betting_instrument.id.venue.value not in self._config.enabled_venues:
            return False

        # Sport/live filter
        if not self._should_process_instrument(betting_instrument):
            return False

        if any(existing.id == betting_instrument.id for existing in self._subscribed_instruments):
            return False

        self._subscribed_instruments.add(betting_instrument)
        if self._config.opportunity_graph_enabled and self._config.graph_rebuild_on_new_instrument:
            self._opportunity_graph.add_instrument(betting_instrument)
        if self._semantic_quote_priority_enabled():
            self._subscribe_cross_venue_common_fixture_quote_ticks()
            self._subscribe_semantic_connected_quote_ticks()
            self._subscribe_semantic_unmatched_quote_probe_ticks()
        else:
            self._subscribe_quote_ticks_for_instrument(betting_instrument)
        return True

    def _coerce_betting_instrument(self, instrument: Instrument | None) -> BettingInstrument | None:
        if isinstance(instrument, BETTING_INSTRUMENT_TYPES):
            return instrument
        if not isinstance(instrument, BinaryOption):
            return None

        source_id = str(instrument.id)
        existing = self._betting_instruments_by_source_id.get(source_id)
        if existing is not None:
            return existing

        transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(instrument)
        if transformed is None:
            return None

        self._betting_instruments_by_source_id[source_id] = transformed
        self._source_ids_by_betting_instrument_id[str(transformed.id)] = instrument.id
        return transformed

    def _semantic_quote_priority_enabled(self) -> bool:
        return self._config.opportunity_graph_enabled and self._matcher.rule_store is not None

    def _semantic_batch_subscription_enabled(self) -> bool:
        return (
            self._semantic_quote_priority_enabled() and self._config.graph_rebuild_on_new_instrument
        )

    def _subscribe_instruments_semantic_batch(
        self,
        instruments: list[Instrument],
    ) -> None:
        subscribed_before = len(self._subscribed_instruments)
        existing_ids = {str(instrument.id) for instrument in self._subscribed_instruments}
        for instrument in instruments:
            betting_instrument = self._coerce_betting_instrument(instrument)
            if betting_instrument is None:
                continue
            if betting_instrument.id.venue.value not in self._config.enabled_venues:
                continue
            if not self._should_process_instrument(betting_instrument):
                continue
            if str(betting_instrument.id) in existing_ids:
                continue
            self._subscribed_instruments.add(betting_instrument)
            existing_ids.add(str(betting_instrument.id))

        if len(self._subscribed_instruments) == subscribed_before:
            return

        self._opportunity_graph.build(list(self._subscribed_instruments))
        self._subscribe_cross_venue_common_fixture_quote_ticks()
        self._subscribe_semantic_connected_quote_ticks()
        self._subscribe_semantic_unmatched_quote_probe_ticks()
        self._log_graph_topology_summary()

    def _subscribe_quote_ticks_for_instrument(self, instrument: BettingInstrument) -> bool:
        quote_instrument_id = self._quote_subscription_instrument_id(instrument)
        instrument_id = str(quote_instrument_id)
        if instrument_id in self._quote_subscribed_instrument_ids:
            return False
        self._quote_subscribed_instrument_ids.add(instrument_id)
        self.subscribe_quote_ticks(quote_instrument_id)
        self.log.info(f"Subscribed to {quote_instrument_id}")
        return True

    def _subscribe_semantic_connected_quote_ticks(self) -> int:
        subscribed_by_venue = self._quote_subscription_counts_by_venue()
        # A cross-venue reserve only makes sense with 2+ enabled venues; a single-venue
        # config has no cross-venue counterpart to protect (#215).
        cross_venue_reserve = (
            max(0, self._config.semantic_unmatched_quote_probe_limit_per_venue)
            if len(self._config.enabled_venues) >= 2
            else 0
        )
        subscribed_count = 0
        for is_cross_venue, node in self._semantic_connected_quote_nodes():
            venue = node.instrument.id.venue.value.upper()
            venue_limit = self._config.semantic_quote_subscription_limit_by_venue.get(venue)
            if venue_limit is not None:
                # Cross-venue-connected nodes may use the full venue ceiling; same-venue-
                # only nodes stop short of a reserved cross-venue sub-budget so they
                # cannot permanently occupy the limit across refresh cycles and starve a
                # later cross-venue counterpart. Cap the reserve at half the ceiling so a
                # small limit is never fully starved; the ceiling itself is never exceeded.
                reserve = min(cross_venue_reserve, venue_limit // 2)
                effective_limit = venue_limit if is_cross_venue else max(0, venue_limit - reserve)
                if subscribed_by_venue[venue] >= effective_limit:
                    continue
            if self._subscribe_quote_ticks_for_instrument(node.instrument):
                subscribed_by_venue[venue] += 1
                subscribed_count += 1
        if subscribed_count:
            self.log.info(
                "Subscribed semantic-connected quote streams: "
                f"new={subscribed_count} "
                f"total={len(self._quote_subscribed_instrument_ids)} "
                "by_venue="
                f"{dict(sorted(self._quote_subscription_counts_by_venue().items()))}",
            )
        return subscribed_count

    def _subscribe_cross_venue_common_fixture_quote_ticks(self) -> int:
        """
        Reserve quote slots for loaded fixtures shared across enabled venues.

        Semantic topology can contain many same-venue edges. If those edges consume
        venue quote limits first, runtime can correctly discover a common
        cross-venue fixture but still report it as ``common_fixture_unquoted``. This
        pass runs before graph-connected subscriptions and spends a bounded reserve
        on instruments whose fixture aliases are present on another venue. It does
        not create semantic edges or execution authority; it only ensures the
        runtime can observe prices for already loaded shared fixtures.

        """
        per_venue_limit = self._config.semantic_unmatched_quote_probe_limit_per_venue
        if per_venue_limit <= 0 or len(self._config.enabled_venues) < 2:
            return 0

        subscribed_snapshot = tuple(self._subscribed_instruments)
        candidate_instruments = [
            instrument
            for instrument in subscribed_snapshot
            if instrument.id.venue.value.upper() in self._config.enabled_venues
            and self._instrument_resolution_horizon_quote_allowed(instrument)
        ]
        if not candidate_instruments:
            return 0

        alias_keys_by_instrument_id, alias_venues_by_key = (
            self._semantic_unmatched_quote_probe_alias_index(candidate_instruments)
        )
        ranked = [
            (
                self._cross_venue_common_fixture_quote_priority(
                    instrument,
                    alias_keys_by_instrument_id=alias_keys_by_instrument_id,
                    alias_venues_by_key=alias_venues_by_key,
                ),
                instrument,
            )
            for instrument in candidate_instruments
            if self._instrument_has_cross_venue_fixture_alias(
                instrument,
                alias_keys_by_instrument_id=alias_keys_by_instrument_id,
                alias_venues_by_key=alias_venues_by_key,
            )
        ]
        if not ranked:
            return 0

        ranked.sort(key=lambda item: item[0])
        subscribed_by_venue = self._quote_subscription_counts_by_venue()
        reserved_by_venue: Counter[str] = Counter()
        subscribed_count = 0
        for _, instrument in ranked:
            venue = instrument.id.venue.value.upper()
            venue_limit = self._config.semantic_quote_subscription_limit_by_venue.get(venue)
            if venue_limit is not None and subscribed_by_venue[venue] >= venue_limit:
                continue
            if reserved_by_venue[venue] >= per_venue_limit:
                continue
            if self._subscribe_quote_ticks_for_instrument(instrument):
                subscribed_by_venue[venue] += 1
                reserved_by_venue[venue] += 1
                subscribed_count += 1

        if subscribed_count:
            self.log.info(
                "Subscribed cross-venue common-fixture quote streams: "
                f"new={subscribed_count} "
                f"total={len(self._quote_subscribed_instrument_ids)} "
                "by_venue="
                f"{dict(sorted(reserved_by_venue.items()))}",
            )
        return subscribed_count

    def _cross_venue_common_fixture_quote_priority(
        self,
        instrument: BettingInstrument,
        *,
        alias_keys_by_instrument_id: dict[str, set[str]],
        alias_venues_by_key: dict[str, set[str]],
    ) -> tuple[int, int, int, int, str]:
        aliases = alias_keys_by_instrument_id.get(str(instrument.id), set())
        other_venue_count = 0
        venue = instrument.id.venue.value.upper()
        for alias in aliases:
            alias_venues = alias_venues_by_key.get(alias, set())
            other_venue_count = max(
                other_venue_count,
                len({alias_venue for alias_venue in alias_venues if alias_venue != venue}),
            )
        return (
            -other_venue_count,
            self._instrument_resolution_horizon_priority(instrument),
            self._instrument_market_family_quote_priority(instrument),
            0 if aliases else 1,
            str(instrument.id),
        )

    @staticmethod
    def _instrument_market_family_quote_priority(instrument: BettingInstrument) -> int:
        raw_family = " ".join(
            str(value or "")
            for value in (
                getattr(instrument, "market_type", ""),
                getattr(instrument, "market_name", ""),
            )
        ).lower()
        if any(
            token in raw_family
            for token in (
                "draw_no_bet",
                "match_odds",
                "moneyline",
                "money_line",
                "winner",
                "match_winner",
            )
        ):
            return 0
        if any(token in raw_family for token in ("spread", "handicap", "point_spread")):
            return 1
        if any(token in raw_family for token in ("total", "over_under")):
            return 2
        return 3

    @staticmethod
    def _instrument_has_cross_venue_fixture_alias(
        instrument: BettingInstrument,
        *,
        alias_keys_by_instrument_id: dict[str, set[str]],
        alias_venues_by_key: dict[str, set[str]],
    ) -> bool:
        venue = instrument.id.venue.value.upper()
        aliases = alias_keys_by_instrument_id.get(str(instrument.id), set())
        for alias in aliases:
            venues = alias_venues_by_key.get(alias, set())
            if any(alias_venue != venue for alias_venue in venues):
                return True
        return False

    def _semantic_connected_quote_nodes(self) -> list[tuple[bool, Any]]:
        """
        Return (is_cross_venue, node) pairs in quote-subscription priority order.

        Multi-venue validation can have thousands of same-venue edges and only a handful
        of cross-venue edges. Venue quote limits should therefore spend their first
        slots on instruments needed to quote cross-venue topology, then strict
        execution-safe edges, then the broader topology set.

        """
        ranked: list[tuple[tuple[int, int, int, int, int, str], Any]] = []
        for node_id, edge_ids in self._opportunity_graph.edge_ids_by_node_id.items():
            if not edge_ids:
                continue
            node = self._opportunity_graph.nodes_by_id.get(node_id)
            if node is None:
                continue
            if not self._resolution_horizon_quote_allowed(node):
                continue
            priority = self._semantic_quote_subscription_priority(node_id, edge_ids)
            ranked.append((priority, node))
        ranked.sort(key=lambda item: item[0])
        # priority[0] is -cross_venue_edges; < 0 means the node participates in at least
        # one cross-venue edge. Surfaced so the subscription pass can protect a reserve
        # for cross-venue nodes from same-venue-only saturation (issue #215).
        return [(priority[0] < 0, node) for priority, node in ranked]

    def _semantic_quote_subscription_priority(
        self,
        node_id: str,
        edge_ids: set[str],
    ) -> tuple[int, int, int, int, int, str]:
        nodes = self._opportunity_graph.nodes_by_id
        edges = self._opportunity_graph.edges_by_id
        node = nodes.get(node_id)
        node_venue = self._node_venue_value(node)
        cross_venue_edges = 0
        execution_safe_edges = 0
        same_venue_eligible_edges = 0
        for edge_id in edge_ids:
            edge = edges.get(edge_id)
            if edge is None:
                continue
            if bool(getattr(edge, "execution_safe", False)):
                execution_safe_edges += 1
            if bool(getattr(edge, "same_venue_execution_eligible", False)):
                same_venue_eligible_edges += 1
            other_node_id = (
                getattr(edge, "target_node_id", None)
                if getattr(edge, "source_node_id", None) == node_id
                else getattr(edge, "source_node_id", None)
            )
            other_node = nodes.get(str(other_node_id)) if other_node_id is not None else None
            other_venue = self._node_venue_value(other_node)
            if node_venue and other_venue and node_venue != other_venue:
                cross_venue_edges += 1
        return (
            -cross_venue_edges,
            self._resolution_horizon_priority(node),
            -execution_safe_edges,
            -same_venue_eligible_edges,
            -len(edge_ids),
            str(getattr(getattr(node, "instrument", None), "id", "")),
        )

    def _resolution_horizon_priority(self, node: object | None) -> int:
        horizon_hours = self._config.max_resolution_horizon_hours
        if horizon_hours is None:
            return 0
        instrument = getattr(node, "instrument", None)
        if instrument is None:
            return 1
        return self._instrument_resolution_horizon_priority(instrument)

    def _instrument_resolution_horizon_priority(self, instrument: BettingInstrument) -> int:
        horizon_hours = self._config.max_resolution_horizon_hours
        if horizon_hours is None:
            return 0
        window = self._instrument_resolution_horizon_window(instrument)
        if window is None:
            return 1
        start_time, end_time = window
        now = datetime.now(tz=UTC)
        stale_grace = timedelta(hours=RESOLUTION_HORIZON_STALE_GRACE_HOURS)
        if end_time < now - stale_grace:
            return 3
        if start_time < now:
            return 0
        horizon = now + timedelta(hours=float(horizon_hours))
        return -1 if start_time <= horizon else 2

    @staticmethod
    def _instrument_resolution_horizon_window(
        instrument: BettingInstrument,
    ) -> tuple[datetime, datetime] | None:
        start_time = instrument.parsed_start_time()
        if start_time is None:
            return None
        raw_start = str(getattr(instrument, "start_time", "") or "").strip()
        if (
            len(raw_start) == 10
            and raw_start[4] == "-"
            and raw_start[7] == "-"
            and raw_start[:4].isdigit()
            and raw_start[5:7].isdigit()
            and raw_start[8:].isdigit()
        ):
            # Polymarket Gamma commonly emits only a fixture date. Treat it as
            # active for the full UTC date so same-day markets do not become
            # stale immediately after midnight.
            return start_time, start_time + timedelta(days=1)
        return start_time, start_time

    def _resolution_horizon_quote_allowed(self, node: object | None) -> bool:
        if self._config.max_resolution_horizon_hours is None:
            return True
        return self._resolution_horizon_priority(node) < 2

    def _instrument_resolution_horizon_quote_allowed(
        self,
        instrument: BettingInstrument,
    ) -> bool:
        if self._config.max_resolution_horizon_hours is None:
            return True
        return self._instrument_resolution_horizon_priority(instrument) < 2

    @staticmethod
    def _node_venue_value(node: object | None) -> str:
        instrument = getattr(node, "instrument", None)
        instrument_id = getattr(instrument, "id", None)
        venue = getattr(instrument_id, "venue", None)
        if venue is None:
            return ""
        return str(getattr(venue, "value", venue)).upper()

    def _subscribe_semantic_unmatched_quote_probe_ticks(self) -> int:
        """
        Subscribe bounded unmatched quote streams for semantic audit venues.

        Semantic mode deliberately prioritizes graph-connected instruments so execution
        diagnostics only reflect promoted-rule topology. Polymarket sports markets can
        be discovered before they have promoted semantic edges, so this probe keeps
        their quote health visible without granting execution authority or creating
        graph edges.

        """
        probe_venues = self._config.semantic_unmatched_quote_probe_venues
        per_venue_limit = self._config.semantic_unmatched_quote_probe_limit_per_venue
        if not probe_venues or per_venue_limit <= 0:
            return 0

        connected_instrument_ids = self._semantic_connected_instrument_ids()
        subscribed_by_venue = self._quote_subscription_counts_by_venue()
        unmatched_probe_subscribed_by_venue: Counter[str] = Counter()
        subscribed_count = 0

        subscribed_snapshot = tuple(self._subscribed_instruments)
        candidate_instruments = list(subscribed_snapshot)
        alias_keys_by_instrument_id, alias_venues_by_key = (
            self._semantic_unmatched_quote_probe_alias_index(candidate_instruments)
        )
        candidate_instruments.sort(
            key=lambda instrument: self._semantic_unmatched_quote_probe_priority(
                instrument,
                alias_keys_by_instrument_id=alias_keys_by_instrument_id,
                alias_venues_by_key=alias_venues_by_key,
            ),
        )
        for instrument in candidate_instruments:
            venue = instrument.id.venue.value.upper()
            if venue not in probe_venues:
                continue
            if str(instrument.id) in connected_instrument_ids:
                continue
            if not self._instrument_resolution_horizon_quote_allowed(instrument):
                continue
            if unmatched_probe_subscribed_by_venue[venue] >= per_venue_limit:
                continue
            if self._subscribe_quote_ticks_for_instrument(instrument):
                subscribed_by_venue[venue] += 1
                unmatched_probe_subscribed_by_venue[venue] += 1
                subscribed_count += 1

        if subscribed_count:
            self.log.info(
                "Subscribed semantic-unmatched quote probe streams: "
                f"venues={sorted(probe_venues)} "
                f"new={subscribed_count} "
                f"total={len(self._quote_subscribed_instrument_ids)}",
            )
        return subscribed_count

    def _semantic_unmatched_quote_probe_alias_index(
        self,
        instruments: list[BettingInstrument],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        alias_keys_by_instrument_id: dict[str, set[str]] = {}
        alias_venues_by_key: dict[str, set[str]] = {}
        for instrument in instruments:
            aliases = self._instrument_event_alias_keys(instrument)
            alias_keys_by_instrument_id[str(instrument.id)] = aliases
            venue = instrument.id.venue.value.upper()
            for alias in aliases:
                alias_venues_by_key.setdefault(alias, set()).add(venue)
        return alias_keys_by_instrument_id, alias_venues_by_key

    def _semantic_unmatched_quote_probe_priority(
        self,
        instrument: BettingInstrument,
        *,
        alias_keys_by_instrument_id: dict[str, set[str]] | None = None,
        alias_venues_by_key: dict[str, set[str]] | None = None,
    ) -> tuple[int, int, int, str]:
        """
        Rank unmatched audit probes toward near-term cross-venue fixture evidence.

        These subscriptions do not create graph edges or execution authority. They only
        keep venues like Polymarket observable when promoted topology is missing, so the
        first slots should prove whether common near-term fixtures are quoted.

        """
        venue = instrument.id.venue.value.upper()
        aliases = (
            alias_keys_by_instrument_id.get(str(instrument.id), set())
            if alias_keys_by_instrument_id is not None
            else self._instrument_event_alias_keys(instrument)
        )
        other_venue_alias_hit = 1
        if aliases and alias_venues_by_key is not None:
            for alias in aliases:
                venues = alias_venues_by_key.get(alias, set())
                if any(alias_venue != venue for alias_venue in venues):
                    other_venue_alias_hit = 0
                    break
        elif aliases:
            for other in tuple(self._subscribed_instruments):
                if other.id.venue.value.upper() == venue:
                    continue
                if aliases.intersection(self._instrument_event_alias_keys(other)):
                    other_venue_alias_hit = 0
                    break
        return (
            other_venue_alias_hit,
            self._instrument_resolution_horizon_priority(instrument),
            0 if aliases else 1,
            str(instrument.id),
        )

    @staticmethod
    def _instrument_event_alias_keys(instrument: BettingInstrument) -> set[str]:
        try:
            return set(
                DEFAULT_FIXTURE_IDENTITY_RESOLVER.event_alias_keys(
                    instrument,
                    include_start_time=False,
                ),
            )
        except (AttributeError, TypeError, ValueError):
            event_key = getattr(instrument, "event_key", lambda include_start_time=False: "")(
                include_start_time=False,
            )
            return {str(event_key)} if event_key else set()

    def _semantic_connected_instrument_ids(self) -> set[str]:
        connected: set[str] = set()
        for node_id, edge_ids in self._opportunity_graph.edge_ids_by_node_id.items():
            if not edge_ids:
                continue
            node = self._opportunity_graph.nodes_by_id.get(node_id)
            if node is not None:
                connected.add(str(node.instrument.id))
        return connected

    def _quote_subscription_counts_by_venue(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for instrument_id in self._quote_subscribed_instrument_ids:
            venue = self._venue_from_instrument_id_text(str(instrument_id))
            if venue:
                counts[venue] += 1
        return counts

    @staticmethod
    def _venue_from_instrument_id_text(instrument_id: str) -> str:
        return instrument_id.rsplit(".", maxsplit=1)[-1].upper() if "." in instrument_id else ""

    def _quote_subscription_instrument_id(
        self,
        instrument: BettingInstrument,
    ) -> InstrumentId:
        return self._source_ids_by_betting_instrument_id.get(str(instrument.id), instrument.id)

    def _log_graph_topology_summary(self) -> None:
        if not self._config.opportunity_graph_enabled:
            return

        graph_stats = self._opportunity_graph.stats()
        self.log.info(
            "Opportunity graph topology: "
            f"nodes={graph_stats['nodes']} "
            f"edges={graph_stats['edges']} "
            f"quote_states={graph_stats['quote_states']} "
            f"connected_nodes={graph_stats['connected_nodes']}",
        )

    def _should_process_instrument(self, instrument: BettingInstrument) -> bool:
        """
        Check if instrument should be processed based on filters.

        Parameters
        ----------
        instrument : BettingInstrument
            Instrument to check.

        Returns
        -------
        bool
            True if instrument passes all filters.

        """
        # Sport filter
        if self._config.sport_filter:
            raw_sport = getattr(instrument, "sport_name", None)
            if raw_sport is None:
                raw_sport = getattr(instrument, "sport", None)
            inst_sport = raw_sport.strip().lower() if isinstance(raw_sport, str) else None
            if inst_sport != self._config.sport_filter:
                return False

        # Resolution horizon filter. For live-pilot cross-venue nodes this keeps
        # stale resolved markets and far-future tail markets out of the graph
        # entirely, not just out of quote subscriptions.
        if (
            self._config.max_resolution_horizon_hours is not None
            and self._instrument_resolution_horizon_priority(instrument) >= 2
        ):
            return False

        # Market timing filter (requires instrument metadata)
        if self._config.market_timing_filter != "all":
            # Check if instrument has live/pre-market indicator
            is_live = self._is_live_market(instrument)

            if self._config.market_timing_filter == "pre_market" and is_live:
                return False
            if self._config.market_timing_filter == "live" and not is_live:
                return False

        return True

    @staticmethod
    def _is_live_market(instrument: BettingInstrument) -> bool:
        """
        Determine if instrument represents a live/in-play market.

        Parameters
        ----------
        instrument : CryptoBettingInstrument
            Instrument to check.

        Returns
        -------
        bool
            True if live market.

        """
        raw_live = getattr(instrument, "live", None)
        if isinstance(raw_live, bool):
            return raw_live

        # Fall back to string heuristics for legacy instrument mocks.
        if hasattr(instrument, "params"):
            params = instrument.params or ""
            if not isinstance(params, str):
                params = str(params)
            params_lower = params.lower()
            return "live" in params_lower or "in_play" in params_lower or "in-play" in params_lower

        return False

    @staticmethod
    def _quote_odds(quote: QuoteTick | None) -> Decimal | None:
        if quote is None:
            return None

        venue = str(quote.instrument_id.venue).upper()
        bid_price = quote.bid_price.as_decimal()
        ask_price = quote.ask_price.as_decimal()

        if venue == "POLYMARKET":
            prediction_price = ask_price if ask_price > 0 else bid_price
            if Decimal(0) < prediction_price < Decimal(1):
                return Decimal(1) / prediction_price
            if prediction_price > Decimal(1):
                return prediction_price
            return None

        # Decimal odds must be > 1 (a "price" of 1.0 or a mis-scaled sub-1 quote is
        # degenerate); returning it would raise ValueError downstream in
        # fee_adjusted_odds and abort the whole quote-handling callback for the tick.
        if venue == "SXBET" and bid_price > 1:
            return bid_price

        if ask_price > 1:
            return ask_price

        if bid_price > 1:
            return bid_price

        return None

    @staticmethod
    def _quote_available_size(quote: QuoteTick | None) -> Decimal:
        return BettingArbitrageStrategy.quote_available_size(quote)

    @staticmethod
    def quote_available_size(quote: QuoteTick | None) -> Decimal:
        if quote is None:
            return Decimal(0)

        bid_price = quote.bid_price.as_decimal()
        ask_price = quote.ask_price.as_decimal()
        bid_size = quote.bid_size.as_decimal()
        ask_size = quote.ask_size.as_decimal()

        venue = str(quote.instrument_id.venue).upper()
        if venue == "POLYMARKET":
            if ask_price > 0:
                return ask_size * ask_price
            if bid_price > 0:
                return bid_size * bid_price
            return Decimal(0)

        if venue == "SXBET" and bid_price > 0:
            return bid_size
        if ask_price > 0:
            return ask_size
        if bid_price > 0:
            return bid_size
        return Decimal(0)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """
        Handle quote tick updates.

        When a new quote arrives:
        1. Update latest quote state
        2. Evaluate affected opportunity graph edges
        3. Execute if auto_execute enabled

        Parameters
        ----------
        tick : QuoteTick
            Latest quote tick.

        """
        # Store latest quote
        strategy_received_ns = self.clock.timestamp_ns()
        self._record_quote_receive_latency(tick, strategy_received_ns)
        self._latest_quotes[str(tick.instrument_id)] = tick

        instrument = self._coerce_betting_instrument(self.cache.instrument(tick.instrument_id))
        if instrument is None:
            return
        tick = self._quote_tick_for_betting_instrument(tick, instrument)

        if self._config.opportunity_graph_enabled:
            self._handle_graph_quote_tick(tick, instrument)
            return

        self._handle_search_quote_tick(tick, instrument)

    def _record_quote_receive_latency(self, tick: QuoteTick, strategy_received_ns: int) -> None:
        venue = str(tick.instrument_id.venue).upper()
        if tick.ts_event > 0:
            elapsed_ns = max(0, strategy_received_ns - int(tick.ts_event))
            self._record_latency_sample(
                self._quote_event_to_strategy_latency_ns,
                elapsed_ns,
            )
            self._record_venue_latency_sample(
                self._quote_event_to_strategy_latency_ns_by_venue,
                venue,
                elapsed_ns,
            )
        if tick.ts_init > 0:
            elapsed_ns = max(0, strategy_received_ns - int(tick.ts_init))
            self._record_latency_sample(
                self._quote_publish_to_strategy_latency_ns,
                elapsed_ns,
            )
            self._record_venue_latency_sample(
                self._quote_publish_to_strategy_latency_ns_by_venue,
                venue,
                elapsed_ns,
            )
        fetch_latency_ns = self._quote_fetch_latency_measurement_ns(tick)
        if fetch_latency_ns is not None:
            self._record_latency_sample(
                self._quote_fetch_latency_ns,
                fetch_latency_ns,
            )
            self._record_venue_latency_sample(
                self._quote_fetch_latency_ns_by_venue,
                venue,
                fetch_latency_ns,
            )

    @staticmethod
    def _quote_tick_for_betting_instrument(
        tick: QuoteTick,
        instrument: CryptoBettingInstrument,
    ) -> QuoteTick:
        if tick.instrument_id == instrument.id:
            return tick
        return QuoteTick(
            instrument_id=instrument.id,
            bid_price=tick.bid_price,
            ask_price=tick.ask_price,
            bid_size=tick.bid_size,
            ask_size=tick.ask_size,
            ts_event=tick.ts_event,
            ts_init=tick.ts_init,
        )

    def _handle_graph_quote_tick(
        self,
        tick: QuoteTick,
        instrument: BettingInstrument,
    ) -> None:
        current_odds = self._quote_odds(tick)
        if current_odds is None:
            return

        now_ns = self.clock.timestamp_ns()
        if str(tick.instrument_id) not in self._opportunity_graph.nodes_by_id:
            if not self._config.graph_rebuild_on_new_instrument:
                return
            self._opportunity_graph.add_instrument(instrument)

        if self._handle_graph_quote_tick_fast(tick, current_odds=current_odds, now_ns=now_ns):
            return

        scan_started_ns = time.perf_counter_ns()
        quote_state, candidates = self._opportunity_graph.update_quote_and_evaluate(
            tick,
            odds=current_odds,
            received_ns=now_ns,
            min_profit_margin=self._config.min_profit_margin,
            now_ns=now_ns,
        )
        self._record_latency_sample(
            self._graph_scan_latency_ns,
            time.perf_counter_ns() - scan_started_ns,
        )
        if quote_state is None:
            return
        if candidates:
            self.log.debug(
                "Opportunity graph quote evaluation: "
                f"instrument_id={tick.instrument_id} "
                "connected_edges="
                f"{self._opportunity_graph.connected_edge_count(str(tick.instrument_id))} "
                f"candidates={len(candidates)}",
            )

        for candidate in candidates:
            self._handle_opportunity_candidate(candidate, now_ns)

    def _handle_graph_quote_tick_fast(
        self,
        tick: QuoteTick,
        *,
        current_odds: Decimal,
        now_ns: int,
    ) -> bool:
        scan_started_ns = time.perf_counter_ns()
        fast_scan = self._opportunity_graph.update_quote_and_scan_fast(
            tick,
            odds=current_odds,
            received_ns=now_ns,
            min_profit_margin=self._config.min_profit_margin,
            now_ns=now_ns,
        )
        self._record_latency_sample(
            self._graph_scan_latency_ns,
            time.perf_counter_ns() - scan_started_ns,
        )
        if fast_scan is not None:
            quote_updated, snapshots = fast_scan
            if not quote_updated:
                return True
            if snapshots:
                self.log.debug(
                    "Opportunity graph quote evaluation: "
                    f"instrument_id={tick.instrument_id} "
                    "connected_edges="
                    f"{self._opportunity_graph.connected_edge_count(str(tick.instrument_id))} "
                    f"candidates={len(snapshots)}",
                )
            self._handle_fast_opportunity_snapshots(snapshots, now_ns)
            return True
        return False

    def _handle_fast_opportunity_snapshots(
        self,
        snapshots: list[FastCandidateSnapshot],
        now_ns: int,
    ) -> None:
        log_summary = False
        for snapshot in snapshots:
            decision_started_ns = time.perf_counter_ns()
            if self._suppress_fast_snapshot_before_context(snapshot, now_ns):
                self._record_latency_sample(
                    self._candidate_decision_latency_ns,
                    time.perf_counter_ns() - decision_started_ns,
                )
                log_summary = True
                continue
            log_summary = self._handle_fast_actionable_snapshot(snapshot, now_ns) or log_summary
            self._record_latency_sample(
                self._candidate_decision_latency_ns,
                time.perf_counter_ns() - decision_started_ns,
            )
        if log_summary:
            self._log_arbitrage_summary()

    def _suppress_fast_snapshot_before_context(
        self,
        snapshot: FastCandidateSnapshot,
        now_ns: int,
    ) -> bool:
        edge_id = snapshot[0]
        opportunity_id = self._fast_opportunity_id(edge_id, snapshot[10], snapshot[5], snapshot[6])
        if self._is_duplicate_opportunity_pair(edge_id, opportunity_id, now_ns):
            self._raw_arbitrage_detections += 1
            self._duplicate_opportunities_suppressed += 1
            return True

        quote_age_a_secs = self._fast_snapshot_quote_age_secs(now_ns, snapshot[8])
        quote_age_b_secs = self._fast_snapshot_quote_age_secs(now_ns, snapshot[9])
        if (
            quote_age_a_secs > self._config.arbitrage_quote_stale_threshold_secs
            or quote_age_b_secs > self._config.arbitrage_quote_stale_threshold_secs
        ):
            self._raw_arbitrage_detections += 1
            self._stale_quote_suppressions += 1
            return True
        return False

    @staticmethod
    def _fast_snapshot_quote_age_secs(now_ns: int, quote_ts_ns: int) -> float:
        if quote_ts_ns <= 0:
            return 0.0
        return max(0.0, (now_ns - int(quote_ts_ns)) / NANOSECONDS_PER_SECOND)

    # skipcq: PYL-R0914
    def _handle_fast_actionable_snapshot(
        self,
        snapshot: FastCandidateSnapshot,
        now_ns: int,
    ) -> bool:
        (
            canonical_pair_id,
            source_node_id,
            target_node_id,
            hedge_type,
            hedge_confidence,
            odds_a_raw,
            odds_b_raw,
            profit_margin_raw,
            quote_ts_a,
            quote_ts_b,
            match_type,
            matcher_suspect,
        ) = snapshot
        self._raw_arbitrage_detections += 1
        if matcher_suspect:
            self._matcher_suspect_suppressions += 1
            return True

        source_node = self._opportunity_graph.nodes_by_id.get(source_node_id)
        target_node = self._opportunity_graph.nodes_by_id.get(target_node_id)
        if not self._node_pair_matches_execution_venue_mode(source_node, target_node):
            return False

        if not self._config.auto_execute and not self._config.opportunity_log_manual_instructions:
            log_profit_margin = profit_margin_raw
            if (
                source_node is not None
                and target_node is not None
                and self._pair_has_configured_fee(
                    source_node.instrument,
                    target_node.instrument,
                )
            ):
                opportunity = self._fast_arbitrage_opportunity(
                    source_node.instrument,
                    target_node.instrument,
                    odds_a_raw=odds_a_raw,
                    odds_b_raw=odds_b_raw,
                    match_type=match_type,
                )
                if opportunity.profit_margin < self._config.min_profit_margin:
                    return False
                log_profit_margin = float(opportunity.profit_margin)

            opportunity_id = self._fast_opportunity_id(
                canonical_pair_id,
                match_type,
                odds_a_raw,
                odds_b_raw,
            )
            self._record_fast_opportunity(canonical_pair_id, opportunity_id, now_ns)
            self._log_fast_arbitrage_snapshot(
                source_node_id,
                target_node_id,
                canonical_pair_id=canonical_pair_id,
                match_type=match_type,
                hedge_type=hedge_type,
                hedge_confidence=hedge_confidence,
                odds_a_raw=odds_a_raw,
                odds_b_raw=odds_b_raw,
                profit_margin_raw=log_profit_margin,
                quote_ts_a=quote_ts_a,
                quote_ts_b=quote_ts_b,
                now_ns=now_ns,
            )
            return True

        if source_node is None or target_node is None:
            return False

        inst_a = source_node.instrument
        inst_b = target_node.instrument
        opportunity = self._fast_arbitrage_opportunity(
            inst_a,
            inst_b,
            odds_a_raw=odds_a_raw,
            odds_b_raw=odds_b_raw,
            match_type=match_type,
        )
        if not self._candidate_matches_execution_venue_mode(opportunity):
            return False
        if opportunity.profit_margin < self._config.min_profit_margin:
            return False

        diagnostics = self._fast_arbitrage_diagnostics(
            opportunity=opportunity,
            canonical_pair_id=canonical_pair_id,
            hedge_match_type=hedge_type,
            hedge_confidence=hedge_confidence,
            quote_ts_a=quote_ts_a,
            quote_ts_b=quote_ts_b,
            now_ns=now_ns,
        )
        self._record_fast_opportunity(
            diagnostics.canonical_pair_id,
            diagnostics.opportunity_id,
            now_ns,
        )
        self._handle_arbitrage_opportunity(opportunity, diagnostics)
        return True

    def _record_fast_opportunity(
        self,
        canonical_pair_id: str,
        opportunity_id: str,
        now_ns: int,
    ) -> None:
        self._record_opportunity_pair(canonical_pair_id, opportunity_id, now_ns)
        self._opportunities_found += 1
        self._executable_candidates += 1

    def _record_opportunity_pair(
        self,
        canonical_pair_id: str,
        opportunity_id: str,
        now_ns: int,
    ) -> None:
        self._seen_opportunity_pairs.add(canonical_pair_id)
        self._active_opportunity_pairs[canonical_pair_id] = OpportunityPairState(
            last_opportunity_id=opportunity_id,
            last_accepted_ns=now_ns,
            last_seen_ns=now_ns,
        )

    def _is_duplicate_opportunity_pair(
        self,
        canonical_pair_id: str,
        opportunity_id: str,
        now_ns: int,
    ) -> bool:
        self._prune_inactive_opportunity_pairs(now_ns)
        state = self._active_opportunity_pairs.get(canonical_pair_id)
        if state is None:
            return False

        cooldown_ns = self._duplicate_suppression_cooldown_ns()
        if now_ns - state.last_seen_ns > cooldown_ns:
            self._active_opportunity_pairs.pop(canonical_pair_id, None)
            return False

        self._active_opportunity_pairs[canonical_pair_id] = OpportunityPairState(
            last_opportunity_id=state.last_opportunity_id,
            last_accepted_ns=state.last_accepted_ns,
            last_seen_ns=now_ns,
        )
        if opportunity_id == state.last_opportunity_id:
            return True
        return now_ns - state.last_accepted_ns <= cooldown_ns

    def _prune_inactive_opportunity_pairs(self, now_ns: int) -> None:
        cooldown_ns = self._duplicate_suppression_cooldown_ns()
        expired = [
            pair_id
            for pair_id, state in self._active_opportunity_pairs.items()
            if now_ns - state.last_seen_ns > cooldown_ns
        ]
        for pair_id in expired:
            self._active_opportunity_pairs.pop(pair_id, None)

    def _duplicate_suppression_cooldown_ns(self) -> int:
        return int(self._config.duplicate_suppression_cooldown_secs * NANOSECONDS_PER_SECOND)

    # skipcq: PYL-R0914
    def _handle_fast_opportunity_candidate(
        self,
        snapshot: FastCandidateSnapshot,
        now_ns: int,
        *,
        emit_summary: bool = True,
        emit_suppression_log: bool = True,
    ) -> bool:
        context = self._fast_candidate_context(snapshot, now_ns)
        if context is None:
            return False
        (
            inst_a,
            inst_b,
            quote_a,
            quote_b,
            hedge_type,
            hedge_confidence,
            odds_a_raw,
            odds_b_raw,
            canonical_pair_id,
            match_type,
            quote_age_a_secs,
            quote_age_b_secs,
            quote_delta_secs,
        ) = context
        self._raw_arbitrage_detections += 1
        if self._suppress_fast_candidate(
            inst_a=inst_a,
            inst_b=inst_b,
            hedge_type=hedge_type,
            hedge_confidence=hedge_confidence,
            quote_a=quote_a,
            quote_b=quote_b,
            odds_a_raw=odds_a_raw,
            odds_b_raw=odds_b_raw,
            canonical_pair_id=canonical_pair_id,
            match_type=match_type,
            quote_age_a_secs=quote_age_a_secs,
            quote_age_b_secs=quote_age_b_secs,
            quote_delta_secs=quote_delta_secs,
            now_ns=now_ns,
            emit_summary=emit_summary,
            emit_suppression_log=emit_suppression_log,
        ):
            return True

        opportunity = self._fast_arbitrage_opportunity(
            inst_a,
            inst_b,
            odds_a_raw=odds_a_raw,
            odds_b_raw=odds_b_raw,
            match_type=match_type,
        )
        if not self._candidate_matches_execution_venue_mode(opportunity):
            return False
        if opportunity.profit_margin < self._config.min_profit_margin:
            return False

        diagnostics = self._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type=hedge_type,
            hedge_confidence=hedge_confidence,
            quote_a=quote_a,
            quote_b=quote_b,
            now_ns=now_ns,
        )
        self._record_opportunity_pair(
            diagnostics.canonical_pair_id,
            diagnostics.opportunity_id,
            now_ns,
        )
        self._opportunities_found += 1
        self._executable_candidates += 1
        self._handle_arbitrage_opportunity(opportunity, diagnostics)
        if emit_summary:
            self._log_arbitrage_summary()
        return True

    # skipcq: PYL-R0914
    def _fast_candidate_context(
        self,
        snapshot: FastCandidateSnapshot,
        now_ns: int,
    ) -> tuple | None:
        (
            _edge_id,
            source_node_id,
            target_node_id,
            hedge_type,
            hedge_confidence,
            odds_a_raw,
            odds_b_raw,
            _profit_margin_raw,
            _quote_ts_a,
            _quote_ts_b,
            *_,
        ) = snapshot
        source_node = self._opportunity_graph.nodes_by_id.get(source_node_id)
        target_node = self._opportunity_graph.nodes_by_id.get(target_node_id)
        quote_a = self._latest_quotes.get(source_node_id)
        quote_b = self._latest_quotes.get(target_node_id)
        if source_node is None or target_node is None or quote_a is None or quote_b is None:
            return None

        inst_a = source_node.instrument
        inst_b = target_node.instrument
        canonical_pair_id = self._canonical_pair_id(inst_a, inst_b)
        match_type = self._opportunity_match_type(inst_a, inst_b)
        quote_age_a_secs = self._quote_age_secs(now_ns, quote_a)
        quote_age_b_secs = self._quote_age_secs(now_ns, quote_b)
        quote_delta_secs = self._quote_pair_skew_secs(quote_a, quote_b)
        return (
            inst_a,
            inst_b,
            quote_a,
            quote_b,
            hedge_type,
            hedge_confidence,
            odds_a_raw,
            odds_b_raw,
            canonical_pair_id,
            match_type,
            quote_age_a_secs,
            quote_age_b_secs,
            quote_delta_secs,
        )

    # skipcq: PYL-R0913, PYL-R0914
    def _suppress_fast_candidate(  # noqa: C901
        self,
        *,
        inst_a: CryptoBettingInstrument,
        inst_b: CryptoBettingInstrument,
        hedge_type: str,
        hedge_confidence: float,
        quote_a: QuoteTick,
        quote_b: QuoteTick,
        odds_a_raw: float,
        odds_b_raw: float,
        canonical_pair_id: str,
        match_type: str,
        quote_age_a_secs: float,
        quote_age_b_secs: float,
        quote_delta_secs: float,
        now_ns: int,
        emit_summary: bool,
        emit_suppression_log: bool,
    ) -> bool:
        opportunity_id = self._fast_opportunity_id(
            canonical_pair_id,
            match_type,
            odds_a_raw,
            odds_b_raw,
        )
        if self._is_duplicate_opportunity_pair(canonical_pair_id, opportunity_id, now_ns):
            self._duplicate_opportunities_suppressed += 1
            if emit_suppression_log:
                self._log_fast_duplicate_suppression(
                    inst_a,
                    inst_b,
                    odds_a_raw,
                    odds_b_raw,
                    canonical_pair_id,
                    match_type,
                    quote_age_a_secs,
                    quote_age_b_secs,
                )
            if emit_summary:
                self._log_arbitrage_summary()
            return True

        freshness = self._quote_freshness_thresholds(inst_a, inst_b)
        fetch_latency_a_secs = self._quote_fetch_latency_secs(quote_a)
        fetch_latency_b_secs = self._quote_fetch_latency_secs(quote_b)
        if (
            fetch_latency_a_secs > freshness.max_fetch_latency_secs
            or fetch_latency_b_secs > freshness.max_fetch_latency_secs
        ):
            self._stale_quote_suppressions += 1
            self._maybe_trigger_stale_quote_refresh(
                inst_a,
                inst_b,
                reason="fetch_latency",
                now_ns=now_ns,
            )
            if emit_suppression_log:
                self.log.info(
                    "Arbitrage candidate suppressed: "
                    f"reason=fetch_latency max_fetch_latency_secs="
                    f"{freshness.max_fetch_latency_secs:.2f} "
                    f"fetch_latency_a_secs={fetch_latency_a_secs:.2f} "
                    f"fetch_latency_b_secs={fetch_latency_b_secs:.2f} "
                    f"canonical_pair_id={canonical_pair_id}",
                )
            if emit_summary:
                self._log_arbitrage_summary()
            return True

        if (
            quote_age_a_secs > freshness.max_quote_age_secs
            or quote_age_b_secs > freshness.max_quote_age_secs
        ):
            self._stale_quote_suppressions += 1
            self._maybe_trigger_stale_quote_refresh(
                inst_a,
                inst_b,
                reason="stale_quote",
                now_ns=now_ns,
            )
            if emit_suppression_log:
                self._log_fast_stale_suppression(
                    inst_a,
                    inst_b,
                    odds_a_raw,
                    odds_b_raw,
                    canonical_pair_id,
                    match_type,
                    quote_age_a_secs,
                    quote_age_b_secs,
                    quote_delta_secs,
                )
            if emit_summary:
                self._log_arbitrage_summary()
            return True

        if quote_delta_secs > freshness.max_pair_skew_secs:
            self._manual_review_suppressions += 1
            if emit_suppression_log:
                self.log.info(
                    "Arbitrage candidate suppressed: "
                    f"reason=cross_cycle max_pair_skew_secs={freshness.max_pair_skew_secs:.2f} "
                    f"quote_delta_secs={quote_delta_secs:.2f} "
                    f"canonical_pair_id={canonical_pair_id}",
                )
            if emit_summary:
                self._log_arbitrage_summary()
            return True

        matcher_suspect, suspect_reason = self._matcher_suspect_reason(inst_a, inst_b)
        if matcher_suspect:
            self._matcher_suspect_suppressions += 1
            if emit_suppression_log:
                self._log_fast_suspect_suppression(
                    inst_a,
                    inst_b,
                    odds_a_raw,
                    odds_b_raw,
                    canonical_pair_id,
                    match_type,
                    hedge_type,
                    hedge_confidence,
                    suspect_reason,
                    quote_age_a_secs,
                    quote_age_b_secs,
                )
            if emit_summary:
                self._log_arbitrage_summary()
            return True
        return False

    # skipcq: PYL-R0913, PYL-R0917
    def _log_fast_duplicate_suppression(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        odds_a: float,
        odds_b: float,
        canonical_pair_id: str,
        match_type: str,
        quote_age_a_secs: float,
        quote_age_b_secs: float,
    ) -> None:
        opportunity_id = self._fast_opportunity_id(
            canonical_pair_id,
            match_type,
            odds_a,
            odds_b,
        )
        instrument_fields = self._fast_diagnostics_instrument_fields(
            instrument_a,
            instrument_b,
            odds_a,
            odds_b,
            quote_age_a_secs,
            quote_age_b_secs,
        )
        self.log.debug(
            "Arbitrage candidate suppressed: "
            f"reason=duplicate opportunity_id={opportunity_id} "
            f"canonical_pair_id={canonical_pair_id}"
            f"{instrument_fields}",
        )

    # skipcq: PYL-R0913, PYL-R0917
    def _log_fast_stale_suppression(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        odds_a: float,
        odds_b: float,
        canonical_pair_id: str,
        match_type: str,
        quote_age_a_secs: float,
        quote_age_b_secs: float,
        quote_delta_secs: float,
    ) -> None:
        opportunity_id = self._fast_opportunity_id(
            canonical_pair_id,
            match_type,
            odds_a,
            odds_b,
        )
        instrument_fields = self._fast_diagnostics_instrument_fields(
            instrument_a,
            instrument_b,
            odds_a,
            odds_b,
            quote_age_a_secs,
            quote_age_b_secs,
        )
        self.log.info(
            "Arbitrage candidate suppressed: "
            f"reason=stale_quote opportunity_id={opportunity_id} "
            f"quote_age_a_secs={quote_age_a_secs:.2f} "
            f"quote_age_b_secs={quote_age_b_secs:.2f} "
            f"quote_delta_secs={quote_delta_secs:.2f}"
            f"{instrument_fields}",
        )

    # skipcq: PYL-R0913, PYL-R0917
    def _log_fast_suspect_suppression(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        odds_a: float,
        odds_b: float,
        canonical_pair_id: str,
        match_type: str,
        hedge_type: str,
        hedge_confidence: float,
        suspect_reason: str,
        quote_age_a_secs: float,
        quote_age_b_secs: float,
    ) -> None:
        opportunity_id = self._fast_opportunity_id(
            canonical_pair_id,
            match_type,
            odds_a,
            odds_b,
        )
        instrument_fields = self._fast_diagnostics_instrument_fields(
            instrument_a,
            instrument_b,
            odds_a,
            odds_b,
            quote_age_a_secs,
            quote_age_b_secs,
        )
        self.log.warning(
            "Arbitrage candidate suppressed: "
            f"reason=matcher_suspect suspect_reason={suspect_reason} "
            f"opportunity_id={opportunity_id} "
            f"event_id_a={instrument_a.event_id} event_id_b={instrument_b.event_id} "
            f"market_id_a={instrument_a.market_id or instrument_a.event_id} "
            f"market_id_b={instrument_b.market_id or instrument_b.event_id} "
            f"match_type={match_type} hedge_match_type={hedge_type} "
            f"confidence={hedge_confidence:.2f}"
            f"{instrument_fields}",
        )

    @staticmethod
    def _fast_opportunity_id(
        canonical_pair_id: str,
        match_type: str,
        odds_a: float,
        odds_b: float,
    ) -> str:
        return f"{canonical_pair_id}|{match_type}|{odds_a}:{odds_b}"

    def _handle_search_quote_tick(
        self,
        tick: QuoteTick,
        instrument: CryptoBettingInstrument,
    ) -> None:
        # Find arbitrage opportunities
        candidates = [
            inst for inst in tuple(self._subscribed_instruments) if inst.id != instrument.id
        ]

        hedges = self._matcher.find_hedges(
            instrument=instrument,
            candidates=candidates,
            include_cross_venue=True,
        )

        # Check each hedge for arbitrage
        for hedge in hedges:
            hedge_quote = self._latest_quotes.get(str(hedge.instrument.id))
            current_odds = self._quote_odds(tick)
            hedge_odds = self._quote_odds(hedge_quote)
            if current_odds is None or hedge_odds is None:
                continue

            opportunity = self._matcher.check_arbitrage(
                instrument,
                hedge.instrument,
                odds_a=current_odds,
                odds_b=hedge_odds,
            )

            if opportunity is not None:
                opportunity = self.fee_adjusted_opportunity(opportunity)

            if opportunity and opportunity.profit_margin >= self._config.min_profit_margin:
                if not self._candidate_matches_execution_venue_mode(opportunity):
                    continue
                self._raw_arbitrage_detections += 1
                now_ns = self.clock.timestamp_ns()
                diagnostics = self._build_arbitrage_diagnostics(
                    opportunity=opportunity,
                    hedge_match_type=hedge.match_type,
                    hedge_confidence=hedge.confidence,
                    quote_a=tick,
                    quote_b=hedge_quote,
                    now_ns=now_ns,
                )
                if self._suppress_arbitrage_candidate(diagnostics):
                    self._log_arbitrage_summary()
                    continue

                self._record_opportunity_pair(
                    diagnostics.canonical_pair_id,
                    diagnostics.opportunity_id,
                    now_ns,
                )
                self._opportunities_found += 1
                self._executable_candidates += 1
                self._handle_arbitrage_opportunity(opportunity, diagnostics)
                self._log_arbitrage_summary()

    def _handle_opportunity_candidate(
        self,
        candidate: OpportunityCandidate,
        now_ns: int,
    ) -> None:
        decision_started_ns = time.perf_counter_ns()
        self._raw_arbitrage_detections += 1
        opportunity = self.fee_adjusted_opportunity(candidate.opportunity)
        if not self._candidate_matches_execution_venue_mode(opportunity):
            self._record_latency_sample(
                self._candidate_decision_latency_ns,
                time.perf_counter_ns() - decision_started_ns,
            )
            return
        if opportunity.profit_margin < self._config.min_profit_margin:
            self._record_latency_sample(
                self._candidate_decision_latency_ns,
                time.perf_counter_ns() - decision_started_ns,
            )
            return

        diagnostics = self._build_arbitrage_diagnostics(
            opportunity=opportunity,
            hedge_match_type=candidate.edge.hedge_type,
            hedge_confidence=candidate.edge.confidence,
            quote_a=candidate.quote_a.quote,
            quote_b=candidate.quote_b.quote,
            now_ns=now_ns,
        )
        if self._suppress_arbitrage_candidate(diagnostics):
            self._log_arbitrage_summary()
            self._record_latency_sample(
                self._candidate_decision_latency_ns,
                time.perf_counter_ns() - decision_started_ns,
            )
            return

        self._record_opportunity_pair(
            diagnostics.canonical_pair_id,
            diagnostics.opportunity_id,
            now_ns,
        )
        self._opportunities_found += 1
        self._executable_candidates += 1
        self._handle_arbitrage_opportunity(opportunity, diagnostics)
        self._log_arbitrage_summary()
        self._record_latency_sample(
            self._candidate_decision_latency_ns,
            time.perf_counter_ns() - decision_started_ns,
        )

    @staticmethod
    def _opportunity_match_type(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> str:
        if instrument_a.market_name == instrument_b.market_name:
            return "same_market"
        if instrument_a.venue_name == instrument_b.venue_name:
            return "cross_market"
        return "cross_venue"

    def _fast_arbitrage_opportunity(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        *,
        odds_a_raw: float,
        odds_b_raw: float,
        match_type: str,
    ) -> ArbitrageOpportunity:
        odds_a = Decimal(str(odds_a_raw))
        odds_b = Decimal(str(odds_b_raw))
        probability_a = Decimal(1) / odds_a
        probability_b = Decimal(1) / odds_b
        total_probability = probability_a + probability_b
        profit_margin = (Decimal(1) / total_probability) - Decimal(1)
        opportunity = ArbitrageOpportunity(
            instrument_a=instrument_a,
            instrument_b=instrument_b,
            probability_a=probability_a,
            probability_b=probability_b,
            total_probability=total_probability,
            profit_margin=profit_margin,
            odds_a=odds_a,
            odds_b=odds_b,
            is_same_venue=instrument_a.venue_name == instrument_b.venue_name,
            match_type=match_type,
            raw_probability_a=probability_a,
            raw_probability_b=probability_b,
            raw_total_probability=total_probability,
            raw_profit_margin=profit_margin,
        )
        return self.fee_adjusted_opportunity(opportunity)

    # skipcq: PYL-R0913, PYL-R0914
    def _fast_arbitrage_diagnostics(
        self,
        *,
        opportunity: ArbitrageOpportunity,
        canonical_pair_id: str,
        hedge_match_type: str,
        hedge_confidence: float,
        quote_ts_a: int,
        quote_ts_b: int,
        now_ns: int,
    ) -> ArbitrageDiagnostics:
        inst_a = opportunity.instrument_a
        inst_b = opportunity.instrument_b
        quote_a = self._latest_quotes.get(str(inst_a.id))
        quote_b = self._latest_quotes.get(str(inst_b.id))
        quote_age_a_secs = self._fast_snapshot_quote_age_secs(now_ns, quote_ts_a)
        quote_age_b_secs = self._fast_snapshot_quote_age_secs(now_ns, quote_ts_b)
        quote_delta_secs = abs(int(quote_ts_a) - int(quote_ts_b)) / NANOSECONDS_PER_SECOND
        fetch_latency_a_secs = self._quote_fetch_latency_secs(quote_a)
        fetch_latency_b_secs = self._quote_fetch_latency_secs(quote_b)
        freshness = self._quote_freshness_thresholds(inst_a, inst_b)
        stale = (
            quote_age_a_secs > freshness.max_quote_age_secs
            or quote_age_b_secs > freshness.max_quote_age_secs
        )
        fetch_latency_stale = (
            fetch_latency_a_secs > freshness.max_fetch_latency_secs
            or fetch_latency_b_secs > freshness.max_fetch_latency_secs
        )
        stake_odds_a, stake_odds_b = self._stake_pricing_odds(opportunity)
        suggested_stake_a, suggested_stake_b, expected_profit = calculate_arbitrage_stakes(
            odds_a=stake_odds_a,
            odds_b=stake_odds_b,
            total_stake=self._config.max_total_stake,
        )
        available_size_a = self._quote_available_size(quote_a)
        available_size_b = self._quote_available_size(quote_b)
        classification, classification_reason = self._classify_arbitrage_candidate(
            stale=stale,
            fetch_latency_stale=fetch_latency_stale,
            matcher_suspect=False,
            suspect_reason="",
            same_quote_cycle=quote_delta_secs <= freshness.max_pair_skew_secs,
            suggested_stake_a=suggested_stake_a,
            suggested_stake_b=suggested_stake_b,
            available_size_a=available_size_a,
            available_size_b=available_size_b,
        )
        return ArbitrageDiagnostics(
            opportunity_id=(
                f"{canonical_pair_id}|{opportunity.match_type}|"
                f"{opportunity.odds_a}:{opportunity.odds_b}"
            ),
            canonical_pair_id=canonical_pair_id,
            match_type=opportunity.match_type,
            hedge_match_type=hedge_match_type,
            hedge_confidence=hedge_confidence,
            instrument_a=inst_a,
            instrument_b=inst_b,
            event_id_a=str(inst_a.event_id),
            event_id_b=str(inst_b.event_id),
            instrument_id_a=str(inst_a.id),
            instrument_id_b=str(inst_b.id),
            event_name_a=inst_a.event_name,
            event_name_b=inst_b.event_name,
            canonical_event_key_a=inst_a.event_key(include_start_time=False),
            canonical_event_key_b=inst_b.event_key(include_start_time=False),
            market_id_a=str(inst_a.market_id or inst_a.event_id),
            market_id_b=str(inst_b.market_id or inst_b.event_id),
            market_name_a=inst_a.market_name,
            market_name_b=inst_b.market_name,
            params_a=inst_a.params,
            params_b=inst_b.params,
            outcome_a=inst_a.outcome,
            outcome_b=inst_b.outcome,
            venue_a=str(inst_a.id.venue),
            venue_b=str(inst_b.id.venue),
            odds_a=opportunity.odds_a,
            odds_b=opportunity.odds_b,
            quote_ts_a=int(quote_ts_a),
            quote_ts_b=int(quote_ts_b),
            quote_cycle_id_a=(
                self._quote_cycle_id(quote_a)
                if quote_a is not None
                else str(int(quote_ts_a) // NANOSECONDS_PER_SECOND)
            ),
            quote_cycle_id_b=(
                self._quote_cycle_id(quote_b)
                if quote_b is not None
                else str(int(quote_ts_b) // NANOSECONDS_PER_SECOND)
            ),
            quote_age_a_secs=quote_age_a_secs,
            quote_age_b_secs=quote_age_b_secs,
            quote_delta_secs=quote_delta_secs,
            fetch_latency_a_secs=fetch_latency_a_secs,
            fetch_latency_b_secs=fetch_latency_b_secs,
            freshness_profile=freshness.profile,
            max_quote_age_secs=freshness.max_quote_age_secs,
            max_pair_skew_secs=freshness.max_pair_skew_secs,
            max_fetch_latency_secs=freshness.max_fetch_latency_secs,
            same_quote_cycle=quote_delta_secs <= freshness.max_pair_skew_secs,
            stale=stale,
            fetch_latency_stale=fetch_latency_stale,
            matcher_suspect=False,
            suspect_reason="",
            suggested_stake_a=suggested_stake_a,
            suggested_stake_b=suggested_stake_b,
            expected_profit=expected_profit,
            raw_profit_margin=opportunity.raw_profit_margin or opportunity.profit_margin,
            fee_adjusted_profit_margin=opportunity.profit_margin,
            fee_drag=opportunity.fee_drag,
            raw_total_probability=opportunity.raw_total_probability
            or opportunity.total_probability,
            fee_adjusted_total_probability=opportunity.total_probability,
            taker_fee_rate_a=opportunity.taker_fee_rate_a,
            taker_fee_rate_b=opportunity.taker_fee_rate_b,
            maker_rebate_rate_a=opportunity.maker_rebate_rate_a,
            maker_rebate_rate_b=opportunity.maker_rebate_rate_b,
            winning_profit_fee_rate_a=opportunity.winning_profit_fee_rate_a,
            winning_profit_fee_rate_b=opportunity.winning_profit_fee_rate_b,
            basket_rebate_rate=opportunity.basket_rebate_rate,
            basket_boost_rate=opportunity.basket_boost_rate,
            available_size_a=available_size_a,
            available_size_b=available_size_b,
            classification=classification,
            classification_reason=classification_reason,
        )

    # skipcq: PYL-R0913, PYL-R0914
    def _log_fast_arbitrage_snapshot(
        self,
        source_node_id: str,
        target_node_id: str,
        *,
        canonical_pair_id: str,
        match_type: str,
        hedge_type: str,
        hedge_confidence: float,
        odds_a_raw: float,
        odds_b_raw: float,
        profit_margin_raw: float,
        quote_ts_a: int,
        quote_ts_b: int,
        now_ns: int,
    ) -> None:
        source_node = self._opportunity_graph.nodes_by_id.get(source_node_id)
        target_node = self._opportunity_graph.nodes_by_id.get(target_node_id)
        if source_node is None or target_node is None:
            return

        instrument_a = source_node.instrument
        instrument_b = target_node.instrument
        quote_age_a_secs = self._fast_snapshot_quote_age_secs(now_ns, quote_ts_a)
        quote_age_b_secs = self._fast_snapshot_quote_age_secs(now_ns, quote_ts_b)
        quote_delta_secs = abs(int(quote_ts_a) - int(quote_ts_b)) / NANOSECONDS_PER_SECOND
        opportunity_id = f"{canonical_pair_id}|{match_type}|{odds_a_raw}:{odds_b_raw}"
        diagnostic_suffix = (
            f" | opportunity_id={opportunity_id} "
            f"match_type={match_type} "
            f"hedge_match_type={hedge_type} "
            f"confidence={hedge_confidence:.2f} "
            f"venue_a={instrument_a.id.venue} venue_b={instrument_b.id.venue} "
            f"event_id_a={instrument_a.event_id} event_id_b={instrument_b.event_id} "
            f"market_id_a={instrument_a.market_id or instrument_a.event_id} "
            f"market_id_b={instrument_b.market_id or instrument_b.event_id} "
            f"market_a={instrument_a.market_name} market_b={instrument_b.market_name} "
            f"outcome_a={instrument_a.outcome} outcome_b={instrument_b.outcome} "
            f"quote_ts_a={int(quote_ts_a)} quote_ts_b={int(quote_ts_b)} "
            f"quote_age_a_secs={quote_age_a_secs:.2f} "
            f"quote_age_b_secs={quote_age_b_secs:.2f} "
            f"quote_delta_secs={quote_delta_secs:.2f} "
            f"same_quote_cycle={quote_delta_secs <= 2.0}"
        )
        self.log.info(
            f"Arbitrage found: {instrument_a.id.symbol} @ {odds_a_raw} vs "
            f"{instrument_b.id.symbol} @ {odds_b_raw} | "
            f"Profit: {profit_margin_raw:.2%}"
            f"{diagnostic_suffix}",
        )

    # skipcq: PYL-R0913, PYL-R0917
    def _fast_diagnostics_instrument_fields(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        odds_a: float,
        odds_b: float,
        quote_age_a_secs: float,
        quote_age_b_secs: float,
    ) -> str:
        if not self._config.opportunity_log_manual_instructions:
            return ""

        return (
            " | Instrument A: "
            f"instrument_id={instrument_a.id} "
            f"venue={instrument_a.id.venue} "
            f"event={instrument_a.event_name!r} "
            f"market={instrument_a.market_name!r} "
            f"selection={instrument_a.outcome!r} "
            f"odds={odds_a} "
            f"market_id={instrument_a.market_id or instrument_a.event_id} "
            f"quote_age_secs={quote_age_a_secs:.2f}; "
            "Instrument B: "
            f"instrument_id={instrument_b.id} "
            f"venue={instrument_b.id.venue} "
            f"event={instrument_b.event_name!r} "
            f"market={instrument_b.market_name!r} "
            f"selection={instrument_b.outcome!r} "
            f"odds={odds_b} "
            f"market_id={instrument_b.market_id or instrument_b.event_id} "
            f"quote_age_secs={quote_age_b_secs:.2f}"
        )

    def _suppress_arbitrage_candidate(self, diagnostics: ArbitrageDiagnostics) -> bool:
        now_ns = self._diagnostics_observed_at_ns(diagnostics)
        if self._is_duplicate_opportunity_pair(
            diagnostics.canonical_pair_id,
            diagnostics.opportunity_id,
            now_ns,
        ):
            self._duplicate_opportunities_suppressed += 1
            self.log.debug(
                "Arbitrage candidate suppressed: "
                f"reason=duplicate opportunity_id={diagnostics.opportunity_id} "
                f"canonical_pair_id={diagnostics.canonical_pair_id}"
                f"{self._diagnostics_instrument_fields(diagnostics)}",
            )
            return True

        if diagnostics.fetch_latency_stale:
            self._stale_quote_suppressions += 1
            self._maybe_trigger_stale_quote_refresh(
                diagnostics.instrument_a,
                diagnostics.instrument_b,
                reason="fetch_latency",
                now_ns=now_ns,
            )
            self.log.info(
                "Arbitrage candidate suppressed: "
                f"reason=fetch_latency classification={diagnostics.classification} "
                f"classification_reason={diagnostics.classification_reason} "
                f"opportunity_id={diagnostics.opportunity_id} "
                f"fetch_latency_a_secs={diagnostics.fetch_latency_a_secs:.2f} "
                f"fetch_latency_b_secs={diagnostics.fetch_latency_b_secs:.2f} "
                f"max_fetch_latency_secs={diagnostics.max_fetch_latency_secs:.2f}"
                f"{self._diagnostics_instrument_fields(diagnostics)}",
            )
            return True

        if diagnostics.stale:
            self._stale_quote_suppressions += 1
            self._maybe_trigger_stale_quote_refresh(
                diagnostics.instrument_a,
                diagnostics.instrument_b,
                reason="stale_quote",
                now_ns=now_ns,
            )
            self.log.info(
                "Arbitrage candidate suppressed: "
                f"reason=stale_quote classification={diagnostics.classification} "
                f"classification_reason={diagnostics.classification_reason} "
                f"opportunity_id={diagnostics.opportunity_id} "
                f"quote_age_a_secs={diagnostics.quote_age_a_secs:.2f} "
                f"quote_age_b_secs={diagnostics.quote_age_b_secs:.2f} "
                f"quote_delta_secs={diagnostics.quote_delta_secs:.2f}"
                f" fetch_latency_a_secs={diagnostics.fetch_latency_a_secs:.2f}"
                f" fetch_latency_b_secs={diagnostics.fetch_latency_b_secs:.2f}"
                f"{self._diagnostics_instrument_fields(diagnostics)}",
            )
            return True

        if diagnostics.matcher_suspect:
            self._matcher_suspect_suppressions += 1
            self.log.warning(
                "Arbitrage candidate suppressed: "
                f"reason=matcher_suspect classification={diagnostics.classification} "
                f"classification_reason={diagnostics.classification_reason} "
                f"suspect_reason={diagnostics.suspect_reason} "
                f"opportunity_id={diagnostics.opportunity_id} "
                f"event_id_a={diagnostics.event_id_a} event_id_b={diagnostics.event_id_b} "
                f"market_id_a={diagnostics.market_id_a} market_id_b={diagnostics.market_id_b} "
                f"match_type={diagnostics.match_type} "
                f"hedge_match_type={diagnostics.hedge_match_type} "
                f"confidence={diagnostics.hedge_confidence:.2f}"
                f"{self._diagnostics_instrument_fields(diagnostics)}",
            )
            return True

        if diagnostics.classification == "liquidity_insufficient":
            self._liquidity_suppressions += 1
            self.log.info(
                "Arbitrage candidate suppressed: "
                f"reason=liquidity_insufficient classification={diagnostics.classification} "
                f"classification_reason={diagnostics.classification_reason} "
                f"opportunity_id={diagnostics.opportunity_id} "
                f"suggested_stake_a={diagnostics.suggested_stake_a} "
                f"available_size_a={diagnostics.available_size_a} "
                f"suggested_stake_b={diagnostics.suggested_stake_b} "
                f"available_size_b={diagnostics.available_size_b}"
                f"{self._diagnostics_instrument_fields(diagnostics)}",
            )
            return True

        if diagnostics.classification == "needs_manual_review":
            self._manual_review_suppressions += 1
            self.log.info(
                "Arbitrage candidate suppressed: "
                f"reason=needs_manual_review classification={diagnostics.classification} "
                f"classification_reason={diagnostics.classification_reason} "
                f"opportunity_id={diagnostics.opportunity_id} "
                f"same_quote_cycle={diagnostics.same_quote_cycle} "
                f"quote_delta_secs={diagnostics.quote_delta_secs:.2f}"
                f" freshness_profile={diagnostics.freshness_profile} "
                f"max_quote_age_secs={diagnostics.max_quote_age_secs:.2f} "
                f"max_pair_skew_secs={diagnostics.max_pair_skew_secs:.2f} "
                f"max_fetch_latency_secs={diagnostics.max_fetch_latency_secs:.2f} "
                f"fetch_latency_a_secs={diagnostics.fetch_latency_a_secs:.2f} "
                f"fetch_latency_b_secs={diagnostics.fetch_latency_b_secs:.2f}"
                f"{self._diagnostics_instrument_fields(diagnostics)}",
            )
            return True

        return False

    @staticmethod
    def _diagnostics_observed_at_ns(diagnostics: ArbitrageDiagnostics) -> int:
        observed_a = int(
            diagnostics.quote_ts_a + diagnostics.quote_age_a_secs * NANOSECONDS_PER_SECOND,
        )
        observed_b = int(
            diagnostics.quote_ts_b + diagnostics.quote_age_b_secs * NANOSECONDS_PER_SECOND,
        )
        return max(observed_a, observed_b, diagnostics.quote_ts_a, diagnostics.quote_ts_b)

    def _diagnostics_instrument_fields(self, diagnostics: ArbitrageDiagnostics) -> str:
        if not self._config.opportunity_log_manual_instructions:
            return ""

        return (
            " | Instrument A: "
            f"instrument_id={diagnostics.instrument_id_a} "
            f"venue={diagnostics.venue_a} "
            f"event={diagnostics.event_name_a!r} "
            f"market={diagnostics.market_name_a!r} "
            f"params={diagnostics.params_a!r} "
            f"selection={diagnostics.outcome_a!r} "
            f"odds={diagnostics.odds_a} "
            f"taker_fee_rate={diagnostics.taker_fee_rate_a} "
            f"maker_rebate_rate={diagnostics.maker_rebate_rate_a} "
            f"winning_profit_fee_rate={diagnostics.winning_profit_fee_rate_a} "
            f"market_id={diagnostics.market_id_a} "
            f"available_size={diagnostics.available_size_a} "
            f"quote_cycle_id={diagnostics.quote_cycle_id_a} "
            f"quote_age_secs={diagnostics.quote_age_a_secs:.2f} "
            f"fetch_latency_secs={diagnostics.fetch_latency_a_secs:.2f}; "
            "Instrument B: "
            f"instrument_id={diagnostics.instrument_id_b} "
            f"venue={diagnostics.venue_b} "
            f"event={diagnostics.event_name_b!r} "
            f"market={diagnostics.market_name_b!r} "
            f"params={diagnostics.params_b!r} "
            f"selection={diagnostics.outcome_b!r} "
            f"odds={diagnostics.odds_b} "
            f"taker_fee_rate={diagnostics.taker_fee_rate_b} "
            f"maker_rebate_rate={diagnostics.maker_rebate_rate_b} "
            f"winning_profit_fee_rate={diagnostics.winning_profit_fee_rate_b} "
            f"market_id={diagnostics.market_id_b} "
            f"available_size={diagnostics.available_size_b} "
            f"quote_cycle_id={diagnostics.quote_cycle_id_b} "
            f"quote_age_secs={diagnostics.quote_age_b_secs:.2f} "
            f"fetch_latency_secs={diagnostics.fetch_latency_b_secs:.2f}"
        )

    def _manual_execution_plan(self, *args: object) -> str:
        if len(args) == 1:
            diagnostics = args[0]
        elif len(args) == 2:
            diagnostics = args[1]
        else:
            msg = "_manual_execution_plan expects diagnostics or opportunity, diagnostics"
            raise TypeError(msg)

        if not isinstance(diagnostics, ArbitrageDiagnostics):
            msg = "_manual_execution_plan requires ArbitrageDiagnostics"
            raise TypeError(msg)
        if not self._config.opportunity_log_manual_instructions:
            return ""

        stake_a = diagnostics.suggested_stake_a
        stake_b = diagnostics.suggested_stake_b
        expected_profit = diagnostics.expected_profit
        return (
            " | Manual execution plan: "
            f"execution_enabled={self._config.auto_execute} "
            "Instrument A: "
            f"bet={stake_a} "
            f"instrument_id={diagnostics.instrument_id_a} "
            f"venue={diagnostics.venue_a} "
            f"event={diagnostics.event_name_a!r} "
            f"market={diagnostics.market_name_a!r} "
            f"params={diagnostics.params_a!r} "
            f"selection={diagnostics.outcome_a!r} "
            f"odds={diagnostics.odds_a} "
            f"market_id={diagnostics.market_id_a} "
            f"available_size={diagnostics.available_size_a} "
            f"quote_cycle_id={diagnostics.quote_cycle_id_a} "
            f"quote_age_secs={diagnostics.quote_age_a_secs:.2f} "
            f"fetch_latency_secs={diagnostics.fetch_latency_a_secs:.2f}; "
            "Instrument B: "
            f"bet={stake_b} "
            f"instrument_id={diagnostics.instrument_id_b} "
            f"venue={diagnostics.venue_b} "
            f"event={diagnostics.event_name_b!r} "
            f"market={diagnostics.market_name_b!r} "
            f"params={diagnostics.params_b!r} "
            f"selection={diagnostics.outcome_b!r} "
            f"odds={diagnostics.odds_b} "
            f"market_id={diagnostics.market_id_b} "
            f"available_size={diagnostics.available_size_b} "
            f"quote_cycle_id={diagnostics.quote_cycle_id_b} "
            f"quote_age_secs={diagnostics.quote_age_b_secs:.2f} "
            f"fetch_latency_secs={diagnostics.fetch_latency_b_secs:.2f}; "
            f"expected_profit={expected_profit} "
            f"raw_profit_margin={diagnostics.raw_profit_margin} "
            f"fee_adjusted_profit_margin={diagnostics.fee_adjusted_profit_margin} "
            f"fee_drag={diagnostics.fee_drag} "
            f"{self._live_execution_fx_breakdown_text(diagnostics)} "
            f"basket_rebate_rate={diagnostics.basket_rebate_rate} "
            f"basket_boost_rate={diagnostics.basket_boost_rate} "
            f"raw_total_probability={diagnostics.raw_total_probability} "
            f"fee_adjusted_total_probability={diagnostics.fee_adjusted_total_probability} "
            f"max_total_stake={self._config.max_total_stake}"
        )

    def _live_execution_fx_breakdown_text(self, diagnostics: ArbitrageDiagnostics) -> str:
        opportunity = ArbitrageOpportunity(
            instrument_a=diagnostics.instrument_a,
            instrument_b=diagnostics.instrument_b,
            probability_a=Decimal(0),
            probability_b=Decimal(0),
            total_probability=Decimal(0),
            profit_margin=diagnostics.fee_adjusted_profit_margin,
            odds_a=diagnostics.odds_a,
            odds_b=diagnostics.odds_b,
            is_same_venue=diagnostics.venue_a == diagnostics.venue_b,
            match_type=diagnostics.match_type,
        )
        conversion_a, conversion_b = self._live_execution_stake_conversions(
            opportunity,
            diagnostics.suggested_stake_a,
            diagnostics.suggested_stake_b,
        )
        usd_a = conversion_a.converted_amount
        usd_b = conversion_b.converted_amount
        blockers = sorted(
            {
                str(conversion.blocker_reason)
                for conversion in (conversion_a, conversion_b)
                if conversion.blocker_reason
            },
        )
        usd_total = usd_a + usd_b if usd_a is not None and usd_b is not None else None
        fx_cost = (
            usd_total - diagnostics.suggested_stake_a - diagnostics.suggested_stake_b
            if usd_total is not None
            else None
        )
        return (
            f"fx_source_a={conversion_a.source} "
            f"fx_source_b={conversion_b.source} "
            f"fx_rate_a={conversion_a.rate} "
            f"fx_rate_b={conversion_b.rate} "
            f"fx_haircut_bps={max(conversion_a.haircut_bps, conversion_b.haircut_bps)} "
            f"usd_equivalent_stake={usd_total} "
            f"fx_cost={fx_cost} "
            f"fx_blockers={blockers}"
        )

    # skipcq: PYL-R0913, PYL-R0914
    def _build_arbitrage_diagnostics(
        self,
        *,
        opportunity: ArbitrageOpportunity,
        hedge_match_type: str,
        hedge_confidence: float,
        quote_a: QuoteTick,
        quote_b: QuoteTick,
        now_ns: int,
    ) -> ArbitrageDiagnostics:
        inst_a = opportunity.instrument_a
        inst_b = opportunity.instrument_b
        canonical_pair_id = self._canonical_pair_id(inst_a, inst_b)
        opportunity_id = (
            f"{canonical_pair_id}|{opportunity.match_type}|"
            f"{opportunity.odds_a}:{opportunity.odds_b}"
        )
        quote_age_a_secs = self._quote_age_secs(now_ns, quote_a)
        quote_age_b_secs = self._quote_age_secs(now_ns, quote_b)
        quote_delta_secs = self._quote_pair_skew_secs(quote_a, quote_b)
        fetch_latency_a_secs = self._quote_fetch_latency_secs(quote_a)
        fetch_latency_b_secs = self._quote_fetch_latency_secs(quote_b)
        freshness = self._quote_freshness_thresholds(inst_a, inst_b)
        stale = (
            quote_age_a_secs > freshness.max_quote_age_secs
            or quote_age_b_secs > freshness.max_quote_age_secs
        )
        fetch_latency_stale = (
            fetch_latency_a_secs > freshness.max_fetch_latency_secs
            or fetch_latency_b_secs > freshness.max_fetch_latency_secs
        )
        matcher_suspect, suspect_reason = self._matcher_suspect_reason(inst_a, inst_b)
        stake_odds_a, stake_odds_b = self._stake_pricing_odds(opportunity)
        suggested_stake_a, suggested_stake_b, expected_profit = calculate_arbitrage_stakes(
            odds_a=stake_odds_a,
            odds_b=stake_odds_b,
            total_stake=self._config.max_total_stake,
        )
        available_size_a = self._quote_available_size(quote_a)
        available_size_b = self._quote_available_size(quote_b)
        classification, classification_reason = self._classify_arbitrage_candidate(
            stale=stale,
            fetch_latency_stale=fetch_latency_stale,
            matcher_suspect=matcher_suspect,
            suspect_reason=suspect_reason,
            same_quote_cycle=quote_delta_secs <= freshness.max_pair_skew_secs,
            suggested_stake_a=suggested_stake_a,
            suggested_stake_b=suggested_stake_b,
            available_size_a=available_size_a,
            available_size_b=available_size_b,
        )
        return ArbitrageDiagnostics(
            opportunity_id=opportunity_id,
            canonical_pair_id=canonical_pair_id,
            match_type=opportunity.match_type,
            hedge_match_type=hedge_match_type,
            hedge_confidence=hedge_confidence,
            instrument_a=inst_a,
            instrument_b=inst_b,
            event_id_a=str(inst_a.event_id),
            event_id_b=str(inst_b.event_id),
            instrument_id_a=str(inst_a.id),
            instrument_id_b=str(inst_b.id),
            event_name_a=inst_a.event_name,
            event_name_b=inst_b.event_name,
            canonical_event_key_a=inst_a.event_key(include_start_time=False),
            canonical_event_key_b=inst_b.event_key(include_start_time=False),
            market_id_a=str(inst_a.market_id or inst_a.event_id),
            market_id_b=str(inst_b.market_id or inst_b.event_id),
            market_name_a=inst_a.market_name,
            market_name_b=inst_b.market_name,
            params_a=inst_a.params,
            params_b=inst_b.params,
            outcome_a=inst_a.outcome,
            outcome_b=inst_b.outcome,
            venue_a=str(inst_a.id.venue),
            venue_b=str(inst_b.id.venue),
            odds_a=opportunity.odds_a,
            odds_b=opportunity.odds_b,
            quote_ts_a=self._quote_decision_timestamp_ns(quote_a),
            quote_ts_b=self._quote_decision_timestamp_ns(quote_b),
            quote_cycle_id_a=self._quote_cycle_id(quote_a),
            quote_cycle_id_b=self._quote_cycle_id(quote_b),
            quote_age_a_secs=quote_age_a_secs,
            quote_age_b_secs=quote_age_b_secs,
            quote_delta_secs=quote_delta_secs,
            fetch_latency_a_secs=fetch_latency_a_secs,
            fetch_latency_b_secs=fetch_latency_b_secs,
            freshness_profile=freshness.profile,
            max_quote_age_secs=freshness.max_quote_age_secs,
            max_pair_skew_secs=freshness.max_pair_skew_secs,
            max_fetch_latency_secs=freshness.max_fetch_latency_secs,
            same_quote_cycle=quote_delta_secs <= freshness.max_pair_skew_secs,
            stale=stale,
            fetch_latency_stale=fetch_latency_stale,
            matcher_suspect=matcher_suspect,
            suspect_reason=suspect_reason,
            suggested_stake_a=suggested_stake_a,
            suggested_stake_b=suggested_stake_b,
            expected_profit=expected_profit,
            raw_profit_margin=opportunity.raw_profit_margin or opportunity.profit_margin,
            fee_adjusted_profit_margin=opportunity.profit_margin,
            fee_drag=opportunity.fee_drag,
            raw_total_probability=opportunity.raw_total_probability
            or opportunity.total_probability,
            fee_adjusted_total_probability=opportunity.total_probability,
            taker_fee_rate_a=opportunity.taker_fee_rate_a,
            taker_fee_rate_b=opportunity.taker_fee_rate_b,
            maker_rebate_rate_a=opportunity.maker_rebate_rate_a,
            maker_rebate_rate_b=opportunity.maker_rebate_rate_b,
            winning_profit_fee_rate_a=opportunity.winning_profit_fee_rate_a,
            winning_profit_fee_rate_b=opportunity.winning_profit_fee_rate_b,
            basket_rebate_rate=opportunity.basket_rebate_rate,
            basket_boost_rate=opportunity.basket_boost_rate,
            available_size_a=available_size_a,
            available_size_b=available_size_b,
            classification=classification,
            classification_reason=classification_reason,
        )

    @staticmethod
    def _canonical_pair_id(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> str:
        return "|".join(sorted([str(instrument_a.id), str(instrument_b.id)]))

    @staticmethod
    def _quote_age_secs(now_ns: int, quote: QuoteTick) -> float:
        return BettingArbitrageStrategy.quote_age_secs(now_ns, quote)

    @staticmethod
    def quote_age_secs(now_ns: int, quote: QuoteTick) -> float:
        timestamp_ns = BettingArbitrageStrategy._quote_decision_timestamp_ns(quote)
        if timestamp_ns <= 0:
            return 0.0
        return max(0.0, (now_ns - timestamp_ns) / NANOSECONDS_PER_SECOND)

    @staticmethod
    def _quote_pair_skew_secs(quote_a: QuoteTick, quote_b: QuoteTick) -> float:
        timestamp_a = BettingArbitrageStrategy._quote_decision_timestamp_ns(quote_a)
        timestamp_b = BettingArbitrageStrategy._quote_decision_timestamp_ns(quote_b)
        if timestamp_a <= 0 or timestamp_b <= 0:
            return 0.0
        return abs(timestamp_a - timestamp_b) / NANOSECONDS_PER_SECOND

    @staticmethod
    def _quote_fetch_latency_secs(quote: QuoteTick | None) -> float:
        return BettingArbitrageStrategy.quote_fetch_latency_secs(quote)

    @staticmethod
    def quote_fetch_latency_secs(quote: QuoteTick | None) -> float:
        latency_ns = BettingArbitrageStrategy._quote_fetch_latency_measurement_ns(quote)
        if latency_ns is None:
            return 0.0
        return max(0.0, latency_ns / NANOSECONDS_PER_SECOND)

    @staticmethod
    def _quote_fetch_latency_measurement_ns(quote: QuoteTick | None) -> int | None:
        if quote is None or quote.ts_event <= 0 or quote.ts_init <= 0:
            return None
        venue = str(getattr(getattr(quote, "instrument_id", None), "venue", "") or "").upper()
        if venue == "POLYMARKET":
            return None
        return max(0, int(quote.ts_init) - int(quote.ts_event))

    @staticmethod
    def _quote_decision_timestamp_ns(quote: QuoteTick | None) -> int:
        if quote is None:
            return 0
        venue = str(getattr(getattr(quote, "instrument_id", None), "venue", "") or "").upper()
        if venue == "POLYMARKET" and quote.ts_init > 0:
            return int(quote.ts_init)
        return int(quote.ts_event or 0)

    @staticmethod
    def _quote_cycle_id(quote: QuoteTick) -> str:
        timestamp_ns = BettingArbitrageStrategy._quote_decision_timestamp_ns(quote)
        if timestamp_ns <= 0:
            return "unknown"
        return str(timestamp_ns // NANOSECONDS_PER_SECOND)

    def _quote_freshness_thresholds(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> QuoteFreshnessThresholds:
        return self.quote_freshness_thresholds(instrument_a, instrument_b)

    def quote_freshness_thresholds(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> QuoteFreshnessThresholds:
        profile = self._config.quote_freshness_profile
        if profile == "custom":
            return QuoteFreshnessThresholds(
                profile="custom",
                max_quote_age_secs=float(self._config.arbitrage_quote_stale_threshold_secs),
                max_pair_skew_secs=float(self._config.quote_max_pair_skew_secs or 5.0),
                max_fetch_latency_secs=float(self._config.quote_max_fetch_latency_secs or 10.0),
            )

        if profile == "pre_match" and not (
            self._is_live_market(instrument_a) or self._is_live_market(instrument_b)
        ):
            return QuoteFreshnessThresholds(
                profile="pre_match",
                max_quote_age_secs=30.0,
                max_pair_skew_secs=5.0,
                max_fetch_latency_secs=10.0,
            )

        live_a = self._live_venue_freshness_thresholds(instrument_a)
        live_b = self._live_venue_freshness_thresholds(instrument_b)
        return QuoteFreshnessThresholds(
            profile="live",
            max_quote_age_secs=min(live_a.max_quote_age_secs, live_b.max_quote_age_secs),
            max_pair_skew_secs=min(live_a.max_pair_skew_secs, live_b.max_pair_skew_secs),
            max_fetch_latency_secs=min(
                live_a.max_fetch_latency_secs,
                live_b.max_fetch_latency_secs,
            ),
        )

    @staticmethod
    def _live_venue_freshness_thresholds(
        instrument: CryptoBettingInstrument,
    ) -> QuoteFreshnessThresholds:
        venue = str(instrument.id.venue).upper()
        if venue == "CLOUDBET":
            return QuoteFreshnessThresholds(
                profile="live",
                max_quote_age_secs=3.0,
                max_pair_skew_secs=1.0,
                max_fetch_latency_secs=2.0,
            )
        if venue == "SXBET":
            return QuoteFreshnessThresholds(
                profile="live",
                max_quote_age_secs=5.0,
                max_pair_skew_secs=1.0,
                max_fetch_latency_secs=3.0,
            )
        return QuoteFreshnessThresholds(
            profile="live",
            max_quote_age_secs=5.0,
            max_pair_skew_secs=1.0,
            max_fetch_latency_secs=3.0,
        )

    @property
    def live_quote_age_slo_secs(self) -> float:
        """
        Maximum live quote age at decision time for runtime diagnostics.
        """
        return float(self._config.live_quote_age_slo_secs)

    def fee_adjusted_opportunity(self, opportunity: ArbitrageOpportunity) -> ArbitrageOpportunity:
        """
        Apply configured venue fees to an opportunity before strategy decisions.
        """
        if opportunity.fee_adjusted:
            return opportunity

        fee_a = fee_adjusted_odds(
            opportunity.odds_a,
            taker_fee_rate=self.venue_taker_fee_rate(opportunity.instrument_a),
            maker_rebate_rate=self.venue_maker_rebate_rate(opportunity.instrument_a),
            winning_profit_fee_rate=self.venue_winning_profit_fee_rate(opportunity.instrument_a),
        )
        fee_b = fee_adjusted_odds(
            opportunity.odds_b,
            taker_fee_rate=self.venue_taker_fee_rate(opportunity.instrument_b),
            maker_rebate_rate=self.venue_maker_rebate_rate(opportunity.instrument_b),
            winning_profit_fee_rate=self.venue_winning_profit_fee_rate(opportunity.instrument_b),
        )
        raw_probability_a = opportunity.raw_probability_a or (Decimal(1) / opportunity.odds_a)
        raw_probability_b = opportunity.raw_probability_b or (Decimal(1) / opportunity.odds_b)
        raw_total_probability = opportunity.raw_total_probability or (
            raw_probability_a + raw_probability_b
        )
        raw_profit_margin = opportunity.raw_profit_margin or (
            (Decimal(1) / raw_total_probability) - Decimal(1)
        )
        basket_rebate_rate = self.pair_basket_rebate_rate(
            opportunity.instrument_a,
            opportunity.instrument_b,
        )
        basket_boost_rate = self.pair_basket_boost_rate(
            opportunity.instrument_a,
            opportunity.instrument_b,
        )
        adjusted_basket = fee_adjusted_basket_margin(
            (fee_a.effective_probability, fee_b.effective_probability),
            raw_probabilities=(raw_probability_a, raw_probability_b),
            basket_rebate_rate=basket_rebate_rate,
            basket_boost_rate=basket_boost_rate,
        )
        adjusted_total_probability = adjusted_basket.effective_total_probability
        adjusted_profit_margin = adjusted_basket.effective_profit_margin
        return replace(
            opportunity,
            probability_a=fee_a.effective_probability,
            probability_b=fee_b.effective_probability,
            total_probability=adjusted_total_probability,
            profit_margin=adjusted_profit_margin,
            raw_probability_a=raw_probability_a,
            raw_probability_b=raw_probability_b,
            raw_total_probability=raw_total_probability,
            raw_profit_margin=raw_profit_margin,
            fee_adjusted=True,
            fee_drag=raw_profit_margin - adjusted_profit_margin,
            fee_adjusted_odds_a=fee_a.effective_odds,
            fee_adjusted_odds_b=fee_b.effective_odds,
            taker_fee_rate_a=fee_a.taker_fee_rate,
            taker_fee_rate_b=fee_b.taker_fee_rate,
            maker_rebate_rate_a=fee_a.maker_rebate_rate,
            maker_rebate_rate_b=fee_b.maker_rebate_rate,
            winning_profit_fee_rate_a=fee_a.winning_profit_fee_rate,
            winning_profit_fee_rate_b=fee_b.winning_profit_fee_rate,
            basket_rebate_rate=adjusted_basket.basket_rebate_rate,
            basket_boost_rate=adjusted_basket.basket_boost_rate,
        )

    def venue_taker_fee_rate(self, instrument: Instrument) -> Decimal:
        """
        Return the taker fee-rate parameter for an instrument venue or market.
        """
        return self._instrument_fee_rate(
            instrument,
            keys=("taker_fee_rate", "fee_rate", "polymarket_fee_rate", "market_fee_rate"),
            bps_keys=("taker_fee_rate_bps", "fee_rate_bps", "feeRateBps", "market_fee_rate_bps"),
            venue_rates=self._config.venue_taker_fee_rates,
        )

    def venue_maker_rebate_rate(self, instrument: Instrument) -> Decimal:
        """
        Return the maker rebate-rate parameter for an instrument venue or market.
        """
        return self._instrument_fee_rate(
            instrument,
            keys=("maker_rebate_rate", "rebate_rate", "polymarket_maker_rebate_rate"),
            bps_keys=("maker_rebate_rate_bps", "rebate_rate_bps", "makerRebateRateBps"),
            venue_rates=self._config.venue_maker_rebate_rates,
        )

    def venue_winning_profit_fee_rate(self, instrument: Instrument) -> Decimal:
        """
        Return the winning-profit commission for an instrument venue or market.
        """
        return self._instrument_fee_rate(
            instrument,
            keys=("winning_profit_fee_rate", "commission_rate", "profit_fee_rate"),
            bps_keys=(
                "winning_profit_fee_rate_bps",
                "commission_rate_bps",
                "profit_fee_rate_bps",
            ),
            venue_rates=self._config.venue_winning_profit_fee_rates,
        )

    def pair_basket_rebate_rate(
        self,
        instrument_a: Instrument,
        instrument_b: Instrument,
    ) -> Decimal:
        """
        Return the basket-level cashback/reward rate for a covered candidate.
        """
        return self._pair_basket_rate(
            instrument_a,
            instrument_b,
            keys=("basket_rebate_rate", "promo_rebate_rate", "reward_rebate_rate"),
            bps_keys=("basket_rebate_rate_bps", "promo_rebate_rate_bps", "reward_rebate_rate_bps"),
            venue_rates=self._config.venue_basket_rebate_rates,
        )

    def pair_basket_boost_rate(
        self,
        instrument_a: Instrument,
        instrument_b: Instrument,
    ) -> Decimal:
        """
        Return the basket-level return boost rate for a covered candidate.
        """
        return self._pair_basket_rate(
            instrument_a,
            instrument_b,
            keys=("basket_boost_rate", "odds_boost_rate", "reward_boost_rate"),
            bps_keys=("basket_boost_rate_bps", "odds_boost_rate_bps", "reward_boost_rate_bps"),
            venue_rates=self._config.venue_basket_boost_rates,
        )

    def fee_adjusted_coverage_basket(
        self,
        instruments: Sequence[Instrument],
        odds: Sequence[Decimal | float | str],
    ) -> FeeAdjustedCoverageBasket:
        """
        Apply fee and promotion policy to an N-leg semantic coverage set.

        Pairwise arbitrage still uses ``fee_adjusted_opportunity``. This helper
        gives coverage proofs and future hyperedge execution diagnostics the
        same fee/VIG treatment, including maker rebates and temporary basket
        rewards, without changing any execution-safety tier.

        """
        if len(instruments) != len(odds):
            msg = f"instruments and odds lengths must match: {len(instruments)} != {len(odds)}"
            raise ValueError(msg)
        if len(instruments) < 2:
            msg = "At least two instruments are required for a coverage basket"
            raise ValueError(msg)
        return fee_adjusted_coverage_basket(
            odds,
            taker_fee_rates=tuple(
                self.venue_taker_fee_rate(instrument) for instrument in instruments
            ),
            maker_rebate_rates=tuple(
                self.venue_maker_rebate_rate(instrument) for instrument in instruments
            ),
            winning_profit_fee_rates=tuple(
                self.venue_winning_profit_fee_rate(instrument) for instrument in instruments
            ),
            basket_rebate_rate=self.coverage_basket_rebate_rate(instruments),
            basket_boost_rate=self.coverage_basket_boost_rate(instruments),
            devig_method=self._config.devig_method,
        )

    def devigged_book(
        self,
        odds: Sequence[Decimal | float | str],
        *,
        method: str | None = None,
    ) -> DeviggedBook | None:
        """
        Return a no-vig probability view for diagnostics when enabled.

        This helper deliberately does not affect executable arbitrage decisions. It is a
        fair-value/reference layer used by runtime reports.

        """
        if not self._config.devig_enabled:
            return None
        return devig_probabilities(odds, method=method or self._config.devig_method)

    @property
    def devig_enabled(self) -> bool:
        return bool(self._config.devig_enabled)

    @property
    def value_diagnostics_enabled(self) -> bool:
        return bool(self._config.value_diagnostics_enabled)

    @property
    def value_execution_enabled(self) -> bool:
        return bool(self._config.value_execution_enabled)

    @property
    def min_value_edge(self) -> Decimal:
        return self._config.min_value_edge

    def coverage_basket_rebate_rate(self, instruments: Sequence[Instrument]) -> Decimal:
        """
        Return the strongest configured cashback/reward rate across a coverage set.
        """
        return self._coverage_basket_rate(
            instruments,
            keys=("basket_rebate_rate", "promo_rebate_rate", "reward_rebate_rate"),
            bps_keys=("basket_rebate_rate_bps", "promo_rebate_rate_bps", "reward_rebate_rate_bps"),
            venue_rates=self._config.venue_basket_rebate_rates,
        )

    def coverage_basket_boost_rate(self, instruments: Sequence[Instrument]) -> Decimal:
        """
        Return the strongest configured return boost rate across a coverage set.
        """
        return self._coverage_basket_rate(
            instruments,
            keys=("basket_boost_rate", "odds_boost_rate", "reward_boost_rate"),
            bps_keys=("basket_boost_rate_bps", "odds_boost_rate_bps", "reward_boost_rate_bps"),
            venue_rates=self._config.venue_basket_boost_rates,
        )

    @classmethod
    def _pair_basket_rate(
        cls,
        instrument_a: Instrument,
        instrument_b: Instrument,
        *,
        keys: tuple[str, ...],
        bps_keys: tuple[str, ...],
        venue_rates: dict[str, Decimal],
    ) -> Decimal:
        rates = (
            cls._instrument_fee_rate(
                instrument_a,
                keys=keys,
                bps_keys=bps_keys,
                venue_rates=venue_rates,
            ),
            cls._instrument_fee_rate(
                instrument_b,
                keys=keys,
                bps_keys=bps_keys,
                venue_rates=venue_rates,
            ),
        )
        return max(rates)

    @classmethod
    def _coverage_basket_rate(
        cls,
        instruments: Sequence[Instrument],
        *,
        keys: tuple[str, ...],
        bps_keys: tuple[str, ...],
        venue_rates: dict[str, Decimal],
    ) -> Decimal:
        if not instruments:
            return Decimal(0)
        return max(
            cls._instrument_fee_rate(
                instrument,
                keys=keys,
                bps_keys=bps_keys,
                venue_rates=venue_rates,
            )
            for instrument in instruments
        )

    @staticmethod
    def _instrument_fee_rate(
        instrument: Instrument,
        *,
        keys: tuple[str, ...],
        bps_keys: tuple[str, ...] = (),
        venue_rates: dict[str, Decimal],
    ) -> Decimal:
        """
        Return instrument-specific fee metadata before falling back to venue defaults.
        """
        info = getattr(instrument, "info", None) or {}
        if isinstance(info, dict):
            rate = BettingArbitrageStrategy._rate_from_mapping(info, keys=keys, bps_keys=bps_keys)
            if rate is not None:
                return rate
            sports_market = info.get("sports_market")
            if isinstance(sports_market, dict):
                rate = BettingArbitrageStrategy._rate_from_mapping(
                    sports_market,
                    keys=keys,
                    bps_keys=bps_keys,
                )
                if rate is not None:
                    return rate
        return venue_rates.get(str(instrument.id.venue).upper(), Decimal(0))

    @staticmethod
    def _rate_from_mapping(
        payload: dict[str, object],
        *,
        keys: tuple[str, ...],
        bps_keys: tuple[str, ...],
    ) -> Decimal | None:
        for key in keys:
            value = payload.get(key)
            if value is not None:
                return normalize_venue_fee_rates({"instrument": str(value)})["INSTRUMENT"]
        for key in bps_keys:
            value = payload.get(key)
            if value is not None:
                rate = Decimal(str(value)) / Decimal(10_000)
                return normalize_venue_fee_rates({"instrument": rate})["INSTRUMENT"]
        return None

    def _pair_has_configured_fee(
        self,
        instrument_a: Instrument,
        instrument_b: Instrument,
    ) -> bool:
        return any(
            (
                self.venue_taker_fee_rate(instrument_a),
                self.venue_taker_fee_rate(instrument_b),
                self.venue_maker_rebate_rate(instrument_a),
                self.venue_maker_rebate_rate(instrument_b),
                self.venue_winning_profit_fee_rate(instrument_a),
                self.venue_winning_profit_fee_rate(instrument_b),
                self.pair_basket_rebate_rate(instrument_a, instrument_b),
                self.pair_basket_boost_rate(instrument_a, instrument_b),
            ),
        )

    @staticmethod
    def _stake_pricing_odds(opportunity: ArbitrageOpportunity) -> tuple[Decimal, Decimal]:
        """
        Return fee-adjusted odds for sizing when available, otherwise raw odds.
        """
        return (
            opportunity.fee_adjusted_odds_a or opportunity.odds_a,
            opportunity.fee_adjusted_odds_b or opportunity.odds_b,
        )

    @staticmethod
    def _order_price_for_instrument(instrument: Instrument, odds: Decimal) -> Decimal:
        """
        Convert strategy decimal odds back to the venue's executable price domain.
        """
        if str(instrument.id.venue).upper() == "POLYMARKET" and odds > 1:
            return Decimal(1) / odds
        return odds

    @staticmethod
    def _is_trusted_same_venue_match_odds_pair(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> bool:
        if instrument_a.venue_name != instrument_b.venue_name:
            return False
        return MarketMatcher.is_trusted_same_venue_event_id_mismatch(instrument_a, instrument_b)

    @staticmethod
    def _matcher_suspect_reason(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> tuple[bool, str]:
        return BettingArbitrageStrategy.matcher_suspect_reason(instrument_a, instrument_b)

    @staticmethod
    def _semantic_fixture_suspect_reason(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> tuple[bool, str]:
        return BettingArbitrageStrategy.semantic_fixture_suspect_reason(
            instrument_a,
            instrument_b,
        )

    @staticmethod
    def matcher_suspect_reason(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> tuple[bool, str]:
        if instrument_a.venue_name == instrument_b.venue_name:
            if instrument_a.event_id != instrument_b.event_id:
                if BettingArbitrageStrategy._is_trusted_same_venue_match_odds_pair(
                    instrument_a,
                    instrument_b,
                ):
                    return False, "none"
                return True, "same_venue_event_id_mismatch"
        else:
            proof = DEFAULT_FIXTURE_IDENTITY_RESOLVER.resolve(instrument_a, instrument_b)
            if not proof.same_fixture:
                return True, proof.blocker_reason or "event_mismatch"
        if (
            instrument_a.market_name == instrument_b.market_name
            and instrument_a.params != instrument_b.params
        ):
            return True, "same_market_params_mismatch"
        return False, "none"

    @staticmethod
    def semantic_fixture_suspect_reason(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> tuple[bool, str]:
        if instrument_a.venue_name == instrument_b.venue_name:
            if instrument_a.event_id == instrument_b.event_id:
                return False, "none"
            if BettingArbitrageStrategy._is_trusted_same_venue_match_odds_pair(
                instrument_a,
                instrument_b,
            ):
                return False, "none"
            return True, "same_venue_event_id_mismatch"
        proof = DEFAULT_FIXTURE_IDENTITY_RESOLVER.resolve(instrument_a, instrument_b)
        if not proof.same_fixture:
            return True, proof.blocker_reason or "event_mismatch"
        return False, "none"

    @staticmethod
    # skipcq: PYL-R0911, PYL-R0913
    def _classify_arbitrage_candidate(
        *,
        stale: bool,
        fetch_latency_stale: bool,
        matcher_suspect: bool,
        suspect_reason: str,
        same_quote_cycle: bool,
        suggested_stake_a: Decimal,
        suggested_stake_b: Decimal,
        available_size_a: Decimal,
        available_size_b: Decimal,
    ) -> tuple[str, str]:
        if fetch_latency_stale:
            return "fetch_latency", "rest_fetch_latency"

        if stale:
            return "stale", "stale_quote"

        if matcher_suspect:
            if suspect_reason in {"same_venue_event_id_mismatch", "event_mismatch"}:
                return "event_mismatch", suspect_reason
            if suspect_reason == "same_market_params_mismatch":
                return "line_mismatch", suspect_reason
            return "needs_manual_review", suspect_reason

        if suggested_stake_a > available_size_a or suggested_stake_b > available_size_b:
            return "liquidity_insufficient", "top_of_book_size"

        if not same_quote_cycle:
            return "needs_manual_review", "cross_cycle_quotes"

        return "valid", "none"

    def _log_arbitrage_summary(self, *, force: bool = False) -> None:
        now_ns = self.clock.timestamp_ns()
        interval_ns = int(
            self._config.arbitrage_summary_interval_secs * NANOSECONDS_PER_SECOND,
        )
        if (
            not force
            and self._last_arbitrage_summary_at_ns
            and now_ns - self._last_arbitrage_summary_at_ns < interval_ns
        ):
            return

        self._last_arbitrage_summary_at_ns = now_ns
        self.log.info(
            "Arbitrage quality summary: "
            f"raw_detections={self._raw_arbitrage_detections} "
            f"valid_opportunities={self._opportunities_found} "
            f"unique_opportunities={len(self._seen_opportunity_pairs)} "
            f"duplicate_suppressions={self._duplicate_opportunities_suppressed} "
            f"stale_quote_suppressions={self._stale_quote_suppressions} "
            f"matcher_suspect_suppressions={self._matcher_suspect_suppressions} "
            f"liquidity_suppressions={self._liquidity_suppressions} "
            f"manual_review_suppressions={self._manual_review_suppressions} "
            f"executable_candidates={self._executable_candidates} "
            f"executed={self._opportunities_executed}",
        )

    def _handle_arbitrage_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        diagnostics: ArbitrageDiagnostics | None = None,
    ) -> None:
        """
        Handle an arbitrage opportunity.

        Parameters
        ----------
        opportunity : ArbitrageOpportunity
            The arbitrage opportunity.
        diagnostics : ArbitrageDiagnostics, optional
            Runtime classification details for the opportunity.

        """
        diagnostic_suffix = ""
        if diagnostics is not None:
            diagnostic_suffix = (
                f" | opportunity_id={diagnostics.opportunity_id} "
                f"match_type={diagnostics.match_type} "
                f"hedge_match_type={diagnostics.hedge_match_type} "
                f"confidence={diagnostics.hedge_confidence:.2f} "
                f"classification={diagnostics.classification} "
                f"classification_reason={diagnostics.classification_reason} "
                f"venue_a={diagnostics.venue_a} venue_b={diagnostics.venue_b} "
                f"event_id_a={diagnostics.event_id_a} event_id_b={diagnostics.event_id_b} "
                f"canonical_event_key_a={diagnostics.canonical_event_key_a!r} "
                f"canonical_event_key_b={diagnostics.canonical_event_key_b!r} "
                f"market_id_a={diagnostics.market_id_a} market_id_b={diagnostics.market_id_b} "
                f"market_a={diagnostics.market_name_a} market_b={diagnostics.market_name_b} "
                f"params_a={diagnostics.params_a!r} params_b={diagnostics.params_b!r} "
                f"outcome_a={diagnostics.outcome_a} outcome_b={diagnostics.outcome_b} "
                f"quote_ts_a={diagnostics.quote_ts_a} quote_ts_b={diagnostics.quote_ts_b} "
                f"quote_cycle_id_a={diagnostics.quote_cycle_id_a} "
                f"quote_cycle_id_b={diagnostics.quote_cycle_id_b} "
                f"quote_age_a_secs={diagnostics.quote_age_a_secs:.2f} "
                f"quote_age_b_secs={diagnostics.quote_age_b_secs:.2f} "
                f"quote_delta_secs={diagnostics.quote_delta_secs:.2f} "
                f"fetch_latency_a_secs={diagnostics.fetch_latency_a_secs:.2f} "
                f"fetch_latency_b_secs={diagnostics.fetch_latency_b_secs:.2f} "
                f"freshness_profile={diagnostics.freshness_profile} "
                f"same_quote_cycle={diagnostics.same_quote_cycle}"
                f" raw_profit_margin={diagnostics.raw_profit_margin} "
                f"fee_adjusted_profit_margin={diagnostics.fee_adjusted_profit_margin} "
                f"fee_drag={diagnostics.fee_drag} "
                f"{self._live_execution_fx_breakdown_text(diagnostics)} "
                f"basket_rebate_rate={diagnostics.basket_rebate_rate} "
                f"basket_boost_rate={diagnostics.basket_boost_rate}"
                f"{self._manual_execution_plan(diagnostics)}"
            )

        msg = (
            f"Arbitrage found: {opportunity.instrument_a.id.symbol} @ {opportunity.odds_a} vs "
            f"{opportunity.instrument_b.id.symbol} @ {opportunity.odds_b} | "
            f"Profit: {opportunity.profit_margin:.2%}"
            f"{diagnostic_suffix}"
        )
        self.log.info(msg)

        if self._config.auto_execute:
            self._execute_arbitrage(opportunity, diagnostics=diagnostics)

    def _execute_arbitrage(
        self,
        opportunity: ArbitrageOpportunity,
        diagnostics: ArbitrageDiagnostics | None = None,
    ) -> None:
        """
        Execute an arbitrage opportunity.

        Steps:
        1. Calculate optimal stakes
        2. Validate with risk engines (if available)
        3. Submit limit orders to both venues

        Parameters
        ----------
        opportunity : ArbitrageOpportunity
            The arbitrage opportunity to execute.
        diagnostics : ArbitrageDiagnostics, optional
            Final runtime quality diagnostics used by live risk gates.

        """
        if self._config.live_execution_armed:
            opportunity, refresh_reasons = self._live_execution_refresh_opportunity(opportunity)
            if refresh_reasons:
                self._record_live_execution_block(refresh_reasons)
                self.log.warning(
                    "Live arbitrage execution blocked before final quote check: "
                    f"reasons={','.join(refresh_reasons)} "
                    f"instrument_a={opportunity.instrument_a.id} "
                    f"instrument_b={opportunity.instrument_b.id}",
                )
                return
        opportunity = self.fee_adjusted_opportunity(opportunity)
        # Calculate optimal stakes
        stake_odds_a, stake_odds_b = self._stake_pricing_odds(opportunity)
        stake_a, stake_b, profit = calculate_arbitrage_stakes(
            odds_a=stake_odds_a,
            odds_b=stake_odds_b,
            total_stake=self._config.max_total_stake,
        )
        risk_reasons = self._live_execution_block_reasons_for(
            opportunity=opportunity,
            stake_a=stake_a,
            stake_b=stake_b,
            diagnostics=diagnostics,
        )
        if risk_reasons:
            self._record_live_execution_block(risk_reasons)
            self.log.warning(
                "Live arbitrage execution blocked: "
                f"reasons={','.join(risk_reasons)} "
                f"instrument_a={opportunity.instrument_a.id} "
                f"instrument_b={opportunity.instrument_b.id} "
                f"stake_a={stake_a} stake_b={stake_b}",
            )
            return

        msg = f"Executing arbitrage: stake_a={stake_a}, stake_b={stake_b}, profit={profit}"
        self.log.info(msg)

        # Create limit orders for both sides
        construction_started_ns = time.perf_counter_ns()
        # Order A (higher odds side)
        instrument_a = opportunity.instrument_a
        instrument_b = opportunity.instrument_b
        order_a = self.order_factory.limit(
            instrument_id=instrument_a.id,
            order_side=OrderSide.BUY,  # Betting is always "buying" the selection
            quantity=instrument_a.make_qty(float(stake_a)),
            price=instrument_a.make_price(
                float(self._order_price_for_instrument(instrument_a, opportunity.odds_a)),
            ),
            time_in_force=TimeInForce.GTC,
        )

        # Order B (lower odds side, hedge)
        order_b = self.order_factory.limit(
            instrument_id=instrument_b.id,
            order_side=OrderSide.BUY,
            quantity=instrument_b.make_qty(float(stake_b)),
            price=instrument_b.make_price(
                float(self._order_price_for_instrument(instrument_b, opportunity.odds_b)),
            ),
            time_in_force=TimeInForce.GTC,
        )
        self._record_latency_sample(
            self._order_construction_latency_ns,
            time.perf_counter_ns() - construction_started_ns,
        )

        # Submit both orders
        # Note: In practice, you'd want to submit these with minimal delay
        # and handle cases where one fills but the other doesn't
        submit_started_ns = time.perf_counter_ns()
        self._live_execution_attempts += 1
        submitted_count = 0
        for order in (order_a, order_b):
            venue = str(order.instrument_id.venue).upper()
            try:
                self.submit_order(order)
                submitted_count += 1
                self._live_execution_submissions_by_venue[venue] += 1
            except Exception as e:  # pragma: no cover - submit_order is normally non-throwing
                self._live_execution_halt_reason = "submit_order_exception"
                self._live_execution_block_reasons["submit_order_exception"] += 1
                self.log.error(f"Live order submission raised for {order.instrument_id}: {e}")
        self._record_latency_sample(
            self._order_submit_latency_ns,
            time.perf_counter_ns() - submit_started_ns,
        )

        if submitted_count == 1:
            self._live_execution_unhedged_exposures += 1
            self._live_execution_halt_reason = "partial_submit_unhedged_exposure"
        if submitted_count != 2:
            return

        self._opportunities_executed += 1
        self._live_execution_submissions += 1
        self._live_execution_notional_used += self._usd_equivalent_notional(
            opportunity,
            stake_a,
            stake_b,
        )

        order_ids = f"{order_a.client_order_id}, {order_b.client_order_id}"
        msg = f"Arbitrage orders submitted: {order_ids}"
        self.log.info(msg)

    def _live_execution_block_reasons_for(
        self,
        *,
        opportunity: ArbitrageOpportunity,
        stake_a: Decimal,
        stake_b: Decimal,
        diagnostics: ArbitrageDiagnostics | None,
    ) -> list[str]:
        reasons: list[str] = []
        reasons.extend(self._live_execution_arming_block_reasons())
        reasons.extend(self._live_execution_venue_mode_block_reasons(opportunity))
        reasons.extend(self._live_execution_cap_block_reasons(opportunity, stake_a, stake_b))
        reasons.extend(self._live_execution_currency_block_reasons(opportunity))
        reasons.extend(self._live_execution_semantic_block_reasons(opportunity))
        reasons.extend(self._live_execution_diagnostic_block_reasons(diagnostics))
        return sorted(set(reasons))

    def _live_execution_refresh_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> tuple[ArbitrageOpportunity, list[str]]:
        quote_a = self._latest_quotes.get(str(opportunity.instrument_a.id))
        quote_b = self._latest_quotes.get(str(opportunity.instrument_b.id))
        if quote_a is None or quote_b is None:
            return opportunity, ["missing_final_quote"]

        odds_a = self._quote_odds(quote_a)
        odds_b = self._quote_odds(quote_b)
        if odds_a is None or odds_b is None or odds_a <= 1 or odds_b <= 1:
            return opportunity, ["missing_final_executable_odds"]

        probability_a = Decimal(1) / odds_a
        probability_b = Decimal(1) / odds_b
        total_probability = probability_a + probability_b
        refreshed = replace(
            opportunity,
            probability_a=probability_a,
            probability_b=probability_b,
            total_probability=total_probability,
            profit_margin=(Decimal(1) / total_probability) - Decimal(1),
            odds_a=odds_a,
            odds_b=odds_b,
        )
        return refreshed, self._live_execution_final_quote_block_reasons(
            refreshed,
            quote_a,
            quote_b,
        )

    def _live_execution_final_quote_block_reasons(
        self,
        opportunity: ArbitrageOpportunity,
        quote_a: QuoteTick,
        quote_b: QuoteTick,
    ) -> list[str]:
        freshness = self._quote_freshness_thresholds(
            opportunity.instrument_a,
            opportunity.instrument_b,
        )
        now_ns = self.clock.timestamp_ns()
        quote_age_a_secs = self._quote_age_secs(now_ns, quote_a)
        quote_age_b_secs = self._quote_age_secs(now_ns, quote_b)
        quote_delta_secs = self._quote_pair_skew_secs(quote_a, quote_b)
        fetch_latency_a_secs = self._quote_fetch_latency_secs(quote_a)
        fetch_latency_b_secs = self._quote_fetch_latency_secs(quote_b)
        stake_odds_a, stake_odds_b = self._stake_pricing_odds(opportunity)
        stake_a, stake_b, _expected_profit = calculate_arbitrage_stakes(
            odds_a=stake_odds_a,
            odds_b=stake_odds_b,
            total_stake=self._config.max_total_stake,
        )

        reasons: list[str] = []
        # Fail closed on an undated quote: a missing/zero decision timestamp yields a
        # deceptive 0.0 age/skew, so the stale/cross-cycle checks below would pass an
        # arbitrarily stale quote straight to live submission. Block it explicitly.
        if (
            self._quote_decision_timestamp_ns(quote_a) <= 0
            or self._quote_decision_timestamp_ns(quote_b) <= 0
        ):
            reasons.append("final_quote_missing_timestamp")
        if (
            quote_age_a_secs > freshness.max_quote_age_secs
            or quote_age_b_secs > freshness.max_quote_age_secs
        ):
            reasons.append("final_quote_stale")
        if quote_delta_secs > freshness.max_pair_skew_secs:
            reasons.append("final_quote_cross_cycle")
        if (
            fetch_latency_a_secs > freshness.max_fetch_latency_secs
            or fetch_latency_b_secs > freshness.max_fetch_latency_secs
        ):
            reasons.append("final_fetch_latency")
        if stake_a > self._quote_available_size(quote_a) or stake_b > self._quote_available_size(
            quote_b,
        ):
            reasons.append("final_liquidity_insufficient")
        if opportunity.profit_margin < self._config.min_profit_margin:
            reasons.append("final_below_min_profit_margin")
        return reasons

    def _live_execution_arming_block_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self._config.live_execution_armed:
            reasons.append("manifest_not_live_armed")
        if not self._live_execution_env_armed():
            reasons.append("env_not_live_armed")
        if self._live_execution_kill_switch_active():
            reasons.append("kill_switch_active")
        if self._live_execution_halt_reason:
            reasons.append(f"halted:{self._live_execution_halt_reason}")
        return reasons

    def _live_execution_venue_mode_block_reasons(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> list[str]:
        mode = self._config.execution_venue_mode
        if mode == "cross_venue" and opportunity.is_same_venue:
            return ["cross_venue_execution_only"]
        if mode == "same_venue" and not opportunity.is_same_venue:
            return ["same_venue_execution_only"]
        return []

    def _candidate_matches_execution_venue_mode(self, opportunity: ArbitrageOpportunity) -> bool:
        return not self._live_execution_venue_mode_block_reasons(opportunity)

    def _node_pair_matches_execution_venue_mode(
        self,
        source_node: object | None,
        target_node: object | None,
    ) -> bool:
        mode = self._config.execution_venue_mode
        if mode == "all" or source_node is None or target_node is None:
            return True
        source_venue = self._node_venue_value(source_node)
        target_venue = self._node_venue_value(target_node)
        if not source_venue or not target_venue:
            return True
        is_same_venue = source_venue == target_venue
        return (mode == "same_venue" and is_same_venue) or (
            mode == "cross_venue" and not is_same_venue
        )

    def _live_execution_cap_block_reasons(
        self,
        opportunity: ArbitrageOpportunity,
        stake_a: Decimal,
        stake_b: Decimal,
    ) -> list[str]:
        reasons: list[str] = []
        conversion_a, conversion_b = self._live_execution_stake_conversions(
            opportunity,
            stake_a,
            stake_b,
        )
        conversion_blockers = [
            conversion.blocker_reason
            for conversion in (conversion_a, conversion_b)
            if conversion.blocker_reason
        ]
        reasons.extend(str(blocker) for blocker in conversion_blockers)
        leg_a = conversion_a.converted_amount or stake_a
        leg_b = conversion_b.converted_amount or stake_b
        if leg_a > self._config.max_leg_stake or leg_b > self._config.max_leg_stake:
            reasons.append("max_leg_stake_exceeded")
        if self._live_execution_notional_used + leg_a + leg_b > self._config.max_daily_notional:
            reasons.append("max_daily_notional_exceeded")
        if self._live_execution_realized_loss >= self._config.max_daily_loss:
            reasons.append("max_daily_loss_exceeded")
        if opportunity.profit_margin < self._config.min_profit_margin:
            reasons.append("below_min_profit_margin")
        return reasons

    def _live_execution_currency_block_reasons(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> list[str]:
        currency_a = self._instrument_currency_code(opportunity.instrument_a)
        currency_b = self._instrument_currency_code(opportunity.instrument_b)
        if not currency_a or not currency_b:
            return ["unknown_settlement_currency"]
        if "PLAY_" in currency_a or "PLAY_" in currency_b:
            return ["sandbox_currency_not_live_settlement"]
        policy = self._portfolio_currency_policy()
        stablecoins = policy.stablecoin_currencies
        if currency_a in stablecoins and currency_b in stablecoins:
            return []
        if opportunity.is_same_venue or self._config.allow_cross_currency_live_execution:
            return []
        if (
            policy.convert(Decimal(1), currency_a).is_available
            and policy.convert(Decimal(1), currency_b).is_available
        ):
            return []
        if currency_a != currency_b:
            return ["cross_currency_live_execution_blocked"]
        return []

    def _usd_equivalent_notional(
        self,
        opportunity: ArbitrageOpportunity,
        stake_a: Decimal,
        stake_b: Decimal,
    ) -> Decimal:
        conversion_a, conversion_b = self._live_execution_stake_conversions(
            opportunity,
            stake_a,
            stake_b,
        )
        if conversion_a.converted_amount is None or conversion_b.converted_amount is None:
            return stake_a + stake_b
        return conversion_a.converted_amount + conversion_b.converted_amount

    def _live_execution_stake_conversions(
        self,
        opportunity: ArbitrageOpportunity,
        stake_a: Decimal,
        stake_b: Decimal,
    ) -> tuple[FxConversion, FxConversion]:
        policy = self._portfolio_currency_policy()
        return (
            policy.convert(stake_a, self._instrument_currency_code(opportunity.instrument_a)),
            policy.convert(stake_b, self._instrument_currency_code(opportunity.instrument_b)),
        )

    def _portfolio_currency_policy(self) -> PortfolioCurrencyPolicy:
        return PortfolioCurrencyPolicy(
            base_currency=self._config.portfolio_base_currency,
            stablecoin_currencies=self._config.stablecoin_currencies,
            stablecoin_haircut_bps=self._config.stablecoin_haircut_bps,
            fx_quote_max_age_secs=self._config.fx_quote_max_age_secs,
            static_fx_rates=self._config.configured_fx_rates,
        )

    @staticmethod
    def _instrument_currency_code(instrument: Instrument) -> str:
        currency = getattr(instrument, "quote_currency", None) or getattr(
            instrument,
            "currency",
            None,
        )
        code = getattr(currency, "code", None)
        return str(code or currency or "").strip().upper()

    def _live_execution_semantic_block_reasons(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> list[str]:
        edge = self._opportunity_edge_for(opportunity.instrument_a, opportunity.instrument_b)
        if edge is None:
            return ["no_semantic_edge"]
        if not self._live_execution_semantic_policy_allows(opportunity, edge):
            return ["semantic_execution_policy_blocked"]
        return []

    @staticmethod
    def _live_execution_diagnostic_block_reasons(
        diagnostics: ArbitrageDiagnostics | None,
    ) -> list[str]:
        if diagnostics is None:
            return []
        reasons: list[str] = []
        if diagnostics.stale:
            reasons.append("stale_quote")
        if diagnostics.fetch_latency_stale:
            reasons.append("fetch_latency")
        if diagnostics.matcher_suspect:
            reasons.append(f"matcher_suspect:{diagnostics.suspect_reason}")
        if not diagnostics.same_quote_cycle:
            reasons.append("cross_cycle_quotes")
        if (
            diagnostics.suggested_stake_a > diagnostics.available_size_a
            or diagnostics.suggested_stake_b > diagnostics.available_size_b
        ):
            reasons.append("liquidity_insufficient")
        return reasons

    def _live_execution_semantic_policy_allows(
        self,
        opportunity: ArbitrageOpportunity,
        edge: object,
    ) -> bool:
        if bool(getattr(edge, "execution_safe", False)) and not opportunity.is_same_venue:
            return True
        if not opportunity.is_same_venue:
            return False
        if not self._config.allow_same_venue_live_execution:
            return False
        if not self._same_venue_runtime_identity_allows(opportunity):
            return False
        if not (
            bool(getattr(edge, "same_venue_execution_eligible", False))
            or bool(getattr(edge, "execution_safe", False))
        ):
            return False
        dangerous_caveats = {
            "unknown_settlement",
            "void_states_present",
            "partial_states_present",
            "push_states_present",
            "unresolved_provider_rule",
            "ambiguous_resolution",
            "price_correlation_not_proof",
        }
        caveats = {str(caveat) for caveat in getattr(edge, "caveats", ())}
        return not caveats.intersection(dangerous_caveats)

    @staticmethod
    def _same_venue_runtime_identity_allows(opportunity: ArbitrageOpportunity) -> bool:
        instrument_a = opportunity.instrument_a
        instrument_b = opportunity.instrument_b
        if str(instrument_a.id.venue).upper() != str(instrument_b.id.venue).upper():
            return False
        if str(instrument_a.event_id or "") != str(instrument_b.event_id or ""):
            return False
        return (
            str(instrument_a.sport_name or "").lower()
            == str(
                instrument_b.sport_name or "",
            ).lower()
        )

    def _opportunity_edge_for(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> object | None:
        edge_id = self._canonical_pair_id(instrument_a, instrument_b)
        return self._opportunity_graph.edges_by_id.get(edge_id)

    def _record_live_execution_block(self, reasons: Sequence[str]) -> None:
        self._live_execution_blocks += 1
        for reason in reasons:
            self._live_execution_block_reasons[reason] += 1

    @staticmethod
    def _live_execution_env_armed() -> bool:
        return os.getenv("BETTING_LIVE_EXECUTION_ARMED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "armed",
        }

    def _live_execution_kill_switch_active(self) -> bool:
        if os.getenv("BETTING_LIVE_EXECUTION_KILL_SWITCH", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "halt",
        }:
            return True
        path = self._config.live_execution_kill_switch_path
        return bool(path and os.path.exists(path))

    def on_order_filled(self, event: Event) -> None:
        """
        Handle order filled events.
        """
        self._record_order_lifecycle_event(event, "filled")
        msg = f"Order filled: {event}"
        self.log.info(msg)

    def on_order_accepted(self, event: Event) -> None:
        """
        Handle order accepted events.
        """
        self._record_order_lifecycle_event(event, "accepted")
        msg = f"Order accepted: {event}"
        self.log.info(msg)

    def on_order_submitted(self, event: Event) -> None:
        """
        Handle order submitted events.
        """
        self._record_order_lifecycle_event(event, "submitted")
        msg = f"Order submitted: {event}"
        self.log.info(msg)

    def on_order_rejected(self, event: Event) -> None:
        """
        Handle order rejected events.
        """
        self._record_order_lifecycle_event(event, "rejected")
        if self._config.live_execution_armed:
            self._live_execution_halt_reason = "order_rejected"
            self._live_execution_unhedged_exposures += 1
            self._live_execution_block_reasons["order_rejected"] += 1
        msg = f"Order rejected: {event}"
        self.log.warning(msg)

    def on_order_denied(self, event: Event) -> None:
        """
        Handle order denied events (e.g. a local RiskEngine denial).

        submit_order enqueues asynchronously and does not raise on a local denial, so a
        denied leg is otherwise swallowed by the base no-op while its sibling may be
        resting or filled — leaving naked directional exposure. Mirror the rejected path
        so a denial halts live execution and is counted as unhedged exposure.
        """
        self._record_order_lifecycle_event(event, "denied")
        if self._config.live_execution_armed:
            self._live_execution_halt_reason = "order_denied"
            self._live_execution_unhedged_exposures += 1
            self._live_execution_block_reasons["order_denied"] += 1
        msg = f"Order denied: {event}"
        self.log.warning(msg)

    def on_order_canceled(self, event: Event) -> None:
        """
        Handle order canceled events.

        A canceled leg (venue-side or operator) after its sibling filled is the same
        unhedged-exposure hazard as a rejection, so halt live execution and count it.
        """
        self._record_order_lifecycle_event(event, "canceled")
        if self._config.live_execution_armed:
            self._live_execution_halt_reason = "order_canceled"
            self._live_execution_unhedged_exposures += 1
            self._live_execution_block_reasons["order_canceled"] += 1
        msg = f"Order canceled: {event}"
        self.log.warning(msg)

    def _record_order_lifecycle_event(self, event: Event, lifecycle: str) -> None:
        instrument_id = getattr(event, "instrument_id", None)
        venue = str(getattr(instrument_id, "venue", "") or "UNKNOWN").upper()
        self._order_lifecycle_counts_by_venue.setdefault(venue, Counter())[lifecycle] += 1

    @staticmethod
    def _record_latency_sample(samples: list[int], elapsed_ns: int) -> None:
        samples.append(max(0, int(elapsed_ns)))
        if len(samples) > LATENCY_SAMPLE_LIMIT:
            del samples[: len(samples) - LATENCY_SAMPLE_LIMIT]

    @staticmethod
    def _record_venue_latency_sample(
        samples_by_venue: dict[str, list[int]],
        venue: str,
        elapsed_ns: int,
    ) -> None:
        key = str(venue or "UNKNOWN").upper()
        BettingArbitrageStrategy._record_latency_sample(
            samples_by_venue.setdefault(key, []),
            elapsed_ns,
        )

    @staticmethod
    def _latency_summary(samples: list[int]) -> dict[str, float | int]:
        if not samples:
            return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}
        ordered = sorted(samples)
        return {
            "count": len(ordered),
            "p50_ms": round(
                BettingArbitrageStrategy._latency_percentile_ms(ordered, 0.50),
                6,
            ),
            "p95_ms": round(
                BettingArbitrageStrategy._latency_percentile_ms(ordered, 0.95),
                6,
            ),
            "p99_ms": round(
                BettingArbitrageStrategy._latency_percentile_ms(ordered, 0.99),
                6,
            ),
            "max_ms": round(ordered[-1] / 1_000_000, 6),
        }

    @classmethod
    def _latency_summary_by_venue(
        cls,
        samples_by_venue: dict[str, list[int]],
    ) -> dict[str, dict[str, float | int]]:
        return {
            venue: cls._latency_summary(samples)
            for venue, samples in sorted(samples_by_venue.items())
        }

    @staticmethod
    def _latency_percentile_ms(ordered_samples: list[int], percentile: float) -> float:
        index = max(0, min(len(ordered_samples) - 1, int((len(ordered_samples) - 1) * percentile)))
        return ordered_samples[index] / 1_000_000

    def get_stats(self) -> dict:
        """
        Get strategy statistics.
        """
        quote_subscription_counts = dict(sorted(self._quote_subscription_counts_by_venue().items()))
        semantic_quote_limits = dict(
            sorted(self._config.semantic_quote_subscription_limit_by_venue.items()),
        )
        event_to_strategy = self._latency_summary_by_venue(
            self._quote_event_to_strategy_latency_ns_by_venue,
        )
        publish_to_strategy = self._latency_summary_by_venue(
            self._quote_publish_to_strategy_latency_ns_by_venue,
        )
        fetch_latency = self._latency_summary_by_venue(self._quote_fetch_latency_ns_by_venue)
        return {
            "subscribed_instruments": len(self._subscribed_instruments),
            "quote_subscribed_instruments": len(self._quote_subscribed_instrument_ids),
            "quote_subscription_counts_by_venue": quote_subscription_counts,
            "semantic_quote_subscription_limit_by_venue": semantic_quote_limits,
            "semantic_quote_subscription_limit_exceeded_by_venue": {
                venue: max(quote_subscription_counts.get(venue, 0) - limit, 0)
                for venue, limit in semantic_quote_limits.items()
                if quote_subscription_counts.get(venue, 0) > limit
            },
            "venue_taker_fee_rates": {
                venue: str(rate)
                for venue, rate in sorted(self._config.venue_taker_fee_rates.items())
            },
            "venue_maker_rebate_rates": {
                venue: str(rate)
                for venue, rate in sorted(self._config.venue_maker_rebate_rates.items())
            },
            "venue_winning_profit_fee_rates": {
                venue: str(rate)
                for venue, rate in sorted(self._config.venue_winning_profit_fee_rates.items())
            },
            "venue_basket_rebate_rates": {
                venue: str(rate)
                for venue, rate in sorted(self._config.venue_basket_rebate_rates.items())
            },
            "venue_basket_boost_rates": {
                venue: str(rate)
                for venue, rate in sorted(self._config.venue_basket_boost_rates.items())
            },
            "devig_enabled": self._config.devig_enabled,
            "devig_method": self._config.devig_method,
            "devig_reference_venues": (
                sorted(self._config.devig_reference_venues)
                if self._config.devig_reference_venues
                else []
            ),
            "value_diagnostics_enabled": self._config.value_diagnostics_enabled,
            "value_execution_enabled": self._config.value_execution_enabled,
            "min_value_edge": str(self._config.min_value_edge),
            "max_resolution_horizon_hours": self._config.max_resolution_horizon_hours,
            "execution_venue_mode": self._config.execution_venue_mode,
            "portfolio_base_currency": self._config.portfolio_base_currency,
            "stablecoin_currencies": sorted(self._config.stablecoin_currencies),
            "stablecoin_haircut_bps": self._config.stablecoin_haircut_bps,
            "fx_quote_max_age_secs": self._config.fx_quote_max_age_secs,
            "configured_fx_rate_pairs": sorted(self._config.configured_fx_rates),
            "fx_policy": {
                "baseCurrency": self._config.portfolio_base_currency,
                "stablecoinCurrencies": sorted(self._config.stablecoin_currencies),
                "stablecoinHaircutBps": self._config.stablecoin_haircut_bps,
                "maxAgeSeconds": self._config.fx_quote_max_age_secs,
                "sourcePriority": ["hyperliquid", "pyth_hermes", "binance", "ecb_reference"],
                "configuredFxRatePairs": sorted(self._config.configured_fx_rates),
            },
            "opportunity_graph_nodes": self._opportunity_graph.node_count,
            "opportunity_graph_edges": self._opportunity_graph.edge_count,
            "opportunity_graph_quote_states": self._opportunity_graph.quote_state_count,
            "opportunity_graph_connected_nodes": self._opportunity_graph.connected_node_count,
            "opportunity_graph_rust_enabled": int(self._opportunity_graph.graph_engine == "rust"),
            "opportunity_graph_topology_source": self._opportunity_graph.topology_source,
            "opportunity_graph_semantic_template_count": (
                self._opportunity_graph.semantic_template_count
            ),
            "opportunity_graph_coverage_proof_count": self._opportunity_graph.coverage_proof_count,
            "opportunity_graph_coverage_hyperedge_count": (
                self._opportunity_graph.coverage_hyperedge_count
            ),
            "opportunity_graph_coverage_summary": (
                self._opportunity_graph.semantic_coverage_summary()
            ),
            "opportunities_found": self._opportunities_found,
            "opportunities_executed": self._opportunities_executed,
            "raw_arbitrage_detections": self._raw_arbitrage_detections,
            "unique_opportunity_pairs": len(self._seen_opportunity_pairs),
            "active_opportunity_pairs": len(self._active_opportunity_pairs),
            "duplicate_suppression_cooldown_secs": (
                self._config.duplicate_suppression_cooldown_secs
            ),
            "duplicate_opportunities_suppressed": self._duplicate_opportunities_suppressed,
            "stale_quote_suppressions": self._stale_quote_suppressions,
            "matcher_suspect_suppressions": self._matcher_suspect_suppressions,
            "liquidity_suppressions": self._liquidity_suppressions,
            "manual_review_suppressions": self._manual_review_suppressions,
            "executable_candidates": self._executable_candidates,
            "live_execution": {
                "auto_execute": self._config.auto_execute,
                "manifest_armed": self._config.live_execution_armed,
                "env_armed": self._live_execution_env_armed(),
                "kill_switch_active": self._live_execution_kill_switch_active(),
                "halt_reason": self._live_execution_halt_reason,
                "allow_same_venue_live_execution": (self._config.allow_same_venue_live_execution),
                "allow_cross_currency_live_execution": (
                    self._config.allow_cross_currency_live_execution
                ),
                "execution_venue_mode": self._config.execution_venue_mode,
                "portfolio_base_currency": self._config.portfolio_base_currency,
                "stablecoin_currencies": sorted(self._config.stablecoin_currencies),
                "stablecoin_haircut_bps": self._config.stablecoin_haircut_bps,
                "fx_quote_max_age_secs": self._config.fx_quote_max_age_secs,
                "max_leg_stake": str(self._config.max_leg_stake),
                "max_daily_notional": str(self._config.max_daily_notional),
                "max_daily_loss": str(self._config.max_daily_loss),
                "notional_used": str(self._live_execution_notional_used),
                "realized_loss": str(self._live_execution_realized_loss),
                "attempts": self._live_execution_attempts,
                "blocks": self._live_execution_blocks,
                "block_reasons": dict(sorted(self._live_execution_block_reasons.items())),
                "submissions": self._live_execution_submissions,
                "submissions_by_venue": dict(
                    sorted(self._live_execution_submissions_by_venue.items()),
                ),
                "unhedged_exposures": self._live_execution_unhedged_exposures,
                "order_lifecycle_counts_by_venue": {
                    venue: dict(sorted(counts.items()))
                    for venue, counts in sorted(self._order_lifecycle_counts_by_venue.items())
                },
            },
            "instrument_refresh_requests": self._instrument_refresh_requests,
            "instrument_refresh_failures": self._instrument_refresh_failures,
            "instrument_refresh_added": self._instrument_refresh_added,
            "instrument_refresh_removed": self._instrument_refresh_removed,
            "instrument_refresh_delisted_removed": self._instrument_refresh_delisted_removed,
            "instrument_refresh_reconciles": self._instrument_refresh_reconciles,
            "instrument_refresh_graph_rebuilds": self._instrument_refresh_graph_rebuilds,
            "instrument_refresh_stale_triggers": self._instrument_refresh_stale_triggers,
            "quote_unsubscribe_requests": self._quote_unsubscribe_requests,
            "instrument_refresh_by_venue": self._instrument_refresh_by_venue_payload(),
            "provider_quote_poll_stats": self._provider_quote_poll_stats(),
            "latency_diagnostics": {
                "quote_event_to_strategy": self._latency_summary(
                    self._quote_event_to_strategy_latency_ns,
                ),
                "quote_publish_to_strategy": self._latency_summary(
                    self._quote_publish_to_strategy_latency_ns,
                ),
                "quote_fetch_latency": self._latency_summary(self._quote_fetch_latency_ns),
                "instrument_refresh_reconcile": self._latency_summary(
                    self._instrument_refresh_reconcile_latency_ns,
                ),
                "graph_scan": self._latency_summary(self._graph_scan_latency_ns),
                "candidate_decision": self._latency_summary(
                    self._candidate_decision_latency_ns,
                ),
                "order_construction": self._latency_summary(
                    self._order_construction_latency_ns,
                ),
                "order_submit": self._latency_summary(self._order_submit_latency_ns),
                "by_venue": {
                    venue: {
                        "quote_event_to_strategy": event_to_strategy.get(venue, {}),
                        "quote_publish_to_strategy": publish_to_strategy.get(venue, {}),
                        "quote_fetch_latency": fetch_latency.get(venue, {}),
                    }
                    for venue in sorted(
                        {
                            *event_to_strategy,
                            *publish_to_strategy,
                            *fetch_latency,
                        },
                    )
                },
            },
            "success_rate": (
                self._opportunities_executed / self._opportunities_found
                if self._opportunities_found > 0
                else 0
            ),
        }

    def _provider_quote_poll_stats(self) -> dict[str, dict[str, object]]:
        stats: dict[str, dict[str, object]] = {}
        for venue_value in sorted(self._config.enabled_venues):
            try:
                raw = self.cache.get(venue_quote_poll_stats_key(venue_value))
            except Exception as exc:
                self.log.debug(
                    f"Unable to read provider quote poll stats: venue={venue_value} error={exc}",
                )
                continue
            payload = decode_venue_quote_poll_stats(raw)
            if payload is None:
                continue
            stats[payload.venue] = {
                "updated_at_ns": payload.updated_at_ns,
                "cycle_id": payload.cycle_id,
                "source": payload.source,
                "subscribed_instrument_count": payload.subscribed_instrument_count,
                "market_count": payload.market_count,
                "quote_count": payload.quote_count,
                "request_count": payload.request_count,
                "event_request_count": payload.event_request_count,
                "line_request_count": payload.line_request_count,
                "pruned_subscription_count": payload.pruned_subscription_count,
                "refilled_subscription_count": payload.refilled_subscription_count,
                "order_count": payload.order_count,
                "empty_market_count": payload.empty_market_count,
                "one_sided_market_count": payload.one_sided_market_count,
                "two_sided_market_count": payload.two_sided_market_count,
                "concurrency": payload.concurrency,
                "backlog_count": payload.backlog_count,
                "cycle_elapsed_secs": round(payload.cycle_elapsed_secs, 6),
                "max_fetch_latency_secs": round(payload.max_fetch_latency_secs, 6),
                "fetch_latency_p50_secs": round(payload.fetch_latency_p50_secs, 6),
                "fetch_latency_p95_secs": round(payload.fetch_latency_p95_secs, 6),
                "fetch_latency_p99_secs": round(payload.fetch_latency_p99_secs, 6),
                "poll_interval_secs": round(payload.poll_interval_secs, 6),
                "poll_target_cycle_secs": round(payload.poll_target_cycle_secs, 6),
                "next_poll_sleep_secs": round(payload.next_poll_sleep_secs, 6),
                "min_concurrency": payload.min_concurrency,
                "max_concurrency": payload.max_concurrency,
                "adaptive_concurrency": payload.adaptive_concurrency,
                "quote_event_timestamp_source": payload.quote_event_timestamp_source,
                "quote_init_timestamp_source": payload.quote_init_timestamp_source,
                "failure_count": payload.failure_count,
                "rate_limit_count": payload.rate_limit_count,
                "backoff_secs": round(payload.backoff_secs, 6),
                "last_error": payload.last_error,
            }
        return stats

    def _instrument_refresh_by_venue_payload(self) -> dict[str, dict[str, int]]:
        venues = sorted(
            {
                *self._instrument_refresh_requests_by_venue.keys(),
                *self._instrument_refresh_failures_by_venue.keys(),
                *self._instrument_refresh_added_by_venue.keys(),
                *self._instrument_refresh_removed_by_venue.keys(),
                *self._instrument_refresh_delisted_removed_by_venue.keys(),
                *self._instrument_refresh_reconciles_by_venue.keys(),
                *self._instrument_refresh_graph_rebuilds_by_venue.keys(),
                *self._instrument_refresh_stale_triggers_by_venue.keys(),
                *self._quote_unsubscribe_requests_by_venue.keys(),
            },
        )
        return {
            venue: {
                "requests": self._instrument_refresh_requests_by_venue.get(venue, 0),
                "failures": self._instrument_refresh_failures_by_venue.get(venue, 0),
                "added": self._instrument_refresh_added_by_venue.get(venue, 0),
                "removed": self._instrument_refresh_removed_by_venue.get(venue, 0),
                "delisted_removed": self._instrument_refresh_delisted_removed_by_venue.get(
                    venue,
                    0,
                ),
                "reconciles": self._instrument_refresh_reconciles_by_venue.get(venue, 0),
                "graph_rebuilds": self._instrument_refresh_graph_rebuilds_by_venue.get(venue, 0),
                "stale_triggers": self._instrument_refresh_stale_triggers_by_venue.get(venue, 0),
                "quote_unsubscribe_requests": self._quote_unsubscribe_requests_by_venue.get(
                    venue,
                    0,
                ),
            }
            for venue in venues
        }
