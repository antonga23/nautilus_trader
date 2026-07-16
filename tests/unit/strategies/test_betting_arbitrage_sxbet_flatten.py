# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the SX.bet naked-leg opposing-back flatten path.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module,protected-access,too-many-arguments
"""
Adversarial proofs for the SX.bet naked-leg flatten.

A SELL on SX.bet cannot reduce exposure: the taker-fill adapter posts the instrument's
own outcome regardless of order side, so a SELL only ADDS a second back. The strategy
therefore flattens a naked back on selection X by placing a marketable back on the
mutually exclusive outcome Y, sized so the two backs hedge, and only within the slippage
bound and real opposing depth. Otherwise it halts and alerts for manual handling.

"""

from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import TestDataStubs


def ensure(condition: bool) -> None:  # skipcq
    if not condition:
        raise AssertionError


def _betting_instrument(*, venue: str, outcome: str) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="Soccer",
        competition_name="Test League",
        market_name="total_goals",
        market_type="total_goals",
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        params="",
        trading_status="ACTIVE",
    )


class _Harness:
    """
    A registered strategy plus the naked leg, its failed sibling, and the opposing
    quote.
    """

    def __init__(
        self,
        *,
        venue: str,
        opposing_bid_price: float,
        opposing_bid_size: float,
        unwind_enabled: bool = True,
        entry_odds: float = 2.0,
        naked_stake: float = 5.0,
    ) -> None:
        self.cache = TestComponentStubs.cache()
        self.naked_instrument = _betting_instrument(venue=venue, outcome="over")
        self.opposing_instrument = _betting_instrument(venue=venue, outcome="under")
        self.cache.add_instrument(self.naked_instrument)
        self.cache.add_instrument(self.opposing_instrument)

        self.strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({venue}),
                unwind_filled_leg_enabled=unwind_enabled,
            ),
        )
        self.strategy.register(
            trader_id=TraderId("TESTER-FLATTEN"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=self.cache,
            clock=TestComponentStubs.clock(),
        )

        # The failed sibling leg — its instrument is the complementary selection.
        self.failed_order = self.strategy.order_factory.limit(
            instrument_id=self.opposing_instrument.id,
            order_side=OrderSide.BUY,
            quantity=self.opposing_instrument.make_qty(naked_stake),
            price=self.opposing_instrument.make_price(entry_odds),
        )
        self.cache.add_order(self.failed_order)
        self.failed_leg_id = str(self.failed_order.client_order_id)

        # The naked (filled) leg on the other outcome.
        self.naked_leg_id = "NAKED-1"
        self.naked_order = SimpleNamespace(
            client_order_id=ClientOrderId(self.naked_leg_id),
            instrument_id=self.naked_instrument.id,
            filled_qty=self.naked_instrument.make_qty(naked_stake),
            avg_px=entry_odds,
        )
        self.strategy._arb_leg_siblings[self.naked_leg_id] = self.failed_leg_id
        self.strategy._arb_leg_siblings[self.failed_leg_id] = self.naked_leg_id

        # Opposing quote: SX.bet exposes the back-taker odds/depth as bid_price/bid_size.
        self.opposing_quote = TestDataStubs.quote_tick(
            instrument=self.opposing_instrument,
            bid_price=opposing_bid_price,
            ask_price=opposing_bid_price + 0.2,
            bid_size=opposing_bid_size,
            ask_size=opposing_bid_size,
        )
        self.strategy._latest_quotes[str(self.opposing_instrument.id)] = self.opposing_quote

        self.submitted: list = []
        self.strategy.submit_order = self.submitted.append

    def flatten(self) -> None:
        self.strategy._handle_naked_filled_leg(self.naked_order, self.failed_leg_id)

    def stats(self) -> dict:
        return self.strategy.get_stats()["live_execution"]

    def assert_no_sell_submitted(self) -> None:
        for order in self.submitted:
            ensure(order.side != OrderSide.SELL)


def test_sxbet_naked_back_with_depth_submits_opposing_back_never_sell():  # skipcq
    # (a) ample opposing depth -> a marketable opposing BACK is submitted, sized to hedge.
    h = _Harness(venue="SXBET", opposing_bid_price=2.0, opposing_bid_size=50.0)

    h.flatten()

    ensure(len(h.submitted) == 1)
    order = h.submitted[0]
    ensure(order.side == OrderSide.BUY)
    h.assert_no_sell_submitted()
    # Flatten backs the OTHER outcome, not a sell on the naked selection.
    ensure(order.instrument_id == h.opposing_instrument.id)
    # naked 5 @ 2.0 hedged by 5 @ 2.0 (returns balance 10 == 10).
    ensure(order.quantity.as_decimal() == Decimal("5.00"))
    ensure(order.price.as_decimal() == Decimal("2.0"))
    stats = h.stats()
    ensure(stats["unwind_exits"] == 1)
    ensure(stats["naked_flatten_halts"] == 0)
    ensure(stats["halt_reason"] is None)


def test_sxbet_insufficient_opposing_depth_halts_without_order():  # skipcq
    # (b) opposing depth below the hedge stake -> halt + alert + counter, no order.
    h = _Harness(venue="SXBET", opposing_bid_price=2.0, opposing_bid_size=2.0)

    h.flatten()

    ensure(len(h.submitted) == 0)
    h.assert_no_sell_submitted()
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 1)
    ensure(stats["halt_reason"] == "naked_leg_flatten_halted")
    ensure(stats["unwind_exits"] == 0)


