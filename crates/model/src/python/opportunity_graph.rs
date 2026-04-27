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
}

impl NodeSnapshot {
    fn from_py(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let dict = value.cast::<PyDict>()?;
        Ok(Self {
            node_id: get_string(dict, "node_id")?,
            venue: get_string(dict, "venue")?,
            event_id: get_string(dict, "event_id")?,
            event_key_no_time: get_string(dict, "event_key_no_time")?,
            market_name: get_string(dict, "market_name")?,
            market_type: get_string(dict, "market_type")?,
            outcome: get_string(dict, "outcome")?,
            selection_key: get_string(dict, "selection_key")?,
            params: get_string(dict, "params")?,
            handicap: get_optional_f64(dict, "handicap")?,
            start_time_ns: get_optional_i64(dict, "start_time_ns")?,
            two_way_market: get_bool(dict, "two_way_market")?,
        })
    }
}

#[derive(Clone, Debug)]
struct EdgeSnapshot {
    edge_id: String,
    source_node_id: String,
    target_node_id: String,
    hedge_type: String,
    confidence: f64,
    same_venue: bool,
    market_relationship_type: String,
    push_capable: bool,
    execution_safe: bool,
    matcher_suspect: bool,
    last_margin: Option<f64>,
    last_evaluated_ns: Option<i64>,
    last_updated_ns: Option<i64>,
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
}

#[pymethods]
impl OpportunityGraphCore {
    #[new]
    #[pyo3(signature = (include_cross_venue=true, min_confidence=0.5))]
    fn new(include_cross_venue: bool, min_confidence: f64) -> Self {
        Self {
            include_cross_venue,
            min_confidence,
            nodes_by_id: HashMap::new(),
            edges_by_id: HashMap::new(),
            edge_ids_by_node_id: HashMap::new(),
            quotes_by_node_id: HashMap::new(),
            event_buckets: HashMap::new(),
            venue_event_buckets: HashMap::new(),
        }
    }

    fn clear(&mut self) {
        self.nodes_by_id.clear();
        self.edges_by_id.clear();
        self.edge_ids_by_node_id.clear();
        self.quotes_by_node_id.clear();
        self.event_buckets.clear();
        self.venue_event_buckets.clear();
    }

    fn build(&mut self, nodes: &Bound<'_, PyAny>) -> PyResult<()> {
        self.clear();
        for item in nodes.try_iter()? {
            self.insert_node(NodeSnapshot::from_py(&item?)?);
        }
        self.rebuild_edges();
        Ok(())
    }

    fn add_instrument(&mut self, node: &Bound<'_, PyAny>) -> PyResult<bool> {
        let snapshot = NodeSnapshot::from_py(node)?;
        if self.nodes_by_id.contains_key(&snapshot.node_id) {
            return Ok(false);
        }
        let node_id = snapshot.node_id.clone();
        self.insert_node(snapshot);
        self.connect_node(&node_id);
        Ok(true)
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
        _received_ns: i64,
        exchange_ts_ns: i64,
        min_profit_margin: f64,
        now_ns: i64,
    ) -> Vec<CandidateSnapshot> {
        if !self.update_quote(node_id, odds, _received_ns, exchange_ts_ns) {
            return Vec::new();
        }
        self.evaluate_connected_edges(node_id, min_profit_margin, now_ns)
    }

