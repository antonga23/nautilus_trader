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
Real-money two-leg arbitrage position tracking, built directly on the native Rust
betting P&L primitives exposed through PyO3.

Why this exists at strategy level rather than in ``Portfolio``: Nautilus ships a tested
bet P&L engine (``Bet`` / ``BetPosition`` / ``calc_bets_pnl``), but ``Portfolio`` only
routes fills through it when the instrument is a ``BettingInstrument``. Our venues use
``CryptoBettingInstrument``, which extends the base ``Instrument``, so the portfolio
bet-position hooks never fire and the engine is dark for us. This tracker feeds the same
``Bet`` primitives directly from ``on_order_filled`` so we recover exact, hand-verifiable
betting P&L for each arbitrage pair.

Joint-payoff model. A same-venue complementary hedge places two BACK bets on the two
mutually exclusive outcomes of one binary market (e.g. HOME -1.5 and AWAY +1.5). The two
bets can never both win, so ``calc_bets_pnl`` / ``BetPosition.add_bet`` -- which assume
every bet is on the *same* selection and sum their win payoffs -- do not model the pair.
Instead we enumerate the outcome space and, per winning outcome, sum each leg's payoff
using the leg's own ``Bet``: a leg whose outcome wins contributes ``outcome_win_payoff``,
a leg whose outcome loses contributes ``outcome_lose_payoff``. Those two methods are
framed relative to the selection the bet is on and already encode BACK vs LAY, so the
joint computation is side-agnostic and never hand-rolls odds math.

"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.core.nautilus_pyo3 import Bet
from nautilus_trader.core.nautilus_pyo3 import BetSide


if TYPE_CHECKING:
    from nautilus_trader.adapters.betting.fx import PortfolioCurrencyPolicy


# Synthetic scenario key for "none of the tracked outcomes win" -- used only while a pair
# has fewer than two distinct outcomes covered (a naked leg), so its downside is surfaced.
_COMPLEMENT_OUTCOME = "__other__"


def _currency_code(value: object) -> str:
    code = getattr(value, "code", None)
    return str(code or value or "").strip().upper()


def bet_side_for_order_side(order_side: object) -> BetSide:
    """
    Map a Nautilus order side onto a betting side.

    A BUY posts (backs) the selection; a SELL lays it. Compared by name so a Cython
    ``OrderSide`` and a PyO3 ``OrderSide`` are both accepted without an enum-type clash.

    """
    name = getattr(order_side, "name", str(order_side)).upper()
    if "BUY" in name:
        return BetSide.BACK
    if "SELL" in name:
        return BetSide.LAY
    raise ValueError(f"Cannot map order side to a bet side: {order_side!r}")


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    as_decimal = getattr(value, "as_decimal", None)
    if callable(as_decimal):
        return as_decimal()
    return Decimal(str(value))


@dataclass
class LegState:
    """
    One leg of an arbitrage pair: every fill accumulates as a native ``Bet`` so partial
    fills and averaged prices are handled by summing per-fill payoffs.
    """

    client_order_id: str
    outcome: str
    side: BetSide
    currency: str = ""
    fills: list[Bet] = field(default_factory=list)

    def add_fill(self, price: object, stake: object) -> None:
        self.fills.append(Bet(_to_decimal(price), _to_decimal(stake), self.side))

    @property
    def filled(self) -> bool:
        return bool(self.fills)

    @property
    def stake(self) -> Decimal:
        return sum((bet.stake for bet in self.fills), Decimal(0))

    @property
    def exposure(self) -> Decimal:
        return sum((bet.exposure() for bet in self.fills), Decimal(0))

    @property
    def win_payoff(self) -> Decimal:
        """
        Net payoff of this leg in the outcome where its selection wins.
        """
        return sum((bet.outcome_win_payoff() for bet in self.fills), Decimal(0))

    @property
    def lose_payoff(self) -> Decimal:
        """
        Net payoff of this leg in an outcome where its selection loses.
        """
        return sum((bet.outcome_lose_payoff() for bet in self.fills), Decimal(0))


