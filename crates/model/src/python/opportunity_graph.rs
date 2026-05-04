// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
//  https://nautechsystems.io
//
//  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// -------------------------------------------------------------------------------------------------

use std::collections::{HashMap, HashSet};

use pyo3::{exceptions::PyKeyError, prelude::*, types::PyDict};
use serde_json::{Value, json};

const HANDICAP_TOLERANCE: f64 = 0.01;
const PROFIT_MARGIN_EPSILON: f64 = 1e-12;
const SIX_HOURS_NS: i64 = 6 * 60 * 60 * 1_000_000_000;

type CandidateSnapshot = (String, String, String, f64, i64, i64);
type FastCandidateSnapshot = (
    String,
    String,
    String,
    String,
    f64,
    f64,
    f64,
    f64,
    i64,
    i64,
    String,
    bool,
);
type EdgeExportSnapshot = (
    String,
    String,
    String,
    String,
    f64,
    bool,
    String,
    bool,
    bool,
    Option<f64>,
    Option<i64>,
    Option<i64>,
);

#[derive(Clone, Debug)]
struct NodeSnapshot {
    node_id: String,
    venue: String,
    event_id: String,
    event_key_no_time: String,
    market_name: String,
    market_type: String,
    outcome: String,
    selection_key: String,
    params: String,
    handicap: Option<f64>,
    start_time_ns: Option<i64>,
    two_way_market: bool,
    semantic_sport: String,
    semantic_scope: String,
    semantic_market_type: String,
    semantic_market_family: String,
    semantic_selection: String,
    semantic_params_key: String,
}

impl NodeSnapshot {
    fn from_py(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let dict = value.cast::<PyDict>()?;
        let market_type = get_string(dict, "market_type")?;
        let outcome = get_string(dict, "outcome")?;
        let params = get_string(dict, "params")?;
        Ok(Self {
            node_id: get_string(dict, "node_id")?,
            venue: get_string(dict, "venue")?,
            event_id: get_string(dict, "event_id")?,
            event_key_no_time: get_string(dict, "event_key_no_time")?,
            market_name: get_string(dict, "market_name")?,
            market_type: market_type.clone(),
            outcome: outcome.clone(),
            selection_key: get_string(dict, "selection_key")?,
            params: params.clone(),
            handicap: get_optional_f64(dict, "handicap")?,
            start_time_ns: get_optional_i64(dict, "start_time_ns")?,
            two_way_market: get_bool(dict, "two_way_market")?,
            semantic_sport: get_optional_string(dict, "semantic_sport")?.unwrap_or_default(),
            semantic_scope: get_optional_string(dict, "semantic_scope")?.unwrap_or_default(),
            semantic_market_type: get_optional_string(dict, "semantic_market_type")?
                .unwrap_or_else(|| market_type.clone()),
            semantic_market_family: get_optional_string(dict, "semantic_market_family")?
                .unwrap_or_else(|| market_type.clone()),
            semantic_selection: get_optional_string(dict, "semantic_selection")?
                .unwrap_or_else(|| outcome.clone()),
            semantic_params_key: get_optional_string(dict, "semantic_params_key")?
                .unwrap_or(params),
        })
    }
}

#[derive(Clone, Debug)]
struct EdgeFlags {
    same_venue: bool,
    push_capable: bool,
    execution_safe: bool,
    matcher_suspect: bool,
}

#[derive(Clone, Debug)]
struct EdgeSnapshot {
    edge_id: String,
    source_node_id: String,
    target_node_id: String,
    hedge_type: String,
    confidence: f64,
    flags: EdgeFlags,
    market_relationship_type: String,
    last_margin: Option<f64>,
    last_evaluated_ns: Option<i64>,
    last_updated_ns: Option<i64>,
    template_id: Option<String>,
    relationship_type: Option<String>,
    promotion_status: Option<String>,
    safety_tier: Option<String>,
    same_venue_execution_eligible: bool,
    partial_settlement: bool,
    caveats: Vec<String>,
}

#[derive(Clone, Debug)]
struct SemanticPatternSnapshot {
    sport: String,
    scope: String,
    market_type: String,
    market_family: String,
    selection: String,
    params_key: String,
}

impl SemanticPatternSnapshot {
    fn from_py(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let dict = value.cast::<PyDict>()?;
        Ok(Self {
            sport: get_string(dict, "sport")?,
            scope: get_string(dict, "scope")?,
            market_type: get_string(dict, "market_type")?,
            market_family: get_string(dict, "market_family")?,
            selection: get_string(dict, "selection")?,
            params_key: get_string(dict, "params_key")?,
        })
    }

    fn matches_node_identity(&self, node: &NodeSnapshot) -> bool {
        self.sport == node.semantic_sport
            && self.scope == node.semantic_scope
            && self.market_type == node.semantic_market_type
            && self.market_family == node.semantic_market_family
            && self.selection == node.semantic_selection
    }
}

#[derive(Clone, Debug)]
struct SemanticTemplateSnapshot {
    template_id: String,
    relationship_type: String,
    pattern_a: SemanticPatternSnapshot,
    pattern_b: SemanticPatternSnapshot,
    confidence: f64,
    provider_scope: Vec<String>,
    venue_agnostic: bool,
    safety_tier: String,
    promotion_status: String,
    push_capable: bool,
    execution_safe: bool,
    same_venue_execution_eligible: bool,
    partial_settlement: bool,
    caveats: Vec<String>,
}

#[derive(Clone, Debug)]
struct SemanticTemplateMatch {
    hedge_type: String,
    confidence: f64,
    push_capable: bool,
    execution_safe: bool,
    template_id: String,
    relationship_type: String,
    promotion_status: String,
    safety_tier: String,
    same_venue_execution_eligible: bool,
    partial_settlement: bool,
    caveats: Vec<String>,
}

#[derive(Clone, Debug)]
struct CoverageProofSnapshot {
    proof_id: String,
    safety_tier: String,
    execution_safe: bool,
}

impl CoverageProofSnapshot {
    fn from_py(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let dict = value.cast::<PyDict>()?;
        Ok(Self {
            proof_id: get_string(dict, "proof_id")?,
            safety_tier: get_optional_string(dict, "safety_tier")?.unwrap_or_default(),
            execution_safe: get_optional_bool(dict, "execution_safe")?.unwrap_or(false),
        })
    }
}

#[derive(Clone, Debug)]
struct CoverageHyperedgeSnapshot {
    hyperedge_id: String,
    coverage_proof_id: String,
    instrument_ids: Vec<String>,
    safety_tier: String,
    execution_safe: bool,
}

impl CoverageHyperedgeSnapshot {
    fn from_py(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let dict = value.cast::<PyDict>()?;
        Ok(Self {
            hyperedge_id: get_string(dict, "hyperedge_id")?,
            coverage_proof_id: get_string(dict, "coverage_proof_id")?,
            instrument_ids: get_string_vec(dict, "instrument_ids")?,
            safety_tier: get_optional_string(dict, "safety_tier")?.unwrap_or_default(),
            execution_safe: get_optional_bool(dict, "execution_safe")?.unwrap_or(false),
        })
    }
}

impl SemanticTemplateSnapshot {
    fn from_py(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let dict = value.cast::<PyDict>()?;
        Ok(Self {
            template_id: get_string(dict, "template_id")?,
            relationship_type: get_string(dict, "relationship_type")?,
            pattern_a: SemanticPatternSnapshot::from_py(
                &dict
                    .get_item("pattern_a")?
                    .ok_or_else(|| PyKeyError::new_err("pattern_a"))?,
            )?,
            pattern_b: SemanticPatternSnapshot::from_py(
                &dict
                    .get_item("pattern_b")?
                    .ok_or_else(|| PyKeyError::new_err("pattern_b"))?,
            )?,
            confidence: get_f64(dict, "confidence")?,
            provider_scope: get_string_vec(dict, "provider_scope")?,
            venue_agnostic: get_bool(dict, "venue_agnostic")?,
            safety_tier: get_string(dict, "safety_tier")?,
            promotion_status: get_string(dict, "promotion_status")?,
            push_capable: get_bool(dict, "push_capable")?,
            execution_safe: get_bool(dict, "execution_safe")?,
            same_venue_execution_eligible: get_optional_bool(
                dict,
                "same_venue_execution_eligible",
            )?
            .unwrap_or(false),
            partial_settlement: get_optional_bool(dict, "partial_settlement")?.unwrap_or(false),
            caveats: get_string_vec(dict, "caveats")?,
        })
    }

    fn matches_pair(
        &self,
        source: &NodeSnapshot,
        target: &NodeSnapshot,
    ) -> Option<SemanticTemplateMatch> {
        if self.template_id.is_empty()
            || self.relationship_type.is_empty()
            || self.promotion_status != "PROMOTED"
            || self.safety_tier == "AUDIT_ONLY"
        {
            return None;
        }
        if !self.applies_to_venues(source, target) {
            return None;
        }
        if !self.patterns_match(source, target) {
            return None;
        }
        let hedge_type = if source.semantic_market_type == target.semantic_market_type
            && source.semantic_params_key == target.semantic_params_key
        {
            "same_market"
        } else {
            "cross_market"
        };
        Some(SemanticTemplateMatch {
            hedge_type: hedge_type.to_string(),
            confidence: self.confidence,
            push_capable: self.push_capable,
            execution_safe: self.execution_safe,
            template_id: self.template_id.clone(),
            relationship_type: self.relationship_type.clone(),
            promotion_status: self.promotion_status.clone(),
            safety_tier: self.safety_tier.clone(),
            same_venue_execution_eligible: self.same_venue_execution_eligible,
            partial_settlement: self.partial_settlement,
            caveats: self.caveats.clone(),
        })
    }

