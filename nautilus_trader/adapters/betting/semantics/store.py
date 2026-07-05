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
# skipcq
"""
Cache-backed storage for semantic betting corpus artifacts and rules.
"""

from __future__ import annotations

import base64
import atexit
from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from nautilus_trader.adapters.betting.semantics.types import CorpusSnapshot
from nautilus_trader.adapters.betting.semantics.types import CoverageHyperedge
from nautilus_trader.adapters.betting.semantics.types import CoverageProof
from nautilus_trader.adapters.betting.semantics.types import MinedRule
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics.types import PromotionStatus
from nautilus_trader.adapters.betting.semantics.types import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics.types import RuleValidationStats
from nautilus_trader.adapters.betting.semantics.types import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics.types import TemplateSupportStats


class FileRuleCache:
    """
    Minimal file-backed cache adapter for semantic rule stores.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._key_index_path = self._path / "keys.json"
        self._lock = threading.RLock()
        self._key_index = self._load_key_index()
        self._dirty_key_index_entries = 0
        self._bulk_depth = 0
        atexit.register(self.flush_key_index)

    @contextmanager
    def bulk_writes(self) -> Iterator[FileRuleCache]:
        # Skip the per-record fsync during a bulk rebuild (e.g. a fresh mine writing
        # thousands of artifacts) and fsync the directory once on exit. The cache is a
        # derived artifact — os.replace keeps each file atomically visible, and a crash
        # mid-bulk simply triggers a re-mine — so per-record durability is unnecessary and
        # the fsync-per-file cost dominates mine wall time (~80x on EBS).
        with self._lock:
            self._bulk_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._bulk_depth -= 1
                if self._bulk_depth <= 0:
                    self._bulk_depth = 0
                    self.flush_key_index()
                    self._fsync_dir()

    def add(self, key: str, value: bytes) -> None:
        cache_path = self._cache_path(key)
        with self._lock:
            self._atomic_write_bytes(cache_path, value)
            if self._key_index.get(key) != cache_path.name:
                self._key_index[key] = cache_path.name
                self._dirty_key_index_entries += 1
            if self._dirty_key_index_entries >= 500:
                self.flush_key_index()

    def get(self, key: str) -> bytes | None:
        cache_path = self._cache_path(key)
        return cache_path.read_bytes() if cache_path.exists() else None

    def flush_key_index(self) -> None:
        with self._lock:
            if self._dirty_key_index_entries <= 0:
                return
            if not self._path.exists():
                self._dirty_key_index_entries = 0
                return
            self._atomic_write_text(
                self._key_index_path,
                json.dumps(self._key_index, sort_keys=True, indent=2),
            )
            self._dirty_key_index_entries = 0

    def _load_key_index(self) -> dict[str, str]:
        if not self._key_index_path.exists():
            return {}
        try:
            payload = json.loads(self._key_index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def _atomic_write_text(self, path: Path, payload: str) -> None:
        self._atomic_write_bytes(path, payload.encode("utf-8"))

    def _atomic_write_bytes(self, path: Path, payload: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self._path)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                # In a bulk rebuild the per-record fsync is deferred to a single
                # directory fsync at bulk_writes() exit; os.replace still gives atomic
                # visibility, so a partially written temp file is never observed.
                if self._bulk_depth <= 0:
                    os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _fsync_dir(self) -> None:
        if not self._path.exists():
            return
        dir_fd = os.open(str(self._path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._path / f"{digest}.bin"


class RuleStore:
    """
    Persists semantic corpus artifacts and validation stats through Nautilus generic
    cache keys.
    """

    CANDIDATE_PREFIX = "betting:semantic_rules:candidate"
    PROMOTED_PREFIX = "betting:semantic_rules:promoted"
    VALIDATION_PREFIX = "betting:semantic_rules:validation"
    SNAPSHOT_PREFIX = "betting:semantic_rules:snapshot"
    NORMALIZED_PREFIX = "betting:semantic_rules:normalized"
    MANIFEST_PREFIX = "betting:semantic_rules:manifest"
    TEMPLATE_CANDIDATE_PREFIX = "betting:semantic_rules:template:candidate"
    TEMPLATE_PROMOTED_PREFIX = "betting:semantic_rules:template:promoted"
    TEMPLATE_SUPPORT_PREFIX = "betting:semantic_rules:template:support"
    COVERAGE_PROOF_PREFIX = "betting:semantic_rules:coverage:proof"
    COVERAGE_HYPEREDGE_PREFIX = "betting:semantic_rules:coverage:hyperedge"

    CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:candidates"
    PROMOTED_INDEX_KEY = "betting:semantic_rules:index:promoted"
    VALIDATION_INDEX_KEY = "betting:semantic_rules:index:validation"
    SNAPSHOT_INDEX_KEY = "betting:semantic_rules:index:snapshots"
    NORMALIZED_INDEX_KEY = "betting:semantic_rules:index:normalized"
    MANIFEST_INDEX_KEY = "betting:semantic_rules:index:manifests"
    TEMPLATE_CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:template_candidates"
    TEMPLATE_PROMOTED_INDEX_KEY = "betting:semantic_rules:index:template_promoted"
    TEMPLATE_SUPPORT_INDEX_KEY = "betting:semantic_rules:index:template_support"
    COVERAGE_PROOF_INDEX_KEY = "betting:semantic_rules:index:coverage_proofs"
    COVERAGE_HYPEREDGE_INDEX_KEY = "betting:semantic_rules:index:coverage_hyperedges"

    def __init__(self, cache: Any) -> None:
        self._cache = cache
        self._index_cache: dict[str, list[str]] = {}
        self._dirty_index_keys: set[str] = set()
        self._dirty_index_entries = 0
        self._deferred_index_write_depth = 0

    @contextmanager
    def defer_index_writes(self) -> Iterator[RuleStore]:
        self._deferred_index_write_depth += 1
        try:
            yield self
        finally:
            self._deferred_index_write_depth -= 1
            if self._deferred_index_write_depth <= 0:
                self._deferred_index_write_depth = 0
                self.flush_indexes()

    @contextmanager
    def bulk_writes(self) -> Iterator[RuleStore]:
        # Delegate to the backing cache's bulk mode (defers per-record fsync) when it
        # supports one; a no-op otherwise. Pairs with defer_index_writes for bulk mines.
        cache_bulk = getattr(self._cache, "bulk_writes", None)
        if callable(cache_bulk):
            with cache_bulk():
                yield self
        else:
            yield self

    def flush_indexes(self) -> None:
        if not self._dirty_index_keys:
            return
        for key in sorted(self._dirty_index_keys):
            self._write_json(key, {"items": self._index_cache.get(key, [])})
        self._dirty_index_keys.clear()
        self._dirty_index_entries = 0

    def save_candidate(self, rule: MinedRule) -> None:
        self._write_bytes(self.candidate_key(rule.rule_id), rule.to_json_bytes())
        self._append_index(self.CANDIDATE_INDEX_KEY, rule.rule_id)

    def save_candidates(self, rules: Iterable[MinedRule]) -> None:
        rule_ids: list[str] = []
        for rule in rules:
            self._write_bytes(self.candidate_key(rule.rule_id), rule.to_json_bytes())
            rule_ids.append(rule.rule_id)
        self._append_index_many(self.CANDIDATE_INDEX_KEY, rule_ids)

    def load_candidate(self, rule_id: str) -> MinedRule | None:
        raw = self._read_bytes(self.candidate_key(rule_id))
        return MinedRule.from_json_bytes(raw) if raw else None

    def save_promoted(self, rule: MinedRule) -> None:
        promoted = MinedRule(
            rule_id=rule.rule_id,
            relationship_type=rule.relationship_type,
            sport=rule.sport,
            venue_scope=rule.venue_scope,
            scope=rule.scope,
            market_a=rule.market_a,
            selection_a=rule.selection_a,
            params_a=rule.params_a,
            market_b=rule.market_b,
            selection_b=rule.selection_b,
            params_b=rule.params_b,
            result_states=rule.result_states,
            settlement_a=rule.settlement_a,
            settlement_b=rule.settlement_b,
            confidence=rule.confidence,
            caveats=rule.caveats,
            promotion_status=PromotionStatus.PROMOTED.value,
            safety_tier=rule.safety_tier,
            eligibility_reasons=rule.eligibility_reasons,
            validation=rule.validation,
            template_id=rule.template_id,
            evidence_event_key=rule.evidence_event_key,
            evidence_record_ids=rule.evidence_record_ids,
        )
        self._write_bytes(self.promoted_key(rule.rule_id), promoted.to_json_bytes())
        self._append_index(self.PROMOTED_INDEX_KEY, rule.rule_id)

    def load_promoted(self, rule_id: str) -> MinedRule | None:
        raw = self._read_bytes(self.promoted_key(rule_id))
        return MinedRule.from_json_bytes(raw) if raw else None

    def save_template_candidate(self, template: SemanticRuleTemplate) -> None:
        self._write_bytes(
            self.template_candidate_key(template.template_id),
            template.to_json_bytes(),
        )
        self._append_index(self.TEMPLATE_CANDIDATE_INDEX_KEY, template.template_id)
        self.save_template_support(template)

    def load_template_candidate(self, template_id: str) -> SemanticRuleTemplate | None:
        raw = self._read_bytes(self.template_candidate_key(template_id))
        return SemanticRuleTemplate.from_json_bytes(raw) if raw else None

    def save_promoted_template(self, template: SemanticRuleTemplate) -> None:
        self._write_bytes(
            self.template_promoted_key(template.template_id),
            template.to_json_bytes(),
        )
        self._append_index(self.TEMPLATE_PROMOTED_INDEX_KEY, template.template_id)
        self.save_template_support(template)

    def load_promoted_template(self, template_id: str) -> SemanticRuleTemplate | None:
        raw = self._read_bytes(self.template_promoted_key(template_id))
        return SemanticRuleTemplate.from_json_bytes(raw) if raw else None

    def save_template_support(self, template: SemanticRuleTemplate) -> None:
        self._write_json(self.template_support_key(template.template_id), asdict(template.support))
        self._append_index(self.TEMPLATE_SUPPORT_INDEX_KEY, template.template_id)

    def load_template_support(self, template_id: str) -> TemplateSupportStats | None:
        payload = self._read_json(self.template_support_key(template_id))
        if payload is None:
            return None
        for key in ("providers", "sports", "example_rule_ids"):
            payload[key] = tuple(payload.get(key, ()))
        return TemplateSupportStats(**payload)

    def save_coverage_proof(self, proof: CoverageProof) -> None:
        self._write_bytes(self.coverage_proof_key(proof.proof_id), proof.to_json_bytes())
        self._append_index(self.COVERAGE_PROOF_INDEX_KEY, proof.proof_id)

    def load_coverage_proof(self, proof_id: str) -> CoverageProof | None:
        raw = self._read_bytes(self.coverage_proof_key(proof_id))
        return CoverageProof.from_json_bytes(raw) if raw else None

    def save_coverage_hyperedge(self, hyperedge: CoverageHyperedge) -> None:
        self._write_bytes(
            self.coverage_hyperedge_key(hyperedge.hyperedge_id),
            hyperedge.to_json_bytes(),
        )
        self._append_index(self.COVERAGE_HYPEREDGE_INDEX_KEY, hyperedge.hyperedge_id)

    def load_coverage_hyperedge(self, hyperedge_id: str) -> CoverageHyperedge | None:
        raw = self._read_bytes(self.coverage_hyperedge_key(hyperedge_id))
        return CoverageHyperedge.from_json_bytes(raw) if raw else None

    def save_validation(self, stats: RuleValidationStats) -> None:
        raw = json.dumps(asdict(stats), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._write_bytes(self.validation_key(stats.rule_id), raw)
        self._append_index(self.VALIDATION_INDEX_KEY, stats.rule_id)

    def load_validation(self, rule_id: str) -> RuleValidationStats | None:
        raw = self._read_bytes(self.validation_key(rule_id))
        if not raw:
            return None
        return RuleValidationStats(**json.loads(raw.decode("utf-8")))

    def save_snapshot(self, snapshot: CorpusSnapshot) -> None:
        payload = {
            "snapshot_id": snapshot.snapshot_id,
            "provider": snapshot.provider,
            "endpoint": snapshot.endpoint,
            "fetched_at": snapshot.fetched_at,
            "payload_b64": base64.b64encode(snapshot.payload).decode("ascii"),
            "source_ref": snapshot.source_ref,
            "content_type": snapshot.content_type,
        }
        self._write_json(self.snapshot_key(snapshot.snapshot_id), payload)
        self._append_index(self.SNAPSHOT_INDEX_KEY, snapshot.snapshot_id)

    def load_snapshot(self, snapshot_id: str) -> CorpusSnapshot | None:
        payload = self._read_json(self.snapshot_key(snapshot_id))
        if payload is None:
            return None
        return CorpusSnapshot(
            snapshot_id=payload["snapshot_id"],
            provider=payload["provider"],
            endpoint=payload["endpoint"],
            fetched_at=payload["fetched_at"],
            payload=base64.b64decode(payload["payload_b64"]),
            source_ref=payload.get("source_ref", ""),
            content_type=payload.get("content_type", "application/json"),
        )

    def save_normalized_selection(self, record: NormalizedSelectionRecord) -> None:
        payload = asdict(record)
        self._write_json(self.normalized_key(record.record_id), payload)
        self._append_index(self.NORMALIZED_INDEX_KEY, record.record_id)

    def load_normalized_selection(self, record_id: str) -> NormalizedSelectionRecord | None:
        payload = self._read_json(self.normalized_key(record_id))
        if payload is None:
            return None
        selection = NormalizedSelection(
            venue=payload["selection"]["venue"],
            instrument_id=payload["selection"]["instrument_id"],
            sport=payload["selection"]["sport"],
            event_key=payload["selection"]["event_key"],
            period=payload["selection"]["period"],
            scope=payload["selection"]["scope"],
            market_type=payload["selection"]["market_type"],
            market_family=payload["selection"]["market_family"],
            selection=payload["selection"]["selection"],
            params=tuple(tuple(item) for item in payload["selection"]["params"]),
            raw_market_name=payload["selection"]["raw_market_name"],
            raw_market_type=payload["selection"]["raw_market_type"],
            raw_outcome=payload["selection"]["raw_outcome"],
            outcome_key=payload["selection"]["outcome_key"],
            rules_flags=tuple(payload["selection"].get("rules_flags", ())),
            resolution_policy=tuple(
                tuple(item) for item in payload["selection"].get("resolution_policy", ())
            ),
            source_ref=payload["selection"].get("source_ref", ""),
        )
        return NormalizedSelectionRecord(
            record_id=payload["record_id"],
            provider=payload["provider"],
            selection=selection,
            manifest_id=payload.get("manifest_id"),
        )

    def save_manifest(self, manifest: RuleCorpusManifest) -> None:
        self._write_json(self.manifest_key(manifest.manifest_id), asdict(manifest))
        self._append_index(self.MANIFEST_INDEX_KEY, manifest.manifest_id)

    def load_manifest(self, manifest_id: str) -> RuleCorpusManifest | None:
        payload = self._read_json(self.manifest_key(manifest_id))
        if payload is None:
            return None
        payload["source_refs"] = tuple(payload.get("source_refs", ()))
        return RuleCorpusManifest(**payload)

    def list_snapshot_ids(self) -> list[str]:
        return self._read_index(self.SNAPSHOT_INDEX_KEY)

    def list_candidate_ids(self) -> list[str]:
        return self._read_index(self.CANDIDATE_INDEX_KEY)

    def list_promoted_ids(self) -> list[str]:
        return self._read_index(self.PROMOTED_INDEX_KEY)

    def list_validation_ids(self) -> list[str]:
        return self._read_index(self.VALIDATION_INDEX_KEY)

    def list_normalized_ids(self) -> list[str]:
        return self._read_index(self.NORMALIZED_INDEX_KEY)

    def list_manifest_ids(self) -> list[str]:
        return self._read_index(self.MANIFEST_INDEX_KEY)

    def list_template_candidate_ids(self) -> list[str]:
        return self._read_index(self.TEMPLATE_CANDIDATE_INDEX_KEY)

    def list_promoted_template_ids(self) -> list[str]:
        return self._read_index(self.TEMPLATE_PROMOTED_INDEX_KEY)

    def list_template_support_ids(self) -> list[str]:
        return self._read_index(self.TEMPLATE_SUPPORT_INDEX_KEY)

    def list_coverage_proof_ids(self) -> list[str]:
        return self._read_index(self.COVERAGE_PROOF_INDEX_KEY)

    def list_coverage_hyperedge_ids(self) -> list[str]:
        return self._read_index(self.COVERAGE_HYPEREDGE_INDEX_KEY)

    @classmethod
    def candidate_key(cls, rule_id: str) -> str:
        return f"{cls.CANDIDATE_PREFIX}:{rule_id}"

    @classmethod
    def promoted_key(cls, rule_id: str) -> str:
        return f"{cls.PROMOTED_PREFIX}:{rule_id}"

    @classmethod
    def validation_key(cls, rule_id: str) -> str:
        return f"{cls.VALIDATION_PREFIX}:{rule_id}"

    @classmethod
    def snapshot_key(cls, snapshot_id: str) -> str:
        return f"{cls.SNAPSHOT_PREFIX}:{snapshot_id}"

    @classmethod
    def normalized_key(cls, record_id: str) -> str:
        return f"{cls.NORMALIZED_PREFIX}:{record_id}"

    @classmethod
    def manifest_key(cls, manifest_id: str) -> str:
        return f"{cls.MANIFEST_PREFIX}:{manifest_id}"

    @classmethod
    def template_candidate_key(cls, template_id: str) -> str:
        return f"{cls.TEMPLATE_CANDIDATE_PREFIX}:{template_id}"

    @classmethod
    def template_promoted_key(cls, template_id: str) -> str:
        return f"{cls.TEMPLATE_PROMOTED_PREFIX}:{template_id}"

    @classmethod
    def template_support_key(cls, template_id: str) -> str:
        return f"{cls.TEMPLATE_SUPPORT_PREFIX}:{template_id}"

    @classmethod
    def coverage_proof_key(cls, proof_id: str) -> str:
        return f"{cls.COVERAGE_PROOF_PREFIX}:{proof_id}"

    @classmethod
    def coverage_hyperedge_key(cls, hyperedge_id: str) -> str:
        return f"{cls.COVERAGE_HYPEREDGE_PREFIX}:{hyperedge_id}"

    def _write_json(self, key: str, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._write_bytes(key, raw)

    def _read_json(self, key: str) -> dict[str, Any] | None:
        raw = self._read_bytes(key)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    def _write_bytes(self, key: str, raw: bytes) -> None:
        self._cache.add(key, gzip.compress(raw, compresslevel=1))

    def _read_bytes(self, key: str) -> bytes | None:
        raw = self._cache.get(key)
        if not raw:
            return None
        return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw

    def _append_index(self, key: str, value: str) -> None:
        self._append_index_many(key, (value,))

    def _append_index_many(self, key: str, values: Iterable[str]) -> None:
        items = self._read_index(key)
        seen = set(items)
        changed = False
        for value in values:
            if value not in seen:
                items.append(value)
                seen.add(value)
                changed = True
        if not changed:
            return
        self._index_cache[key] = items
        if self._deferred_index_write_depth > 0:
            self._dirty_index_keys.add(key)
            self._dirty_index_entries += 1
            if self._dirty_index_entries >= 500:
                self.flush_indexes()
            return
        self._write_json(key, {"items": items})

    def _read_index(self, key: str) -> list[str]:
        cached = self._index_cache.get(key)
        if cached is not None:
            return list(cached)
        payload = self._read_json(key)
        if payload is None:
            self._index_cache[key] = []
            return []
        items = payload.get("items", [])
        parsed = [str(item) for item in items]
        self._index_cache[key] = parsed
        return list(parsed)