@dataclass
class ArbPairState:
    """
    Both legs of one arbitrage pair, keyed internally by leg client order id.
    """

    pair_id: str
    legs: dict[str, LegState] = field(default_factory=dict)
    settled: bool = False
    void: bool = False
    winning_outcome: str | None = None
    realized_pnl: Decimal | None = None
    policy: PortfolioCurrencyPolicy | None = None

    @property
    def filled_legs(self) -> list[LegState]:
        return [leg for leg in self.legs.values() if leg.filled]

    @property
    def base_currency(self) -> str:
        if self.policy is not None:
            return _currency_code(self.policy.base_currency) or "USD"
        return "USD"

    @property
    def is_cross_currency(self) -> bool:
        # Only distinct, known currency codes count: an unknown ("") leg currency must not
        # spuriously mark a single-currency pair as cross-currency (which would drop it
        # into the conversion path and diverge from the pre-PR raw sum).
        return len({leg.currency for leg in self.filled_legs if leg.currency}) > 1

    @property
    def is_fully_hedged(self) -> bool:
        return len({leg.outcome for leg in self.filled_legs}) >= 2

    @property
    def exposure(self) -> Decimal:
        return sum((leg.exposure for leg in self.filled_legs), Decimal(0))

    def outcome_pnls(self) -> dict[str, Decimal | None]:
        """
        P&L in each candidate winning outcome while the pair is open.

        Each covered outcome maps to the joint payoff if that outcome wins, expressed in
        the base currency when the legs settle in different currencies (see
        ``_joint_payoff``). When fewer than two outcomes are covered (a naked single
        leg), a synthetic complement scenario is added so the unhedged downside is
        visible rather than hidden. A ``None`` value marks an outcome whose base-currency
        payoff cannot be computed because a leg currency is not convertible.

        """
        legs = self.filled_legs
        outcomes = {leg.outcome for leg in legs}
        pnls: dict[str, Decimal | None] = {}
        for outcome in outcomes:
            pnls[outcome] = self._joint_payoff(outcome)
        if len(outcomes) < 2:
            pnls[_COMPLEMENT_OUTCOME] = self._joint_payoff(_COMPLEMENT_OUTCOME)
        return pnls

    def _joint_payoff(self, winning_outcome: str) -> Decimal | None:
        """
        Joint two-leg payoff if ``winning_outcome`` wins.

        Same-currency legs (every leg settling in one currency, e.g. USDC vs USDC) sum
        their native payoffs directly, reproducing the single-currency behaviour exactly.
        Cross-currency legs convert each leg payoff into the base currency
        PESSIMISTICALLY by sign, so the floor is a genuine lower bound. A received
        (positive) payoff takes the haircut-reducing ``convert_payoff`` (smallest base
        value, never over-stating the edge); a loss (negative payoff) has its magnitude
        converted with the inflating ``convert`` and re-negated (largest base loss).
        Converting a loss with the reducing haircut would shrink it toward zero, and
        against an identity/base-currency leg (scaled by 1.0) the differing factors could
        flip the worst case positive — a false locked floor on a pair that actually loses.
        ``None`` is returned when a leg currency has no available rate: the pair then
        bears currency risk and must not report a bogus locked floor.

        """
        legs = self.filled_legs
        if not self.is_cross_currency:
            total = Decimal(0)
            for leg in legs:
                total += leg.win_payoff if leg.outcome == winning_outcome else leg.lose_payoff
            return total
        if self.policy is None:
            return None
        total = Decimal(0)
        for leg in legs:
            native = leg.win_payoff if leg.outcome == winning_outcome else leg.lose_payoff
            if native < 0:
                conversion = self.policy.convert(-native, leg.currency)
                if conversion.converted_amount is None:
                    return None
                total -= conversion.converted_amount
            else:
                conversion = self.policy.convert_payoff(native, leg.currency)
                if conversion.converted_amount is None:
                    return None
                total += conversion.converted_amount
        return total

    @property
    def floor_currency_risk(self) -> bool:
        """
        True when an open pair cannot compute a base-currency floor for some outcome.
        """
        if self.settled:
            return False
        pnls = self.outcome_pnls()
        return bool(pnls) and any(v is None for v in pnls.values())

    def guaranteed_pnl(self) -> Decimal | None:
        """
        Worst-case base-currency P&L across outcomes while open (the arb floor).

        None if settled/empty, or if the pair bears unconverted currency risk (some
        outcome payoff cannot be expressed in the base currency) — never a raw sum of
        mismatched currencies.

        """
        if self.settled:
            return None
        pnls = self.outcome_pnls()
        if not pnls or any(v is None for v in pnls.values()):
            return None
        return min(v for v in pnls.values() if v is not None)

    def best_case_pnl(self) -> Decimal | None:
        if self.settled:
            return None
        pnls = self.outcome_pnls()
        if not pnls or any(v is None for v in pnls.values()):
            return None
        return max(v for v in pnls.values() if v is not None)

    def settle(self, winning_outcome: str | None = None, *, void: bool = False) -> Decimal | None:
        """
        Realize base-currency P&L on settlement.

        ``void`` refunds both stakes, so realized P&L is exactly zero. Otherwise realized
        P&L is the base-currency joint payoff for ``winning_outcome`` (an outcome not
        backed by any leg settles every leg at its lose payoff). Cross-currency legs are
        normalised through ``convert_payoff``; a leg with no available rate realizes
        ``None`` (currency-risk-bearing) rather than a raw sum of mismatched currencies.

        """
        self.settled = True
        if void:
            self.void = True
            self.winning_outcome = None
            self.realized_pnl = Decimal(0)
            return self.realized_pnl
        if winning_outcome is None:
            raise ValueError("winning_outcome is required unless the pair is void")
        self.winning_outcome = winning_outcome
        self.realized_pnl = self._joint_payoff(winning_outcome)
        return self.realized_pnl

    def summary(self) -> dict:
        outcome_pnls = {} if self.settled else self.outcome_pnls()
        return {
            "pair_id": self.pair_id,
            "settled": self.settled,
            "void": self.void,
            "fully_hedged": self.is_fully_hedged,
            "cross_currency": self.is_cross_currency,
            "base_currency": self.base_currency,
            "floor_currency_risk": self.floor_currency_risk,
            "winning_outcome": self.winning_outcome,
            "exposure": self.exposure,
            "guaranteed_pnl": self.guaranteed_pnl(),
            "best_case_pnl": self.best_case_pnl(),
            "realized_pnl": self.realized_pnl,
            "outcome_pnls": dict(outcome_pnls),
            "legs": [
                {
                    "client_order_id": leg.client_order_id,
                    "outcome": leg.outcome,
                    "side": str(leg.side),
                    "currency": leg.currency,
                    "fills": len(leg.fills),
                    "stake": leg.stake,
                    "exposure": leg.exposure,
                }
                for leg in self.legs.values()
            ],
        }