    fn patterns_match(&self, source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
        self.patterns_match_order(&self.pattern_a, source, &self.pattern_b, target)
            || self.patterns_match_order(&self.pattern_b, source, &self.pattern_a, target)
    }

    fn patterns_match_order(
        &self,
        pattern_a: &SemanticPatternSnapshot,
        node_a: &NodeSnapshot,
        pattern_b: &SemanticPatternSnapshot,
        node_b: &NodeSnapshot,
    ) -> bool {
        if !(pattern_a.matches_node_identity(node_a) && pattern_b.matches_node_identity(node_b)) {
            return false;
        }
        if pattern_a.params_key == node_a.semantic_params_key
            && pattern_b.params_key == node_b.semantic_params_key
        {
            return true;
        }
        line_params_compatible(pattern_a, node_a, pattern_b, node_b)
    }

    fn applies_to_venues(&self, source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
        if self.venue_agnostic {
            return true;
        }
        if self.provider_scope.is_empty() {
            return false;
        }
        self.provider_scope.contains(&source.venue) && self.provider_scope.contains(&target.venue)
    }
}

#[derive(Clone, Copy, Debug)]
struct QuoteSnapshot {
    odds: f64,
    exchange_ts_ns: i64,
}

#[derive(Debug)]
#[pyclass(module = "nautilus_trader.core.nautilus_pyo3.model")]
pub struct OpportunityGraphCore {
    include_cross_venue: bool,
    min_confidence: f64,
    nodes_by_id: HashMap<String, NodeSnapshot>,
    edges_by_id: HashMap<String, EdgeSnapshot>,
    edge_ids_by_node_id: HashMap<String, Vec<String>>,
    quotes_by_node_id: HashMap<String, QuoteSnapshot>,
    event_buckets: HashMap<String, Vec<String>>,
    venue_event_buckets: HashMap<String, Vec<String>>,
    semantic_templates: Vec<SemanticTemplateSnapshot>,
    coverage_proofs: Vec<CoverageProofSnapshot>,
    coverage_hyperedges: Vec<CoverageHyperedgeSnapshot>,
}

#[pymethods]
impl OpportunityGraphCore {
    #[new]
    #[pyo3(signature = (include_cross_venue=true, min_confidence=0.5))]
    fn new(include_cross_venue: bool, min_confidence: f64) -> Self {
        Self {
            include_cross_venue,
            min_confidence,
            nodes_by_id: HashMap::default(),
            edges_by_id: HashMap::default(),
            edge_ids_by_node_id: HashMap::default(),
            quotes_by_node_id: HashMap::default(),
            event_buckets: HashMap::default(),
            venue_event_buckets: HashMap::default(),
            semantic_templates: Vec::default(),
            coverage_proofs: Vec::default(),
            coverage_hyperedges: Vec::default(),
        }
    }

    fn clear(&mut self) {
        self.nodes_by_id.clear();
        self.edges_by_id.clear();
        self.edge_ids_by_node_id.clear();
        self.quotes_by_node_id.clear();
        self.event_buckets.clear();
        self.venue_event_buckets.clear();
        self.semantic_templates.clear();
        self.coverage_proofs.clear();
        self.coverage_hyperedges.clear();
    }

    fn build(&mut self, nodes: &Bound<'_, PyAny>) -> PyResult<()> {
        self.clear();
        for item in nodes.try_iter()? {
            self.insert_node(NodeSnapshot::from_py(&item?)?);
        }
        self.rebuild_edges();
        Ok(())
    }

    fn build_with_edges(
        &mut self,
        nodes: &Bound<'_, PyAny>,
        edges: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        self.clear();
        for item in nodes.try_iter()? {
            self.insert_node(NodeSnapshot::from_py(&item?)?);
        }
        for item in edges.try_iter()? {
            let edge = self.edge_from_py(&item?)?;
            self.insert_edge(edge);
        }
        Ok(())
    }

    fn load_semantic_templates(&mut self, templates: &Bound<'_, PyAny>) -> PyResult<usize> {
        self.semantic_templates.clear();
        for item in templates.try_iter()? {
            self.semantic_templates
                .push(SemanticTemplateSnapshot::from_py(&item?)?);
        }
        Ok(self.semantic_templates.len())
    }

    fn load_semantic_coverage(
        &mut self,
        proofs: &Bound<'_, PyAny>,
        hyperedges: &Bound<'_, PyAny>,
    ) -> PyResult<(usize, usize)> {
        self.coverage_proofs.clear();
        self.coverage_hyperedges.clear();
        for item in proofs.try_iter()? {
            self.coverage_proofs
                .push(CoverageProofSnapshot::from_py(&item?)?);
        }
        for item in hyperedges.try_iter()? {
            self.coverage_hyperedges
                .push(CoverageHyperedgeSnapshot::from_py(&item?)?);
        }
        Ok((self.coverage_proofs.len(), self.coverage_hyperedges.len()))
    }

    fn build_semantic(
        &mut self,
        nodes: &Bound<'_, PyAny>,
        templates: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        self.clear();
        self.load_semantic_templates(templates)?;
        for item in nodes.try_iter()? {
            self.insert_node(NodeSnapshot::from_py(&item?)?);
        }
        self.rebuild_semantic_edges();
        Ok(())
    }

    fn add_instrument(&mut self, node: &Bound<'_, PyAny>) -> PyResult<bool> {
        let snapshot = NodeSnapshot::from_py(node)?;
        if self.nodes_by_id.contains_key(&snapshot.node_id) {
            return Ok(false);
        }
        let mut node_id = String::default();
        node_id.clone_from(&snapshot.node_id);
        self.insert_node(snapshot);
        self.connect_node(&node_id);
        Ok(true)
    }

    fn add_instrument_semantic(&mut self, node: &Bound<'_, PyAny>) -> PyResult<bool> {
        let snapshot = NodeSnapshot::from_py(node)?;
        if self.nodes_by_id.contains_key(&snapshot.node_id) {
            return Ok(false);
        }
        let mut node_id = String::default();
        node_id.clone_from(&snapshot.node_id);
        self.insert_node(snapshot);
        self.connect_node_semantic(&node_id);
        Ok(true)
    }

    fn add_edge(&mut self, edge: &Bound<'_, PyAny>) -> PyResult<bool> {
        let edge = self.edge_from_py(edge)?;
        Ok(self.insert_edge(edge))
    }

    fn update_quote(
        &mut self,
        node_id: &str,
        odds: f64,
        _received_ns: i64,
        exchange_ts_ns: i64,
    ) -> bool {
        if !self.nodes_by_id.contains_key(node_id) || odds <= 0.0 {
            return false;
        }
        self.quotes_by_node_id.insert(
            node_id.to_string(),
            QuoteSnapshot {
                odds,
                exchange_ts_ns,
            },
        );
        true
    }

    fn evaluate_updated_node(
        &mut self,
        node_id: &str,
        min_profit_margin: f64,
        now_ns: i64,
    ) -> Vec<CandidateSnapshot> {
        self.evaluate_connected_edges(node_id, min_profit_margin, now_ns)
    }

    fn update_quote_and_evaluate(
        &mut self,
        node_id: &str,
        odds: f64,
        received_ns: i64,
        exchange_ts_ns: i64,
        min_profit_margin: f64,
        now_ns: i64,
    ) -> Vec<CandidateSnapshot> {
        if !self.update_quote(node_id, odds, received_ns, exchange_ts_ns) {
            return Vec::default();
        }
        self.evaluate_connected_edges(node_id, min_profit_margin, now_ns)
    }

    fn update_quote_and_scan_fast(
        &mut self,
        node_id: &str,
        odds: f64,
        received_ns: i64,
        exchange_ts_ns: i64,
        min_profit_margin: f64,
        now_ns: i64,
    ) -> Vec<FastCandidateSnapshot> {
        if !self.update_quote(node_id, odds, received_ns, exchange_ts_ns) {
            return Vec::default();
        }
        self.evaluate_connected_edges_fast(node_id, min_profit_margin, now_ns)
    }

    fn connected_edge_count(&self, node_id: &str) -> usize {
        self.edge_ids_by_node_id
            .get(node_id)
            .map_or(0, std::vec::Vec::len)
    }

    fn node_count(&self) -> usize {
        self.nodes_by_id.len()
    }

    fn edge_count(&self) -> usize {
        self.edges_by_id.len()
    }

    fn quote_state_count(&self) -> usize {
        self.quotes_by_node_id.len()
    }

    fn semantic_template_count(&self) -> usize {
        self.semantic_templates.len()
    }

    fn coverage_proof_count(&self) -> usize {
        self.coverage_proofs.len()
    }

    fn coverage_hyperedge_count(&self) -> usize {
        self.coverage_hyperedges.len()
    }

