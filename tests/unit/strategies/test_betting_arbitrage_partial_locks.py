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
Runtime routing for partial-compatible locks.

Mirrors the void-compatible-middle routing tests: the partial-lock gate opens behind
``execute_partial_compatible_locks`` for a promoted (``execution_safe``) partial hedge, with
the partial-lock margin floor above the ordinary arb floor and a half-grade venue guard that
rejects any SX.bet / Polymarket leg (only Cloudbet settles half-lines at half stake).

"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.semantics import RelationshipType
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


def ensure(condition: bool) -> None:  # skipcq
    if not condition:
        raise AssertionError


def _instrument(*, venue: str, outcome: str, line: str) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.5,
        currency=Currency.from_str("USDC"),
        params=f"line={line}",
        handicap=None,
        trading_status="ACTIVE",
    )


def _partial_lock_edge(
    *,
    caveats=("partial_settlement_present", "price_correlation_not_proof", "validate_venue_rules"),
    relationship_type=RelationshipType.PARTIAL_SETTLEMENT_HEDGE.value,
):
    # A promoted partial lock: unlike the void middle, the runtime recogniser requires the
    # promotion tier's proof, so ``execution_safe`` and ``partial_settlement`` are True.
    return SimpleNamespace(
        relationship_type=relationship_type,
        caveats=caveats,
        execution_safe=True,
        partial_settlement=True,
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
        trader_id=TraderId("TESTER-PLOCK"),
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


def test_partial_lock_config_floor_must_exceed_arb_floor():
    # The partial-lock floor must sit strictly above the ordinary arb floor, enforced only
    # when the opt-in is on (mirrors the middle-floor invariant).
    with pytest.raises(ValueError, match="min_partial_lock_profit_margin"):
        BettingArbitrageConfig(
            auto_execute=False,
            execute_partial_compatible_locks=True,
            min_profit_margin=Decimal("0.02"),
            min_partial_lock_profit_margin=Decimal("0.02"),
        )
    # Off by default: an equal floor is fine when the opt-in is off.
    BettingArbitrageConfig(
        auto_execute=False,
        min_profit_margin=Decimal("0.02"),
        min_partial_lock_profit_margin=Decimal("0.02"),
    )


def test_same_venue_partial_lock_staged_under_flag(monkeypatch):
    # Cloudbet-only (half-grade) partial lock clears every live gate: staged, unarmed.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        execute_partial_compatible_locks=True,
        min_partial_lock_profit_margin=Decimal("0.10"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="CLOUDBET", outcome="away", line="0.25")
    _register_edge(strategy, home, away, _partial_lock_edge())
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_block_reasons_for(
        opportunity=opportunity,
        stake_a=Decimal("53.68"),
        stake_b=Decimal("46.32"),
        diagnostics=None,
    )

    ensure(reasons == [])


def test_flag_off_keeps_partial_lock_blocked(monkeypatch):
    # Flag off -> the recogniser stays closed and the edge routes back to the generic
    # (blocked) semantic policy (its price-correlation caveat is dangerous there).
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(execute_partial_compatible_locks=False)
    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="CLOUDBET", outcome="away", line="0.25")
    _register_edge(strategy, home, away, _partial_lock_edge())
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure(reasons == ["semantic_execution_policy_blocked"])


def test_partial_lock_cross_venue_requires_half_grade_venue(monkeypatch):
    # SX.bet grades half-lines as full WON/LOST, so a CLOUDBET+SXBET partial lock is blocked
    # by the half-grade venue guard even though it clears the middle whitelist.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        execute_partial_compatible_locks=True,
        min_partial_lock_profit_margin=Decimal("0.10"),
        execution_venue_mode="cross_venue",
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="SXBET", outcome="away", line="0.25")
    _register_edge(strategy, home, away, _partial_lock_edge())
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure("partial_lock_requires_half_grade_venue" in reasons)


def test_partial_lock_polymarket_leg_blocked(monkeypatch):
    # A Polymarket leg pays a taker fee not refunded on a push, and is not half-grade.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        enabled_venues=frozenset({"CLOUDBET", "POLYMARKET"}),
        execute_partial_compatible_locks=True,
        min_partial_lock_profit_margin=Decimal("0.10"),
        execution_venue_mode="cross_venue",
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="POLYMARKET", outcome="away", line="0.25")
    _register_edge(strategy, home, away, _partial_lock_edge())
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure("partial_lock_polymarket_push_fee" in reasons)


