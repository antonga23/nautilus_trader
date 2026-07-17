# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for cross-leg partial-fill imbalance detection.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module,protected-access,too-many-arguments
"""
Adversarial proofs for cross-leg partial-fill imbalance detection.

Neither betting venue offers fill-or-kill (SX.bet's maker/taker payloads carry no
all-or-nothing flag), so a two-leg arb can end up with one leg matched far more than its
sibling and left directionally exposed. When ``max_leg_fill_imbalance_pct`` is set, the
relative gap between the two legs' accumulated matched stake -- currency-normalized for a
cross-currency pair -- is checked on every fill, and a gap above the threshold routes the
over-filled leg into the same naked-leg flatten path used for a terminally failed sibling.
The guard is disabled by default, only ever reuses the existing flatten machinery, and must
never raise into the fill handler.

"""

from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs


def ensure(condition: bool) -> None:  # skipcq
    if not condition:
        raise AssertionError


def _betting_instrument(
    *,
    venue: str,
    outcome: str,
    currency: str = "USDC",
) -> CryptoBettingInstrument:
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
        currency=Currency.from_str(currency),
        params="",
        trading_status="ACTIVE",
    )


class _Harness:
    """
    A registered strategy with two sibling legs, plus a spy over the naked-leg flatten
    so tests assert only the imbalance ROUTING decision, not the (separately proven)
    flatten internals.
    """

    def __init__(
        self,
        *,
        imbalance_pct: float | None,
        venue_a: str = "SXBET",
        venue_b: str = "SXBET",
        currency_a: str = "USDC",
        currency_b: str = "USDC",
        configured_fx_rates: dict | None = None,
        unwind_enabled: bool = True,
        spy_flatten: bool = True,
    ) -> None:
        self.cache = TestComponentStubs.cache()
        self.instrument_a = _betting_instrument(
            venue=venue_a,
            outcome="over",
            currency=currency_a,
        )
        self.instrument_b = _betting_instrument(
            venue=venue_b,
            outcome="under",
            currency=currency_b,
        )
        self.cache.add_instrument(self.instrument_a)
        self.cache.add_instrument(self.instrument_b)

        self.strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(
                enabled_venues=frozenset({venue_a, venue_b}),
                unwind_filled_leg_enabled=unwind_enabled,
                max_leg_fill_imbalance_pct=imbalance_pct,
                configured_fx_rates=configured_fx_rates or {},
            ),
        )
        self.strategy.register(
            trader_id=TraderId("TESTER-IMBAL"),
            portfolio=TestComponentStubs.portfolio(),
            msgbus=TestComponentStubs.msgbus(),
            cache=self.cache,
            clock=TestComponentStubs.clock(),
        )

        self.submitted: list = []

        def _submit(order):
            self.cache.add_order(order)
            self.submitted.append(order)

        self.strategy.submit_order = _submit

        self.order_a = self.strategy.order_factory.limit(
            instrument_id=self.instrument_a.id,
            order_side=OrderSide.BUY,
            quantity=self.instrument_a.make_qty(10.0),
            price=self.instrument_a.make_price(2.0),
        )
        self.order_b = self.strategy.order_factory.limit(
            instrument_id=self.instrument_b.id,
            order_side=OrderSide.BUY,
            quantity=self.instrument_b.make_qty(10.0),
            price=self.instrument_b.make_price(2.0),
        )
        self.cache.add_order(self.order_a)
        self.cache.add_order(self.order_b)
        self.leg_a = str(self.order_a.client_order_id)
        self.leg_b = str(self.order_b.client_order_id)
        self.strategy._arb_leg_siblings[self.leg_a] = self.leg_b
        self.strategy._arb_leg_siblings[self.leg_b] = self.leg_a

        self.flattened: list = []
        if spy_flatten:
            self.strategy._handle_naked_filled_leg = (  # type: ignore[method-assign]
                lambda order, failed_leg_id: self.flattened.append(
                    (str(order.client_order_id), failed_leg_id),
                )
            )

    def seed_fill(self, order, *, qty: float, px: float = 2.0) -> None:
        """
        Apply a (partial) fill to a cache order and mirror it into the position tracker,
        exactly as ``on_order_filled`` would in production.
        """
        instrument = self.cache.instrument(order.instrument_id)
        order.apply(TestEventStubs.order_submitted(order))
        order.apply(TestEventStubs.order_accepted(order))
        order.apply(
            TestEventStubs.order_filled(
                order,
                instrument=instrument,
                last_qty=instrument.make_qty(qty),
                last_px=instrument.make_price(px),
            ),
        )
        self.cache.update_order(order)
        self.strategy._arb_position_tracker.record_fill(
            str(order.client_order_id),
            instrument.outcome,
            OrderSide.BUY,
            px,
            qty,
            sibling_id=self.strategy._arb_leg_siblings.get(str(order.client_order_id)),
            currency=str(instrument.quote_currency),
            venue=str(order.instrument_id.venue),
        )

    def trigger(self, order) -> None:
        self.strategy._handle_partial_fill_imbalance(
            SimpleNamespace(client_order_id=order.client_order_id),
        )

    def stats(self) -> dict:
        return self.strategy.get_stats()["live_execution"]


def test_imbalance_below_threshold_does_not_flatten():  # skipcq
    # (a) both legs matched to near-equal stake -> gap under the threshold -> no flatten.
    h = _Harness(imbalance_pct=0.2)
    h.seed_fill(h.order_a, qty=5.0)
    h.seed_fill(h.order_b, qty=5.0)

    h.trigger(h.order_a)

    ensure(h.flattened == [])
    ensure(h.stats()["leg_imbalance_flattens"] == 0)


