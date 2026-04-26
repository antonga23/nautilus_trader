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
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.message import Event
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
class ArbitrageDiagnostics:
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
    market_id_a: str
    market_id_b: str
    market_name_a: str
    market_name_b: str
    outcome_a: str
    outcome_b: str
    venue_a: str
    venue_b: str
    odds_a: Decimal
    odds_b: Decimal
    quote_ts_a: int
    quote_ts_b: int
    quote_age_a_secs: float
    quote_age_b_secs: float
    quote_delta_secs: float
    same_quote_cycle: bool
    stale: bool
    matcher_suspect: bool
    suspect_reason: str


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
    arbitrage_summary_interval_secs : float, default 60.0
        Minimum interval between arbitrage quality summary log lines.
    opportunity_graph_enabled : bool, default True
        Use the persistent opportunity graph instead of quote-time hedge discovery.
    opportunity_log_manual_instructions : bool, default True
        Include manual execution fields in arbitrage logs.
    graph_rebuild_on_new_instrument : bool, default True
        Add newly observed instruments to the opportunity graph incrementally.

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
    arbitrage_summary_interval_secs: float = 60.0
    opportunity_graph_enabled: bool = True
    opportunity_log_manual_instructions: bool = True
    graph_rebuild_on_new_instrument: bool = True

    def __post_init__(self) -> None:
        enabled_venues = frozenset(self.enabled_venues or DEFAULT_ENABLED_VENUES)
        normalized_sport_filter = self.sport_filter.strip().lower() if self.sport_filter else None
        market_timing_filter = self.market_timing_filter if not self.exclude_live else "pre_market"

        if market_timing_filter not in VALID_MARKET_TIMINGS:
            msg = (
                f"Invalid market_timing_filter: {market_timing_filter}. "
                f"Must be one of {VALID_MARKET_TIMINGS}"
            )
            raise ValueError(msg)

        msgspec.structs.force_setattr(self, "enabled_venues", enabled_venues)
        msgspec.structs.force_setattr(self, "sport_filter", normalized_sport_filter)
        msgspec.structs.force_setattr(self, "market_timing_filter", market_timing_filter)