def test_partial_lock_below_floor_blocked(monkeypatch):
    # Margin between the ordinary floor and the higher partial-lock floor -> blocked.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        execute_partial_compatible_locks=True,
        min_profit_margin=Decimal("0.01"),
        min_partial_lock_profit_margin=Decimal("0.20"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="CLOUDBET", outcome="away", line="0.25")
    _register_edge(strategy, home, away, _partial_lock_edge())
    # ~5% margin: clears min_profit_margin but not the 20% partial-lock floor.
    opportunity = _opportunity(home, away, odds_a=Decimal("2.10"), odds_b=Decimal("2.10"))

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure("below_min_partial_lock_profit_margin" in reasons)


def test_unknown_risk_edge_not_recognised_as_partial_lock(monkeypatch):
    # An edge carrying an UNKNOWN settlement risk is not a clean partial lock, so the flag
    # does not open it -> it routes back to the generic (blocked) policy.
    monkeypatch.setenv("BETTING_LIVE_EXECUTION_ARMED", "1")
    strategy = _strategy(
        execute_partial_compatible_locks=True,
        min_partial_lock_profit_margin=Decimal("0.10"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="CLOUDBET", outcome="away", line="0.25")
    edge = _partial_lock_edge(
        caveats=("partial_settlement_present", "unknown_settlement_present"),
    )
    _register_edge(strategy, home, away, edge)
    opportunity = _opportunity(home, away)

    reasons = strategy._live_execution_semantic_block_reasons(opportunity)

    ensure(reasons == ["semantic_execution_policy_blocked"])


def test_partial_lock_staged_with_bet_type_label():
    # A partial-lock-eligible edge is STAGED (unarmed) and labelled PARTIAL_LOCK so the
    # operator approves the break-even-on-partial bet knowingly.
    strategy = _strategy(
        execute_partial_compatible_locks=True,
        min_partial_lock_profit_margin=Decimal("0.10"),
    )
    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="CLOUDBET", outcome="away", line="0.25")
    _register_edge(strategy, home, away, _partial_lock_edge())
    opportunity = _opportunity(home, away)

    record = strategy._store_pending_approval(
        opportunity=opportunity,
        diagnostics=None,
        stake_a=Decimal("53.68"),
        stake_b=Decimal("46.32"),
        expected_profit=Decimal("35.25"),
        now_ns=strategy.clock.timestamp_ns(),
    )

    ensure(record.bet_type == "PARTIAL_LOCK")
    ensure(record.relationship_type == RelationshipType.PARTIAL_SETTLEMENT_HEDGE.value)


def test_deploy_soccer_canary_stages_partial_lock_while_unarmed():
    # The soccer shard sets allow/execute_partial_compatible_locks=true with every execution
    # flag false: the opt-in opens staging only, nothing is armed.
    strategy = _strategy(
        auto_execute=False,
        live_execution_armed=False,
        value_execution_enabled=False,
        allow_partial_compatible_locks=True,
        execute_partial_compatible_locks=True,
        min_profit_margin=Decimal("0.02"),
        min_partial_lock_profit_margin=Decimal("0.025"),
    )
    ensure(strategy._config.auto_execute is False)
    ensure(strategy._config.live_execution_armed is False)
    ensure(strategy._config.value_execution_enabled is False)

    home = _instrument(venue="CLOUDBET", outcome="home", line="0.25")
    away = _instrument(venue="CLOUDBET", outcome="away", line="0.25")
    _register_edge(strategy, home, away, _partial_lock_edge())
    opportunity = _opportunity(home, away)

    record = strategy._store_pending_approval(
        opportunity=opportunity,
        diagnostics=None,
        stake_a=Decimal("53.68"),
        stake_b=Decimal("46.32"),
        expected_profit=Decimal("35.25"),
        now_ns=strategy.clock.timestamp_ns(),
    )

    ensure(record.bet_type == "PARTIAL_LOCK")
    ensure(record.approval_id in strategy._pending_approvals)
