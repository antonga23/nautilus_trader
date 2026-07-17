"""
Shared fixtures for the standing-miner tests.

Hermetic: the mine phase (venue corpus refresh) is always monkeypatched; slim
export and distribution tests run against a real rule store on tmp dirs. No
test touches a venue API or the network.

"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _import(name: str) -> Any:
    return importlib.import_module(name)


@pytest.fixture(scope="session")
def semantics_types() -> Any:
    return _import("nautilus_trader.adapters.betting.semantics.types")


@pytest.fixture(scope="session")
def semantics_store() -> Any:
    return _import("nautilus_trader.adapters.betting.semantics.store")


@pytest.fixture(scope="session")
def semantic_cache() -> Any:
    return _import("nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(days: float) -> str:
    return _iso(datetime.now(UTC) - timedelta(days=days))


class MasterBuilder:
    """
    Builds a real master rule store on a tmp dir with the real store APIs.
    """

    def __init__(self, master_dir: Path, types_mod: Any, store_mod: Any, cache_mod: Any) -> None:
        self.master_dir = master_dir
        self._types = types_mod
        self._cache_mod = cache_mod
        self.store = store_mod.RuleStore(store_mod.FileRuleCache(master_dir))

    def _pattern(self, key: str, sport: str, selection: str) -> Any:
        return self._types.SelectionPattern(
            pattern_id=f"pattern:{key}:{selection}",
            sport=sport,
            scope="full_time",
            market_type="TOTALS",
            market_family="TOTALS",
            selection=selection,
            params=(("line", "2.5"),),
            result_states=("HOME", "AWAY"),
            settlement=("WIN", "LOSE"),
        )

    def make_template(
        self,
        template_id: str,
        *,
        sport: str = "soccer",
        tier: str = "EXECUTION_SAFE",
        last_seen_at: str | None = None,
    ) -> Any:
        return self._types.SemanticRuleTemplate(
            template_id=template_id,
            relationship_type="COMPLEMENTARY_COVERAGE",
            sport=sport,
            scope="full_time",
            pattern_a=self._pattern(template_id, sport, "over"),
            pattern_b=self._pattern(template_id, sport, "under"),
            result_states=("HOME", "AWAY"),
            settlement_a=("WIN", "LOSE"),
            settlement_b=("LOSE", "WIN"),
            confidence=1.0,
            caveats=(),
            support=self._types.TemplateSupportStats(
                template_id=template_id,
                observed_count=12,
                event_count=4,
                provider_count=1,
                providers=("SXBET",),
                sports=(sport,),
                last_seen_at=last_seen_at,
            ),
            provider_scope=("SXBET",),
            promotion_status="PROMOTED",
            safety_tier=tier,
        )

    def add_promoted_template(
        self,
        template_id: str,
        *,
        sport: str = "soccer",
        tier: str = "EXECUTION_SAFE",
        last_seen_at: str | None = None,
    ) -> Any:
        template = self.make_template(
            template_id,
            sport=sport,
            tier=tier,
            last_seen_at=last_seen_at,
        )
        self.store.save_promoted_template(template)
        return template

    def add_coverage(self, key: str, *, sport: str = "soccer", tier: str = "EXECUTION_SAFE") -> Any:
        universe = self._types.OutcomeUniverse.from_state_ids(
            sport=sport,
            scope="full_time",
            state_ids=("HOME", "AWAY"),
        )
        predicate = self._types.SelectionPredicate(
            predicate_id=f"predicate:{key}",
            instrument_id=f"{key}-instrument",
            sport=sport,
            scope="full_time",
            market_type="TOTALS",
            market_family="TOTALS",
            selection="over",
            params=(("line", "2.5"),),
            result_states=("HOME", "AWAY"),
            win_states=("HOME",),
            lose_states=("AWAY",),
            provider="SXBET",
            event_key=f"{key}-event",
        )
        coverage_set = self._types.CoverageSet.create(
            sport=sport,
            scope="full_time",
            event_key=f"{key}-event",
            provider_scope=("SXBET",),
            predicate_ids=(predicate.predicate_id,),
            market_families=("TOTALS",),
        )
        proof = self._types.CoverageProof(
            proof_id=f"proof:{key}",
            universe=universe,
            coverage_set=coverage_set,
            predicates=(predicate,),
            complete=True,
            win_covered_states=("HOME", "AWAY"),
            overlapping_win_states=(),
            gaps=(),
            risks=(),
            blocker_reasons=(),
            relationship_type="COMPLEMENTARY_COVERAGE",
            safety_tier=tier,
            execution_safe=True,
        )
        hyperedge = self._types.CoverageHyperedge.from_proof(proof)
        self.store.save_coverage_proof(proof)
        self.store.save_coverage_hyperedge(hyperedge)
        return proof, hyperedge

    def add_manifest(self, manifest_id: str) -> Any:
        manifest = self._types.RuleCorpusManifest(
            manifest_id=manifest_id,
            provider="SXBET",
            fetched_at=_days_ago(0.0),
            endpoint_version="v1",
            sport_count=1,
            event_count=2,
            selection_count=4,
            market_taxonomy_hash="hash",
        )
        self.store.save_manifest(manifest)
        return manifest

    def add_junk(self) -> None:
        """
        Write every artifact family the slim export must drop.
        """
        self.store.save_snapshot(
            self._types.CorpusSnapshot(
                snapshot_id="snap-1",
                provider="SXBET",
                endpoint="/markets/active",
                fetched_at=_days_ago(0.0),
                payload=b"{}",
            ),
        )
        selection = self._types.NormalizedSelection(
            venue="SXBET",
            instrument_id="junk-instrument",
            sport="soccer",
            event_key="junk-event",
            period="full_time",
            scope="full_time",
            market_type="TOTALS",
            market_family="TOTALS",
            selection="over",
            params=(("line", "2.5"),),
            raw_market_name="Totals",
            raw_market_type="totals",
            raw_outcome="Over 2.5",
            outcome_key="over-2.5",
        )
        self.store.save_normalized_selection(
            self._types.NormalizedSelectionRecord(
                record_id="rec-1",
                provider="SXBET",
                selection=selection,
            ),
        )
        rule = self._types.MinedRule(
            rule_id="rule-1",
            relationship_type="COMPLEMENTARY_COVERAGE",
            sport="soccer",
            market_a="TOTALS",
            selection_a="over",
            params_a=(("line", "2.5"),),
            market_b="TOTALS",
            selection_b="under",
            params_b=(("line", "2.5"),),
            result_states=("HOME", "AWAY"),
            settlement_a=("WIN", "LOSE"),
            settlement_b=("LOSE", "WIN"),
            confidence=1.0,
            caveats=(),
            venue_scope=("SXBET",),
        )
        self.store.save_candidate(rule)
        self.store.save_template_candidate(self.make_template("template:candidate-junk"))
        self.store.save_validation(
            self._types.RuleValidationStats(
                rule_id="rule-1",
                venue_id="SXBET",
                sport="soccer",
            ),
        )

    def write_marker(self, scope: str | None = "scope-test") -> None:
        payload = {
            "version": self._cache_mod.SEMANTIC_CACHE_COMPATIBILITY_VERSION,
            "scope": scope,
        }
        marker = self.master_dir / self._cache_mod.SEMANTIC_CACHE_COMPATIBILITY_FILE
        marker.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture
def master_builder(
    tmp_path: Path,
    semantics_types: Any,
    semantics_store: Any,
    semantic_cache: Any,
) -> MasterBuilder:
    return MasterBuilder(tmp_path / "master", semantics_types, semantics_store, semantic_cache)


@pytest.fixture
def days_ago() -> Any:
    return _days_ago