    fn semantic_coverage_summary_json(&self) -> String {
        let mut proof_tiers: HashMap<String, usize> = HashMap::default();
        let mut hyperedge_tiers: HashMap<String, usize> = HashMap::default();
        for proof in &self.coverage_proofs {
            *proof_tiers.entry(proof.safety_tier.clone()).or_default() += 1;
        }
        for hyperedge in &self.coverage_hyperedges {
            *hyperedge_tiers
                .entry(hyperedge.safety_tier.clone())
                .or_default() += 1;
        }
        json!({
            "coverageProofCount": self.coverage_proofs.len(),
            "coverageHyperedgeCount": self.coverage_hyperedges.len(),
            "executionSafeCoverageProofCount": self.coverage_proofs.iter().filter(|proof| proof.execution_safe).count(),
            "executionSafeCoverageHyperedgeCount": self.coverage_hyperedges.iter().filter(|hyperedge| hyperedge.execution_safe).count(),
            "proofSafetyTierCounts": proof_tiers,
            "hyperedgeSafetyTierCounts": hyperedge_tiers,
            "sampleProofIds": self.coverage_proofs.iter().take(5).map(|proof| proof.proof_id.as_str()).collect::<Vec<_>>(),
            "sampleHyperedges": self.coverage_hyperedges.iter().take(5).map(|hyperedge| {
                json!({
                    "hyperedgeId": hyperedge.hyperedge_id.as_str(),
                    "coverageProofId": hyperedge.coverage_proof_id.as_str(),
                    "instrumentIds": &hyperedge.instrument_ids,
                })
            }).collect::<Vec<_>>(),
        })
        .to_string()
    }

    fn edge_snapshots(&self) -> Vec<EdgeExportSnapshot> {
        self.edges_by_id
            .values()
            .map(|edge| {
                (
                    edge.edge_id.clone(),
                    edge.source_node_id.clone(),
                    edge.target_node_id.clone(),
                    edge.hedge_type.clone(),
                    edge.confidence,
                    edge.flags.same_venue,
                    semantic_edge_metadata(edge),
                    edge.flags.push_capable,
                    edge.flags.execution_safe,
                    edge.last_margin,
                    edge.last_evaluated_ns,
                    edge.last_updated_ns,
                )
            })
            .collect()
    }
}

impl OpportunityGraphCore {
    fn insert_node(&mut self, node: NodeSnapshot) {
        let mut node_id = String::default();
        node_id.clone_from(&node.node_id);
        self.edge_ids_by_node_id.entry(node_id.clone()).or_default();
        self.event_buckets
            .entry(node.event_key_no_time.clone())
            .or_default()
            .push(node_id.clone());
        self.venue_event_buckets
            .entry(format!("{}|{}", node.venue, node.event_id))
            .or_default()
            .push(node_id.clone());
        self.nodes_by_id.insert(node_id, node);
    }

    fn rebuild_edges(&mut self) {
        self.edges_by_id.clear();
        for edge_ids in self.edge_ids_by_node_id.values_mut() {
            edge_ids.clear();
        }

        let mut visited_pairs = HashSet::default();
        let event_buckets: Vec<Vec<String>> = self.event_buckets.values().cloned().collect();
        for bucket in event_buckets {
            self.connect_bucket(&bucket, &mut visited_pairs);
        }
        let venue_event_buckets: Vec<Vec<String>> =
            self.venue_event_buckets.values().cloned().collect();
        for bucket in venue_event_buckets {
            self.connect_bucket(&bucket, &mut visited_pairs);
        }
    }

    fn rebuild_semantic_edges(&mut self) {
        self.edges_by_id.clear();
        for edge_ids in self.edge_ids_by_node_id.values_mut() {
            edge_ids.clear();
        }

        let mut visited_pairs = HashSet::default();
        let event_buckets: Vec<Vec<String>> = self.event_buckets.values().cloned().collect();
        for bucket in event_buckets {
            self.connect_semantic_bucket(&bucket, &mut visited_pairs);
        }
        let venue_event_buckets: Vec<Vec<String>> =
            self.venue_event_buckets.values().cloned().collect();
        for bucket in venue_event_buckets {
            self.connect_semantic_bucket(&bucket, &mut visited_pairs);
        }
    }

    fn connect_node(&mut self, node_id: &str) {
        let mut visited_pairs = HashSet::default();
        let Some(node) = self.nodes_by_id.get(node_id) else {
            return;
        };
        let mut event_key_no_time = String::default();
        event_key_no_time.clone_from(&node.event_key_no_time);
        let venue_event_key = format!("{}|{}", node.venue, node.event_id);

        if let Some(bucket) = self.event_buckets.get(&event_key_no_time).cloned() {
            self.connect_node_to_bucket(node_id, &bucket, &mut visited_pairs);
        }
        if let Some(bucket) = self.venue_event_buckets.get(&venue_event_key).cloned() {
            self.connect_node_to_bucket(node_id, &bucket, &mut visited_pairs);
        }
    }

    fn connect_node_semantic(&mut self, node_id: &str) {
        let mut visited_pairs = HashSet::default();
        let Some(node) = self.nodes_by_id.get(node_id) else {
            return;
        };
        let mut event_key_no_time = String::default();
        event_key_no_time.clone_from(&node.event_key_no_time);
        let venue_event_key = format!("{}|{}", node.venue, node.event_id);

        if let Some(bucket) = self.event_buckets.get(&event_key_no_time).cloned() {
            self.connect_node_to_semantic_bucket(node_id, &bucket, &mut visited_pairs);
        }
        if let Some(bucket) = self.venue_event_buckets.get(&venue_event_key).cloned() {
            self.connect_node_to_semantic_bucket(node_id, &bucket, &mut visited_pairs);
        }
    }

    fn connect_bucket(&mut self, bucket: &[String], visited_pairs: &mut HashSet<String>) {
        for (index, source_id) in bucket.iter().enumerate() {
            for target_id in bucket.iter().skip(index + 1) {
                self.connect_pair(source_id, target_id, visited_pairs);
            }
        }
    }

    fn connect_semantic_bucket(&mut self, bucket: &[String], visited_pairs: &mut HashSet<String>) {
        for (index, source_id) in bucket.iter().enumerate() {
            for target_id in bucket.iter().skip(index + 1) {
                self.connect_pair_semantic(source_id, target_id, visited_pairs);
            }
        }
    }

    fn connect_node_to_bucket(
        &mut self,
        node_id: &str,
        bucket: &[String],
        visited_pairs: &mut HashSet<String>,
    ) {
        for target_id in bucket {
            if target_id != node_id {
                self.connect_pair(node_id, target_id, visited_pairs);
            }
        }
    }

    fn connect_node_to_semantic_bucket(
        &mut self,
        node_id: &str,
        bucket: &[String],
        visited_pairs: &mut HashSet<String>,
    ) {
        for target_id in bucket {
            if target_id != node_id {
                self.connect_pair_semantic(node_id, target_id, visited_pairs);
            }
        }
    }

    fn connect_pair(
        &mut self,
        source_id: &str,
        target_id: &str,
        visited_pairs: &mut HashSet<String>,
    ) {
        let pair_id = edge_id(source_id, target_id);
        if !visited_pairs.insert(pair_id.clone()) {
            return;
        }

        let Some(source) = self.nodes_by_id.get(source_id) else {
            return;
        };
        let Some(target) = self.nodes_by_id.get(target_id) else {
            return;
        };

        if !self.include_cross_venue && source.venue != target.venue {
            return;
        }
        if !self.is_event_match(source, target) {
            return;
        }

        let hedge = if is_same_market_hedge(source, target) {
            Some(("same_market", 1.0))
        } else {
            let confidence = cross_market_confidence(source, target);
            if confidence > 0.0 {
                Some(("cross_market", confidence))
            } else {
                None
            }
        };

        let Some((hedge_type, confidence)) = hedge else {
            return;
        };
        if confidence < self.min_confidence {
            return;
        }

        self.upsert_edge(source_id, target_id, hedge_type, confidence, pair_id);
    }

    fn connect_pair_semantic(
        &mut self,
        source_id: &str,
        target_id: &str,
        visited_pairs: &mut HashSet<String>,
    ) {
        let pair_id = edge_id(source_id, target_id);
        if !visited_pairs.insert(pair_id.clone()) {
            return;
        }

        let Some(source) = self.nodes_by_id.get(source_id) else {
            return;
        };
        let Some(target) = self.nodes_by_id.get(target_id) else {
            return;
        };

        if !self.include_cross_venue && source.venue != target.venue {
            return;
        }
        if !self.is_event_match(source, target) {
            return;
        }

        let Some(template_match) = self
            .semantic_templates
            .iter()
            .find_map(|template| template.matches_pair(source, target))
        else {
            return;
        };

        if template_match.confidence < self.min_confidence {
            return;
        }

        self.upsert_semantic_edge(source_id, target_id, template_match, pair_id);
    }