def test_sxbet_no_opposing_quote_halts_without_order():  # skipcq
    # (b') no opposing quote at all -> halt, no order.
    h = _Harness(venue="SXBET", opposing_bid_price=2.0, opposing_bid_size=50.0)
    h.strategy._latest_quotes.pop(str(h.opposing_instrument.id))

    h.flatten()

    ensure(len(h.submitted) == 0)
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 1)
    ensure(stats["halt_reason"] == "naked_leg_flatten_halted")


def test_sxbet_slippage_beyond_bound_halts_without_order():  # skipcq
    # (c) opposing odds so low the synthetic lay breaches the slippage bound -> halt.
    # entry 2.0, opposing 1.5 -> effective lay 1.5/0.5 = 3.0 >> 2.0 * (1 + 0.005).
    h = _Harness(venue="SXBET", opposing_bid_price=1.5, opposing_bid_size=500.0)

    h.flatten()

    ensure(len(h.submitted) == 0)
    h.assert_no_sell_submitted()
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 1)
    ensure(stats["halt_reason"] == "naked_leg_flatten_halted")
    ensure(stats["unwind_exits"] == 0)


def test_sxbet_kill_switch_blocks_flatten(monkeypatch):  # skipcq
    # (d) kill switch active -> no flatten at all (blocked before the flatten path).
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_KILL_SWITCH", "1")
    h = _Harness(venue="SXBET", opposing_bid_price=2.0, opposing_bid_size=50.0)

    h.flatten()

    ensure(len(h.submitted) == 0)
    stats = h.stats()
    # Kill switch returns before the flatten path, so no flatten-specific halt is recorded.
    ensure(stats["naked_flatten_halts"] == 0)
    ensure(stats["unwind_exits"] == 0)


def test_sxbet_flatten_disabled_by_flag_does_not_submit():  # skipcq
    # Gate: default-off flag leaves the naked leg untouched.
    h = _Harness(
        venue="SXBET",
        opposing_bid_price=2.0,
        opposing_bid_size=50.0,
        unwind_enabled=False,
    )

    h.flatten()

    ensure(len(h.submitted) == 0)
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 0)
    ensure(stats["unwind_exits"] == 0)


def test_sxbet_flatten_completes_tracked_pair():  # skipcq
    # (f) the position tracker reflects the flatten as the pair's second outcome.
    h = _Harness(venue="SXBET", opposing_bid_price=2.0, opposing_bid_size=50.0)
    tracker = h.strategy._arb_position_tracker
    # The naked leg's own fill was already recorded by on_order_filled in production.
    tracker.record_fill(
        h.naked_leg_id,
        h.naked_instrument.outcome,
        "BUY",
        2.0,
        5.0,
        sibling_id=h.failed_leg_id,
    )
    pair_id = tracker.pair_key(h.naked_leg_id, h.failed_leg_id)
    ensure(tracker.pair(pair_id).is_fully_hedged is False)

    h.flatten()

    flatten_order = h.submitted[0]
    flatten_id = str(flatten_order.client_order_id)
    # Simulate the flatten fill arriving through the normal order-event path.
    h.strategy.on_order_filled(
        SimpleNamespace(
            client_order_id=ClientOrderId(flatten_id),
            instrument_id=h.opposing_instrument.id,
            order_side=OrderSide.BUY,
            last_px=2.0,
            last_qty=5.0,
        ),
    )

    pair = tracker.pair(pair_id)
    ensure(pair.is_fully_hedged is True)
    pnls = pair.outcome_pnls()
    # Two 5 @ 2.0 backs on the mutually exclusive outcomes; each leg's win payoff is 5.00.
    # The flatten (opposing) leg is on SXBET, so when it wins its net profit is charged the
    # 4% winning-profit commission: 5.00 * 0.96 - 5.00 = 4.80 - 5.00 = -0.20. The naked leg
    # carries no venue tag in this fixture, so its winning outcome bears no commission and
    # stays break-even.
    ensure(pnls[h.naked_instrument.outcome] == Decimal(0))
    ensure(pnls[h.opposing_instrument.outcome] == Decimal("-0.20"))
    ensure(pair.guaranteed_pnl() == Decimal("-0.20"))