def test_imbalance_above_threshold_routes_over_filled_leg_to_flatten():  # skipcq
    # (b) leg A fully matched, sibling B never filled -> 100% gap -> the OVER-filled leg is
    # handed to the naked-leg flatten path, with the under-filled sibling as the failed leg.
    h = _Harness(imbalance_pct=0.2)
    h.seed_fill(h.order_a, qty=5.0)  # B is left unfilled.

    h.trigger(h.order_a)

    ensure(h.flattened == [(h.leg_a, h.leg_b)])
    ensure(h.stats()["leg_imbalance_flattens"] == 1)
    # Marked terminal so a later event cannot route the same pair twice.
    pair_key = "|".join(sorted((h.leg_a, h.leg_b)))
    ensure(pair_key in h.strategy._unwound_arb_pairs)
    h.trigger(h.order_a)
    ensure(h.stats()["leg_imbalance_flattens"] == 1)


def test_imbalance_disabled_by_default_takes_no_action():  # skipcq
    # (c) default None disables the guard: a 100% gap is left untouched (behavior unchanged).
    h = _Harness(imbalance_pct=None)
    h.seed_fill(h.order_a, qty=5.0)  # B unfilled, maximally imbalanced.

    h.trigger(h.order_a)

    ensure(h.flattened == [])
    ensure(h.stats()["leg_imbalance_flattens"] == 0)


def test_imbalance_handler_never_raises_on_odd_tracker_state():  # skipcq
    # (d) a bug/odd state inside the tracker must be swallowed: the fill handler must never
    # raise. Force pair_for_leg to blow up and confirm neither the handler nor on_order_filled
    # propagates it.
    h = _Harness(imbalance_pct=0.2)
    h.seed_fill(h.order_a, qty=5.0)

    def _boom(_key):
        raise RuntimeError("tracker exploded")

    h.strategy._arb_position_tracker.pair_for_leg = _boom

    h.trigger(h.order_a)  # must not raise
    h.strategy.on_order_filled(
        SimpleNamespace(
            client_order_id=h.order_a.client_order_id,
            instrument_id=h.instrument_a.id,
            order_side=OrderSide.BUY,
            last_px=2.0,
            last_qty=5.0,
        ),
    )  # must not raise

    ensure(h.flattened == [])
    ensure(h.stats()["leg_imbalance_flattens"] == 0)


def test_imbalance_is_currency_normalized_across_a_cross_currency_pair():  # skipcq
    # (e) raw stakes are EQUAL (5 EUR vs 5 USDC), so a naive same-number comparison sees a
    # zero gap. Normalized into USD (EUR/USD 1.10 plus the stablecoin haircut) the EUR leg is
    # worth ~5.5055 vs ~5.005, a ~9% gap that clears the 5% threshold -> flatten. This proves
    # the imbalance is measured on a currency-normalized footing, not on raw stake numbers.
    h = _Harness(
        imbalance_pct=0.05,
        venue_a="CLOUDBET",
        venue_b="SXBET",
        currency_a="EUR",
        currency_b="USDC",
        configured_fx_rates={"EUR/USD": Decimal("1.10")},
    )
    h.seed_fill(h.order_a, qty=5.0)
    h.seed_fill(h.order_b, qty=5.0)

    # Raw stakes are identical: without normalization there is no imbalance to act on.
    pair = h.strategy._arb_position_tracker.pair_for_leg(h.leg_a)
    ensure(pair.legs[h.leg_a].stake == pair.legs[h.leg_b].stake)

    h.trigger(h.order_a)

    ensure(h.flattened == [(h.leg_a, h.leg_b)])
    ensure(h.stats()["leg_imbalance_flattens"] == 1)


def test_imbalance_end_to_end_submits_opposing_back_via_on_order_filled():  # skipcq
    # Wiring proof: driven through the real on_order_filled path with live quotes, an
    # imbalanced fill reaches the existing SX.bet opposing-back flatten and submits a BACK
    # (never a SELL) on the complementary selection.
    h = _Harness(imbalance_pct=0.2, spy_flatten=False)
    opposing_quote = TestDataStubs.quote_tick(
        instrument=h.instrument_b,
        bid_price=2.0,
        ask_price=2.2,
        bid_size=50.0,
        ask_size=50.0,
    )
    h.strategy._latest_quotes[str(h.instrument_b.id)] = opposing_quote

    instrument = h.instrument_a
    h.order_a.apply(TestEventStubs.order_submitted(h.order_a))
    h.order_a.apply(TestEventStubs.order_accepted(h.order_a))
    fill = TestEventStubs.order_filled(
        h.order_a,
        instrument=instrument,
        last_qty=instrument.make_qty(5.0),
        last_px=instrument.make_price(2.0),
    )
    h.order_a.apply(fill)
    h.cache.update_order(h.order_a)

    h.strategy.on_order_filled(fill)  # B never filled -> imbalance routes A to flatten.

    ensure(len(h.submitted) == 1)
    order = h.submitted[0]
    ensure(order.side == OrderSide.BUY)
    ensure(order.instrument_id == h.instrument_b.id)
    ensure(h.stats()["leg_imbalance_flattens"] == 1)
    ensure(h.stats()["unwind_exits"] == 1)
