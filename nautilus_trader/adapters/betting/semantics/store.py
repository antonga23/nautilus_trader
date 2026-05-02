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
Cache-backed storage for semantic betting corpus artifacts and rules.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from nautilus_trader.adapters.betting.semantics.types import CorpusSnapshot
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

    def add(self, key: str, value: bytes) -> None:
        cache_path = self._cache_path(key)
        self._write_bytes_atomic(cache_path, value)

    def get(self, key: str) -> bytes | None:
        cache_path = self._cache_path(key)
        return cache_path.read_bytes() if cache_path.exists() else None

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._path / f"{digest}.bin"

    @staticmethod
    def _write_bytes_atomic(path: Path, payload: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


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

    CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:candidates"
    PROMOTED_INDEX_KEY = "betting:semantic_rules:index:promoted"
    VALIDATION_INDEX_KEY = "betting:semantic_rules:index:validation"
    SNAPSHOT_INDEX_KEY = "betting:semantic_rules:index:snapshots"
    NORMALIZED_INDEX_KEY = "betting:semantic_rules:index:normalized"
    MANIFEST_INDEX_KEY = "betting:semantic_rules:index:manifests"
    TEMPLATE_CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:template_candidates"
    TEMPLATE_PROMOTED_INDEX_KEY = "betting:semantic_rules:index:template_promoted"
    TEMPLATE_SUPPORT_INDEX_KEY = "betting:semantic_rules:index:template_support"

    def __init__(self, cache) -> None:
        self._cache = cache
        self._index_cache: dict[str, list[str]] = {}
        self._dirty_index_keys: set[str] = set()
        self._defer_index_writes = 0

    @contextmanager
    def batched_indexes(self):
        self.begin_batched_indexes()
        try:
            yield self
        finally:
            self.end_batched_indexes()

    def begin_batched_indexes(self) -> None:
        self._defer_index_writes += 1

    def end_batched_indexes(self) -> None:
        self._defer_index_writes -= 1
        if self._defer_index_writes == 0:
            self._flush_dirty_indexes()

    def save_candidate(self, rule: MinedRule) -> None:
        self._write_bytes(self.candidate_key(rule.rule_id), rule.to_json_bytes())
        self._append_index(self.CANDIDATE_INDEX_KEY, rule.rule_id)

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
        items = self._index_items(key)
        if value not in items:
            items.append(value)
            self._dirty_index_keys.add(key)
            if self._defer_index_writes == 0:
                self._write_index(key, items)
                self._dirty_index_keys.discard(key)

    def _read_index(self, key: str) -> list[str]:
        return list(self._index_items(key))

    def _index_items(self, key: str) -> list[str]:
        cached = self._index_cache.get(key)
        if cached is not None:
            return cached
        payload = self._read_json(key)
        if payload is None:
            items: list[str] = []
        else:
            items = [str(item) for item in payload.get("items", [])]
        self._index_cache[key] = items
        return items

    def _write_index(self, key: str, items: list[str]) -> None:
        self._write_json(key, {"items": list(items)})
        self._index_cache[key] = list(items)

    def _flush_dirty_indexes(self) -> None:
        for key in sorted(self._dirty_index_keys):
            self._write_index(key, self._index_items(key))
        self._dirty_index_keys.clear()