    fn upsert_edge(
        &mut self,
        source_id: &str,
        target_id: &str,
        hedge_type: &str,
        confidence: f64,
        edge_id: String,
    ) {
        if let Some(existing) = self.edges_by_id.get_mut(&edge_id) {
            if confidence > existing.confidence {
                existing.hedge_type = hedge_type.to_string();
                existing.confidence = confidence;
                existing.source_node_id = source_id.to_string();
                existing.target_node_id = target_id.to_string();
            }
            return;
        }

        let source = &self.nodes_by_id[source_id];
        let target = &self.nodes_by_id[target_id];
        let push_capable =
            is_push_capable(&source.market_type) || is_push_capable(&target.market_type);
        let matcher_suspect = source.venue == target.venue
            && source.event_id != target.event_id
            && !is_trusted_same_venue_event_id_mismatch(source, target);
        let edge = EdgeSnapshot {
            edge_id: edge_id.clone(),
            source_node_id: source_id.to_string(),
            target_node_id: target_id.to_string(),
            hedge_type: hedge_type.to_string(),
            confidence,
            flags: EdgeFlags {
                same_venue: source.venue == target.venue,
                push_capable,
                execution_safe: !push_capable,
                matcher_suspect,
            },
            market_relationship_type: if source.market_name == target.market_name {
                "same_market".to_string()
            } else {
                "cross_market".to_string()
            },
            last_margin: None,
            last_evaluated_ns: None,
            last_updated_ns: None,
            template_id: None,
            relationship_type: None,
            promotion_status: None,
            safety_tier: None,
            same_venue_execution_eligible: false,
            partial_settlement: false,
            caveats: Vec::default(),
        };
        self.edges_by_id.insert(edge_id.clone(), edge);
        self.edge_ids_by_node_id
            .entry(source_id.to_string())
            .or_default()
            .push(edge_id.clone());
        self.edge_ids_by_node_id
            .entry(target_id.to_string())
            .or_default()
            .push(edge_id);
    }

    fn upsert_semantic_edge(
        &mut self,
        source_id: &str,
        target_id: &str,
        template_match: SemanticTemplateMatch,
        edge_id: String,
    ) {
        if let Some(existing) = self.edges_by_id.get_mut(&edge_id) {
            if template_match.confidence > existing.confidence
                || (template_match.execution_safe && !existing.flags.execution_safe)
            {
                existing.hedge_type = template_match.hedge_type;
                existing.confidence = template_match.confidence;
                existing.source_node_id = source_id.to_string();
                existing.target_node_id = target_id.to_string();
                existing.flags.push_capable = template_match.push_capable;
                existing.flags.execution_safe = template_match.execution_safe;
                existing.template_id = Some(template_match.template_id);
                existing.relationship_type = Some(template_match.relationship_type);
                existing.promotion_status = Some(template_match.promotion_status);
                existing.safety_tier = Some(template_match.safety_tier);
                existing.same_venue_execution_eligible =
                    template_match.same_venue_execution_eligible;
                existing.partial_settlement = template_match.partial_settlement;
                existing.caveats = template_match.caveats;
            }
            return;
        }

        let source = &self.nodes_by_id[source_id];
        let target = &self.nodes_by_id[target_id];
        let matcher_suspect = source.venue == target.venue
            && source.event_id != target.event_id
            && !is_trusted_same_venue_event_id_mismatch(source, target);
        let market_relationship_type = if source.semantic_market_type == target.semantic_market_type
            && source.semantic_params_key == target.semantic_params_key
        {
            "same_market"
        } else {
            "cross_market"
        };
        let edge = EdgeSnapshot {
            edge_id: edge_id.clone(),
            source_node_id: source_id.to_string(),
            target_node_id: target_id.to_string(),
            hedge_type: template_match.hedge_type,
            confidence: template_match.confidence,
            flags: EdgeFlags {
                same_venue: source.venue == target.venue,
                push_capable: template_match.push_capable,
                execution_safe: template_match.execution_safe,
                matcher_suspect,
            },
            market_relationship_type: market_relationship_type.to_string(),
            last_margin: None,
            last_evaluated_ns: None,
            last_updated_ns: None,
            template_id: Some(template_match.template_id),
            relationship_type: Some(template_match.relationship_type),
            promotion_status: Some(template_match.promotion_status),
            safety_tier: Some(template_match.safety_tier),
            same_venue_execution_eligible: template_match.same_venue_execution_eligible,
            partial_settlement: template_match.partial_settlement,
            caveats: template_match.caveats,
        };
        self.insert_edge(edge);
    }

    fn edge_from_py(&self, value: &Bound<'_, PyAny>) -> PyResult<EdgeSnapshot> {
        let dict = value.cast::<PyDict>()?;
        let source_node_id = get_string(dict, "source_node_id")?;
        let target_node_id = get_string(dict, "target_node_id")?;
        let edge_id_value = get_optional_string(dict, "edge_id")?
            .unwrap_or_else(|| edge_id(&source_node_id, &target_node_id));
        let source = self.nodes_by_id.get(&source_node_id);
        let target = self.nodes_by_id.get(&target_node_id);
        let same_venue = get_optional_bool(dict, "same_venue")?.unwrap_or_else(|| {
            source
                .zip(target)
                .is_some_and(|(left, right)| left.venue == right.venue)
        });
        let push_capable = get_optional_bool(dict, "push_capable")?.unwrap_or(false);
        Ok(EdgeSnapshot {
            edge_id: edge_id_value,
            source_node_id,
            target_node_id,
            hedge_type: get_optional_string(dict, "hedge_type")?
                .unwrap_or_else(|| "semantic".to_string()),
            confidence: get_optional_f64(dict, "confidence")?.unwrap_or(1.0),
            flags: EdgeFlags {
                same_venue,
                push_capable,
                execution_safe: get_optional_bool(dict, "execution_safe")?.unwrap_or(!push_capable),
                matcher_suspect: get_optional_bool(dict, "matcher_suspect")?.unwrap_or(false),
            },
            market_relationship_type: get_optional_string(dict, "market_relationship_type")?
                .unwrap_or_else(|| "cross_market".to_string()),
            last_margin: None,
            last_evaluated_ns: None,
            last_updated_ns: None,
            template_id: get_optional_string(dict, "template_id")?,
            relationship_type: get_optional_string(dict, "relationship_type")?,
            promotion_status: get_optional_string(dict, "promotion_status")?,
            safety_tier: get_optional_string(dict, "safety_tier")?,
            same_venue_execution_eligible: get_optional_bool(
                dict,
                "same_venue_execution_eligible",
            )?
            .unwrap_or(false),
            partial_settlement: get_optional_bool(dict, "partial_settlement")?.unwrap_or(false),
            caveats: get_string_vec(dict, "caveats")?,
        })
    }

    fn insert_edge(&mut self, edge: EdgeSnapshot) -> bool {
        if !self.nodes_by_id.contains_key(&edge.source_node_id)
            || !self.nodes_by_id.contains_key(&edge.target_node_id)
            || edge.confidence < self.min_confidence
        {
            return false;
        }
        let edge_id_value = edge.edge_id.clone();
        let source_node_id = edge.source_node_id.clone();
        let target_node_id = edge.target_node_id.clone();
        let replaced = self
            .edges_by_id
            .insert(edge_id_value.clone(), edge)
            .is_some();
        if !replaced {
            self.edge_ids_by_node_id
                .entry(source_node_id)
                .or_default()
                .push(edge_id_value.clone());
            self.edge_ids_by_node_id
                .entry(target_node_id)
                .or_default()
                .push(edge_id_value);
        }
        true
    }

    fn is_event_match(&self, source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
        if source.venue == target.venue {
            if source.event_id == target.event_id {
                return true;
            }
            return is_trusted_same_venue_event_id_mismatch(source, target);
        }
        if source.event_key_no_time != target.event_key_no_time {
            return false;
        }
        match (source.start_time_ns, target.start_time_ns) {
            (Some(source_start), Some(target_start)) => {
                (source_start - target_start).abs() <= SIX_HOURS_NS
            }
            _ => self.start_time_cluster_count_for_pair(source, target) == 1,
        }
    }

    fn start_time_cluster_count_for_pair(
        &self,
        source: &NodeSnapshot,
        target: &NodeSnapshot,
    ) -> usize {
        let Some(bucket) = self.event_buckets.get(&source.event_key_no_time) else {
            return 0;
        };
        let mut starts: Vec<i64> = bucket
            .iter()
            .filter_map(|node_id| {
                let node = self.nodes_by_id.get(node_id)?;
                if node.venue == source.venue || node.venue == target.venue {
                    node.start_time_ns
                } else {
                    None
                }
            })
            .collect();
        starts.sort_unstable();
        let Some(mut cluster_anchor) = starts.first().copied() else {
            return 0;
        };

        let mut clusters = 1;
        for start in starts.into_iter().skip(1) {
            if start - cluster_anchor > SIX_HOURS_NS {
                clusters += 1;
                cluster_anchor = start;
            }
        }
        clusters
    }

    fn evaluate_connected_edges(
        &mut self,
        node_id: &str,
        min_profit_margin: f64,
        now_ns: i64,
    ) -> Vec<CandidateSnapshot> {
        let Some(updated_quote) = self.quotes_by_node_id.get(node_id).copied() else {
            return Vec::default();
        };
        let Some(edge_ids) = self.edge_ids_by_node_id.get(node_id).cloned() else {
            return Vec::default();
        };

        let mut candidates = Vec::default();
        for edge_id in edge_ids {
            let Some(edge) = self.edges_by_id.get_mut(&edge_id) else {
                continue;
            };
            if edge.flags.push_capable || !edge.flags.execution_safe {
                continue;
            }
            let other_node_id = if edge.source_node_id == node_id {
                edge.target_node_id.clone()
            } else {
                edge.source_node_id.clone()
            };
            let Some(other_quote) = self.quotes_by_node_id.get(&other_node_id).copied() else {
                continue;
            };

            let margin = profit_margin(updated_quote.odds, other_quote.odds);
            edge.last_margin = Some(margin);
            edge.last_evaluated_ns = Some(now_ns);
            edge.last_updated_ns = Some(now_ns);
            if margin + PROFIT_MARGIN_EPSILON < min_profit_margin {
                continue;
            }

            candidates.push((
                edge_id,
                node_id.to_string(),
                other_node_id,
                margin,
                updated_quote.exchange_ts_ns,
                other_quote.exchange_ts_ns,
            ));
        }
        candidates
    }

