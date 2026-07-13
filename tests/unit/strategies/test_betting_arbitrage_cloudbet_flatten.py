# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the CLOUDBET naked-leg complementary-back flatten path.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module,protected-access,too-many-arguments
"""
Adversarial proofs for the CLOUDBET naked-leg flatten.

CLOUDBET is a sportsbook: its place-bets path rejects any non-BACK side outright (a SELL /
LAY raises before submission) and a SELL/LAY would only add a second stake. A naked back on
selection X is therefore flattened by placing a marketable back on the mutually exclusive
outcome Y of the *same CLOUDBET market*, sized so the two backs hedge, and only within the
slippage bound and the real opposing depth. In a cross-venue arb the failed sibling lives on
the other venue, so the complement is resolved from the CLOUDBET market itself rather than
from the sibling leg. When no marketable opposing depth exists the leg halts and alerts for
manual handling — it never emits a SELL/LAY and never silently leaves the exposure.

"""

from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.examples.strategies.betting_arbitrage import UNWIND_EXIT_SUPPORTED_VENUES
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
    A registered strategy with a naked CLOUDBET leg, its same-market complementary
    selection, and the opposing CLOUDBET quote.

    The failed sibling is modelled on SXBET — the cross-venue arb scenario — so the flatten
    must resolve the complement from the CLOUDBET market rather than from the sibling leg.

    """

    def __init__(
        self,
        *,
        opposing_ask_price: float = 2.0,
        opposing_ask_size: float = 50.0,
        include_opposing: bool = True,
        include_opposing_quote: bool = True,
        unwind_enabled: bool = True,
        entry_odds: float = 2.0,
        naked_stake: float = 5.0,
    ) -> None:
        self.cache = TestComponentStubs.cache()
        self.naked_instrument = _betting_instrument(venue="CLOUDBET", outcome="over")
        self.opposing_instrument = _betting_instrument(venue="CLOUDBET", outcome="under")
        self.cache.add_instrument(self.naked_instrument)
        if include_opposing:
            self.cache.add_instrument(self.opposing_instrument)

        # A same-event selection on the OTHER venue: the cross-venue failed sibling. It
        # must never be picked as the CLOUDBET complement (wrong venue).
        self.sx_sibling_instrument = _betting_instrument(venue="SXBET", outcome="under")
        self.cache.add_instrument(self.sx_sibling_instrument)

        self.strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({"CLOUDBET", "SXBET"}),
                unwind_filled_leg_enabled=unwind_enabled,
            ),
        )
        self.strategy.register(
            trader_id=TraderId("TESTER-CB-FLATTEN"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=self.cache,
            clock=TestComponentStubs.clock(),
        )

        # The failed sibling leg on SXBET (the other venue).
        self.failed_order = self.strategy.order_factory.limit(
            instrument_id=self.sx_sibling_instrument.id,
            order_side=OrderSide.BUY,
            quantity=self.sx_sibling_instrument.make_qty(naked_stake),
            price=self.sx_sibling_instrument.make_price(entry_odds),
        )
        self.cache.add_order(self.failed_order)
        self.failed_leg_id = str(self.failed_order.client_order_id)

        # The naked (filled) CLOUDBET leg.
        self.naked_leg_id = "NAKED-CB-1"
        self.naked_order = SimpleNamespace(
            client_order_id=ClientOrderId(self.naked_leg_id),
            instrument_id=self.naked_instrument.id,
            filled_qty=self.naked_instrument.make_qty(naked_stake),
            avg_px=entry_odds,
        )
        self.strategy._arb_leg_siblings[self.naked_leg_id] = self.failed_leg_id
        self.strategy._arb_leg_siblings[self.failed_leg_id] = self.naked_leg_id

        # CLOUDBET exposes the marketable back odds/depth on the ASK side.
        if include_opposing_quote:
            self.opposing_quote = TestDataStubs.quote_tick(
                instrument=self.opposing_instrument,
                bid_price=opposing_ask_price - 0.2,
                ask_price=opposing_ask_price,
                bid_size=opposing_ask_size,
                ask_size=opposing_ask_size,
            )
            self.strategy._latest_quotes[str(self.opposing_instrument.id)] = self.opposing_quote

        self.submitted: list = []
        self.strategy.submit_order = self.submitted.append

    def flatten(self) -> None:
        self.strategy._handle_naked_filled_leg(self.naked_order, self.failed_leg_id)

    def stats(self) -> dict:
        return self.strategy.get_stats()["live_execution"]

    def assert_never_sell_or_lay(self) -> None:
        for order in self.submitted:
            ensure(order.side == OrderSide.BUY)
            ensure(order.side != OrderSide.SELL)


def test_cloudbet_naked_back_with_depth_submits_opposing_back_never_sell():  # skipcq
    # (a) ample opposing depth -> exactly one marketable opposing BACK, sized to hedge.
    h = _Harness(opposing_ask_price=2.0, opposing_ask_size=50.0)

    h.flatten()

    ensure(len(h.submitted) == 1)
    order = h.submitted[0]
    ensure(order.side == OrderSide.BUY)
    h.assert_never_sell_or_lay()
    # Flatten backs the OTHER CLOUDBET outcome, not a sell on the naked selection, and
    # not the SXBET sibling.
    ensure(order.instrument_id == h.opposing_instrument.id)
    ensure(order.instrument_id != h.naked_instrument.id)
    ensure(order.instrument_id != h.sx_sibling_instrument.id)
    # naked 5 @ 2.0 hedged by 5 @ 2.0 (returns balance 10 == 10).
    ensure(order.quantity.as_decimal() == Decimal("5.00"))
    ensure(order.price.as_decimal() == Decimal("2.0"))
    stats = h.stats()
    ensure(stats["unwind_exits"] == 1)
    ensure(stats["naked_flatten_halts"] == 0)
    ensure(stats["halt_reason"] is None)


def test_cloudbet_no_complementary_selection_halts_without_order():  # skipcq
    # (b) the CLOUDBET market has no opposing selection cached -> halt + alert, no order.
    h = _Harness(include_opposing=False)

    h.flatten()

    ensure(len(h.submitted) == 0)
    h.assert_never_sell_or_lay()
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 1)
    ensure(stats["halt_reason"] == "naked_leg_flatten_halted")
    ensure(stats["unwind_exits"] == 0)


def test_cloudbet_insufficient_opposing_depth_halts_without_order():  # skipcq
    # (b') opposing depth below the hedge stake -> halt + alert + counter, no order.
    h = _Harness(opposing_ask_price=2.0, opposing_ask_size=2.0)

    h.flatten()

    ensure(len(h.submitted) == 0)
    h.assert_never_sell_or_lay()
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 1)
    ensure(stats["halt_reason"] == "naked_leg_flatten_halted")
    ensure(stats["unwind_exits"] == 0)


def test_cloudbet_no_opposing_quote_halts_without_order():  # skipcq
    # (b'') the complement exists but has no quote -> halt, no order.
    h = _Harness(include_opposing_quote=False)

    h.flatten()

    ensure(len(h.submitted) == 0)
    h.assert_never_sell_or_lay()
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 1)
    ensure(stats["halt_reason"] == "naked_leg_flatten_halted")
    ensure(stats["unwind_exits"] == 0)


def test_cloudbet_slippage_beyond_bound_halts_without_order():  # skipcq
    # (c) opposing odds so low the synthetic lay breaches the slippage bound -> halt.
    # entry 2.0, opposing 1.5 -> effective lay 1.5/0.5 = 3.0 >> 2.0 * (1 + 0.005).
    h = _Harness(opposing_ask_price=1.5, opposing_ask_size=500.0)

    h.flatten()

    ensure(len(h.submitted) == 0)
    h.assert_never_sell_or_lay()
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 1)
    ensure(stats["halt_reason"] == "naked_leg_flatten_halted")
    ensure(stats["unwind_exits"] == 0)


def test_cloudbet_slippage_within_bound_submits_back():  # skipcq
    # (c') just inside the bound: entry 2.0, opposing 3.0 -> effective lay 3.0/2.0 = 1.5,
    # which is <= 2.0 * (1 + 0.005), so the flatten proceeds as a BACK.
    h = _Harness(opposing_ask_price=3.0, opposing_ask_size=500.0)

    h.flatten()

    ensure(len(h.submitted) == 1)
    order = h.submitted[0]
    ensure(order.side == OrderSide.BUY)
    h.assert_never_sell_or_lay()
    stats = h.stats()
    ensure(stats["unwind_exits"] == 1)
    ensure(stats["naked_flatten_halts"] == 0)


def test_cloudbet_flatten_disabled_by_flag_does_not_submit():  # skipcq
    # Gate: default-off flag leaves the naked leg untouched.
    h = _Harness(unwind_enabled=False)

    h.flatten()

    ensure(len(h.submitted) == 0)
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 0)
    ensure(stats["unwind_exits"] == 0)


def test_cloudbet_kill_switch_blocks_flatten(monkeypatch):  # skipcq
    # Kill switch active -> no flatten at all (blocked before the flatten path).
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_KILL_SWITCH", "1")
    h = _Harness(opposing_ask_price=2.0, opposing_ask_size=50.0)

    h.flatten()

    ensure(len(h.submitted) == 0)
    stats = h.stats()
    ensure(stats["naked_flatten_halts"] == 0)
    ensure(stats["unwind_exits"] == 0)


def test_cloudbet_excluded_from_sell_based_bounded_exit_venues():  # skipcq
    # (d) no code path can emit a SELL for a CLOUDBET flatten. The only SELL emitter is
    # _attempt_bounded_exit, gated on UNWIND_EXIT_SUPPORTED_VENUES; CLOUDBET is absent, so
    # even calling it directly on a CLOUDBET leg submits nothing.
    ensure("CLOUDBET" not in UNWIND_EXIT_SUPPORTED_VENUES)

    h = _Harness(opposing_ask_price=2.0, opposing_ask_size=50.0)
    # A well-formed naked-selection quote that a SELL-based exit would otherwise act on.
    naked_quote = TestDataStubs.quote_tick(
        instrument=h.naked_instrument,
        bid_price=2.0,
        ask_price=2.2,
        bid_size=50.0,
        ask_size=50.0,
    )
    h.strategy._latest_quotes[str(h.naked_instrument.id)] = naked_quote

    h.strategy._attempt_bounded_exit(h.naked_order)

    ensure(len(h.submitted) == 0)
    h.assert_never_sell_or_lay()


def test_cloudbet_flatten_completes_tracked_pair():  # skipcq
    # The position tracker reflects the flatten as the pair's second outcome.
    h = _Harness(opposing_ask_price=2.0, opposing_ask_size=50.0)
    tracker = h.strategy._arb_position_tracker
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
    ensure(pnls[h.naked_instrument.outcome] == Decimal(0))
    ensure(pnls[h.opposing_instrument.outcome] == Decimal(0))
    ensure(pair.guaranteed_pnl() == Decimal(0))
