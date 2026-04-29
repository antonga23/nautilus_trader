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
# skipcq: PYL-E0611, PYL-R0902, PYL-R0913
# pylint: disable=no-name-in-module,too-many-arguments,too-many-instance-attributes
"""
Persistent opportunity graph for betting arbitrage strategies.

The graph keeps betting-domain hedge topology out of the quote hot path. Market matching rules
still come from :class:`MarketMatcher`, but they are applied when instruments are loaded or added.
Quote ticks only update node state and re-evaluate edges adjacent to the changed instrument.

"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import os
from typing import Any

from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import HedgeCandidate
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.semantics import MarketNormalizer
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SelectionPattern
from nautilus_trader.model.data import QuoteTick

_OPPORTUNITY_GRAPH_CORE_CLS: Any | None
try:
    from nautilus_trader.core.nautilus_pyo3 import (
        OpportunityGraphCore as _pyo3_opportunity_graph_core_cls,
    )
except (ImportError, ModuleNotFoundError):
    _OPPORTUNITY_GRAPH_CORE_CLS = None
else:
    _OPPORTUNITY_GRAPH_CORE_CLS = _pyo3_opportunity_graph_core_cls


FastCandidateSnapshot = tuple[
    str,
    str,
    str,
    str,
    float,
    float,
    float,
    float,
    int,
    int,
    str,
    bool,
]


@dataclass(frozen=True)
# skipcq: PYL-R0902
class OpportunityNode:  # skipcq
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
# skipcq: PYL-R0902
class OpportunityEdge:  # skipcq
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

    def __init__(
        self,
        matcher: MarketMatcher,
        *,
        include_cross_venue: bool = True,
        engine: str = "auto",
    ) -> None:
        """
        Initialize the graph matcher and optional Rust engine.
        """
        env_engine = os.getenv("NAUTILUS_BETTING_OPPORTUNITY_GRAPH_ENGINE", "").strip().lower()
        if env_engine:
            engine = env_engine
        if engine not in {"auto", "python", "rust", "semantic_rust"}:
            msg = f"Invalid opportunity graph engine: {engine}"
            raise ValueError(msg)
        if engine in {"rust", "semantic_rust"} and _OPPORTUNITY_GRAPH_CORE_CLS is None:
            msg = "Rust OpportunityGraphCore is unavailable"
            raise ImportError(msg)

        self._matcher = matcher
        self._include_cross_venue = include_cross_venue
        self._engine = engine
        use_rust_core = engine in {"auto", "rust", "semantic_rust"}
        self._rust_core: Any | None = (
            _OPPORTUNITY_GRAPH_CORE_CLS(include_cross_venue, matcher.min_confidence)
            if use_rust_core and _OPPORTUNITY_GRAPH_CORE_CLS is not None
            else None
        )
        if engine == "semantic_rust" and not self._rust_core_supports_semantic_topology():
            msg = "Rust OpportunityGraphCore semantic topology API is unavailable"
            raise ImportError(msg)
        self._topology_source = "python"
        self._semantic_template_count = 0
        self._rust_semantic_templates_loaded = False
        self.nodes_by_id: dict[str, OpportunityNode] = {}
        self.edges_by_id: dict[str, OpportunityEdge] = {}
        self.edge_ids_by_node_id: dict[str, set[str]] = {}
        self.quotes_by_node_id: dict[str, QuoteState] = {}

    @property
    def node_count(self) -> int:
        """
        Return the number of indexed opportunity graph nodes.
        """
        return len(self.nodes_by_id)

    @property
    def edge_count(self) -> int:
        """
        Return the number of hedge relationships currently tracked.
        """
        return len(self.edges_by_id)

    @property
    def quote_state_count(self) -> int:
        """
        Return the number of nodes with an active quote snapshot.
        """
        if self._rust_core is not None:
            return self._rust_core.quote_state_count()
        return len(self.quotes_by_node_id)

    def clear(self) -> None:
        """
        Reset all graph topology and cached quote state.
        """
        if self._rust_core is not None:
            self._rust_core.clear()
        self.nodes_by_id.clear()
        self.edges_by_id.clear()
        self.edge_ids_by_node_id.clear()
        self.quotes_by_node_id.clear()

    def build(self, instruments: list[CryptoBettingInstrument]) -> None:
        """
        Build graph topology from a complete instrument snapshot.
        """
        self.clear()
        rust_nodes: list[dict[str, object]] = []
        for instrument in instruments:
            node = self._node_from_instrument(instrument)
            self.nodes_by_id[node.node_id] = node
            self.edge_ids_by_node_id.setdefault(node.node_id, set())
            rust_nodes.append(self._node_payload_from_node(node, instrument))

        semantic_templates = self._semantic_template_payloads()
        if self._rust_core is not None and self._should_use_semantic_rust(semantic_templates):
            self._rust_core.build_semantic(rust_nodes, semantic_templates)
            self._rust_semantic_templates_loaded = True
            self._semantic_template_count = len(semantic_templates)
            self._topology_source = "rust_semantic"
            self._sync_edges_from_rust()
            return

        if self._rust_core is not None and self._should_use_legacy_rust():
            self._rust_core.build(rust_nodes)
            self._rust_semantic_templates_loaded = False
            self._semantic_template_count = 0
            self._topology_source = "rust_legacy"
            self._sync_edges_from_rust()
            return

        self._topology_source = "python"
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

        if self._rust_core is not None and (
            self._should_use_semantic_rust(self._semantic_template_payloads())
            or self._should_use_legacy_rust()
        ):
            payload = self._node_payload_from_node(node, instrument)
            semantic_templates = self._semantic_template_payloads()
            if self._should_use_semantic_rust(semantic_templates):
                self._ensure_rust_semantic_templates_loaded(semantic_templates)
                added = self._rust_core.add_instrument_semantic(payload)
                self._topology_source = "rust_semantic"
            else:
                added = self._rust_core.add_instrument(payload)
                self._topology_source = "rust_legacy"
            if added:
                self._sync_edges_from_rust()
            else:
                self.nodes_by_id.pop(node.node_id, None)
                self.edge_ids_by_node_id.pop(node.node_id, None)
            return added

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
        if self._rust_core is not None:
            self._rust_core.update_quote(
                node_id,
                float(odds),
                received_ns,
                int(quote.ts_event),
            )
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

        if self._rust_core is not None:
            snapshots = self._rust_core.evaluate_updated_node(
                node_id,
                float(min_profit_margin),
                now_ns,
            )
            return self._candidates_from_rust_snapshots(
                snapshots,
                min_profit_margin=min_profit_margin,
                now_ns=now_ns,
            )

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

    # skipcq: PYL-R0913
    def update_quote_and_evaluate(
        self,
        quote: QuoteTick,
        *,
        odds: Decimal,
        received_ns: int,
        min_profit_margin: Decimal,
        now_ns: int,
    ) -> tuple[QuoteState | None, list[OpportunityCandidate]]:
        """
        Update latest quote state and evaluate affected graph edges in one operation.
        """
        if self._rust_core is None:
            quote_state = self.update_quote(quote, odds=odds, received_ns=received_ns)
            if quote_state is None:
                return None, []
            return quote_state, self.evaluate_updated_node(
                str(quote.instrument_id),
                min_profit_margin=min_profit_margin,
                now_ns=now_ns,
            )

        node_id = str(quote.instrument_id)
        if node_id not in self.nodes_by_id:
            return None, []

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
        snapshots = self._rust_core.update_quote_and_evaluate(
            node_id,
            float(odds),
            received_ns,
            int(quote.ts_event),
            float(min_profit_margin),
            now_ns,
        )
        return state, self._candidates_from_rust_snapshots(
            snapshots,
            min_profit_margin=min_profit_margin,
            now_ns=now_ns,
        )

    # skipcq: PYL-R0913
    def update_quote_and_scan_fast(
        self,
        quote: QuoteTick,
        *,
        odds: Decimal,
        received_ns: int,
        min_profit_margin: Decimal,
        now_ns: int,
    ) -> tuple[bool, list[FastCandidateSnapshot]] | None:
        """
        Update quote state and return compact Rust scan results for strategy hot paths.
        """
        if self._rust_core is None:
            return None

        node_id = str(quote.instrument_id)
        if node_id not in self.nodes_by_id:
            return False, []

        snapshots = self._rust_core.update_quote_and_scan_fast(
            node_id,
            float(odds),
            received_ns,
            int(quote.ts_event),
            float(min_profit_margin),
            now_ns,
        )
        return True, [
            snapshot
            for snapshot in snapshots
            if ((edge := self.edges_by_id.get(snapshot[0])) is not None and edge.execution_safe)
        ]

    def connected_edge_count(self, node_id: str) -> int:
        """
        Return the number of hedge edges incident to the given node.
        """
        return len(self.edge_ids_by_node_id.get(node_id, set()))

    @property
    def connected_node_count(self) -> int:
        return sum(1 for edge_ids in self.edge_ids_by_node_id.values() if edge_ids)

    def stats(self) -> dict[str, int]:
        """
        Return lightweight topology counters for diagnostics and tests.
        """
        return {
            "nodes": self.node_count,
            "edges": self.edge_count,
            "quote_states": self.quote_state_count,
            "connected_nodes": self.connected_node_count,
            "rust_enabled": int(self._rust_core is not None),
            "semantic_template_count": self._semantic_template_count,
        }

    @property
    def graph_engine(self) -> str:
        return "rust" if self._rust_core is not None else "python"

    @property
    def topology_source(self) -> str:
        return self._topology_source

    @property
    def semantic_template_count(self) -> int:
        return self._semantic_template_count

    def _sync_edges_from_rust(self) -> None:
        if self._rust_core is None:
            return
        self.edges_by_id.clear()
        self.edge_ids_by_node_id = {node_id: set() for node_id in self.nodes_by_id}
        for snapshot in self._rust_core.edge_snapshots():
            (
                edge_id,
                source_node_id,
                target_node_id,
                hedge_type,
                confidence,
                same_venue,
                raw_market_relationship_type,
                rust_push_capable,
                rust_execution_safe,
                last_margin,
                last_evaluated_ns,
                last_updated_ns,
            ) = snapshot[:12]
            metadata = self._rust_edge_metadata(raw_market_relationship_type)
            if not metadata and len(snapshot) > 12:
                metadata = self._rust_edge_metadata(snapshot[12])
            market_relationship_type = (
                self._metadata_str(metadata, "market_relationship_type")
                or raw_market_relationship_type
            )
            template_id = self._metadata_str(metadata, "template_id")
            relationship_type = self._metadata_str(metadata, "relationship_type")
            promotion_status = self._metadata_str(metadata, "promotion_status")
            safety_tier = self._metadata_str(metadata, "safety_tier")
            same_venue_execution_eligible = bool(
                metadata.get("same_venue_execution_eligible"),
            )
            partial_settlement = bool(metadata.get("partial_settlement"))
            caveats = self._metadata_str_tuple(metadata, "caveats")
            source_node = self.nodes_by_id.get(source_node_id)
            target_node = self.nodes_by_id.get(target_node_id)
            if source_node is None or target_node is None:
                continue

            hedge = self._best_public_hedge_candidate(
                source_node.instrument,
                target_node.instrument,
            )
            if hedge is None and not template_id:
                continue

            edge = OpportunityEdge(
                edge_id=edge_id,
                source_node_id=source_node_id,
                target_node_id=target_node_id,
                hedge_type=hedge.match_type
                if hedge is not None and hedge.match_type
                else hedge_type,
                confidence=hedge.confidence
                if hedge is not None and hedge.confidence
                else confidence,
                same_venue=same_venue,
                market_relationship_type=market_relationship_type,
                push_capable=hedge.push_capable if hedge is not None else rust_push_capable,
                execution_safe=(hedge.execution_safe if hedge is not None else rust_execution_safe),
                rule_id=hedge.rule_id if hedge is not None else None,
                template_id=hedge.template_id if hedge is not None else template_id,
                relationship_type=(
                    hedge.relationship_type if hedge is not None else relationship_type
                ),
                caveats=hedge.caveats if hedge is not None else caveats,
                promotion_status=(
                    hedge.promotion_status if hedge is not None else promotion_status
                ),
                safety_tier=hedge.safety_tier if hedge is not None else safety_tier,
                same_venue_execution_eligible=(
                    hedge.same_venue_execution_eligible
                    if hedge is not None
                    else same_venue_execution_eligible
                ),
                void_capable=hedge.push_capable if hedge is not None else rust_push_capable,
                partial_settlement=(
                    hedge.partial_settlement if hedge is not None else partial_settlement
                ),
                last_margin=Decimal(str(last_margin)) if last_margin is not None else None,
                last_evaluated_ns=last_evaluated_ns,
                last_updated_ns=last_updated_ns,
            )
            self.edges_by_id[edge_id] = edge
            self.edge_ids_by_node_id.setdefault(source_node_id, set()).add(edge_id)
            self.edge_ids_by_node_id.setdefault(target_node_id, set()).add(edge_id)

    def _candidates_from_rust_snapshots(
        self,
        snapshots: list[tuple[str, str, str, float, int, int]],
        *,
        min_profit_margin: Decimal,
        now_ns: int,
    ) -> list[OpportunityCandidate]:
        candidates: list[OpportunityCandidate] = []
        for edge_id, source_node_id, target_node_id, _, _, _ in snapshots:
            edge = self.edges_by_id.get(edge_id)
            if edge is None or not edge.execution_safe:
                continue
            source_quote = self.quotes_by_node_id.get(source_node_id)
            target_quote = self.quotes_by_node_id.get(target_node_id)
            if source_quote is None or target_quote is None:
                continue

            opportunity = self._make_opportunity_from_quotes(
                source_quote=source_quote,
                target_quote=target_quote,
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
                    quote_a=source_quote,
                    quote_b=target_quote,
                    updated_node_id=source_node_id,
                ),
            )
        return candidates

    @staticmethod
    def _rust_edge_metadata(raw: object) -> dict[str, object]:
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

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
        seen_target_ids: set[str] = set()
        for hedge in hedges:
            normalized_hedge = self._normalize_public_hedge(instrument, hedge)
            if normalized_hedge is None:
                continue
            seen_target_ids.add(str(normalized_hedge.instrument.id))
            self._upsert_edge(
                source=instrument,
                target=normalized_hedge.instrument,
                hedge=normalized_hedge,
            )

        for candidate in candidates:
            if candidate.id == instrument.id or str(candidate.id) in seen_target_ids:
                continue
            if not self._include_cross_venue and candidate.venue_name != instrument.venue_name:
                continue
            if not self._matcher._is_hedge_event_match(instrument, candidate, candidates):
                continue
            if self._matcher._is_same_market_hedge(instrument, candidate):
                continue

            legacy_hedge = self._legacy_cross_market_candidate(instrument, candidate)
            if legacy_hedge is None:
                continue

            self._upsert_edge(
                source=instrument,
                target=candidate,
                hedge=legacy_hedge,
            )

    def _normalize_public_hedge(
        self,
        source: CryptoBettingInstrument,
        hedge: HedgeCandidate,
    ) -> HedgeCandidate | None:
        if (
            hedge.relationship_type == "EQUIVALENT_SELECTION"
            and not hedge.same_venue_execution_eligible
        ):
            return None
        if self._matcher._is_same_market_hedge(source, hedge.instrument) and not hedge.push_capable:
            return self._matcher._legacy_same_market_candidate(hedge.instrument)
        return hedge

    def _public_hedge_candidate(
        self,
        source: CryptoBettingInstrument,
        target: CryptoBettingInstrument,
    ) -> HedgeCandidate | None:
        semantic_hedge = self._matcher._semantic_hedge_candidate(source, target)
        if semantic_hedge is not None:
            return self._normalize_public_hedge(source, semantic_hedge)
        if self._matcher._is_same_market_hedge(source, target):
            return self._matcher._legacy_same_market_candidate(target)
        return self._legacy_cross_market_candidate(source, target)

    def _best_public_hedge_candidate(
        self,
        source: CryptoBettingInstrument,
        target: CryptoBettingInstrument,
    ) -> HedgeCandidate | None:
        forward = self._public_hedge_candidate(source, target)
        reverse = self._public_hedge_candidate(target, source)
        if reverse is None:
            return forward
        if forward is None or reverse.confidence > forward.confidence:
            return reverse
        return forward

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

    def _make_opportunity_from_quotes(
        self,
        *,
        source_quote: QuoteState,
        target_quote: QuoteState,
    ) -> ArbitrageOpportunity | None:
        source_node = self.nodes_by_id[source_quote.node_id]
        target_node = self.nodes_by_id[target_quote.node_id]
        source_instrument = source_node.instrument
        target_instrument = target_node.instrument
        market_a = MarketType.from_string(source_instrument.market_name)
        market_b = MarketType.from_string(target_instrument.market_name)
        if (
            market_a in MarketMatcher.PUSH_CAPABLE_MARKETS
            or market_b in MarketMatcher.PUSH_CAPABLE_MARKETS
        ):
            return None

        probability_a = Decimal(1) / source_quote.odds
        probability_b = Decimal(1) / target_quote.odds
        total_probability = probability_a + probability_b
        profit_margin = (Decimal(1) / total_probability) - Decimal(1)
        is_same_venue = source_instrument.venue_name == target_instrument.venue_name
        if source_instrument.market_name == target_instrument.market_name:
            match_type = "same_market"
        elif is_same_venue:
            match_type = "cross_market"
        else:
            match_type = "cross_venue"

        return ArbitrageOpportunity(
            instrument_a=source_instrument,
            instrument_b=target_instrument,
            probability_a=probability_a,
            probability_b=probability_b,
            total_probability=total_probability,
            profit_margin=profit_margin,
            odds_a=source_quote.odds,
            odds_b=target_quote.odds,
            is_same_venue=is_same_venue,
            match_type=match_type,
        )

    def _legacy_cross_market_candidate(
        self,
        source: CryptoBettingInstrument,
        target: CryptoBettingInstrument,
    ) -> HedgeCandidate | None:
        market_a = MarketType.from_string(source.market_name)
        market_b = MarketType.from_string(target.market_name)
        outcome_a = Outcome.from_string(source.outcome)
        outcome_b = Outcome.from_string(target.outcome)
        confidence = self._matcher._calculate_cross_market_confidence(
            market_a,
            market_b,
            outcome_a,
            outcome_b,
            source,
            target,
        )
        if confidence < self._matcher.min_confidence:
            return None

        return HedgeCandidate(
            instrument=target,
            match_type="cross_market",
            confidence=confidence,
            relationship_type="COMPLEMENTARY_COVERAGE",
            execution_safe=True,
            same_venue_execution_eligible=False,
            safety_tier="EXECUTION_SAFE",
            promotion_status="PROMOTED",
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

    @classmethod
    def _node_payload_from_node(
        cls,
        node: OpportunityNode,
        instrument: CryptoBettingInstrument,
    ) -> dict[str, object]:
        event_key_func = cls._safe_attr(instrument, "event_key", None)
        if callable(event_key_func):
            event_key_no_time = str(event_key_func(include_start_time=False))
        else:
            event_key_no_time = node.canonical_event_key

        selection_key_func = cls._safe_attr(instrument, "selection_key", None)
        if callable(selection_key_func):
            selection_key = str(selection_key_func())
        else:
            selection_key = node.outcome.lower().replace(" ", "_").replace("-", "_")

        raw_market_type = str(cls._safe_attr(instrument, "market_type", ""))
        market_type = MarketType.from_string(raw_market_type or node.market_name).value
        semantic_payload = cls._semantic_node_payload(instrument)

        return {
            "node_id": node.node_id,
            "venue": node.venue,
            "event_id": node.event_id,
            "event_key_no_time": event_key_no_time,
            "market_name": node.market_name,
            "market_type": market_type,
            "outcome": Outcome.from_string(node.outcome).value,
            "selection_key": selection_key,
            "params": node.params,
            "handicap": node.handicap,
            "start_time_ns": cls._start_time_ns(instrument),
            "two_way_market": node.two_way_market,
            **semantic_payload,
        }

    @classmethod
    def _semantic_node_payload(cls, instrument: CryptoBettingInstrument) -> dict[str, object]:
        try:
            normalized = MarketNormalizer.normalize(instrument)
        except (AttributeError, TypeError, ValueError):
            raw_market_type = str(cls._safe_attr(instrument, "market_type", ""))
            market_type = MarketType.from_string(
                raw_market_type or str(cls._safe_attr(instrument, "market_name", "")),
            ).value
            return {
                "semantic_sport": str(cls._safe_attr(instrument, "sport_name", "")).lower(),
                "semantic_scope": "full_time",
                "semantic_market_type": market_type,
                "semantic_market_family": market_type,
                "semantic_selection": Outcome.from_string(
                    str(cls._safe_attr(instrument, "outcome", "unknown")),
                ).value,
                "semantic_params_key": str(cls._safe_attr(instrument, "params", "")),
            }
        return {
            "semantic_sport": normalized.sport,
            "semantic_scope": normalized.scope,
            "semantic_market_type": normalized.market_type,
            "semantic_market_family": normalized.market_family,
            "semantic_selection": normalized.selection,
            "semantic_params_key": cls._params_key(normalized.params),
        }

    def _semantic_template_payloads(self) -> list[dict[str, object]]:
        rule_store = self._semantic_rule_store()
        if rule_store is None or not hasattr(rule_store, "list_promoted_template_ids"):
            return []

        payloads: list[dict[str, object]] = []
        for template_id in rule_store.list_promoted_template_ids():
            template = rule_store.load_promoted_template(template_id)
            if template is None:
                continue
            payloads.append(
                {
                    "template_id": template.template_id,
                    "relationship_type": template.relationship_type,
                    "pattern_a": self._semantic_pattern_payload(template.pattern_a),
                    "pattern_b": self._semantic_pattern_payload(template.pattern_b),
                    "confidence": template.confidence,
                    "provider_scope": list(template.provider_scope),
                    "venue_agnostic": template.venue_agnostic,
                    "safety_tier": template.safety_tier,
                    "promotion_status": template.promotion_status,
                    "caveats": list(template.caveats),
                    "push_capable": template.has_void,
                    "partial_settlement": template.has_partial,
                    "execution_safe": template.execution_safe,
                    "same_venue_execution_eligible": template.same_venue_execution_eligible,
                },
            )
        return payloads

    @classmethod
    def _semantic_pattern_payload(cls, pattern: SelectionPattern) -> dict[str, object]:
        return {
            "sport": pattern.sport,
            "scope": pattern.scope,
            "market_type": pattern.market_type,
            "market_family": pattern.market_family,
            "selection": pattern.selection,
            "params_key": cls._params_key(pattern.params),
        }

    @staticmethod
    def _params_key(params: tuple[tuple[str, str], ...] | object) -> str:
        if isinstance(params, tuple):
            return json.dumps(list(params), sort_keys=True, separators=(",", ":"))
        return str(params or "")

    def _should_use_semantic_rust(self, semantic_templates: list[dict[str, object]]) -> bool:
        return self._rust_core_supports_semantic_topology() and (
            self._engine == "semantic_rust" or (self._engine == "auto" and bool(semantic_templates))
        )

    def _should_use_legacy_rust(self) -> bool:
        return self._rust_core is not None and (
            self._engine == "rust"
            or (self._engine == "auto" and self._semantic_rule_store() is None)
        )

    def _rust_core_supports_semantic_topology(self) -> bool:
        return self._rust_core is not None and all(
            callable(getattr(self._rust_core, method, None))
            for method in (
                "build_semantic",
                "add_instrument_semantic",
                "load_semantic_templates",
            )
        )

    @staticmethod
    def _metadata_str(metadata: dict[str, object], key: str) -> str | None:
        value = metadata.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _metadata_str_tuple(metadata: dict[str, object], key: str) -> tuple[str, ...]:
        value = metadata.get(key)
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,) if value else ()
        if isinstance(value, list | tuple):
            return tuple(str(item) for item in value)
        return (str(value),)

    def _semantic_rule_store(self) -> RuleStore | None:
        return self._matcher.rule_store

    def _ensure_rust_semantic_templates_loaded(
        self,
        semantic_templates: list[dict[str, object]],
    ) -> None:
        if self._rust_core is None:
            return
        if self._rust_semantic_templates_loaded and (
            self._semantic_template_count == len(semantic_templates)
        ):
            return
        self._rust_core.load_semantic_templates(semantic_templates)
        self._rust_semantic_templates_loaded = True
        self._semantic_template_count = len(semantic_templates)

    @classmethod
    def _start_time_ns(cls, instrument: CryptoBettingInstrument) -> int | None:
        parsed_start_time_func = cls._safe_attr(instrument, "parsed_start_time", None)
        if not callable(parsed_start_time_func):
            return None

        start_time = parsed_start_time_func()
        if not isinstance(start_time, datetime):
            return None
        return int(start_time.timestamp() * 1_000_000_000)

    @staticmethod
    def _safe_attr(
        obj: object,
        name: str,
        default: object,
    ) -> object:
        try:
            return getattr(obj, name)
        except AttributeError:
            return default

    @staticmethod
    def _edge_id(source_id: str, target_id: str) -> str:
        return "|".join(sorted([source_id, target_id]))
