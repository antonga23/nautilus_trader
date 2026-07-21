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

The graph keeps betting-domain hedge topology out of the quote hot path. In semantic Rust mode,
topology comes only from promoted semantic payloads loaded from :class:`MarketMatcher`'s rule
store. Quote ticks only update node state and re-evaluate edges adjacent to the changed
instrument.

"""

from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import logging
import os
import time
from typing import Any

from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.fixture_identity import DEFAULT_FIXTURE_IDENTITY_RESOLVER
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


logger = logging.getLogger(__name__)


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
        self._coverage_proof_count = 0
        self._coverage_hyperedge_count = 0
        self._coverage_summary_payload: dict[str, object] = self._empty_coverage_summary()
        self._semantic_template_payloads_cache: list[dict[str, object]] | None = None
        self._semantic_template_payloads_cache_store: RuleStore | None = None
        self._semantic_template_payloads_cache_generation: int | None = None
        self._semantic_template_payloads_cache_counts: tuple[int, int] = (0, 0)
        self._semantic_coverage_payloads_cache: (
            tuple[list[dict[str, object]], list[dict[str, object]]] | None
        ) = None
        self._semantic_coverage_payloads_cache_store: RuleStore | None = None
        self._semantic_coverage_payloads_cache_generation: int | None = None
        self._semantic_coverage_payloads_cache_summary: dict[str, object] = (
            self._empty_coverage_summary()
        )
        self._rust_semantic_templates_loaded = False
        self._rust_semantic_coverage_store: RuleStore | None = None
        self._rust_semantic_coverage_generation: int | None = None
        self.nodes_by_id: dict[str, OpportunityNode] = {}
        self.edges_by_id: dict[str, OpportunityEdge] = {}
        self.edge_ids_by_node_id: dict[str, set[str]] = {}
        self.quotes_by_node_id: dict[str, QuoteState] = {}
        self._mirrored_nodes_by_id: dict[str, OpportunityNode] = {}
        self._cross_venue_edges_dropped_missing_endpoint = 0
        self.edge_sync_full_runs = 0
        self.edge_sync_delta_runs = 0
        self.last_edge_sync_ns = 0

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
        rust_core = self._active_rust_core()
        if rust_core is not None:
            return rust_core.quote_state_count()
        return len(self.quotes_by_node_id)

    def clear(self) -> None:
        """
        Reset all graph topology and cached quote state.
        """
        if self._rust_core is not None:
            self._rust_core.clear()
        # Rust clear() wipes its coverage store, so the next coverage load must not
        # be skipped by the generation guard.
        self._rust_semantic_coverage_store = None
        self._rust_semantic_coverage_generation = None
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
        self._mirrored_nodes_by_id = dict(self.nodes_by_id)

        semantic_templates = self._semantic_template_payloads()
        coverage_proofs, coverage_hyperedges = self._semantic_coverage_payloads()
        if self._rust_core is not None and self._should_use_semantic_rust(semantic_templates):
            self._rust_core.build_semantic(rust_nodes, semantic_templates)
            self._load_rust_semantic_coverage(coverage_proofs, coverage_hyperedges)
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
        self._rust_semantic_templates_loaded = False
        self._semantic_template_count = len(semantic_templates)
        for instrument in instruments:
            self._add_edges_for_instrument(instrument, instruments)

    def add_instrument(self, instrument: CryptoBettingInstrument) -> bool:
        """
        Add one instrument and incrementally connect it to existing topology.
        """
        node = self._node_from_instrument(instrument)
        if node.node_id in self.nodes_by_id:
            return False

        self.nodes_by_id[node.node_id] = node
        self.edge_ids_by_node_id.setdefault(node.node_id, set())
        self._mirrored_nodes_by_id[node.node_id] = node

        if self._rust_core is not None and (
            self._should_use_semantic_rust(self._semantic_template_payloads())
            or self._should_use_legacy_rust()
        ):
            payload = self._node_payload_from_node(node, instrument)
            semantic_templates = self._semantic_template_payloads()
            coverage_proofs, coverage_hyperedges = self._semantic_coverage_payloads()
            if self._should_use_semantic_rust(semantic_templates):
                self._ensure_rust_semantic_templates_loaded(semantic_templates)
                self._load_rust_semantic_coverage(coverage_proofs, coverage_hyperedges)
                added = self._rust_core.add_instrument_semantic(payload)
                self._topology_source = "rust_semantic"
            else:
                added = self._rust_core.add_instrument(payload)
                self._topology_source = "rust_legacy"
            # Rust returns False only when the node already exists (idempotent),
            # never on genuine failure. Keep the freshly mirrored node so the
            # Python mirror stays consistent with Rust; popping it here would
            # strand a node Rust is quoting, making quotes for it drop forever.
            # Rust only touched the new node's buckets, so mirroring only that
            # node's edges keeps this O(bucket) instead of O(all edges).
            self._sync_edges_for_node(node.node_id)
            return added

        existing = [
            existing_node.instrument
            for existing_node in list(self.nodes_by_id.values())
            if existing_node.node_id != node.node_id
        ]
        candidates = [*existing, instrument]
        semantic_templates = self._semantic_template_payloads()
        self._rust_semantic_templates_loaded = False
        self._semantic_template_count = len(semantic_templates)
        self._add_edges_for_instrument(instrument, candidates)
        for existing_instrument in existing:
            self._add_edges_for_instrument(existing_instrument, candidates)
        return True

    def remove_instrument(self, node_id: str) -> bool:
        """
        Remove one instrument and its incident edges from the graph.

        Removal is a pure detach: it cannot create or re-rank edges (every surviving
        edge's endpoints, buckets, and template match are untouched by the removal),
        so former neighbors need no re-match — their edge sets only shrink by the
        returned removed edge ids, which are mirrored out of the Python maps here.

        """
        node = self.nodes_by_id.pop(node_id, None)
        self._mirrored_nodes_by_id.pop(node_id, None)
        self.quotes_by_node_id.pop(node_id, None)

        removed_edge_ids: list[str] = []
        rust_core = self._active_rust_core()
        rust_remove = getattr(rust_core, "remove_instrument", None) if rust_core else None
        if callable(rust_remove):
            removed_edge_ids = list(rust_remove(node_id))
        if not removed_edge_ids:
            removed_edge_ids = list(self.edge_ids_by_node_id.get(node_id, ()))

        for edge_id in removed_edge_ids:
            edge = self.edges_by_id.pop(edge_id, None)
            if edge is None:
                continue
            for endpoint_id in (edge.source_node_id, edge.target_node_id):
                endpoint_edge_ids = self.edge_ids_by_node_id.get(endpoint_id)
                if endpoint_edge_ids is not None:
                    endpoint_edge_ids.discard(edge_id)
        self.edge_ids_by_node_id.pop(node_id, None)
        return node is not None or bool(removed_edge_ids)

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
        rust_core = self._active_rust_core()
        if rust_core is not None:
            rust_core.update_quote(
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

        rust_core = self._active_rust_core()
        if rust_core is not None:
            snapshots = rust_core.evaluate_updated_node(
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
        rust_core = self._active_rust_core()
        if rust_core is None:
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
        snapshots = rust_core.update_quote_and_evaluate(
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
        rust_core = self._active_rust_core()
        if rust_core is None:
            return None

        node_id = str(quote.instrument_id)
        if node_id not in self.nodes_by_id:
            return False, []

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
        snapshots = rust_core.update_quote_and_scan_fast(
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
            if (
                (edge := self.edges_by_id.get(snapshot[0])) is not None
                and (edge.execution_safe or edge.same_venue_execution_eligible)
            )
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
            "coverage_proof_count": self._coverage_proof_count,
            "coverage_hyperedge_count": self._coverage_hyperedge_count,
            "cross_venue_edges_dropped_missing_endpoint": (
                self._cross_venue_edges_dropped_missing_endpoint
            ),
            "edge_sync_full_runs": self.edge_sync_full_runs,
            "edge_sync_delta_runs": self.edge_sync_delta_runs,
            "last_edge_sync_ns": self.last_edge_sync_ns,
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

    @property
    def coverage_proof_count(self) -> int:
        return self._coverage_proof_count

    @property
    def coverage_hyperedge_count(self) -> int:
        return self._coverage_hyperedge_count

    @property
    def cross_venue_edges_dropped_missing_endpoint(self) -> int:
        return self._cross_venue_edges_dropped_missing_endpoint

    def semantic_coverage_summary(self) -> dict[str, object]:
        """
        Return coverage proof and hyperedge diagnostics loaded into the graph core.
        """
        if self._rust_core is not None and hasattr(
            self._rust_core,
            "semantic_coverage_summary_json",
        ):
            raw_summary = self._rust_core.semantic_coverage_summary_json()
            try:
                payload = json.loads(raw_summary)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Rust semantic_coverage_summary_json returned malformed payload (%s: %s); "
                    "falling back to python coverage summary. Raw sample: %.200r",
                    type(exc).__name__,
                    exc,
                    raw_summary,
                )
                return dict(self._coverage_summary_payload)
            if isinstance(payload, dict):
                # Rust owns the hot-path counts, while Python retains predicate metadata
                # needed by runtime diagnostics to map semantic cache legs back to nodes.
                python_payload = dict(self._coverage_summary_payload)
                for sample_key in ("sampleProofIds", "sampleProofs", "sampleHyperedges"):
                    if python_payload.get(sample_key):
                        payload[sample_key] = python_payload[sample_key]
                return payload
        return dict(self._coverage_summary_payload)

    def _sync_edges_from_rust(self) -> None:
        if self._rust_core is None:
            return
        started_ns = time.perf_counter_ns()
        self.edges_by_id.clear()
        self.edge_ids_by_node_id = {node_id: set() for node_id in self.nodes_by_id}
        dropped_cross_venue = 0
        for snapshot in self._rust_core.edge_snapshots():
            dropped_cross_venue += self._mirror_rust_edge_snapshot(snapshot)
        self._record_dropped_cross_venue_edges(dropped_cross_venue)
        self.edge_sync_full_runs += 1
        self.last_edge_sync_ns = time.perf_counter_ns() - started_ns

    def _sync_edges_for_node(self, node_id: str) -> None:
        if self._rust_core is None:
            return
        snapshot_source = getattr(self._rust_core, "edge_snapshots_for_node", None)
        if not callable(snapshot_source):
            # Older Rust core without the filtered export: keep correctness via full sync.
            self._sync_edges_from_rust()
            return
        started_ns = time.perf_counter_ns()
        self._detach_edges_for_node(node_id)
        self.edge_ids_by_node_id.setdefault(node_id, set())
        dropped_cross_venue = 0
        for snapshot in snapshot_source(node_id):
            dropped_cross_venue += self._mirror_rust_edge_snapshot(snapshot)
        if dropped_cross_venue:
            self._cross_venue_edges_dropped_missing_endpoint += dropped_cross_venue
            logger.warning(
                "Dropped %d cross-venue Rust edges for node %s whose endpoint nodes are "
                "missing from the Python mirror",
                dropped_cross_venue,
                node_id,
            )
        self.edge_sync_delta_runs += 1
        self.last_edge_sync_ns = time.perf_counter_ns() - started_ns

    def _detach_edges_for_node(self, node_id: str) -> None:
        for edge_id in list(self.edge_ids_by_node_id.get(node_id, ())):
            edge = self.edges_by_id.pop(edge_id, None)
            self.edge_ids_by_node_id[node_id].discard(edge_id)
            if edge is None:
                continue
            other_node_id = (
                edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
            )
            other_edge_ids = self.edge_ids_by_node_id.get(other_node_id)
            if other_edge_ids is not None:
                other_edge_ids.discard(edge_id)

    def _mirror_rust_edge_snapshot(self, snapshot: tuple) -> int:
        """
        Mirror one Rust edge snapshot into the Python maps.

        Returns the number of cross-venue edges dropped for missing endpoints (0 or 1)
        so callers can aggregate the diagnostic counter.

        """
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
            self._metadata_str(metadata, "market_relationship_type") or raw_market_relationship_type
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
        # A cross-venue topology-only edge (Rust decoupled venue scope for
        # observability) must NEVER be re-flagged executable by the public-hedge
        # mirror path — it exists purely to feed crossVenueCandidateCount/RAG.
        rust_topology_only = not rust_execution_safe and (
            safety_tier == "TOPOLOGY_SAFE" or "cross_venue_topology_only" in caveats
        )
        source_node, target_node = self._resolve_edge_endpoint_nodes(
            source_node_id,
            target_node_id,
        )
        if source_node is None or target_node is None:
            return int(not same_venue)

        hedge = self._best_public_hedge_candidate(
            source_node.instrument,
            target_node.instrument,
        )
        if hedge is None and not template_id and market_relationship_type != "same_market":
            return 0
        hedge_match_type: str | None = None
        hedge_confidence: float | None = None
        hedge_push_capable = rust_push_capable
        hedge_execution_safe = rust_execution_safe
        hedge_rule_id: str | None = None
        hedge_template_id = template_id
        hedge_relationship_type: str | None = None
        hedge_caveats: tuple[str, ...] = ()
        hedge_promotion_status = promotion_status
        hedge_safety_tier = safety_tier
        hedge_same_venue_execution_eligible = same_venue_execution_eligible
        hedge_partial_settlement = partial_settlement
        if hedge is not None:
            hedge_match_type = hedge.match_type
            hedge_confidence = hedge.confidence
            hedge_push_capable = hedge.push_capable
            hedge_relationship_type = hedge.relationship_type
            hedge_caveats = hedge.caveats
            hedge_partial_settlement = hedge.partial_settlement
            if template_id or hedge.relationship_type is not None:
                hedge_execution_safe = hedge.execution_safe
            if template_id:
                hedge_rule_id = hedge.rule_id
                hedge_template_id = hedge.template_id
                hedge_promotion_status = hedge.promotion_status
                hedge_safety_tier = hedge.safety_tier
                hedge_same_venue_execution_eligible = hedge.same_venue_execution_eligible

        if rust_topology_only:
            hedge_execution_safe = False
            hedge_same_venue_execution_eligible = False
            hedge_safety_tier = safety_tier or "TOPOLOGY_SAFE"
            hedge_caveats = tuple(
                dict.fromkeys((*hedge_caveats, *caveats, "cross_venue_topology_only")),
            )

        edge = OpportunityEdge(
            edge_id=edge_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            hedge_type=hedge_match_type or hedge_type,
            confidence=hedge_confidence or confidence,
            same_venue=same_venue,
            market_relationship_type=market_relationship_type,
            push_capable=hedge_push_capable,
            execution_safe=hedge_execution_safe,
            rule_id=hedge_rule_id,
            template_id=hedge_template_id,
            relationship_type=hedge_relationship_type or relationship_type,
            caveats=hedge_caveats or caveats,
            promotion_status=hedge_promotion_status,
            safety_tier=hedge_safety_tier,
            same_venue_execution_eligible=hedge_same_venue_execution_eligible,
            void_capable=hedge_push_capable,
            partial_settlement=hedge_partial_settlement,
            last_margin=Decimal(str(last_margin)) if last_margin is not None else None,
            last_evaluated_ns=last_evaluated_ns,
            last_updated_ns=last_updated_ns,
        )
        self.edges_by_id[edge_id] = edge
        self.edge_ids_by_node_id.setdefault(source_node_id, set()).add(edge_id)
        self.edge_ids_by_node_id.setdefault(target_node_id, set()).add(edge_id)
        return 0

    def _record_dropped_cross_venue_edges(self, dropped: int) -> None:
        self._cross_venue_edges_dropped_missing_endpoint = dropped
        if dropped:
            logger.warning(
                "Dropped %d cross-venue Rust edges whose endpoint nodes are missing "
                "from the Python mirror; their legs cannot be classified cross-venue "
                "at quote-subscription time",
                dropped,
            )

    def _resolve_edge_endpoint_nodes(
        self,
        source_node_id: str,
        target_node_id: str,
    ) -> tuple[OpportunityNode | None, OpportunityNode | None]:
        source_node = self.nodes_by_id.get(source_node_id) or self._restore_mirrored_node(
            source_node_id,
        )
        target_node = self.nodes_by_id.get(target_node_id) or self._restore_mirrored_node(
            target_node_id,
        )
        return source_node, target_node

    def _restore_mirrored_node(self, node_id: str) -> OpportunityNode | None:
        # Rust can keep a node the Python mirror lost across concurrent rebuilds.
        # Dropping its edges here would silently unflag the legs as cross-venue at
        # quote-subscription time, so restore the node from the last mirrored snapshot.
        node = self._mirrored_nodes_by_id.get(node_id)
        if node is None:
            return None
        self.nodes_by_id[node_id] = node
        self.edge_ids_by_node_id.setdefault(node_id, set())
        return node

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
            if edge is None or not (edge.execution_safe or edge.same_venue_execution_eligible):
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
        for hedge in hedges:
            normalized_hedge = self._normalize_public_hedge(instrument, hedge)
            if normalized_hedge is None:
                continue
            self._upsert_edge(
                source=instrument,
                target=normalized_hedge.instrument,
                hedge=normalized_hedge,
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
            return self._matcher._same_market_complement_candidate(target)
        return None

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
            raw_probability_a=probability_a,
            raw_probability_b=probability_b,
            raw_total_probability=total_probability,
            raw_profit_margin=profit_margin,
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
            # Bucket on the same expanding canonical form the runtime probe computes,
            # so the graph can never fall behind the probe (a multi-word sport rendered
            # with an underscore here and a space there used to orphan identical
            # fixtures). Only a genuine resolver event key is canonicalized; the bare
            # instrument-id fallback is not an event key and is left untouched.
            raw_event_key_no_time = str(event_key_func(include_start_time=False))
            event_key_no_time = (
                DEFAULT_FIXTURE_IDENTITY_RESOLVER.canonical_event_key_text(raw_event_key_no_time)
                or raw_event_key_no_time
            )
        else:
            raw_event_key_no_time = node.canonical_event_key
            event_key_no_time = raw_event_key_no_time
        event_alias_keys_func = cls._safe_attr(instrument, "event_alias_keys", None)
        if callable(event_alias_keys_func):
            try:
                raw_event_alias_keys = event_alias_keys_func(include_start_time=False)
            except (AttributeError, TypeError, ValueError):
                raw_event_alias_keys = ()
            event_alias_keys = cls._event_alias_keys_payload(raw_event_alias_keys)
            if not event_alias_keys:
                event_alias_keys = (event_key_no_time,)
            # Keep both the canonical and the raw resolver forms as aliases so a peer
            # node buckets whichever form it carries.
            event_alias_keys = cls._canonicalized_alias_keys(event_alias_keys, event_key_no_time)
        else:
            event_alias_keys = (event_key_no_time,)

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
            "event_alias_keys": event_alias_keys,
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

    @staticmethod
    def _event_alias_keys_payload(value: object) -> tuple[str, ...]:
        if not isinstance(value, list | tuple | set | frozenset):
            return ()
        return tuple(str(key) for key in value if str(key))

    @staticmethod
    def _canonicalized_alias_keys(
        alias_keys: tuple[str, ...],
        canonical_event_key: str,
    ) -> tuple[str, ...]:
        keys = {canonical_event_key} if canonical_event_key else set()
        for key in alias_keys:
            if not key:
                continue
            keys.add(key)
            canonical = DEFAULT_FIXTURE_IDENTITY_RESOLVER.canonical_event_key_text(key)
            if canonical:
                keys.add(canonical)
        return tuple(sorted(keys))

    @classmethod
    def _semantic_node_payload(cls, instrument: CryptoBettingInstrument) -> dict[str, object]:
        try:
            normalized = MarketNormalizer.normalize(instrument)
        except (AttributeError, TypeError, ValueError) as exc:
            raw_market_type = str(cls._safe_attr(instrument, "market_type", ""))
            market_type = MarketType.from_string(
                raw_market_type or str(cls._safe_attr(instrument, "market_name", "")),
            ).value
            logger.warning(
                "MarketNormalizer.normalize failed for instrument %s (%s: %s); "
                "emitting unnormalized semantic identity so it cannot masquerade as a full_time match",
                cls._safe_attr(instrument, "id", "<unknown>"),
                type(exc).__name__,
                exc,
            )
            return {
                "semantic_sport": str(cls._safe_attr(instrument, "sport_name", "")).lower(),
                "semantic_scope": "unnormalized",
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
            self._coverage_proof_count = 0
            self._coverage_hyperedge_count = 0
            self._semantic_template_payloads_cache = None
            self._semantic_template_payloads_cache_store = None
            self._semantic_template_payloads_cache_generation = None
            return []

        # This loads and gunzips every promoted template from disk. On a large
        # multivenue store that is thousands of files, and the probe status
        # writer calls it every heartbeat, so re-reading it each time starves the
        # quote-poll loops. Templates only change when the store does, so serve a
        # cached view keyed on the store's monotonic generation and rebuild only
        # after a fresh mine / refresh bumps it.
        generation = getattr(rule_store, "generation", None)
        if (
            generation is not None
            and self._semantic_template_payloads_cache is not None
            and self._semantic_template_payloads_cache_store is rule_store
            and self._semantic_template_payloads_cache_generation == generation
        ):
            self._coverage_proof_count, self._coverage_hyperedge_count = (
                self._semantic_template_payloads_cache_counts
            )
            return self._semantic_template_payloads_cache

        self._coverage_proof_count = (
            len(rule_store.list_coverage_proof_ids())
            if hasattr(rule_store, "list_coverage_proof_ids")
            else 0
        )
        self._coverage_hyperedge_count = (
            len(rule_store.list_coverage_hyperedge_ids())
            if hasattr(rule_store, "list_coverage_hyperedge_ids")
            else 0
        )

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

        self._semantic_template_payloads_cache = payloads
        self._semantic_template_payloads_cache_store = rule_store
        self._semantic_template_payloads_cache_generation = generation
        self._semantic_template_payloads_cache_counts = (
            self._coverage_proof_count,
            self._coverage_hyperedge_count,
        )
        return payloads

    def _semantic_coverage_payloads(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        rule_store = self._semantic_rule_store()
        if rule_store is None:
            self._coverage_proof_count = 0
            self._coverage_hyperedge_count = 0
            self._coverage_summary_payload = self._empty_coverage_summary()
            self._semantic_coverage_payloads_cache = None
            self._semantic_coverage_payloads_cache_store = None
            self._semantic_coverage_payloads_cache_generation = None
            return [], []

        # Same disk-read hazard as _semantic_template_payloads: every call re-loads
        # and gunzips each coverage proof and hyperedge, and the incremental
        # add_instrument path calls this per added instrument. Serve a cached view
        # keyed on the same store generation so both caches invalidate together.
        generation = getattr(rule_store, "generation", None)
        if (
            generation is not None
            and self._semantic_coverage_payloads_cache is not None
            and self._semantic_coverage_payloads_cache_store is rule_store
            and self._semantic_coverage_payloads_cache_generation == generation
        ):
            cached_proofs, cached_hyperedges = self._semantic_coverage_payloads_cache
            self._coverage_proof_count = len(cached_proofs)
            self._coverage_hyperedge_count = len(cached_hyperedges)
            self._coverage_summary_payload = self._semantic_coverage_payloads_cache_summary
            return cached_proofs, cached_hyperedges

        proof_payloads: list[dict[str, object]] = []
        proofs_by_id: dict[str, Any] = {}
        if hasattr(rule_store, "list_coverage_proof_ids"):
            for proof_id in rule_store.list_coverage_proof_ids():
                proof = rule_store.load_coverage_proof(proof_id)
                if proof is None:
                    continue
                proofs_by_id[proof.proof_id] = proof
                proof_payloads.append(
                    {
                        "proof_id": proof.proof_id,
                        "sport": proof.universe.sport,
                        "scope": proof.universe.scope,
                        "provider_scope": list(proof.coverage_set.provider_scope),
                        "predicate_count": len(proof.predicates),
                        "instrument_ids": [
                            predicate.instrument_id for predicate in proof.predicates
                        ],
                        "complete": proof.complete,
                        "win_covered_states": list(proof.win_covered_states),
                        "overlapping_win_states": list(proof.overlapping_win_states),
                        "gap_count": len(proof.gaps),
                        "risk_count": len(proof.risks),
                        "gaps": [gap.reason for gap in proof.gaps],
                        "risks": [risk.reason for risk in proof.risks],
                        "safety_tier": proof.safety_tier,
                        "execution_safe": proof.execution_safe,
                        "same_venue_execution_eligible": proof.same_venue_execution_eligible,
                        "relationship_type": proof.relationship_type,
                        "blocker_reasons": list(proof.blocker_reasons),
                        "predicates": [
                            self._coverage_predicate_payload(predicate)
                            for predicate in proof.predicates
                        ],
                    },
                )

        hyperedge_payloads: list[dict[str, object]] = []
        if hasattr(rule_store, "list_coverage_hyperedge_ids"):
            for hyperedge_id in rule_store.list_coverage_hyperedge_ids():
                hyperedge = rule_store.load_coverage_hyperedge(hyperedge_id)
                if hyperedge is None:
                    continue
                proof = proofs_by_id.get(hyperedge.coverage_proof_id)
                hyperedge_payloads.append(
                    {
                        "hyperedge_id": hyperedge.hyperedge_id,
                        "coverage_proof_id": hyperedge.coverage_proof_id,
                        "instrument_ids": list(hyperedge.instrument_ids),
                        "provider_scope": list(hyperedge.provider_scope),
                        "relationship_type": hyperedge.relationship_type,
                        "safety_tier": hyperedge.safety_tier,
                        "execution_safe": hyperedge.execution_safe,
                        "caveats": list(hyperedge.caveats),
                        "predicates": (
                            [
                                self._coverage_predicate_payload(predicate)
                                for predicate in proof.predicates
                            ]
                            if proof is not None
                            else []
                        ),
                    },
                )

        self._coverage_proof_count = len(proof_payloads)
        self._coverage_hyperedge_count = len(hyperedge_payloads)
        self._coverage_summary_payload = self._coverage_summary_from_payloads(
            proof_payloads,
            hyperedge_payloads,
        )
        self._semantic_coverage_payloads_cache = (proof_payloads, hyperedge_payloads)
        self._semantic_coverage_payloads_cache_store = rule_store
        self._semantic_coverage_payloads_cache_generation = generation
        self._semantic_coverage_payloads_cache_summary = self._coverage_summary_payload
        return proof_payloads, hyperedge_payloads

    @staticmethod
    def _empty_coverage_summary() -> dict[str, object]:
        return {
            "coverageProofCount": 0,
            "coverageHyperedgeCount": 0,
            "executionSafeCoverageProofCount": 0,
            "executionSafeCoverageHyperedgeCount": 0,
            "sameVenueEligibleCoverageProofCount": 0,
            "proofSafetyTierCounts": {},
            "hyperedgeSafetyTierCounts": {},
            "proofRelationshipTypeCounts": {},
            "proofBlockerReasonCounts": {},
            "proofGapReasonCounts": {},
            "proofRiskReasonCounts": {},
            "sampleProofIds": [],
            "sampleProofs": [],
            "sampleHyperedges": [],
        }

    def _coverage_summary_from_payloads(
        self,
        proof_payloads: list[dict[str, object]],
        hyperedge_payloads: list[dict[str, object]],
    ) -> dict[str, object]:
        proof_tiers = Counter(
            self._coverage_safe_string(payload.get("safety_tier")) for payload in proof_payloads
        )
        proof_relationships = Counter(
            self._coverage_safe_string(payload.get("relationship_type"))
            for payload in proof_payloads
        )
        proof_blockers = Counter(
            self._coverage_safe_string(reason)
            for payload in proof_payloads
            for reason in self._coverage_string_sequence(payload.get("blocker_reasons"))
        )
        proof_gaps = Counter(
            self._coverage_safe_string(reason)
            for payload in proof_payloads
            for reason in self._coverage_string_sequence(payload.get("gaps"))
        )
        proof_risks = Counter(
            self._coverage_safe_string(reason)
            for payload in proof_payloads
            for reason in self._coverage_string_sequence(payload.get("risks"))
        )
        hyperedge_tiers = Counter(
            self._coverage_safe_string(payload.get("safety_tier")) for payload in hyperedge_payloads
        )
        return {
            "coverageProofCount": len(proof_payloads),
            "coverageHyperedgeCount": len(hyperedge_payloads),
            "executionSafeCoverageProofCount": sum(
                1 for payload in proof_payloads if bool(payload.get("execution_safe"))
            ),
            "executionSafeCoverageHyperedgeCount": sum(
                1 for payload in hyperedge_payloads if bool(payload.get("execution_safe"))
            ),
            "sameVenueEligibleCoverageProofCount": sum(
                1
                for payload in proof_payloads
                if bool(payload.get("same_venue_execution_eligible"))
            ),
            "proofSafetyTierCounts": dict(sorted(proof_tiers.items())),
            "hyperedgeSafetyTierCounts": dict(sorted(hyperedge_tiers.items())),
            "proofRelationshipTypeCounts": dict(sorted(proof_relationships.items())),
            "proofBlockerReasonCounts": dict(sorted(proof_blockers.items())),
            "proofGapReasonCounts": dict(sorted(proof_gaps.items())),
            "proofRiskReasonCounts": dict(sorted(proof_risks.items())),
            "sampleProofIds": [
                self._coverage_safe_string(payload.get("proof_id"))
                for payload in proof_payloads[:10]
                if payload.get("proof_id")
            ],
            "sampleProofs": proof_payloads[:10],
            "sampleHyperedges": self._coverage_sample_hyperedges(hyperedge_payloads),
        }

    def _coverage_sample_hyperedges(
        self,
        hyperedge_payloads: list[dict[str, object]],
        *,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        if len(hyperedge_payloads) <= limit:
            return hyperedge_payloads

        active_nodes = tuple(self.nodes_by_id.values())
        active_providers = {node.venue.upper() for node in active_nodes}
        active_event_keys = self._coverage_active_event_keys(active_nodes)
        if not active_providers and not active_event_keys:
            return hyperedge_payloads[:limit]

        scored: list[tuple[int, int, dict[str, object]]] = []
        for index, payload in enumerate(hyperedge_payloads):
            score = self._coverage_hyperedge_sample_score(
                payload,
                active_providers=active_providers,
                active_event_keys=active_event_keys,
            )
            scored.append((score, index, payload))
        relevant = [item for item in scored if item[0] > 0]
        if not relevant:
            return hyperedge_payloads[:limit]
        return [
            payload
            for _, _, payload in sorted(relevant, key=lambda item: (-item[0], item[1]))[:limit]
        ]

    def _coverage_hyperedge_sample_score(
        self,
        payload: dict[str, object],
        *,
        active_providers: set[str],
        active_event_keys: set[str],
    ) -> int:
        provider_scope = {
            str(provider).upper()
            for provider in self._coverage_string_sequence(payload.get("provider_scope"))
            if provider
        }
        predicates = payload.get("predicates")
        predicate_payloads = predicates if isinstance(predicates, list) else []
        predicate_providers = {
            str(predicate.get("provider") or "").upper()
            for predicate in predicate_payloads
            if isinstance(predicate, dict)
        }
        predicate_event_keys = {
            self._coverage_event_key_no_time(str(predicate.get("event_key") or ""))
            for predicate in predicate_payloads
            if isinstance(predicate, dict)
        }
        score = 0
        if provider_scope & active_providers:
            score += 4
        if predicate_providers & active_providers:
            score += 2
        if predicate_event_keys & active_event_keys:
            score += 4
        if bool(payload.get("execution_safe")):
            score += 1
        return score

    def _coverage_active_event_keys(
        self,
        active_nodes: tuple[OpportunityNode, ...] | None = None,
    ) -> set[str]:
        keys: set[str] = set()
        for node in active_nodes or tuple(self.nodes_by_id.values()):
            keys.add(self._coverage_event_key_no_time(node.canonical_event_key))
            event_key = self._safe_attr(node.instrument, "event_key", None)
            if callable(event_key):
                with suppress(AttributeError, TypeError, ValueError):
                    keys.add(
                        self._coverage_event_key_no_time(str(event_key(include_start_time=False))),
                    )
            event_alias_keys = self._safe_attr(node.instrument, "event_alias_keys", None)
            if callable(event_alias_keys):
                try:
                    aliases = event_alias_keys(include_start_time=False)
                except (AttributeError, TypeError, ValueError):
                    aliases = ()
                if isinstance(aliases, str | bytes):
                    aliases = (aliases,)
                for alias in aliases or ():
                    keys.add(self._coverage_event_key_no_time(str(alias)))
        return {key for key in keys if key}

    @staticmethod
    def _coverage_event_key_no_time(value: str) -> str:
        raw = str(value or "")
        if not raw:
            return ""
        parts = [part.strip() for part in raw.replace("|", ":").split(":") if part.strip()]
        normalized_parts: list[str] = []
        for part in parts:
            lowered = part.lower()
            if "t" in lowered and lowered[:4].isdigit():
                continue
            if len(lowered) >= 4 and lowered[:4].isdigit():
                continue
            normalized = part.replace("_", " ").strip().lower()
            if normalized:
                normalized_parts.append(normalized)
        return ":".join(normalized_parts)

    @staticmethod
    def _coverage_string_sequence(value: object) -> tuple[object, ...]:
        if value is None:
            return ()
        if isinstance(value, list | tuple | set):
            return tuple(value)
        return (value,)

    @staticmethod
    def _coverage_safe_string(value: object) -> str:
        return str(value or "UNKNOWN")

    @classmethod
    def _coverage_predicate_payload(cls, predicate: object) -> dict[str, object]:
        params = getattr(predicate, "params", ())
        return {
            "predicate_id": cls._coverage_safe_string(getattr(predicate, "predicate_id", "")),
            "instrument_id": cls._coverage_safe_string(getattr(predicate, "instrument_id", "")),
            "provider": cls._coverage_safe_string(getattr(predicate, "provider", "")),
            "event_key": cls._coverage_safe_string(getattr(predicate, "event_key", "")),
            "sport": cls._coverage_safe_string(getattr(predicate, "sport", "")),
            "scope": cls._coverage_safe_string(getattr(predicate, "scope", "")),
            "market_type": cls._coverage_safe_string(getattr(predicate, "market_type", "")),
            "market_family": cls._coverage_safe_string(getattr(predicate, "market_family", "")),
            "selection": cls._coverage_safe_string(getattr(predicate, "selection", "")),
            "params": list(params) if isinstance(params, tuple | list) else [],
            "params_key": cls._params_key(params),
            "result_states": list(getattr(predicate, "result_states", ()) or ()),
            "win_states": list(getattr(predicate, "win_states", ()) or ()),
            "void_states": list(getattr(predicate, "void_states", ()) or ()),
            "partial_states": list(getattr(predicate, "partial_states", ()) or ()),
            "unknown_states": list(getattr(predicate, "unknown_states", ()) or ()),
            "provider_rule_flags": list(getattr(predicate, "provider_rule_flags", ()) or ()),
            "caveats": list(getattr(predicate, "caveats", ()) or ()),
        }

    def _load_rust_semantic_coverage(
        self,
        coverage_proofs: list[dict[str, object]],
        coverage_hyperedges: list[dict[str, object]],
    ) -> None:
        if self._rust_core is None:
            return
        loader = getattr(self._rust_core, "load_semantic_coverage", None)
        if not callable(loader):
            return
        # Re-marshalling every proof and hyperedge into Rust dominates the cost of an
        # incremental add, and the payloads only change when the store does. Skip the
        # reload while the generation last loaded is current; clear() resets the marker
        # so full builds (and the semantic cache reload path, which also swaps in a
        # fresh RuleStore) always load.
        rule_store = self._semantic_rule_store()
        generation = getattr(rule_store, "generation", None) if rule_store is not None else None
        if (
            generation is not None
            and self._rust_semantic_coverage_store is rule_store
            and self._rust_semantic_coverage_generation == generation
        ):
            return
        loader(coverage_proofs, coverage_hyperedges)
        self._rust_semantic_coverage_store = rule_store
        self._rust_semantic_coverage_generation = generation

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
            semantic_params = [
                (key, value) for key, value in params if str(key).lower() not in {"period"}
            ]
            return json.dumps(semantic_params, sort_keys=True, separators=(",", ":"))
        return str(params or "")

    def supports_incremental_refresh(self) -> bool:
        """
        Return True when add/remove deltas can be applied without a full rebuild.

        Requires the Rust core removal + filtered edge-export bindings when Rust
        topology is active; the pure-Python engine always maintains its own maps
        incrementally.

        """
        rust_core = self._active_rust_core()
        if rust_core is None:
            return True
        return all(
            callable(getattr(rust_core, method, None))
            for method in ("remove_instrument", "edge_snapshots_for_node")
        )

    def semantic_templates_stale(self) -> bool:
        """
        Return True when promoted template content may differ from what the graph core
        last loaded (rule store swapped or its generation bumped), in which case only a
        full build re-adopts the new templates.
        """
        rule_store = self._semantic_rule_store()
        if rule_store is None:
            return False
        if self._semantic_template_payloads_cache_store is not rule_store:
            return True
        generation = getattr(rule_store, "generation", None)
        if generation is None:
            return True
        return self._semantic_template_payloads_cache_generation != generation

    def _should_use_semantic_rust(self, semantic_templates: list[dict[str, object]]) -> bool:
        if not self._rust_core_supports_semantic_topology():
            return False
        if self._engine == "semantic_rust":
            return True
        return self._engine == "auto" and bool(semantic_templates)

    def _should_use_legacy_rust(self) -> bool:
        return self._rust_core is not None and (
            self._engine == "rust"
            or (self._engine == "auto" and self._semantic_rule_store() is None)
        )

    def _using_rust_topology(self) -> bool:
        return self._rust_core is not None and self._topology_source.startswith("rust")

    def _active_rust_core(self) -> Any | None:
        return self._rust_core if self._using_rust_topology() else None

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
        raw_start = str(cls._safe_attr(instrument, "start_time", "") or "").strip()
        if (
            len(raw_start) == 10
            and raw_start[4] == "-"
            and raw_start[7] == "-"
            and raw_start[:4].isdigit()
            and raw_start[5:7].isdigit()
            and raw_start[8:].isdigit()
        ):
            return None

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