    fn evaluate_connected_edges_fast(
        &mut self,
        node_id: &str,
        min_profit_margin: f64,
        now_ns: i64,
    ) -> Vec<FastCandidateSnapshot> {
        let Some(updated_quote) = self.quotes_by_node_id.get(node_id).copied() else {
            return Vec::default();
        };
        let Some(edge_ids) = self.edge_ids_by_node_id.get(node_id).cloned() else {
            return Vec::default();
        };

        let mut candidates = Vec::default();
        for edge_id in edge_ids {
            let Some(edge) = self.edges_by_id.get_mut(&edge_id) else {
                continue;
            };
            if edge.flags.push_capable || !edge.flags.execution_safe {
                continue;
            }
            let other_node_id = if edge.source_node_id == node_id {
                edge.target_node_id.clone()
            } else {
                edge.source_node_id.clone()
            };
            let Some(other_quote) = self.quotes_by_node_id.get(&other_node_id).copied() else {
                continue;
            };

            let margin = profit_margin(updated_quote.odds, other_quote.odds);
            edge.last_margin = Some(margin);
            edge.last_evaluated_ns = Some(now_ns);
            edge.last_updated_ns = Some(now_ns);
            if margin + PROFIT_MARGIN_EPSILON < min_profit_margin {
                continue;
            }

            candidates.push((
                edge_id,
                node_id.to_string(),
                other_node_id,
                edge.hedge_type.clone(),
                edge.confidence,
                updated_quote.odds,
                other_quote.odds,
                margin,
                updated_quote.exchange_ts_ns,
                other_quote.exchange_ts_ns,
                fast_match_type(edge).to_string(),
                edge.flags.matcher_suspect,
            ));
        }
        candidates
    }
}

fn fast_match_type(edge: &EdgeSnapshot) -> &'static str {
    if edge.market_relationship_type == "same_market" {
        "same_market"
    } else if edge.flags.same_venue {
        "cross_market"
    } else {
        "cross_venue"
    }
}

fn get_string(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    dict.get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(key.to_string()))?
        .extract()
}

fn get_f64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
    dict.get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(key.to_string()))?
        .extract()
}

fn get_bool(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    dict.get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(key.to_string()))?
        .extract()
}

fn get_optional_string(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract().map(Some),
        _ => Ok(None),
    }
}

fn get_optional_bool(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<bool>> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract().map(Some),
        _ => Ok(None),
    }
}

fn get_optional_f64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<f64>> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract().map(Some),
        _ => Ok(None),
    }
}

fn get_optional_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract().map(Some),
        _ => Ok(None),
    }
}

fn get_string_vec(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<String>> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract(),
        _ => Ok(Vec::default()),
    }
}

fn edge_id(source_id: &str, target_id: &str) -> String {
    if source_id <= target_id {
        format!("{source_id}|{target_id}")
    } else {
        format!("{target_id}|{source_id}")
    }
}

fn semantic_edge_metadata(edge: &EdgeSnapshot) -> String {
    json!({
        "template_id": edge.template_id.as_deref(),
        "relationship_type": edge.relationship_type.as_deref(),
        "promotion_status": edge.promotion_status.as_deref(),
        "safety_tier": edge.safety_tier.as_deref(),
        "market_relationship_type": edge.market_relationship_type.as_str(),
        "same_venue_execution_eligible": edge.same_venue_execution_eligible,
        "partial_settlement": edge.partial_settlement,
        "caveats": &edge.caveats,
    })
    .to_string()
}

fn line_params_compatible(
    pattern_a: &SemanticPatternSnapshot,
    node_a: &NodeSnapshot,
    pattern_b: &SemanticPatternSnapshot,
    node_b: &NodeSnapshot,
) -> bool {
    let Some(pattern_line_a) = only_line_param(&pattern_a.params_key) else {
        return false;
    };
    let Some(pattern_line_b) = only_line_param(&pattern_b.params_key) else {
        return false;
    };
    let Some(node_line_a) = only_line_param(&node_a.semantic_params_key) else {
        return false;
    };
    let Some(node_line_b) = only_line_param(&node_b.semantic_params_key) else {
        return false;
    };

    if approx_eq(pattern_line_a, pattern_line_b) && approx_eq(node_line_a, node_line_b) {
        return true;
    }

    approx_eq(pattern_line_a + pattern_line_b, 0.0) && approx_eq(node_line_a + node_line_b, 0.0)
}

fn only_line_param(params_key: &str) -> Option<f64> {
    let params: Value = serde_json::from_str(params_key).ok()?;
    let pairs = params.as_array()?;
    if pairs.len() != 1 {
        return None;
    }
    let pair = pairs.first()?.as_array()?;
    if pair.len() != 2 || pair.first()?.as_str()? != "line" {
        return None;
    }
    match pair.get(1)? {
        Value::String(value) => value.parse::<f64>().ok(),
        Value::Number(value) => value.as_f64(),
        _ => None,
    }
}

fn approx_eq(left: f64, right: f64) -> bool {
    (left - right).abs() <= HANDICAP_TOLERANCE
}

fn is_push_capable(market_type: &str) -> bool {
    matches!(market_type, "draw_no_bet" | "asian_handicap")
}

fn is_same_market_hedge(source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
    if source.venue == target.venue
        && source.event_id != target.event_id
        && !is_trusted_same_venue_event_id_mismatch(source, target)
    {
        return false;
    }
    if source.market_name != target.market_name || source.params != target.params {
        return false;
    }
    if source.market_type == "match_odds" && !(source.two_way_market && target.two_way_market) {
        return false;
    }
    is_opposite_outcome(source, target)
}

fn is_trusted_same_venue_event_id_mismatch(source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
    if source.venue != target.venue || source.venue != "SXBET" {
        return false;
    }
    if source.event_key_no_time != target.event_key_no_time {
        return false;
    }
    if source.market_name != target.market_name || source.params != target.params {
        return false;
    }
    if source.market_type != "match_odds" || target.market_type != "match_odds" {
        return false;
    }
    if !(source.two_way_market && target.two_way_market) {
        return false;
    }
    is_opposite_outcome(source, target)
}

fn is_opposite_outcome(source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
    if matches!(source.outcome.as_str(), "home" | "away")
        && matches!(target.outcome.as_str(), "home" | "away")
    {
        return source.selection_key != target.selection_key;
    }

    matches!(
        (source.outcome.as_str(), target.outcome.as_str()),
        ("over", "under") | ("under", "over") | ("yes", "no") | ("no", "yes")
    )
}

fn cross_market_confidence(source: &NodeSnapshot, target: &NodeSnapshot) -> f64 {
    if !can_hedge_market(&source.market_type, &target.market_type) {
        return 0.0;
    }

    if let Some(confidence) = match_odds_double_chance_confidence(source, target) {
        return confidence;
    }
    if let Some(confidence) = asian_handicap_confidence(source, target) {
        return confidence;
    }
    if matches!(
        source.market_type.as_str(),
        "draw_no_bet" | "asian_handicap"
    ) && matches!(
        target.market_type.as_str(),
        "draw_no_bet" | "asian_handicap"
    ) && outcome_opposite(&source.outcome) == Some(target.outcome.as_str())
    {
        return 0.75;
    }
    0.0
}

fn can_hedge_market(source: &str, target: &str) -> bool {
    match source {
        "both_teams_to_score" => target == "both_teams_to_score",
        "total_goals" => target == "total_goals",
        "team_total_goals" => target == "team_total_goals",
        "draw_no_bet" => target == "draw_no_bet",
        "match_odds" => matches!(target, "double_chance" | "asian_handicap"),
        "double_chance" => matches!(target, "asian_handicap" | "match_odds"),
        "asian_handicap" => matches!(target, "asian_handicap" | "draw_no_bet" | "double_chance"),
        "match_odds_period_first_half" => target == "asian_handicap_period_first_half",
        "match_odds_period_second_half" => target == "asian_handicap_period_second_half",
        "asian_handicap_period_first_half" => {
            matches!(
                target,
                "match_odds_period_first_half" | "asian_handicap_period_first_half"
            )
        }
        "team_total_goals_period_first_half" => target == "team_total_goals_period_first_half",
        "team_total_goals_period_second_half" => target == "team_total_goals_period_second_half",
        _ => false,
    }
}

fn match_odds_double_chance_confidence(
    source: &NodeSnapshot,
    target: &NodeSnapshot,
) -> Option<f64> {
    match (
        source.market_type.as_str(),
        target.market_type.as_str(),
        source.outcome.as_str(),
        target.outcome.as_str(),
    ) {
        ("match_odds", "double_chance", "home", "away_draw")
        | ("match_odds", "double_chance", "draw", "home_away")
        | ("match_odds", "double_chance", "away", "home_draw")
        | ("double_chance", "match_odds", "away_draw", "home")
        | ("double_chance", "match_odds", "home_away", "draw")
        | ("double_chance", "match_odds", "home_draw", "away") => Some(0.95),
        ("match_odds", "double_chance", _, _) | ("double_chance", "match_odds", _, _) => Some(0.0),
        _ => None,
    }
}