class ArbPositionTracker:
    """
    Tracks two-leg arbitrage pairs and their real-money P&L using the native ``Bet``
    engine directly, keyed by the sorted pair of sibling client order ids (the same
    canonical key the strategy uses for unwind bookkeeping).
    """

    def __init__(self, policy: PortfolioCurrencyPolicy | None = None) -> None:
        self._pairs: dict[str, ArbPairState] = {}
        self._leg_to_pair: dict[str, str] = {}
        self._policy = policy

    @staticmethod
    def pair_key(leg_a_id: object, leg_b_id: object) -> str:
        return "|".join(sorted((str(leg_a_id), str(leg_b_id))))

    def _resolve_pair_id(self, client_order_id: str, sibling_id: object) -> str:
        if sibling_id in (None, "", "unknown"):
            return self._leg_to_pair.get(client_order_id, client_order_id)
        return self.pair_key(client_order_id, sibling_id)

    def record_fill(
        self,
        client_order_id: object,
        outcome: object,
        order_side: object,
        last_px: object,
        last_qty: object,
        sibling_id: object = None,
        currency: object = None,
    ) -> ArbPairState:
        """
        Record a single fill (or partial fill) for a leg, accumulating it as a ``Bet``.

        ``currency`` is the leg's settlement currency; a pair whose legs settle in
        different currencies has its joint payoff and floor normalised into the base
        currency of the tracker's ``PortfolioCurrencyPolicy``.

        """
        coid = str(client_order_id)
        pair_id = self._resolve_pair_id(coid, sibling_id)
        pair = self._pairs.get(pair_id)
        if pair is None:
            pair = ArbPairState(pair_id=pair_id, policy=self._policy)
            self._pairs[pair_id] = pair
        self._leg_to_pair[coid] = pair_id
        leg = pair.legs.get(coid)
        if leg is None:
            leg = LegState(
                client_order_id=coid,
                outcome=str(outcome),
                side=bet_side_for_order_side(order_side),
                currency=_currency_code(currency),
            )
            pair.legs[coid] = leg
        leg.add_fill(last_px, last_qty)
        return pair

    def link_leg_to_pair(self, leg_id: object, existing_leg_id: object) -> str | None:
        """
        Attach a not-yet-filled hedging leg to the pair of an already-tracked leg.

        Used when a naked leg is flattened by backing the complementary outcome: the new
        back forms the pair's second outcome, so its incoming fills must accumulate into
        the existing pair rather than open a standalone one. Returns the pair id, or None
        when the existing leg is not tracked.

        """
        pair_id = self._leg_to_pair.get(str(existing_leg_id))
        if pair_id is None:
            return None
        self._leg_to_pair[str(leg_id)] = pair_id
        return pair_id

    def settle(
        self,
        pair_id: str,
        winning_outcome: str | None = None,
        *,
        void: bool = False,
    ) -> Decimal | None:
        pair = self._pairs[pair_id]
        return pair.settle(winning_outcome, void=void)

    def settle_by_leg(
        self,
        client_order_id: object,
        winning_outcome: str | None = None,
        *,
        void: bool = False,
    ) -> Decimal | None:
        pair_id = self._leg_to_pair[str(client_order_id)]
        return self.settle(pair_id, winning_outcome, void=void)

    def pair(self, pair_id: str) -> ArbPairState | None:
        return self._pairs.get(pair_id)

    def pair_for_leg(self, client_order_id: object) -> ArbPairState | None:
        pair_id = self._leg_to_pair.get(str(client_order_id))
        return self._pairs.get(pair_id) if pair_id is not None else None

    def summary(self) -> dict:
        """
        Compact snapshot for the runtime probe / nodeops dashboard.
        """
        pairs = [pair.summary() for pair in self._pairs.values()]
        open_pairs = [p for p in self._pairs.values() if not p.settled]
        realized_total = sum(
            (p.realized_pnl for p in self._pairs.values() if p.realized_pnl is not None),
            Decimal(0),
        )
        guaranteed_total = sum(
            (g for p in open_pairs if (g := p.guaranteed_pnl()) is not None),
            Decimal(0),
        )
        exposure_total = sum((p.exposure for p in open_pairs), Decimal(0))
        currency_risk_pairs = sum(1 for p in open_pairs if p.floor_currency_risk)
        return {
            "pairs_tracked": len(self._pairs),
            "pairs_open": len(open_pairs),
            "open_exposure": exposure_total,
            "open_guaranteed_pnl": guaranteed_total,
            "open_currency_risk_pairs": currency_risk_pairs,
            "realized_pnl": realized_total,
            "pairs": pairs,
        }
