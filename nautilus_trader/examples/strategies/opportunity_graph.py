# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Persistent opportunity graph for betting arbitrage strategies.

The graph keeps betting-domain hedge topology out of the quote hot path. Market matching rules
still come from :class:`MarketMatcher`, but they are applied when instruments are loaded or added.
Quote ticks only update node state and re-evaluate edges adjacent to the changed instrument.

"""

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import HedgeCandidate
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.model.data import QuoteTick


@dataclass(frozen=True)
class OpportunityNode:
    """
    A graph node representing one venue-specific tradable betting instrument.
    """

    node_id: str
    instrument_id: str
    venue: str
    canonical_event_key: str
    canonical_outcome_key: str
    event_id: str
    event_name: str
    market_id: str
    market_type: str
    market_name: str
    outcome: str
    params: str
    handicap: float | None
    live: bool
    two_way_market: bool
    instrument: CryptoBettingInstrument


@dataclass
class OpportunityEdge:
    """
    A precomputed hedge/opportunity relationship between two nodes.
    """

    edge_id: str
    source_node_id: str
    target_node_id: str
    hedge_type: str
    confidence: float
    same_venue: bool
    market_relationship_type: str
    push_capable: bool
    execution_safe: bool
    rule_id: str | None = None
    template_id: str | None = None
    relationship_type: str | None = None
    caveats: tuple[str, ...] = ()
    promotion_status: str | None = None
    safety_tier: str | None = None
    same_venue_execution_eligible: bool = False
    void_capable: bool = False
    partial_settlement: bool = False
    last_margin: Decimal | None = None
    last_evaluated_ns: int | None = None
    last_updated_ns: int | None = None


@dataclass
class QuoteState:
    """
    Latest quote state for a graph node.
    """

    node_id: str
    quote: QuoteTick
    odds: Decimal
    bid_odds: Decimal
    ask_odds: Decimal
    received_ns: int
    exchange_ts_ns: int


@dataclass(frozen=True)
class OpportunityCandidate:
    """
    A computed candidate produced by evaluating one graph edge.
    """

    edge: OpportunityEdge
    opportunity: ArbitrageOpportunity
    quote_a: QuoteState
    quote_b: QuoteState
    updated_node_id: str


class OpportunityGraph:
    """
    Maintains betting arbitrage topology and quote state.

    The graph distinguishes a semantic betting outcome from a venue-specific tradable
    instrument. Nodes are keyed by Nautilus instrument id; edges encode valid hedge
    relationships discovered by the existing MarketMatcher.

    """

    def __init__(self, matcher: MarketMatcher, *, include_cross_venue: bool = True) -> None:
        self._matcher = matcher
        self._include_cross_venue = include_cross_venue
        self.nodes_by_id: dict[str, OpportunityNode] = {}
        self.edges_by_id: dict[str, OpportunityEdge] = {}
        self.edge_ids_by_node_id: dict[str, set[str]] = {}
        self.quotes_by_node_id: dict[str, QuoteState] = {}

    @property
    def node_count(self) -> int:
        return len(self.nodes_by_id)

    @property
    def edge_count(self) -> int:
        return len(self.edges_by_id)

    @property
    def quote_state_count(self) -> int:
        return len(self.quotes_by_node_id)

    def clear(self) -> None:
        self.nodes_by_id.clear()
        self.edges_by_id.clear()
        self.edge_ids_by_node_id.clear()
        self.quotes_by_node_id.clear()

    def build(self, instruments: list[CryptoBettingInstrument]) -> None:
        """
        Build graph topology from a complete instrument snapshot.
        """
        self.clear()
        for instrument in instruments:
            node = self._node_from_instrument(instrument)
            self.nodes_by_id[node.node_id] = node
            self.edge_ids_by_node_id.setdefault(node.node_id, set())

        for instrument in instruments:
            self._add_edges_for_instrument(instrument, instruments)

    def add_instrument(self, instrument: CryptoBettingInstrument) -> bool:
        """
        Add one instrument and incrementally connect it to existing topology.
        """
        node = self._node_from_instrument(instrument)
        if node.node_id in self.nodes_by_id:
            return False

        existing = [existing_node.instrument for existing_node in self.nodes_by_id.values()]
        self.nodes_by_id[node.node_id] = node
        self.edge_ids_by_node_id.setdefault(node.node_id, set())

        candidates = [*existing, instrument]
        self._add_edges_for_instrument(instrument, candidates)
        for existing_instrument in existing:
            self._add_edges_for_instrument(existing_instrument, candidates)
        return True

    def update_quote(
        self,
        quote: QuoteTick,
        *,
        odds: Decimal,
        received_ns: int,
    ) -> QuoteState | None:
        """
        Update latest quote state for a graph node.
        """
        node_id = str(quote.instrument_id)
        if node_id not in self.nodes_by_id:
            return None

        state = QuoteState(
            node_id=node_id,
            quote=quote,
            odds=odds,
            bid_odds=quote.bid_price.as_decimal(),
            ask_odds=quote.ask_price.as_decimal(),
            received_ns=received_ns,
            exchange_ts_ns=int(quote.ts_event),
        )
        self.quotes_by_node_id[node_id] = state
        return state

    def evaluate_updated_node(
        self,
        node_id: str,
        *,
        min_profit_margin: Decimal,
        now_ns: int,
    ) -> list[OpportunityCandidate]:
        """
        Evaluate only edges connected to the updated node.
        """
        updated_quote = self.quotes_by_node_id.get(node_id)
        if updated_quote is None:
            return []

        candidates: list[OpportunityCandidate] = []
        for edge_id in self.edge_ids_by_node_id.get(node_id, set()):
            edge = self.edges_by_id[edge_id]
            other_node_id = (
                edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
            )
            other_quote = self.quotes_by_node_id.get(other_node_id)
            if other_quote is None:
                continue

            opportunity = self._check_edge_opportunity(
                edge=edge,
                source_quote=updated_quote,
                target_quote=other_quote,
            )
            if opportunity is None:
                continue

            edge.last_margin = opportunity.profit_margin
            edge.last_evaluated_ns = now_ns
            edge.last_updated_ns = now_ns
            if opportunity.profit_margin < min_profit_margin:
                continue

            candidates.append(
                OpportunityCandidate(
                    edge=edge,
                    opportunity=opportunity,
                    quote_a=updated_quote,
                    quote_b=other_quote,
                    updated_node_id=node_id,
                ),
            )

        return candidates

    def connected_edge_count(self, node_id: str) -> int:
        return len(self.edge_ids_by_node_id.get(node_id, set()))

    def stats(self) -> dict[str, int]:
        return {
            "nodes": self.node_count,
            "edges": self.edge_count,
            "quote_states": self.quote_state_count,
        }

    def _add_edges_for_instrument(
        self,
        instrument: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
    ) -> None:
        hedges = self._matcher.find_hedges(
            instrument=instrument,
            candidates=candidates,
            include_cross_venue=self._include_cross_venue,
        )
        for hedge in hedges:
            self._upsert_edge(
                source=instrument,
                target=hedge.instrument,
                hedge=hedge,
            )

    def _upsert_edge(
        self,
        *,
        source: CryptoBettingInstrument,
        target: CryptoBettingInstrument,
        hedge: HedgeCandidate,
    ) -> None:
        source_id = str(source.id)
        target_id = str(target.id)
        if source_id == target_id:
            return

        edge_id = self._edge_id(source_id, target_id)
        existing = self.edges_by_id.get(edge_id)
        if existing is not None:
            if hedge.confidence > existing.confidence:
                existing.hedge_type = hedge.match_type
                existing.confidence = hedge.confidence
                existing.source_node_id = source_id
                existing.target_node_id = target_id
                existing.rule_id = hedge.rule_id
                existing.template_id = hedge.template_id
                existing.relationship_type = hedge.relationship_type
                existing.caveats = hedge.caveats
                existing.promotion_status = hedge.promotion_status
                existing.safety_tier = hedge.safety_tier
                existing.push_capable = hedge.push_capable
                existing.void_capable = hedge.push_capable
                existing.partial_settlement = hedge.partial_settlement
                existing.execution_safe = hedge.execution_safe
                existing.same_venue_execution_eligible = hedge.same_venue_execution_eligible
            return

        edge = OpportunityEdge(
            edge_id=edge_id,
            source_node_id=source_id,
            target_node_id=target_id,
            hedge_type=hedge.match_type,
            confidence=hedge.confidence,
            same_venue=source.venue_name == target.venue_name,
            market_relationship_type=(
                "same_market" if source.market_name == target.market_name else "cross_market"
            ),
            push_capable=hedge.push_capable,
            execution_safe=hedge.execution_safe,
            rule_id=hedge.rule_id,
            template_id=hedge.template_id,
            relationship_type=hedge.relationship_type,
            caveats=hedge.caveats,
            promotion_status=hedge.promotion_status,
            safety_tier=hedge.safety_tier,
            same_venue_execution_eligible=hedge.same_venue_execution_eligible,
            void_capable=hedge.push_capable,
            partial_settlement=hedge.partial_settlement,
        )
        self.edges_by_id[edge_id] = edge
        self.edge_ids_by_node_id.setdefault(source_id, set()).add(edge_id)
        self.edge_ids_by_node_id.setdefault(target_id, set()).add(edge_id)

    def _check_edge_opportunity(
        self,
        *,
        edge: OpportunityEdge,
        source_quote: QuoteState,
        target_quote: QuoteState,
    ) -> ArbitrageOpportunity | None:
        if not edge.execution_safe:
            return None

        source_node = self.nodes_by_id[source_quote.node_id]
        target_node = self.nodes_by_id[target_quote.node_id]
        return self._matcher.check_arbitrage(
            source_node.instrument,
            target_node.instrument,
            odds_a=source_quote.odds,
            odds_b=target_quote.odds,
        )

    @classmethod
    def _node_from_instrument(cls, instrument: CryptoBettingInstrument) -> OpportunityNode:
        info = cls._safe_attr(instrument, "info", {})
        if not isinstance(info, dict):
            info = {}

        event_key_func = cls._safe_attr(instrument, "event_key", None)
        if callable(event_key_func):
            canonical_event_key = str(event_key_func(include_start_time=True))
        else:
            canonical_event_key = str(instrument.id)

        selection_key_func = cls._safe_attr(instrument, "selection_key", None)
        if callable(selection_key_func):
            selection_key = str(selection_key_func())
        else:
            selection_key = str(cls._safe_attr(instrument, "outcome", "unknown"))

        market_name = str(cls._safe_attr(instrument, "market_name", "unknown"))
        market_type = str(cls._safe_attr(instrument, "market_type", market_name))
        params = str(cls._safe_attr(instrument, "params", ""))
        handicap_value = cls._safe_attr(instrument, "handicap", None)
        handicap = float(handicap_value) if isinstance(handicap_value, int | float) else None
        canonical_outcome_key = "|".join(
            [
                canonical_event_key,
                market_type or market_name,
                params,
                selection_key,
            ],
        )
        return OpportunityNode(
            node_id=str(instrument.id),
            instrument_id=str(instrument.id),
            venue=str(instrument.id.venue),
            canonical_event_key=canonical_event_key,
            canonical_outcome_key=canonical_outcome_key,
            event_id=str(cls._safe_attr(instrument, "event_id", instrument.id.symbol.value)),
            event_name=str(cls._safe_attr(instrument, "event_name", instrument.id.symbol.value)),
            market_id=str(
                cls._safe_attr(
                    instrument,
                    "market_id",
                    cls._safe_attr(instrument, "event_id", instrument.id.symbol.value),
                )
                or cls._safe_attr(instrument, "event_id", instrument.id.symbol.value),
            ),
            market_type=market_type,
            market_name=market_name,
            outcome=str(cls._safe_attr(instrument, "outcome", "unknown")),
            params=params,
            handicap=handicap,
            live=cls._safe_attr(instrument, "live", False) is True,
            two_way_market=info.get("is_two_way_market") is True,
            instrument=instrument,
        )

    @staticmethod
    def _safe_attr(
        instrument: CryptoBettingInstrument,
        name: str,
        default: object,
    ) -> object:
        try:
            return getattr(instrument, name)
        except AttributeError:
            return default

    @staticmethod
    def _edge_id(source_id: str, target_id: str) -> str:
        return "|".join(sorted([source_id, target_id]))