class BettingArbitrageStrategy(Strategy):
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
        super().__init__(config)
        self._config = config

        # Market matcher for finding arbitrage
        self._matcher = MarketMatcher()
        self._opportunity_graph = OpportunityGraph(self._matcher)

        # Tracking
        self._subscribed_instruments: set[CryptoBettingInstrument] = set()
        self._latest_quotes: dict[str, QuoteTick] = {}
        self._opportunities_found = 0
        self._opportunities_executed = 0
        self._raw_arbitrage_detections = 0
        self._duplicate_opportunities_suppressed = 0
        self._stale_quote_suppressions = 0
        self._matcher_suspect_suppressions = 0
        self._executable_candidates = 0
        self._seen_opportunity_pairs: set[str] = set()
        self._last_arbitrage_summary_at_ns = 0

    def on_start(self) -> None:
        """
        Actions to perform on strategy start.
        """
        self.log.info("BettingArbitrageStrategy starting...")
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
            f"manual_instructions={self._config.opportunity_log_manual_instructions}"
        )
        self.log.info(msg)
        self._subscribe_cached_instruments()

    def on_stop(self) -> None:
        """
        Actions to perform on strategy stop.
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
            f"quote_states={graph_stats['quote_states']}",
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

        quote_state = self._opportunity_graph.update_quote(
            tick,
            odds=current_odds,
            received_ns=now_ns,
        )
        if quote_state is None:
            return

        candidates = self._opportunity_graph.evaluate_updated_node(
            str(tick.instrument_id),
            min_profit_margin=self._config.min_profit_margin,
            now_ns=now_ns,
        )
        if candidates:
            self.log.debug(
                "Opportunity graph quote evaluation: "
                f"instrument_id={tick.instrument_id} "
                f"connected_edges={self._opportunity_graph.connected_edge_count(str(tick.instrument_id))} "
                f"candidates={len(candidates)}",
            )

        for candidate in candidates:
            self._handle_opportunity_candidate(candidate, now_ns)

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
                diagnostics = self._build_arbitrage_diagnostics(
                    opportunity=opportunity,
                    hedge_match_type=hedge.match_type,
                    hedge_confidence=hedge.confidence,
                    quote_a=tick,
                    quote_b=hedge_quote,
                    now_ns=self.clock.timestamp_ns(),
                )
                if self._suppress_arbitrage_candidate(diagnostics):
                    self._log_arbitrage_summary()
                    continue

                self._seen_opportunity_pairs.add(diagnostics.canonical_pair_id)
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

        self._seen_opportunity_pairs.add(diagnostics.canonical_pair_id)
        self._opportunities_found += 1
        self._executable_candidates += 1
        self._handle_arbitrage_opportunity(candidate.opportunity, diagnostics)
        self._log_arbitrage_summary()

    def _suppress_arbitrage_candidate(self, diagnostics: ArbitrageDiagnostics) -> bool:
        if diagnostics.canonical_pair_id in self._seen_opportunity_pairs:
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
                f"reason=stale_quote opportunity_id={diagnostics.opportunity_id} "
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
                f"reason=matcher_suspect suspect_reason={diagnostics.suspect_reason} "
                f"opportunity_id={diagnostics.opportunity_id} "
                f"event_id_a={diagnostics.event_id_a} event_id_b={diagnostics.event_id_b} "
                f"market_id_a={diagnostics.market_id_a} market_id_b={diagnostics.market_id_b} "
                f"match_type={diagnostics.match_type} hedge_match_type={diagnostics.hedge_match_type} "
                f"confidence={diagnostics.hedge_confidence:.2f}"
                f"{self._diagnostics_instrument_fields(diagnostics)}",
            )
            return True

        return False

    def _diagnostics_instrument_fields(self, diagnostics: ArbitrageDiagnostics) -> str:
        if not self._config.opportunity_log_manual_instructions:
            return ""

        return (
            " | Instrument A: "
            f"instrument_id={diagnostics.instrument_id_a} "
            f"venue={diagnostics.venue_a} "
            f"event={diagnostics.event_name_a!r} "
            f"market={diagnostics.market_name_a!r} "
            f"selection={diagnostics.outcome_a!r} "
            f"odds={diagnostics.odds_a} "
            f"market_id={diagnostics.market_id_a} "
            f"quote_age_secs={diagnostics.quote_age_a_secs:.2f}; "
            "Instrument B: "
            f"instrument_id={diagnostics.instrument_id_b} "
            f"venue={diagnostics.venue_b} "
            f"event={diagnostics.event_name_b!r} "
            f"market={diagnostics.market_name_b!r} "
            f"selection={diagnostics.outcome_b!r} "
            f"odds={diagnostics.odds_b} "
            f"market_id={diagnostics.market_id_b} "
            f"quote_age_secs={diagnostics.quote_age_b_secs:.2f}"
        )

    def _manual_execution_plan(
        self,
        opportunity: ArbitrageOpportunity,
        diagnostics: ArbitrageDiagnostics,
    ) -> str:
        if not self._config.opportunity_log_manual_instructions:
            return ""

        stake_a, stake_b, expected_profit = calculate_arbitrage_stakes(
            odds_a=opportunity.odds_a,
            odds_b=opportunity.odds_b,
            total_stake=self._config.max_total_stake,
        )
        return (
            " | Manual execution plan: "
            f"execution_enabled={self._config.auto_execute} "
            "Instrument A: "
            f"bet={stake_a} "
            f"instrument_id={diagnostics.instrument_id_a} "
            f"venue={diagnostics.venue_a} "
            f"event={diagnostics.event_name_a!r} "
            f"market={diagnostics.market_name_a!r} "
            f"selection={diagnostics.outcome_a!r} "
            f"odds={diagnostics.odds_a} "
            f"market_id={diagnostics.market_id_a}; "
            "Instrument B: "
            f"bet={stake_b} "
            f"instrument_id={diagnostics.instrument_id_b} "
            f"venue={diagnostics.venue_b} "
            f"event={diagnostics.event_name_b!r} "
            f"market={diagnostics.market_name_b!r} "
            f"selection={diagnostics.outcome_b!r} "
            f"odds={diagnostics.odds_b} "
            f"market_id={diagnostics.market_id_b}; "
            f"expected_profit={expected_profit} "
            f"max_total_stake={self._config.max_total_stake}"
        )

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
            market_id_a=str(inst_a.market_id or inst_a.event_id),
            market_id_b=str(inst_b.market_id or inst_b.event_id),
            market_name_a=inst_a.market_name,
            market_name_b=inst_b.market_name,
            outcome_a=inst_a.outcome,
            outcome_b=inst_b.outcome,
            venue_a=str(inst_a.id.venue),
            venue_b=str(inst_b.id.venue),
            odds_a=opportunity.odds_a,
            odds_b=opportunity.odds_b,
            quote_ts_a=int(quote_a.ts_event),
            quote_ts_b=int(quote_b.ts_event),
            quote_age_a_secs=quote_age_a_secs,
            quote_age_b_secs=quote_age_b_secs,
            quote_delta_secs=quote_delta_secs,
            same_quote_cycle=quote_delta_secs <= 2.0,
            stale=stale,
            matcher_suspect=matcher_suspect,
            suspect_reason=suspect_reason,
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
    def _matcher_suspect_reason(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> tuple[bool, str]:
        if instrument_a.venue_name == instrument_b.venue_name and (
            instrument_a.event_id != instrument_b.event_id
        ):
            return True, "same_venue_event_id_mismatch"
        if not instrument_a.matches_event(instrument_b):
            return True, "event_mismatch"
        if (
            instrument_a.market_name == instrument_b.market_name
            and instrument_a.params != instrument_b.params
        ):
            return True, "same_market_params_mismatch"
        return False, "none"

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
            f"unique_opportunities={len(self._seen_opportunity_pairs)} "
            f"duplicate_suppressions={self._duplicate_opportunities_suppressed} "
            f"stale_quote_suppressions={self._stale_quote_suppressions} "
            f"matcher_suspect_suppressions={self._matcher_suspect_suppressions} "
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
                f"venue_a={diagnostics.venue_a} venue_b={diagnostics.venue_b} "
                f"event_id_a={diagnostics.event_id_a} event_id_b={diagnostics.event_id_b} "
                f"market_id_a={diagnostics.market_id_a} market_id_b={diagnostics.market_id_b} "
                f"market_a={diagnostics.market_name_a} market_b={diagnostics.market_name_b} "
                f"outcome_a={diagnostics.outcome_a} outcome_b={diagnostics.outcome_b} "
                f"quote_ts_a={diagnostics.quote_ts_a} quote_ts_b={diagnostics.quote_ts_b} "
                f"quote_age_a_secs={diagnostics.quote_age_a_secs:.2f} "
                f"quote_age_b_secs={diagnostics.quote_age_b_secs:.2f} "
                f"quote_delta_secs={diagnostics.quote_delta_secs:.2f} "
                f"same_quote_cycle={diagnostics.same_quote_cycle}"
                f"{self._manual_execution_plan(opportunity, diagnostics)}"
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
            "opportunities_found": self._opportunities_found,
            "opportunities_executed": self._opportunities_executed,
            "raw_arbitrage_detections": self._raw_arbitrage_detections,
            "duplicate_opportunities_suppressed": self._duplicate_opportunities_suppressed,
            "stale_quote_suppressions": self._stale_quote_suppressions,
            "matcher_suspect_suppressions": self._matcher_suspect_suppressions,
            "executable_candidates": self._executable_candidates,
            "success_rate": (
                self._opportunities_executed / self._opportunities_found
                if self._opportunities_found > 0
                else 0
            ),
        }