    fn update_quote_and_scan_fast(
        &mut self,
        node_id: &str,
        odds: f64,
        _received_ns: i64,
        exchange_ts_ns: i64,
        min_profit_margin: f64,
        now_ns: i64,
    ) -> Vec<FastCandidateSnapshot> {
        if !self.update_quote(node_id, odds, _received_ns, exchange_ts_ns) {
            return Vec::new();
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

    fn edge_snapshots(
        &self,
    ) -> Vec<(
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
    )> {
        self.edges_by_id
            .values()
            .map(|edge| {
                (
                    edge.edge_id.clone(),
                    edge.source_node_id.clone(),
                    edge.target_node_id.clone(),
                    edge.hedge_type.clone(),
                    edge.confidence,
                    edge.same_venue,
                    edge.market_relationship_type.clone(),
                    edge.push_capable,
                    edge.execution_safe,
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
        let node_id = node.node_id.clone();
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

        let mut visited_pairs = HashSet::new();
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

    fn connect_node(&mut self, node_id: &str) {
        let mut visited_pairs = HashSet::new();
        let Some(node) = self.nodes_by_id.get(node_id) else {
            return;
        };
        let event_key_no_time = node.event_key_no_time.clone();
        let venue_event_key = format!("{}|{}", node.venue, node.event_id);

        if let Some(bucket) = self.event_buckets.get(&event_key_no_time).cloned() {
            self.connect_node_to_bucket(node_id, &bucket, &mut visited_pairs);
        }
        if let Some(bucket) = self.venue_event_buckets.get(&venue_event_key).cloned() {
            self.connect_node_to_bucket(node_id, &bucket, &mut visited_pairs);
        }
    }

    fn connect_bucket(&mut self, bucket: &[String], visited_pairs: &mut HashSet<String>) {
        for (index, source_id) in bucket.iter().enumerate() {
            for target_id in bucket.iter().skip(index + 1) {
                self.connect_pair(source_id, target_id, visited_pairs);
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
        let matcher_suspect = source.venue == target.venue && source.event_id != target.event_id;
        let edge = EdgeSnapshot {
            edge_id: edge_id.clone(),
            source_node_id: source_id.to_string(),
            target_node_id: target_id.to_string(),
            hedge_type: hedge_type.to_string(),
            confidence,
            same_venue: source.venue == target.venue,
            market_relationship_type: if source.market_name == target.market_name {
                "same_market".to_string()
            } else {
                "cross_market".to_string()
            },
            push_capable,
            execution_safe: !push_capable,
            matcher_suspect,
            last_margin: None,
            last_evaluated_ns: None,
            last_updated_ns: None,
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

    fn is_event_match(&self, source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
        if source.venue == target.venue && source.event_id == target.event_id {
            return true;
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
            return Vec::new();
        };
        let Some(edge_ids) = self.edge_ids_by_node_id.get(node_id).cloned() else {
            return Vec::new();
        };

        let mut candidates = Vec::new();
        for edge_id in edge_ids {
            let Some(edge) = self.edges_by_id.get_mut(&edge_id) else {
                continue;
            };
            if edge.push_capable {
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
            return Vec::new();
        };
        let Some(edge_ids) = self.edge_ids_by_node_id.get(node_id).cloned() else {
            return Vec::new();
        };

        let mut candidates = Vec::new();
        for edge_id in edge_ids {
            let Some(edge) = self.edges_by_id.get_mut(&edge_id) else {
                continue;
            };
            if edge.push_capable {
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
                edge.matcher_suspect,
            ));
        }
        candidates
    }
}

fn fast_match_type(edge: &EdgeSnapshot) -> &'static str {
    if edge.market_relationship_type == "same_market" {
        "same_market"
    } else if edge.same_venue {
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

fn get_bool(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    dict.get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(key.to_string()))?
        .extract()
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

fn edge_id(source_id: &str, target_id: &str) -> String {
    if source_id <= target_id {
        format!("{source_id}|{target_id}")
    } else {
        format!("{target_id}|{source_id}")
    }
}

fn is_push_capable(market_type: &str) -> bool {
    matches!(market_type, "draw_no_bet" | "asian_handicap")
}

fn is_same_market_hedge(source: &NodeSnapshot, target: &NodeSnapshot) -> bool {
    if source.market_name != target.market_name || source.params != target.params {
        return false;
    }
    if source.market_type == "match_odds" && !(source.two_way_market && target.two_way_market) {
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
        dict
    }

    #[test]
    fn same_market_opposite_outcomes_connect() {
        let mut core = OpportunityGraphCore::new(true, 0.5);
        core.insert_node(node("a", "over"));
        core.insert_node(node("b", "under"));
        core.rebuild_edges();

        assert_eq!(core.edge_count(), 1);
        assert_eq!(core.connected_edge_count("a"), 1);
    }

    #[test]
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

    #[test]
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

    #[test]
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

    #[test]
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

    #[test]
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
        assert_eq!(core.edge_snapshots()[0].4, 0.95);

        let mut filtered = OpportunityGraphCore::new(true, 0.96);
        filtered.insert_node(match_odds);
        filtered.insert_node(double_chance);
        filtered.rebuild_edges();
        assert_eq!(filtered.edge_count(), 0);
    }

    #[test]
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
        assert_eq!(cross_market_confidence(&asian_home, &draw_no_bet), 0.75);
        assert_eq!(cross_market_confidence(&total_goals, &asian_home), 0.0);
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

    #[test]
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

    #[test]
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

    #[test]
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

    #[test]
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
        assert_eq!(snapshot.4, 1.0);
    }

    #[test]
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

    #[test]
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
        assert_eq!(candidates[0].4, 1.0);
        assert_eq!(candidates[0].5, 2.55);
        assert_eq!(candidates[0].6, 2.4);
        assert!(candidates[0].7 > 0.0);
        assert_eq!(candidates[0].8, 101);
        assert_eq!(candidates[0].9, 100);
        assert_eq!(candidates[0].10, "same_market");
        assert!(!candidates[0].11);
        assert_eq!(snapshots[0].9, Some(candidates[0].7));
        assert_eq!(snapshots[0].10, Some(12));
        assert_eq!(snapshots[0].11, Some(12));
    }

    #[test]
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
