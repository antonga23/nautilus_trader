# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for opt-in execution of positive-EV void-compatible middles.
# -------------------------------------------------------------------------------------------------
# skipcq: BAN-B101
# bandit:skip=B101
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212
# pylint: disable=missing-function-docstring,no-name-in-module,protected-access,too-many-arguments
"""
Adversarial proofs for the void-compatible-middle execution opt-in.

A middle backs two mutually exclusive selections whose settlement vectors leave no state
where both lose but include a shared VOID/PUSH state. Payoff-equalisation sizing (the same
solver as any two-leg arb) wins the whole edge on a decisive state and refunds both stakes
on the push, so the push is an automatic break-even. Everything stays staged, not armed:
the four ``has_void`` gates open together behind ``execute_void_compatible_middles`` and the
staged record is labelled ``MIDDLE`` for the operator.

"""

from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.odds import calculate_arbitrage_stakes
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.semantics import RelationshipType
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


def ensure(condition: bool) -> None:  # skipcq
    if not condition:
        raise AssertionError


def _instrument(
    *,
    venue: str,
    outcome: str,
    line: str,
    currency: str = "USDC",
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="baseball",
        competition_name="Test League",
        market_name="run_line",
        market_type="asian_handicap",
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.5,
        currency=Currency.from_str(currency),
        params=f"line={line}",
        handicap=None,
        trading_status="ACTIVE",
    )


def _middle_edge(caveats=("void_states_present", "price_correlation_not_proof")):
    # A runtime edge carries the classifier's caveats and the VOID_COMPATIBLE_HEDGE
    # relationship; its tier booleans stay False at runtime (stats=None keeps it
    # TOPOLOGY_SAFE), so the middle path must authorise it without them.
    return SimpleNamespace(
        relationship_type=RelationshipType.VOID_COMPATIBLE_HEDGE.value,
        caveats=caveats,
        execution_safe=False,
        same_venue_execution_eligible=False,
    )


def _strategy(**overrides: object) -> BettingArbitrageStrategy:
    config_kwargs: dict[str, object] = {
        "enabled_venues": frozenset({"CLOUDBET", "SXBET"}),
        "auto_execute": True,
        "live_execution_armed": True,
        "max_total_stake": Decimal(100),
        "max_leg_stake": Decimal(100),
        "max_daily_notional": Decimal(1000),
        "min_profit_margin": Decimal("0.01"),
    }
    config_kwargs.update(overrides)
    strategy = BettingArbitrageStrategy(
        config=BettingArbitrageConfig(**config_kwargs),  # type: ignore[arg-type]
    )
    strategy.register(
        trader_id=TraderId("TESTER-MID"),
        portfolio=TestComponentStubs.portfolio(),
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
    )
    return strategy


def _opportunity(
    instrument_a,
    instrument_b,
    *,
    odds_a=Decimal("2.52"),
    odds_b=Decimal("2.92"),
) -> ArbitrageOpportunity:
    prob_a = Decimal(1) / odds_a
    prob_b = Decimal(1) / odds_b
    total = prob_a + prob_b
    return ArbitrageOpportunity(
        instrument_a=instrument_a,
        instrument_b=instrument_b,
        probability_a=prob_a,
        probability_b=prob_b,
        total_probability=total,
        profit_margin=(Decimal(1) / total) - Decimal(1),
        odds_a=odds_a,
        odds_b=odds_b,
        is_same_venue=instrument_a.venue_name == instrument_b.venue_name,
        match_type="cross_market"
        if instrument_a.venue_name == instrument_b.venue_name
        else "cross_venue",
    )


def _register_edge(strategy, instrument_a, instrument_b, edge) -> None:
    edge_id = strategy._canonical_pair_id(instrument_a, instrument_b)
    strategy.opportunity_graph.edges_by_id[edge_id] = edge


def test_live_example_middle_sizing_and_break_even():
    # (a) CLOUDBET same-venue HOME -1 @2.52 / AWAY +1 @2.92, total 100.
    stake_a, stake_b, decisive_profit = calculate_arbitrage_stakes(
        Decimal("2.52"),
        Decimal("2.92"),
        Decimal(100),
    )
    ensure(stake_a == Decimal("53.68"))
    ensure(stake_b == Decimal("46.32"))
    # Decisive state pays ~135.26 on either leg -> +35.25 after the 100 outlay.
    ensure(stake_a * Decimal("2.52") == Decimal("135.2736"))
    ensure(stake_b * Decimal("2.92") == Decimal("135.2544"))
    ensure(decisive_profit == Decimal("35.25"))
    # Push refunds both stakes -> the 100 outlay is returned for exactly break-even.
    push_return = stake_a + stake_b
    ensure(push_return == Decimal(100))
    ensure(push_return - Decimal(100) == Decimal(0))


