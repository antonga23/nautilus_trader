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

from dataclasses import dataclass
from decimal import Decimal

import msgspec

from nautilus_trader.adapters.betting.common.odds import calculate_arbitrage_stakes
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.message import Event
from nautilus_trader.examples.strategies.opportunity_graph import FastCandidateSnapshot
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityCandidate
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.instruments.base import Instrument
from nautilus_trader.trading.strategy import Strategy


VALID_MARKET_TIMINGS = frozenset({"all", "pre_market", "live"})
DEFAULT_ENABLED_VENUES = frozenset({"CLOUDBET", "SXBET", "10BET"})
NANOSECONDS_PER_SECOND = 1_000_000_000


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
    same_quote_cycle: bool
    stale: bool
    matcher_suspect: bool
    suspect_reason: str
    suggested_stake_a: Decimal
    suggested_stake_b: Decimal
    expected_profit: Decimal
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
        opportunity_graph_engine = self.opportunity_graph_engine.strip().lower()

        if market_timing_filter not in VALID_MARKET_TIMINGS:
            msg = (
                f"Invalid market_timing_filter: {market_timing_filter}. "
                f"Must be one of {VALID_MARKET_TIMINGS}"
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

        msgspec.structs.force_setattr(self, "enabled_venues", enabled_venues)
        msgspec.structs.force_setattr(self, "sport_filter", normalized_sport_filter)
        msgspec.structs.force_setattr(self, "market_timing_filter", market_timing_filter)
        msgspec.structs.force_setattr(self, "opportunity_graph_engine", opportunity_graph_engine)
        msgspec.structs.force_setattr(self, "semantic_rule_cache_dir", semantic_rule_cache_dir)


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
        self._subscribed_instruments: set[CryptoBettingInstrument] = set()
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
        self._seen_opportunity_pairs: set[str] = set()
        self._active_opportunity_pairs: dict[str, OpportunityPairState] = {}
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
            f"summary_interval_secs={self._config.arbitrage_summary_interval_secs} "
            f"opportunity_graph_enabled={self._config.opportunity_graph_enabled} "
            f"opportunity_graph_engine={self._config.opportunity_graph_engine} "
            f"manual_instructions={self._config.opportunity_log_manual_instructions}"
        )
        self.log.info(msg)
        self._subscribe_cached_instruments()

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
        msg = f"Opportunities found: {self._opportunities_found}"
        self.log.info(msg)
        msg = f"Opportunities executed: {self._opportunities_executed}"
        self.log.info(msg)
        self._log_arbitrage_summary(force=True)

    def subscribe_instruments(self, instruments: list[CryptoBettingInstrument]) -> None:
        """
        Subscribe to instruments for arbitrage monitoring.

        Applies filtering by:
        - Enabled venues
        - Sport (if sport_filter specified)

        Parameters
        ----------
        instruments : list[CryptoBettingInstrument]
            Instruments to monitor.

        """
        subscribed_before = len(self._subscribed_instruments)
        for instrument in instruments:
            if not self._maybe_subscribe_instrument(instrument):
                continue
        if len(self._subscribed_instruments) != subscribed_before:
            self._log_graph_topology_summary()

    def on_instrument(self, instrument: Instrument) -> None:
        """
        Subscribe a newly seen betting instrument when it passes strategy filters.
        """
        if isinstance(instrument, CryptoBettingInstrument):
            self._maybe_subscribe_instrument(instrument)

    def _subscribe_cached_instruments(self) -> None:
        cached_instruments = [
            instrument
            for instrument in self.cache.instruments()
            if isinstance(instrument, CryptoBettingInstrument)
        ]
        if not cached_instruments:
            self.log.warning("No cached betting instruments available at strategy start")
            return
        self.subscribe_instruments(cached_instruments)

    def _maybe_subscribe_instrument(self, instrument: CryptoBettingInstrument) -> bool:
        # Venue filter
        if instrument.id.venue.value not in self._config.enabled_venues:
            return False

        # Sport/live filter
        if not self._should_process_instrument(instrument):
            return False

        if any(existing.id == instrument.id for existing in self._subscribed_instruments):
            return False

        self._subscribed_instruments.add(instrument)
        if self._config.opportunity_graph_enabled and self._config.graph_rebuild_on_new_instrument:
            self._opportunity_graph.add_instrument(instrument)
        self.subscribe_quote_ticks(instrument.id)
        self.log.info(f"Subscribed to {instrument.id}")
        return True

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

    def _should_process_instrument(self, instrument: CryptoBettingInstrument) -> bool:
        """
        Check if instrument should be processed based on filters.

        Parameters
        ----------
        instrument : CryptoBettingInstrument
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
    def _is_live_market(instrument: CryptoBettingInstrument) -> bool:
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

        bid_price = quote.bid_price.as_decimal()
        if str(quote.instrument_id.venue) == "SXBET" and bid_price > 0:
            return bid_price

        ask_price = quote.ask_price.as_decimal()
        if ask_price > 0:
            return ask_price

        if bid_price > 0:
            return bid_price

        return None

    @staticmethod
    def _quote_available_size(quote: QuoteTick | None) -> Decimal:
        if quote is None:
            return Decimal(0)

        bid_price = quote.bid_price.as_decimal()
        ask_price = quote.ask_price.as_decimal()
        bid_size = quote.bid_size.as_decimal()
        ask_size = quote.ask_size.as_decimal()

        if str(quote.instrument_id.venue) == "SXBET" and bid_price > 0:
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
        self._latest_quotes[str(tick.instrument_id)] = tick

        # Get instrument
        instrument = self.cache.instrument(tick.instrument_id)
        if not isinstance(instrument, CryptoBettingInstrument):
            return

        if self._config.opportunity_graph_enabled:
            self._handle_graph_quote_tick(tick, instrument)
            return

        self._handle_search_quote_tick(tick, instrument)

    def _handle_graph_quote_tick(
        self,
        tick: QuoteTick,
        instrument: CryptoBettingInstrument,
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

        quote_state, candidates = self._opportunity_graph.update_quote_and_evaluate(
            tick,
            odds=current_odds,
            received_ns=now_ns,
            min_profit_margin=self._config.min_profit_margin,
            now_ns=now_ns,
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
        fast_scan = self._opportunity_graph.update_quote_and_scan_fast(
            tick,
            odds=current_odds,
            received_ns=now_ns,
            min_profit_margin=self._config.min_profit_margin,
            now_ns=now_ns,
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
            if self._suppress_fast_snapshot_before_context(snapshot, now_ns):
                log_summary = True
                continue
            log_summary = self._handle_fast_actionable_snapshot(snapshot, now_ns) or log_summary
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

        if not self._config.auto_execute and not self._config.opportunity_log_manual_instructions:
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
                profit_margin_raw=profit_margin_raw,
                quote_ts_a=quote_ts_a,
                quote_ts_b=quote_ts_b,
                now_ns=now_ns,
            )
            return True

        source_node = self._opportunity_graph.nodes_by_id.get(source_node_id)
        target_node = self._opportunity_graph.nodes_by_id.get(target_node_id)
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
            last_opportunity_id=opportunity_id,
            last_seen_ns=now_ns,
        )
        return True

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
        quote_delta_secs = abs(int(quote_a.ts_event) - int(quote_b.ts_event)) / (
            NANOSECONDS_PER_SECOND
        )
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
    def _suppress_fast_candidate(
        self,
        *,
        inst_a: CryptoBettingInstrument,
        inst_b: CryptoBettingInstrument,
        hedge_type: str,
        hedge_confidence: float,
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

        if (
            quote_age_a_secs > self._config.arbitrage_quote_stale_threshold_secs
            or quote_age_b_secs > self._config.arbitrage_quote_stale_threshold_secs
        ):
            self._stale_quote_suppressions += 1
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
        candidates = [inst for inst in self._subscribed_instruments if inst.id != instrument.id]

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

            if opportunity and opportunity.profit_margin >= self._config.min_profit_margin:
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
        self._raw_arbitrage_detections += 1
        diagnostics = self._build_arbitrage_diagnostics(
            opportunity=candidate.opportunity,
            hedge_match_type=candidate.edge.hedge_type,
            hedge_confidence=candidate.edge.confidence,
            quote_a=candidate.quote_a.quote,
            quote_b=candidate.quote_b.quote,
            now_ns=now_ns,
        )
        if self._suppress_arbitrage_candidate(diagnostics):
            self._log_arbitrage_summary()
            return

        self._record_opportunity_pair(
            diagnostics.canonical_pair_id,
            diagnostics.opportunity_id,
            now_ns,
        )
        self._opportunities_found += 1
        self._executable_candidates += 1
        self._handle_arbitrage_opportunity(candidate.opportunity, diagnostics)
        self._log_arbitrage_summary()

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

    @staticmethod
    def _fast_arbitrage_opportunity(
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
        return ArbitrageOpportunity(
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
        )

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
        stale = (
            quote_age_a_secs > self._config.arbitrage_quote_stale_threshold_secs
            or quote_age_b_secs > self._config.arbitrage_quote_stale_threshold_secs
        )
        suggested_stake_a, suggested_stake_b, expected_profit = calculate_arbitrage_stakes(
            odds_a=opportunity.odds_a,
            odds_b=opportunity.odds_b,
            total_stake=self._config.max_total_stake,
        )
        available_size_a = self._quote_available_size(quote_a)
        available_size_b = self._quote_available_size(quote_b)
        classification, classification_reason = self._classify_arbitrage_candidate(
            stale=stale,
            matcher_suspect=False,
            suspect_reason="",
            same_quote_cycle=quote_delta_secs <= 2.0,
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
            same_quote_cycle=quote_delta_secs <= 2.0,
            stale=stale,
            matcher_suspect=False,
            suspect_reason="",
            suggested_stake_a=suggested_stake_a,
            suggested_stake_b=suggested_stake_b,
            expected_profit=expected_profit,
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

        if diagnostics.stale:
            self._stale_quote_suppressions += 1
            self.log.info(
                "Arbitrage candidate suppressed: "
                f"reason=stale_quote classification={diagnostics.classification} "
                f"classification_reason={diagnostics.classification_reason} "
                f"opportunity_id={diagnostics.opportunity_id} "
                f"quote_age_a_secs={diagnostics.quote_age_a_secs:.2f} "
                f"quote_age_b_secs={diagnostics.quote_age_b_secs:.2f} "
                f"quote_delta_secs={diagnostics.quote_delta_secs:.2f}"
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
            f"market_id={diagnostics.market_id_a} "
            f"available_size={diagnostics.available_size_a} "
            f"quote_cycle_id={diagnostics.quote_cycle_id_a} "
            f"quote_age_secs={diagnostics.quote_age_a_secs:.2f}; "
            "Instrument B: "
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
            f"quote_age_secs={diagnostics.quote_age_b_secs:.2f}"
        )

    def _manual_execution_plan(self, *args) -> str:
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
            f"quote_age_secs={diagnostics.quote_age_a_secs:.2f}; "
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
            f"quote_age_secs={diagnostics.quote_age_b_secs:.2f}; "
            f"expected_profit={expected_profit} "
            f"max_total_stake={self._config.max_total_stake}"
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
        quote_delta_secs = abs(int(quote_a.ts_event) - int(quote_b.ts_event)) / (
            NANOSECONDS_PER_SECOND
        )
        stale = (
            quote_age_a_secs > self._config.arbitrage_quote_stale_threshold_secs
            or quote_age_b_secs > self._config.arbitrage_quote_stale_threshold_secs
        )
        matcher_suspect, suspect_reason = self._matcher_suspect_reason(inst_a, inst_b)
        suggested_stake_a, suggested_stake_b, expected_profit = calculate_arbitrage_stakes(
            odds_a=opportunity.odds_a,
            odds_b=opportunity.odds_b,
            total_stake=self._config.max_total_stake,
        )
        available_size_a = self._quote_available_size(quote_a)
        available_size_b = self._quote_available_size(quote_b)
        classification, classification_reason = self._classify_arbitrage_candidate(
            stale=stale,
            matcher_suspect=matcher_suspect,
            suspect_reason=suspect_reason,
            same_quote_cycle=quote_delta_secs <= 2.0,
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
            quote_ts_a=int(quote_a.ts_event),
            quote_ts_b=int(quote_b.ts_event),
            quote_cycle_id_a=self._quote_cycle_id(quote_a),
            quote_cycle_id_b=self._quote_cycle_id(quote_b),
            quote_age_a_secs=quote_age_a_secs,
            quote_age_b_secs=quote_age_b_secs,
            quote_delta_secs=quote_delta_secs,
            same_quote_cycle=quote_delta_secs <= 2.0,
            stale=stale,
            matcher_suspect=matcher_suspect,
            suspect_reason=suspect_reason,
            suggested_stake_a=suggested_stake_a,
            suggested_stake_b=suggested_stake_b,
            expected_profit=expected_profit,
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
        if quote.ts_event <= 0:
            return 0.0
        return max(0.0, (now_ns - int(quote.ts_event)) / NANOSECONDS_PER_SECOND)

    @staticmethod
    def _quote_cycle_id(quote: QuoteTick) -> str:
        if quote.ts_event <= 0:
            return "unknown"
        return str(int(quote.ts_event) // NANOSECONDS_PER_SECOND)

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
        if not instrument_a.matches_event(instrument_b):
            return True, "event_mismatch"
        if (
            instrument_a.market_name == instrument_b.market_name
            and instrument_a.params != instrument_b.params
        ):
            return True, "same_market_params_mismatch"
        if instrument_a.venue_name == instrument_b.venue_name and (
            instrument_a.event_id != instrument_b.event_id
        ):
            if BettingArbitrageStrategy._is_trusted_same_venue_match_odds_pair(
                instrument_a,
                instrument_b,
            ):
                return False, "none"
            return True, "same_venue_event_id_mismatch"
        return False, "none"

    @staticmethod
    # skipcq: PYL-R0911, PYL-R0913
    def _classify_arbitrage_candidate(
        *,
        stale: bool,
        matcher_suspect: bool,
        suspect_reason: str,
        same_quote_cycle: bool,
        suggested_stake_a: Decimal,
        suggested_stake_b: Decimal,
        available_size_a: Decimal,
        available_size_b: Decimal,
    ) -> tuple[str, str]:
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
                f"same_quote_cycle={diagnostics.same_quote_cycle}"
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
            self._execute_arbitrage(opportunity)

    def _execute_arbitrage(
        self,
        opportunity: ArbitrageOpportunity,
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

        """
        # Calculate optimal stakes
        stake_a, stake_b, profit = calculate_arbitrage_stakes(
            odds_a=opportunity.odds_a,
            odds_b=opportunity.odds_b,
            total_stake=self._config.max_total_stake,
        )

        msg = f"Executing arbitrage: stake_a={stake_a}, stake_b={stake_b}, profit={profit}"
        self.log.info(msg)

        # Create limit orders for both sides
        # Order A (higher odds side)
        instrument_a = opportunity.instrument_a
        instrument_b = opportunity.instrument_b
        order_a = self.order_factory.limit(
            instrument_id=instrument_a.id,
            order_side=OrderSide.BUY,  # Betting is always "buying" the selection
            quantity=instrument_a.make_qty(float(stake_a)),
            price=instrument_a.make_price(float(opportunity.odds_a)),
            time_in_force=TimeInForce.GTC,
        )

        # Order B (lower odds side, hedge)
        order_b = self.order_factory.limit(
            instrument_id=instrument_b.id,
            order_side=OrderSide.BUY,
            quantity=instrument_b.make_qty(float(stake_b)),
            price=instrument_b.make_price(float(opportunity.odds_b)),
            time_in_force=TimeInForce.GTC,
        )

        # Submit both orders
        # Note: In practice, you'd want to submit these with minimal delay
        # and handle cases where one fills but the other doesn't
        self.submit_order(order_a)
        self.submit_order(order_b)

        self._opportunities_executed += 1

        order_ids = f"{order_a.client_order_id}, {order_b.client_order_id}"
        msg = f"Arbitrage orders submitted: {order_ids}"
        self.log.info(msg)

    def on_order_filled(self, event: Event) -> None:
        """
        Handle order filled events.
        """
        msg = f"Order filled: {event}"
        self.log.info(msg)

    def on_order_rejected(self, event: Event) -> None:
        """
        Handle order rejected events.
        """
        msg = f"Order rejected: {event}"
        self.log.warning(msg)

    def get_stats(self) -> dict:
        """
        Get strategy statistics.
        """
        return {
            "subscribed_instruments": len(self._subscribed_instruments),
            "opportunity_graph_nodes": self._opportunity_graph.node_count,
            "opportunity_graph_edges": self._opportunity_graph.edge_count,
            "opportunity_graph_quote_states": self._opportunity_graph.quote_state_count,
            "opportunity_graph_connected_nodes": self._opportunity_graph.connected_node_count,
            "opportunity_graph_rust_enabled": int(self._opportunity_graph.graph_engine == "rust"),
            "opportunity_graph_topology_source": self._opportunity_graph.topology_source,
            "opportunity_graph_semantic_template_count": (
                self._opportunity_graph.semantic_template_count
            ),
            "opportunities_found": self._opportunities_found,
            "opportunities_executed": self._opportunities_executed,
            "raw_arbitrage_detections": self._raw_arbitrage_detections,
            "duplicate_opportunities_suppressed": self._duplicate_opportunities_suppressed,
            "stale_quote_suppressions": self._stale_quote_suppressions,
            "matcher_suspect_suppressions": self._matcher_suspect_suppressions,
            "liquidity_suppressions": self._liquidity_suppressions,
            "manual_review_suppressions": self._manual_review_suppressions,
            "executable_candidates": self._executable_candidates,
            "success_rate": (
                self._opportunities_executed / self._opportunities_found
                if self._opportunities_found > 0
                else 0
            ),
        }