fn asian_handicap_confidence(source: &NodeSnapshot, target: &NodeSnapshot) -> Option<f64> {
    if source.market_type != "asian_handicap" || target.market_type != "asian_handicap" {
        return None;
    }
    let source_handicap = source.handicap.unwrap_or_default();
    let target_handicap = target.handicap.unwrap_or_default();
    if (source_handicap + target_handicap).abs() < HANDICAP_TOLERANCE
        && matches!(
            (source.outcome.as_str(), target.outcome.as_str()),
            ("home", "away") | ("away", "home")
        )
    {
        return Some(0.85);
    }
    Some(0.0)
}

fn outcome_opposite(outcome: &str) -> Option<&'static str> {
    match outcome {
        "home" => Some("away"),
        "away" => Some("home"),
        "over" => Some("under"),
        "under" => Some("over"),
        "yes" => Some("no"),
        "no" => Some("yes"),
        _ => None,
    }
}

fn profit_margin(odds_a: f64, odds_b: f64) -> f64 {
    1.0 / ((1.0 / odds_a) + (1.0 / odds_b)) - 1.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyList;
    use rstest::rstest;

    fn assert_approx_eq(left: f64, right: f64) {
        assert!((left - right).abs() < f64::EPSILON);
    }

    fn node(id: &str, outcome: &str) -> NodeSnapshot {
        node_with(
            id,
            "SXBET",
            "event-1",
            "Total Goals",
            "total_goals",
            outcome,
        )
    }

    fn node_with(
        id: &str,
        venue: &str,
        event_id: &str,
        market_name: &str,
        market_type: &str,
        outcome: &str,
    ) -> NodeSnapshot {
        NodeSnapshot {
            node_id: id.to_string(),
            venue: venue.to_string(),
            event_id: event_id.to_string(),
            event_key_no_time: "soccer:team a:team b".to_string(),
            market_name: market_name.to_string(),
            market_type: market_type.to_string(),
            outcome: outcome.to_string(),
            selection_key: outcome.to_string(),
            params: "line=2.5".to_string(),
            handicap: None,
            start_time_ns: Some(1_778_000_000_000_000_000),
            two_way_market: false,
            semantic_sport: "soccer".to_string(),
            semantic_scope: "full_time".to_string(),
            semantic_market_type: market_type.to_string(),
            semantic_market_family: market_type.to_string(),
            semantic_selection: outcome.to_string(),
            semantic_params_key: "line=2.5".to_string(),
        }
    }

    fn py_payload<'py>(py: Python<'py>, node: &NodeSnapshot) -> Bound<'py, PyDict> {
        let dict = PyDict::new(py);
        dict.set_item("node_id", &node.node_id).unwrap();
        dict.set_item("venue", &node.venue).unwrap();
        dict.set_item("event_id", &node.event_id).unwrap();
        dict.set_item("event_key_no_time", &node.event_key_no_time)
            .unwrap();
        dict.set_item("market_name", &node.market_name).unwrap();
        dict.set_item("market_type", &node.market_type).unwrap();
        dict.set_item("outcome", &node.outcome).unwrap();
        dict.set_item("selection_key", &node.selection_key).unwrap();
        dict.set_item("params", &node.params).unwrap();
        match node.handicap {
            Some(handicap) => dict.set_item("handicap", handicap).unwrap(),
            None => dict.set_item("handicap", py.None()).unwrap(),
        }
        match node.start_time_ns {
            Some(start_time_ns) => dict.set_item("start_time_ns", start_time_ns).unwrap(),
            None => dict.set_item("start_time_ns", py.None()).unwrap(),
        }
        dict.set_item("two_way_market", node.two_way_market)
            .unwrap();
        dict.set_item("semantic_sport", &node.semantic_sport)
            .unwrap();
        dict.set_item("semantic_scope", &node.semantic_scope)
            .unwrap();
        dict.set_item("semantic_market_type", &node.semantic_market_type)
            .unwrap();
        dict.set_item("semantic_market_family", &node.semantic_market_family)
            .unwrap();
        dict.set_item("semantic_selection", &node.semantic_selection)
            .unwrap();
        dict.set_item("semantic_params_key", &node.semantic_params_key)
            .unwrap();
        dict
    }

    fn py_pattern<'py>(py: Python<'py>, market_type: &str, selection: &str) -> Bound<'py, PyDict> {
        let dict = PyDict::new(py);
        dict.set_item("sport", "soccer").unwrap();
        dict.set_item("scope", "full_time").unwrap();
        dict.set_item("market_type", market_type).unwrap();
        dict.set_item("market_family", market_type).unwrap();
        dict.set_item("selection", selection).unwrap();
        dict.set_item("params_key", "line=2.5").unwrap();
        dict
    }

    fn py_pattern_with_params<'py>(
        py: Python<'py>,
        market_type: &str,
        selection: &str,
        params_key: &str,
    ) -> Bound<'py, PyDict> {
        let dict = py_pattern(py, market_type, selection);
        dict.set_item("params_key", params_key).unwrap();
        dict
    }

    fn py_semantic_template<'py>(
        py: Python<'py>,
        provider_scope: Vec<&str>,
        venue_agnostic: bool,
    ) -> Bound<'py, PyDict> {
        let dict = PyDict::new(py);
        dict.set_item("template_id", "template-total-goals")
            .unwrap();
        dict.set_item("relationship_type", "COMPLEMENTARY_COVERAGE")
            .unwrap();
        dict.set_item("pattern_a", py_pattern(py, "total_goals", "over"))
            .unwrap();
        dict.set_item("pattern_b", py_pattern(py, "total_goals", "under"))
            .unwrap();
        dict.set_item("confidence", 1.0).unwrap();
        dict.set_item("provider_scope", provider_scope).unwrap();
        dict.set_item("venue_agnostic", venue_agnostic).unwrap();
        dict.set_item("safety_tier", "EXECUTION_SAFE").unwrap();
        dict.set_item("promotion_status", "PROMOTED").unwrap();
        dict.set_item("push_capable", false).unwrap();
        dict.set_item("execution_safe", true).unwrap();
        dict
    }

    fn py_edge<'py>(py: Python<'py>, source_id: &str, target_id: &str) -> Bound<'py, PyDict> {
        let dict = PyDict::new(py);
        dict.set_item("edge_id", edge_id(source_id, target_id))
            .unwrap();
        dict.set_item("source_node_id", source_id).unwrap();
        dict.set_item("target_node_id", target_id).unwrap();
        dict.set_item("hedge_type", "same_market").unwrap();
        dict.set_item("confidence", 1.0).unwrap();
        dict.set_item("same_venue", true).unwrap();
        dict.set_item("market_relationship_type", "same_market")
            .unwrap();
        dict.set_item("push_capable", false).unwrap();
        dict.set_item("execution_safe", true).unwrap();
        dict.set_item("matcher_suspect", false).unwrap();
        dict
    }

    #[rstest]
    fn same_market_opposite_outcomes_connect() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        core.rebuild_edges();

        assert_eq!(core.edge_count(), 1);
        assert_eq!(core.connected_edge_count("a"), 1);
    }

    #[rstest]
    fn quote_evaluation_filters_by_margin() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        core.rebuild_edges();
        assert!(core.update_quote("a", 2.4, 10, 10));
        assert!(core.update_quote("b", 2.55, 11, 11));

        let candidates = core.evaluate_connected_edges("a", 0.01, 12);

        assert_eq!(candidates.len(), 1);
        assert!(candidates[0].3 > 0.0);
    }

    #[rstest]
    fn build_and_add_from_python_payloads() {
        pyo3::Python::initialize();

        Python::attach(|py| {
            let over = py_payload(py, &node("a", "over"));
            let nodes = PyList::empty(py);
            nodes.append(&over).unwrap();

            let mut core = OpportunityGraphCore::new(true, 0.5);
            core.build(nodes.as_any()).unwrap();
            assert_eq!(core.node_count(), 1);

            let mut under_snapshot = node("b", "under");
            under_snapshot.handicap = Some(1.5);
            under_snapshot.start_time_ns = None;
            let under = py_payload(py, &under_snapshot);
            assert!(core.add_instrument(under.as_any()).unwrap());

            assert_eq!(core.edge_count(), 1);
            assert!(!core.add_instrument(over.as_any()).unwrap());
            assert_eq!(core.connected_edge_count("missing"), 0);
            assert!(core.update_quote("a", 2.4, 1, 1));
            assert_eq!(core.quote_state_count(), 1);
            assert!(core.evaluate_updated_node("a", 0.01, 2).is_empty());
            assert!(core.update_quote("b", 2.55, 2, 2));

            let candidates = core.evaluate_updated_node("b", 0.01, 3);

            assert_eq!(candidates.len(), 1);
            assert_eq!(candidates[0].1, "b");
            assert_eq!(candidates[0].2, "a");
        });
    }

    #[rstest]
    fn explicit_edge_payloads_drive_fast_scan_without_heuristics() {
        pyo3::Python::initialize();

        Python::attach(|py| {
            let mut source = node("a", "over");
            let mut target = node("b", "under");
            source.params = "line=3.5".to_string();
            target.params = "line=4.5".to_string();
            let nodes = PyList::empty(py);
            nodes.append(py_payload(py, &source)).unwrap();
            nodes.append(py_payload(py, &target)).unwrap();
            let edges = PyList::empty(py);
            edges.append(py_edge(py, "a", "b")).unwrap();

            let mut core = OpportunityGraphCore::new(true, 0.5);
            core.build_with_edges(nodes.as_any(), edges.as_any())
                .unwrap();

            assert_eq!(core.edge_count(), 1);
            assert!(core.update_quote("a", 2.4, 10, 100));
            let candidates = core.update_quote_and_scan_fast("b", 2.55, 11, 101, 0.01, 12);
            assert_eq!(candidates.len(), 1);
            assert_eq!(candidates[0].3, "same_market");
        });
    }

    #[rstest]
    fn semantic_templates_are_the_only_authority_in_semantic_mode() {
        pyo3::Python::initialize();

        Python::attach(|py| {
            let nodes = PyList::empty(py);
            nodes.append(py_payload(py, &node("a", "over"))).unwrap();
            nodes.append(py_payload(py, &node("b", "under"))).unwrap();
            let empty_templates = PyList::empty(py);

            let mut core = OpportunityGraphCore::new(true, 0.5);
            core.build_semantic(nodes.as_any(), empty_templates.as_any())
                .unwrap();
            assert_eq!(core.edge_count(), 0);

            let templates = PyList::empty(py);
            templates
                .append(py_semantic_template(py, vec!["SXBET"], false))
                .unwrap();
            core.build_semantic(nodes.as_any(), templates.as_any())
                .unwrap();

            assert_eq!(core.semantic_template_count(), 1);
            assert_eq!(core.edge_count(), 1);
            assert!(core.update_quote("a", 2.4, 10, 100));
            let candidates = core.update_quote_and_scan_fast("b", 2.55, 11, 101, 0.01, 12);
            assert_eq!(candidates.len(), 1);
        });
    }

    #[rstest]
    fn semantic_provider_scope_filters_edges() {
        pyo3::Python::initialize();

        Python::attach(|py| {
            let nodes = PyList::empty(py);
            nodes.append(py_payload(py, &node("a", "over"))).unwrap();
            nodes
                .append(py_payload(
                    py,
                    &node_with(
                        "b",
                        "BLACKBET",
                        "event-2",
                        "Total Goals",
                        "total_goals",
                        "under",
                    ),
                ))
                .unwrap();
            let templates = PyList::empty(py);
            templates
                .append(py_semantic_template(py, vec!["SXBET"], false))
                .unwrap();

            let mut core = OpportunityGraphCore::new(true, 0.5);
            core.build_semantic(nodes.as_any(), templates.as_any())
                .unwrap();
            assert_eq!(core.edge_count(), 0);

            let venue_agnostic = PyList::empty(py);
            venue_agnostic
                .append(py_semantic_template(py, vec![], true))
                .unwrap();
            core.build_semantic(nodes.as_any(), venue_agnostic.as_any())
                .unwrap();
            assert_eq!(core.edge_count(), 1);
        });
    }

    #[rstest]
    fn semantic_line_params_generalize_same_and_opposite_line_templates() {
        pyo3::Python::initialize();

        Python::attach(|py| {
            let mut over = node_with("over", "SXBET", "event-1", "Totals", "TOTALS", "OVER");
            over.params = "line=2.5".to_string();
            over.semantic_params_key = "[[\"line\",\"2.5\"]]".to_string();
            let mut under = node_with("under", "SXBET", "event-1", "Totals", "TOTALS", "UNDER");
            under.params = "line=2.5".to_string();
            under.semantic_params_key = "[[\"line\",\"2.5\"]]".to_string();

            let nodes = PyList::empty(py);
            nodes.append(py_payload(py, &over)).unwrap();
            nodes.append(py_payload(py, &under)).unwrap();

            let same_line_template = PyDict::new(py);
            same_line_template
                .set_item("template_id", "template-total-goals-any-line")
                .unwrap();
            same_line_template
                .set_item("relationship_type", "COMPLEMENTARY_COVERAGE")
                .unwrap();
            same_line_template
                .set_item(
                    "pattern_a",
                    py_pattern_with_params(py, "TOTALS", "OVER", "[[\"line\",\"52.5\"]]"),
                )
                .unwrap();
            same_line_template
                .set_item(
                    "pattern_b",
                    py_pattern_with_params(py, "TOTALS", "UNDER", "[[\"line\",\"52.5\"]]"),
                )
                .unwrap();
            same_line_template.set_item("confidence", 1.0).unwrap();
            same_line_template
                .set_item("provider_scope", vec!["SXBET"])
                .unwrap();
            same_line_template
                .set_item("venue_agnostic", false)
                .unwrap();
            same_line_template
                .set_item("safety_tier", "EXECUTION_SAFE")
                .unwrap();
            same_line_template
                .set_item("promotion_status", "PROMOTED")
                .unwrap();
            same_line_template.set_item("push_capable", false).unwrap();
            same_line_template.set_item("execution_safe", true).unwrap();

            let templates = PyList::empty(py);
            templates.append(same_line_template).unwrap();

            let mut core = OpportunityGraphCore::new(true, 0.5);
            core.build_semantic(nodes.as_any(), templates.as_any())
                .unwrap();
            assert_eq!(core.edge_count(), 1);

            let mut home = node_with(
                "home",
                "SXBET",
                "event-2",
                "Spread",
                "ASIAN_HANDICAP",
                "HOME",
            );
            home.params = "line=-3.5".to_string();
            home.semantic_params_key = "[[\"line\",\"-3.5\"]]".to_string();
            let mut away = node_with(
                "away",
                "SXBET",
                "event-2",
                "Spread",
                "ASIAN_HANDICAP",
                "AWAY",
            );
            away.params = "line=3.5".to_string();
            away.semantic_params_key = "[[\"line\",\"3.5\"]]".to_string();
            let spread_nodes = PyList::empty(py);
            spread_nodes.append(py_payload(py, &home)).unwrap();
            spread_nodes.append(py_payload(py, &away)).unwrap();

            let opposite_line_template = PyDict::new(py);
            opposite_line_template
                .set_item("template_id", "template-spread-any-opposite-line")
                .unwrap();
            opposite_line_template
                .set_item("relationship_type", "COMPLEMENTARY_COVERAGE")
                .unwrap();
            opposite_line_template
                .set_item(
                    "pattern_a",
                    py_pattern_with_params(py, "ASIAN_HANDICAP", "HOME", "[[\"line\",\"-1.5\"]]"),
                )
                .unwrap();
            opposite_line_template
                .set_item(
                    "pattern_b",
                    py_pattern_with_params(py, "ASIAN_HANDICAP", "AWAY", "[[\"line\",\"1.5\"]]"),
                )
                .unwrap();
            opposite_line_template.set_item("confidence", 1.0).unwrap();
            opposite_line_template
                .set_item("provider_scope", vec!["SXBET"])
                .unwrap();
            opposite_line_template
                .set_item("venue_agnostic", false)
                .unwrap();
            opposite_line_template
                .set_item("safety_tier", "EXECUTION_SAFE")
                .unwrap();
            opposite_line_template
                .set_item("promotion_status", "PROMOTED")
                .unwrap();
            opposite_line_template
                .set_item("push_capable", true)
                .unwrap();
            opposite_line_template
                .set_item("execution_safe", false)
                .unwrap();

            let spread_templates = PyList::empty(py);
            spread_templates.append(opposite_line_template).unwrap();

            core.build_semantic(spread_nodes.as_any(), spread_templates.as_any())
                .unwrap();
            assert_eq!(core.edge_count(), 1);
        });
    }

    #[rstest]
    fn same_venue_event_id_mismatch_is_rejected_unless_trusted_match_odds() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        let source = node_with(
            "a",
            "SXBET",
            "market-a",
            "Total Goals",
            "total_goals",
            "over",
        );
        let target = node_with(
            "b",
            "SXBET",
            "market-b",
            "Total Goals",
            "total_goals",
            "under",
        );
        core.insert_node(source);
        core.insert_node(target);
        core.rebuild_edges();
        assert_eq!(core.edge_count(), 0);

        let mut home = node_with(
            "home",
            "SXBET",
            "market-home",
            "match_odds",
            "match_odds",
            "home",
        );
        home.params.clear();
        home.two_way_market = true;
        let mut away = node_with(
            "away",
            "SXBET",
            "market-away",
            "match_odds",
            "match_odds",
            "away",
        );
        away.params.clear();
        away.two_way_market = true;
        core.clear();
        core.insert_node(home);
        core.insert_node(away);
        core.rebuild_edges();

        assert_eq!(core.edge_count(), 1);
        core.update_quote("home", 2.4, 10, 100);
        let candidates = core.update_quote_and_scan_fast("away", 2.55, 11, 101, 0.01, 12);
        assert_eq!(candidates.len(), 1);
        assert!(!candidates[0].11);
    }

    #[rstest]
    fn cross_venue_disabled_and_quote_guards() {
        let mut core = OpportunityGraphCore::new(false, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node_with(
            "b",
            "BLACKBET",
            "event-2",
            "Total Goals",
            "total_goals",
            "under",
        ));
        core.rebuild_edges();

        assert_eq!(core.edge_count(), 0);
        assert!(!core.update_quote("missing", 2.0, 1, 1));
        assert!(!core.update_quote("a", 0.0, 1, 1));
        assert!(core.evaluate_connected_edges("a", 0.01, 2).is_empty());
        core.clear();
        assert_eq!(core.node_count(), 0);
    }

    #[rstest]
    fn update_quote_and_evaluate_invalid_node_returns_empty() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        core.rebuild_edges();

        assert!(
            core.update_quote_and_evaluate("missing", 2.4, 1, 1, 0.01, 2)
                .is_empty()
        );
    }

    #[rstest]
    fn cross_market_edges_obey_confidence_threshold() {
        let mut match_odds = node_with(
            "home",
            "SXBET",
            "event-1",
            "Match Odds",
            "match_odds",
            "home",
        );
        match_odds.params.clear();
        match_odds.two_way_market = true;
        let mut double_chance = node_with(
            "away_draw",
            "SXBET",
            "event-1",
            "Double Chance",
            "double_chance",
            "away_draw",
        );
        double_chance.params.clear();

        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(match_odds.clone());
        core.insert_node(double_chance.clone());
        core.rebuild_edges();
        assert_eq!(core.edge_count(), 1);
        assert_approx_eq(core.edge_snapshots()[0].4, 0.95);

        let mut filtered = OpportunityGraphCore::new(true, 0.96);
        filtered.insert_node(match_odds);
        filtered.insert_node(double_chance);
        filtered.rebuild_edges();
        assert_eq!(filtered.edge_count(), 0);
    }

    #[rstest]
    fn cross_market_confidence_variants_are_covered() {
        let mut asian_home = node_with(
            "asian_home",
            "SXBET",
            "event-1",
            "Asian Handicap",
            "asian_handicap",
            "home",
        );
        asian_home.handicap = Some(1.5);
        let mut asian_away = node_with(
            "asian_away",
            "SXBET",
            "event-1",
            "Asian Handicap",
            "asian_handicap",
            "away",
        );
        asian_away.handicap = Some(-1.5);
        let draw_no_bet = node_with(
            "draw_no_bet",
            "SXBET",
            "event-1",
            "Draw No Bet",
            "draw_no_bet",
            "away",
        );
        let total_goals = node("total", "over");

        assert_eq!(
            asian_handicap_confidence(&asian_home, &asian_away),
            Some(0.85)
        );
        asian_away.handicap = Some(0.5);
        assert_eq!(
            asian_handicap_confidence(&asian_home, &asian_away),
            Some(0.0)
        );
        assert_eq!(asian_handicap_confidence(&asian_home, &total_goals), None);
        assert_approx_eq(cross_market_confidence(&asian_home, &draw_no_bet), 0.75);
        assert_approx_eq(cross_market_confidence(&total_goals, &asian_home), 0.0);
        assert_eq!(
            match_odds_double_chance_confidence(&asian_home, &draw_no_bet),
            None
        );
        assert_eq!(outcome_opposite("home"), Some("away"));
        assert_eq!(outcome_opposite("away"), Some("home"));
        assert_eq!(outcome_opposite("over"), Some("under"));
        assert_eq!(outcome_opposite("under"), Some("over"));
        assert_eq!(outcome_opposite("yes"), Some("no"));
        assert_eq!(outcome_opposite("no"), Some("yes"));
        assert_eq!(outcome_opposite("other"), None);
        assert!(can_hedge_market("double_chance", "match_odds"));
        assert!(can_hedge_market("asian_handicap", "double_chance"));
        assert!(can_hedge_market(
            "match_odds_period_first_half",
            "asian_handicap_period_first_half"
        ));
        assert!(can_hedge_market(
            "match_odds_period_second_half",
            "asian_handicap_period_second_half"
        ));
        assert!(can_hedge_market(
            "asian_handicap_period_first_half",
            "match_odds_period_first_half"
        ));
        assert!(can_hedge_market(
            "team_total_goals_period_first_half",
            "team_total_goals_period_first_half"
        ));
        assert!(can_hedge_market(
            "team_total_goals_period_second_half",
            "team_total_goals_period_second_half"
        ));
        assert!(!can_hedge_market("unknown", "match_odds"));
    }

    #[rstest]
    fn push_capable_edges_are_not_returned_as_candidates() {
        let home = node_with(
            "home",
            "SXBET",
            "event-1",
            "Draw No Bet",
            "draw_no_bet",
            "home",
        );
        let away = node_with(
            "away",
            "SXBET",
            "event-1",
            "Draw No Bet",
            "draw_no_bet",
            "away",
        );
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(home);
        core.insert_node(away);
        core.rebuild_edges();

        assert_eq!(core.edge_count(), 1);
        assert!(core.edge_snapshots()[0].7);
        assert!(core.update_quote("home", 2.4, 1, 1));
        assert!(core.update_quote("away", 2.55, 2, 2));
        assert!(core.evaluate_connected_edges("home", 0.01, 3).is_empty());
    }

    #[rstest]
    fn missing_start_time_ambiguity_uses_pair_venues_only() {
        let mut early = node("early", "over");
        early.start_time_ns = Some(1_000);
        let mut late = node("late", "under");
        late.event_id = "event-2".to_string();
        late.start_time_ns = Some(SIX_HOURS_NS + 2_000);
        let mut missing = node_with(
            "missing",
            "BLACKBET",
            "event-3",
            "Total Goals",
            "total_goals",
            "under",
        );
        missing.start_time_ns = None;

        let mut ambiguous = OpportunityGraphCore::new(true, 0.5);
        ambiguous.insert_node(early.clone());
        ambiguous.insert_node(late);
        ambiguous.insert_node(missing.clone());
        ambiguous.rebuild_edges();
        assert_eq!(ambiguous.edge_count(), 0);

        let mut unambiguous = OpportunityGraphCore::new(true, 0.5);
        unambiguous.insert_node(early);
        unambiguous.insert_node(missing);
        unambiguous.rebuild_edges();
        assert_eq!(unambiguous.edge_count(), 1);
    }

    #[rstest]
    fn event_matching_rejects_distinct_keys_and_empty_start_clusters() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        let source = node("a", "over");
        let mut target = node_with(
            "b",
            "BLACKBET",
            "event-2",
            "Total Goals",
            "total_goals",
            "under",
        );
        target.event_key_no_time = "soccer:team c:team d".to_string();

        assert!(!core.is_event_match(&source, &target));
        assert_eq!(core.start_time_cluster_count_for_pair(&source, &target), 0);

        target
            .event_key_no_time
            .clone_from(&source.event_key_no_time);
        target.start_time_ns = None;
        let mut missing_source = source.clone();
        missing_source.start_time_ns = None;
        let mut unrelated = node_with(
            "c",
            "OTHER",
            "event-3",
            "Total Goals",
            "total_goals",
            "over",
        );
        unrelated.start_time_ns = Some(10_000);
        core.insert_node(missing_source.clone());
        core.insert_node(target.clone());
        core.insert_node(unrelated);

        assert_eq!(
            core.start_time_cluster_count_for_pair(&missing_source, &target),
            0
        );
        assert!(!core.is_event_match(&missing_source, &target));
    }

    #[rstest]
    fn existing_edge_is_updated_when_higher_confidence_arrives() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        let edge_id = edge_id("a", "b");

        core.upsert_edge("a", "b", "cross_market", 0.5, edge_id.clone());
        core.upsert_edge("b", "a", "same_market", 1.0, edge_id);

        let snapshot = core.edge_snapshots().remove(0);
        assert_eq!(snapshot.1, "b");
        assert_eq!(snapshot.2, "a");
        assert_eq!(snapshot.3, "same_market");
        assert_approx_eq(snapshot.4, 1.0);
    }

    #[rstest]
    fn evaluation_updates_edge_snapshot_metadata() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        core.rebuild_edges();
        core.update_quote("a", 2.4, 10, 100);
        core.update_quote("b", 2.55, 11, 101);

        let candidates = core.update_quote_and_evaluate("a", 2.4, 12, 102, 0.01, 13);
        let snapshots = core.edge_snapshots();

        assert_eq!(candidates.len(), 1);
        assert_eq!(snapshots[0].9, Some(candidates[0].3));
        assert_eq!(snapshots[0].10, Some(13));
        assert_eq!(snapshots[0].11, Some(13));
    }

    #[rstest]
    fn fast_scan_returns_primitive_candidate_payload() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        core.rebuild_edges();
        core.update_quote("a", 2.4, 10, 100);

        let candidates = core.update_quote_and_scan_fast("b", 2.55, 11, 101, 0.01, 12);
        let snapshots = core.edge_snapshots();

        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].1, "b");
        assert_eq!(candidates[0].2, "a");
        assert_eq!(candidates[0].3, "same_market");
        assert_approx_eq(candidates[0].4, 1.0);
        assert_approx_eq(candidates[0].5, 2.55);
        assert_approx_eq(candidates[0].6, 2.4);
        assert!(candidates[0].7 > 0.0);
        assert_eq!(candidates[0].8, 101);
        assert_eq!(candidates[0].9, 100);
        assert_eq!(candidates[0].10, "same_market");
        assert!(!candidates[0].11);
        assert_eq!(snapshots[0].9, Some(candidates[0].7));
        assert_eq!(snapshots[0].10, Some(12));
        assert_eq!(snapshots[0].11, Some(12));
    }

    #[rstest]
    fn unprofitable_and_missing_other_quote_evaluations_are_filtered() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        core.rebuild_edges();
        assert!(core.update_quote("a", 1.8, 10, 100));
        assert!(core.evaluate_connected_edges("a", 0.01, 101).is_empty());
        assert!(core.update_quote("b", 1.8, 11, 102));

        let candidates = core.evaluate_connected_edges("a", 0.01, 103);

        assert!(candidates.is_empty());
        assert!(core.edge_snapshots()[0].9.is_some());
    }
}