def test_same_venue_middle_staged_under_flag(monkeypatch):
    # (a) with the flag on, the exact live example clears every live gate (staged, unarmed).
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        execute_void_compatible_middles=True,
        min_middle_profit_margin=Decimal("0.10"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="CLOUDBET", outcome="away", line="1")
    _register_edge(strategy, home, away, _middle_edge())
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_block_reasons_for(
        opportunity=opportunity,
        stake_a=Decimal("53.68"),
        stake_b=Decimal("46.32"),
        diagnostics=None,
    )

    ensure(reasons == [])


def test_flag_off_keeps_middle_blocked(monkeypatch):
    # (b) flag off -> byte-identical to develop: the same candidate is blocked by the
    # generic semantic policy (void caveats remain dangerous).
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(execute_void_compatible_middles=False)
    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="CLOUDBET", outcome="away", line="1")
    _register_edge(strategy, home, away, _middle_edge())
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure(reasons == ["semantic_execution_policy_blocked"])


def test_polymarket_leg_middle_blocked_even_under_flag(monkeypatch):
    # (c) a PM leg pays a taker fee at placement that is NOT refunded on push, so a
    # PM-leg middle books a real loss on the break-even state -> hard exclusion.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        enabled_venues=frozenset({"CLOUDBET", "POLYMARKET"}),
        execute_void_compatible_middles=True,
        min_middle_profit_margin=Decimal("0.10"),
        execution_venue_mode="cross_venue",
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="POLYMARKET", outcome="away", line="1")
    _register_edge(strategy, home, away, _middle_edge())
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure("middle_polymarket_push_fee" in reasons)


def test_middle_below_floor_blocked(monkeypatch):
    # (d) margin between the ordinary floor and the higher middle floor -> blocked.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        execute_void_compatible_middles=True,
        min_profit_margin=Decimal("0.01"),
        min_middle_profit_margin=Decimal("0.20"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="CLOUDBET", outcome="away", line="1")
    _register_edge(strategy, home, away, _middle_edge())
    # ~5% margin: clears min_profit_margin but not the 20% middle floor.
    opportunity = _opportunity(home, away, odds_a=Decimal("2.10"), odds_b=Decimal("2.10"))

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure("below_min_middle_profit_margin" in reasons)


def test_non_void_risk_stays_blocked_under_flag(monkeypatch):
    # (e) an edge carrying a non-void settlement risk (PARTIAL) is not a clean middle, so
    # the flag does not open it -> it routes back to the generic (blocked) policy.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        execute_void_compatible_middles=True,
        min_middle_profit_margin=Decimal("0.10"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="CLOUDBET", outcome="away", line="1")
    edge = _middle_edge(
        caveats=("void_states_present", "partial_states_present", "price_correlation_not_proof"),
    )
    _register_edge(strategy, home, away, edge)
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure(reasons == ["semantic_execution_policy_blocked"])


def test_middle_is_staged_with_bet_type_label():
    # (h) a middle-eligible edge is STAGED (unarmed) and labelled MIDDLE, with the profit
    # split into decisive vs push(=0) so the operator approves the break-even knowingly.
    strategy = _strategy(
        execute_void_compatible_middles=True,
        min_middle_profit_margin=Decimal("0.10"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="CLOUDBET", outcome="away", line="1")
    _register_edge(strategy, home, away, _middle_edge())
    opportunity = _opportunity(home, away)

    record = strategy._store_pending_approval(
        opportunity=opportunity,
        diagnostics=None,
        stake_a=Decimal("53.68"),
        stake_b=Decimal("46.32"),
        expected_profit=Decimal("35.25"),
        now_ns=strategy.clock.timestamp_ns(),
    )
    payload = record.to_payload()

    ensure(record.bet_type == "MIDDLE")
    ensure(payload["bet_type"] == "MIDDLE")
    ensure(payload["relationship_type"] == RelationshipType.VOID_COMPATIBLE_HEDGE.value)
    ensure(payload["push_outcome"] == "break_even")
    ensure(payload["decisive_profit"] == "35.25")
    ensure(payload["push_profit"] == "0")
    # Staged, never submitted: the approval queue is the only place this middle lives.
    ensure(strategy._approvals_staged == 1)
    ensure(record.approval_id in strategy._pending_approvals)


def test_deploy_config_stages_middle_while_execution_stays_unarmed():
    # The cross-venue shard + baseball manifests set execute_void_compatible_middles=true
    # with every execution flag false. The flag opens staging only: a void-compatible middle
    # is labelled MIDDLE and lands in the manual-approval queue, and no armed flag is flipped
    # (nothing executes without a separate arming step).
    strategy = _strategy(
        auto_execute=False,
        live_execution_armed=False,
        value_execution_enabled=False,
        execute_void_compatible_middles=True,
        min_profit_margin=Decimal("0.02"),
        min_middle_profit_margin=Decimal("0.025"),
    )
    # The opt-in did not arm any execution path.
    ensure(strategy._config.auto_execute is False)
    ensure(strategy._config.live_execution_armed is False)
    ensure(strategy._config.value_execution_enabled is False)

    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="SXBET", outcome="away", line="1")
    _register_edge(strategy, home, away, _middle_edge())
    opportunity = _opportunity(home, away)

    record = strategy._store_pending_approval(
        opportunity=opportunity,
        diagnostics=None,
        stake_a=Decimal("53.68"),
        stake_b=Decimal("46.32"),
        expected_profit=Decimal("35.25"),
        now_ns=strategy.clock.timestamp_ns(),
    )

    ensure(record.bet_type == "MIDDLE")
    ensure(strategy._approvals_staged == 1)
    ensure(record.approval_id in strategy._pending_approvals)


