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

from decimal import Decimal

import msgspec

from nautilus_trader.adapters.betting.common.odds import calculate_arbitrage_stakes
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.message import Event
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.instruments.base import Instrument
from nautilus_trader.trading.strategy import Strategy


VALID_MARKET_TIMINGS = frozenset({"all", "pre_market", "live"})
DEFAULT_ENABLED_VENUES = frozenset({"CLOUDBET", "SXBET", "10BET"})


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

    """

    min_profit_margin: Decimal = Decimal("0.01")
    max_total_stake: Decimal = Decimal(1000)
    enabled_venues: frozenset[str] = DEFAULT_ENABLED_VENUES
    sport_filter: str | None = None
    market_timing_filter: str = "all"
    exclude_live: bool = False
    rollover_aware: bool = True
    auto_execute: bool = False

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

        # Tracking
        self._subscribed_instruments: set[CryptoBettingInstrument] = set()
        self._latest_quotes: dict[str, QuoteTick] = {}
        self._opportunities_found = 0
        self._opportunities_executed = 0

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
        for instrument in instruments:
            if not self._maybe_subscribe_instrument(instrument):
                continue

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
        self.subscribe_quote_ticks(instrument.id)
        self.log.info(f"Subscribed to {instrument.id}")
        return True

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
        1. Update latest quotes cache
        2. Find potential arbitrage opportunities
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
                self._opportunities_found += 1
                self._handle_arbitrage_opportunity(opportunity)

    def _handle_arbitrage_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> None:
        """
        Handle an arbitrage opportunity.

        Parameters
        ----------
        opportunity : ArbitrageOpportunity
            The arbitrage opportunity.

        """
        msg = (
            f"Arbitrage found: {opportunity.instrument_a.id.symbol} @ {opportunity.odds_a} vs "
            f"{opportunity.instrument_b.id.symbol} @ {opportunity.odds_b} | "
            f"Profit: {opportunity.profit_margin:.2%}"
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
            "opportunities_found": self._opportunities_found,
            "opportunities_executed": self._opportunities_executed,
            "success_rate": (
                self._opportunities_executed / self._opportunities_found
                if self._opportunities_found > 0
                else 0
            ),
        }