def test_plain_arb_edge_is_not_labelled_middle():
    # An ordinary complementary-coverage edge stays ARB even with the flag on.
    strategy = _strategy(
        execute_void_compatible_middles=True,
        min_middle_profit_margin=Decimal("0.10"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="-1")
    away = _instrument(venue="CLOUDBET", outcome="away", line="1")
    _register_edge(
        strategy,
        home,
        away,
        SimpleNamespace(
            relationship_type=RelationshipType.COMPLEMENTARY_COVERAGE.value,
            caveats=(),
            execution_safe=True,
            same_venue_execution_eligible=False,
        ),
    )
    opportunity = _opportunity(home, away)

    record = strategy._store_pending_approval(
        opportunity=opportunity,
        diagnostics=None,
        stake_a=Decimal("53.68"),
        stake_b=Decimal("46.32"),
        expected_profit=Decimal("35.25"),
        now_ns=strategy.clock.timestamp_ns(),
    )

    ensure(record.bet_type == "ARB")
    ensure(record.to_payload()["push_outcome"] is None)


def test_naked_middle_leg_flatten_completes_the_middle():
    # (g) one middle leg fills, the sibling submit fails: the flatten backs the sibling
    # selection (which IS the other middle leg) and completes the pair -> no double-place,
    # no silently unhedged one-sided bet.
    cache = TestComponentStubs.cache()
    filled = _instrument(venue="SXBET", outcome="home", line="-1")
    sibling = _instrument(venue="SXBET", outcome="away", line="1")
    cache.add_instrument(filled)
    cache.add_instrument(sibling)

    strategy = BettingArbitrageStrategy(
        config=BettingArbitrageConfig(
            enabled_venues=frozenset({"SXBET"}),
            execute_void_compatible_middles=True,
            min_middle_profit_margin=Decimal("0.10"),
            unwind_filled_leg_enabled=True,
        ),
    )
    strategy.register(
        trader_id=TraderId("TESTER-MID-FLAT"),
        portfolio=TestComponentStubs.portfolio(),
        msgbus=TestComponentStubs.msgbus(),
        cache=cache,
        clock=TestComponentStubs.clock(),
    )

    failed_order = strategy.order_factory.limit(
        instrument_id=sibling.id,
        order_side=OrderSide.BUY,
        quantity=sibling.make_qty(5.0),
        price=sibling.make_price(2.0),
    )
    cache.add_order(failed_order)
    failed_leg_id = str(failed_order.client_order_id)

    naked_leg_id = "NAKED-MID-1"
    naked_order = SimpleNamespace(
        client_order_id=ClientOrderId(naked_leg_id),
        instrument_id=filled.id,
        filled_qty=filled.make_qty(5.0),
        avg_px=2.0,
    )
    strategy._arb_leg_siblings[naked_leg_id] = failed_leg_id
    strategy._arb_leg_siblings[failed_leg_id] = naked_leg_id

    from nautilus_trader.test_kit.stubs.data import TestDataStubs

    strategy._latest_quotes[str(sibling.id)] = TestDataStubs.quote_tick(
        instrument=sibling,
        bid_price=2.0,
        ask_price=2.2,
        bid_size=50.0,
        ask_size=50.0,
    )
    submitted: list = []
    strategy.submit_order = submitted.append

    strategy._handle_naked_filled_leg(naked_order, failed_leg_id)

    # Exactly one BACK on the sibling (the other middle leg) — completes the middle,
    # never a SELL, never a second order.
    ensure(len(submitted) == 1)
    order = submitted[0]
    ensure(order.side == OrderSide.BUY)
    ensure(order.instrument_id == sibling.id)
    stats = strategy.get_stats()["live_execution"]
    ensure(stats["unwind_exits"] == 1)
    ensure(stats["naked_flatten_halts"] == 0)
