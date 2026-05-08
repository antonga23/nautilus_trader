#!/usr/bin/env python3
"""
Lightweight semantic-cache completion verifier for deployed strategy nodes.

This intentionally avoids importing Nautilus modules so the EC2 runtime verifier can
inspect the file-backed semantic cache without a repo virtualenv.

"""

from __future__ import annotations

import argparse
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any
import zlib


MANIFEST_INDEX_KEY = "betting:semantic_rules:index:manifests"
NORMALIZED_INDEX_KEY = "betting:semantic_rules:index:normalized"
CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:candidates"
TEMPLATE_CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:template_candidates"
TEMPLATE_PROMOTED_INDEX_KEY = "betting:semantic_rules:index:template_promoted"
COVERAGE_PROOF_INDEX_KEY = "betting:semantic_rules:index:coverage_proofs"
COVERAGE_HYPEREDGE_INDEX_KEY = "betting:semantic_rules:index:coverage_hyperedges"

MANIFEST_PREFIX = "betting:semantic_rules:manifest"
NORMALIZED_PREFIX = "betting:semantic_rules:normalized"
CANDIDATE_PREFIX = "betting:semantic_rules:candidate"
TEMPLATE_CANDIDATE_PREFIX = "betting:semantic_rules:template:candidate"
TEMPLATE_PROMOTED_PREFIX = "betting:semantic_rules:template:promoted"
COVERAGE_PROOF_PREFIX = "betting:semantic_rules:coverage:proof"
COVERAGE_HYPEREDGE_PREFIX = "betting:semantic_rules:coverage:hyperedge"


@dataclass(frozen=True)
class CacheCollections:
    manifests: list[dict[str, Any]]
    normalized_records: list[dict[str, Any]]
    candidate_rules: list[dict[str, Any]]
    template_candidates: list[dict[str, Any]]
    promoted_templates: list[dict[str, Any]]
    coverage_proofs: list[dict[str, Any]]
    coverage_hyperedges: list[dict[str, Any]]
    load_errors: list[dict[str, str]]


@dataclass(frozen=True)
class SelectionCounters:
    manifests_by_provider: Counter[str]
    selections_by_provider: Counter[str]
    selections_by_sport: Counter[str]
    sports_by_provider: dict[str, set[str]]


@dataclass(frozen=True)
class CandidateCounters:
    event_candidates_by_provider: Counter[str]
    event_candidates_by_sport: Counter[str]
    coverage_proofs_by_provider: Counter[str]
    coverage_proofs_by_sport: Counter[str]
    coverage_hyperedges_by_provider: Counter[str]
    coverage_hyperedges_by_sport: Counter[str]
    providers_by_sport: dict[str, set[str]]
    template_candidates_by_provider: Counter[str]
    template_candidates_by_sport: Counter[str]


@dataclass(frozen=True)
class PromotionCounters:
    promoted_by_provider: Counter[str]
    execution_safe_by_provider: Counter[str]
    safety_tier_counts: Counter[str]


def _cache_file(cache_dir: Path, key: str) -> Path:
    key_index_path = cache_dir / "keys.json"
    if key_index_path.exists():
        try:
            key_index = json.loads(key_index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            key_index = {}
        indexed_name = key_index.get(key) if isinstance(key_index, dict) else None
        if indexed_name:
            return cache_dir / str(indexed_name)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.bin"


def _record_load_error(
    errors: list[dict[str, str]] | None,
    *,
    key: str,
    reason: str,
    detail: BaseException | str,
) -> None:
    if errors is None:
        return
    errors.append(
        {
            "key": key,
            "reason": reason,
            "detail": str(detail),
        },
    )


def _read_bytes(
    cache_dir: Path,
    key: str,
    errors: list[dict[str, str]] | None = None,
) -> bytes | None:
    path = _cache_file(cache_dir, key)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        _record_load_error(errors, key=key, reason="unreadable_cache_payload", detail=exc)
        return None


def _read_json(
    cache_dir: Path,
    key: str,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    raw = _read_bytes(cache_dir, key, errors)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _record_load_error(errors, key=key, reason="invalid_cache_json", detail=exc)
        return None
    return payload if isinstance(payload, dict) else None


def _read_index(
    cache_dir: Path,
    key: str,
    errors: list[dict[str, str]] | None = None,
) -> list[str]:
    payload = _read_json(cache_dir, key, errors) or {}
    return [str(item) for item in payload.get("items", [])]


def _load_many(
    cache_dir: Path,
    index_key: str,
    prefix: str,
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item_id in _read_index(cache_dir, index_key, errors):
        payload = _read_json(cache_dir, f"{prefix}:{item_id}", errors)
        if payload is not None:
            payloads.append(payload)
    return payloads


def _normalize_sport(sport: object) -> str:
    normalized = str(sport or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "soccer/football": "soccer",
        "soccer_football": "soccer",
        "football": "american_football",
        "hockey": "ice_hockey",
    }
    return aliases.get(normalized, normalized)


def _providers_from_support(template: dict[str, Any]) -> tuple[str, ...]:
    support = template.get("support")
    if isinstance(support, dict):
        return tuple(str(item).upper() for item in support.get("providers", []) if item)
    return ()


def _observed_count(template: dict[str, Any]) -> int:
    support = template.get("support")
    if not isinstance(support, dict):
        return 0
    try:
        return int(support.get("observed_count") or 0)
    except (TypeError, ValueError):
        return 0


def _is_execution_safe_template(template: dict[str, Any]) -> bool:
    return (
        bool(template.get("execution_safe")) or str(template.get("safety_tier")) == "EXECUTION_SAFE"
    )


def _provider_report(
    *,
    provider: str,
    manifest_count: int,
    selection_count: int,
    event_candidate_count: int,
    coverage_proof_count: int,
    coverage_hyperedge_count: int,
    template_candidate_count: int,
    promoted_template_count: int,
    execution_safe_template_count: int,
    sports: tuple[str, ...],
) -> dict[str, Any]:
    blockers: list[str] = []
    semantic_candidate_count = event_candidate_count + coverage_proof_count
    if manifest_count == 0:
        blockers.append("missing_manifest")
    if selection_count == 0:
        blockers.append("no_normalized_selections")
    if semantic_candidate_count == 0:
        blockers.append("no_semantic_candidates")
    return {
        "provider": provider,
        "manifest_count": manifest_count,
        "selection_count": selection_count,
        "event_candidate_count": event_candidate_count,
        "coverage_proof_count": coverage_proof_count,
        "coverage_hyperedge_count": coverage_hyperedge_count,
        "semantic_candidate_count": semantic_candidate_count,
        "template_candidate_count": template_candidate_count,
        "promoted_template_count": promoted_template_count,
        "execution_safe_template_count": execution_safe_template_count,
        "sports": sports,
        "blockers": tuple(blockers),
        "passed": not blockers,
    }


def _sport_report(
    *,
    sport: str,
    selection_count: int,
    event_candidate_count: int,
    coverage_proof_count: int,
    coverage_hyperedge_count: int,
    template_candidate_count: int,
    providers: tuple[str, ...],
    min_candidates: int,
    target_candidates: int,
) -> dict[str, Any]:
    blockers: list[str] = []
    semantic_candidate_count = event_candidate_count + coverage_proof_count
    if selection_count == 0:
        blockers.append("no_normalized_selections")
    if semantic_candidate_count == 0:
        blockers.append("no_semantic_candidates")
    elif semantic_candidate_count < min_candidates:
        blockers.append("below_min_candidate_count")
    return {
        "sport": sport,
        "selection_count": selection_count,
        "event_candidate_count": event_candidate_count,
        "coverage_proof_count": coverage_proof_count,
        "coverage_hyperedge_count": coverage_hyperedge_count,
        "semantic_candidate_count": semantic_candidate_count,
        "template_candidate_count": template_candidate_count,
        "provider_count": len(providers),
        "providers": providers,
        "min_candidates": min_candidates,
        "target_candidates": target_candidates,
        "blockers": tuple(blockers),
        "passed": not blockers,
        "target_reached": semantic_candidate_count >= target_candidates,
    }


def _load_cache_collections(cache_dir: Path) -> CacheCollections:
    load_errors: list[dict[str, str]] = []
    return CacheCollections(
        manifests=_load_many(cache_dir, MANIFEST_INDEX_KEY, MANIFEST_PREFIX, load_errors),
        normalized_records=_load_many(
            cache_dir,
            NORMALIZED_INDEX_KEY,
            NORMALIZED_PREFIX,
            load_errors,
        ),
        candidate_rules=_load_many(cache_dir, CANDIDATE_INDEX_KEY, CANDIDATE_PREFIX, load_errors),
        template_candidates=_load_many(
            cache_dir,
            TEMPLATE_CANDIDATE_INDEX_KEY,
            TEMPLATE_CANDIDATE_PREFIX,
            load_errors,
        ),
        promoted_templates=_load_many(
            cache_dir,
            TEMPLATE_PROMOTED_INDEX_KEY,
            TEMPLATE_PROMOTED_PREFIX,
            load_errors,
        ),
        coverage_proofs=_load_many(
            cache_dir,
            COVERAGE_PROOF_INDEX_KEY,
            COVERAGE_PROOF_PREFIX,
            load_errors,
        ),
        coverage_hyperedges=_load_many(
            cache_dir,
            COVERAGE_HYPEREDGE_INDEX_KEY,
            COVERAGE_HYPEREDGE_PREFIX,
            load_errors,
        ),
        load_errors=load_errors,
    )


def _selection_counters(collections: CacheCollections) -> SelectionCounters:
    sports_by_provider: dict[str, set[str]] = defaultdict(set)
    for record in collections.normalized_records:
        provider = str(record.get("provider", "")).upper()
        sport = _normalize_sport((record.get("selection") or {}).get("sport"))
        sports_by_provider[provider].add(sport)

    return SelectionCounters(
        manifests_by_provider=Counter(
            str(item.get("provider", "")).upper() for item in collections.manifests
        ),
        selections_by_provider=Counter(
            str(item.get("provider", "")).upper() for item in collections.normalized_records
        ),
        selections_by_sport=Counter(
            _normalize_sport((item.get("selection") or {}).get("sport"))
            for item in collections.normalized_records
        ),
        sports_by_provider=sports_by_provider,
    )


def _template_candidate_counters(
    collections: CacheCollections,
) -> CandidateCounters:
    event_candidates_by_provider: Counter[str] = Counter()
    event_candidates_by_sport: Counter[str] = Counter()
    coverage_proofs_by_provider: Counter[str] = Counter()
    coverage_proofs_by_sport: Counter[str] = Counter()
    coverage_hyperedges_by_provider: Counter[str] = Counter()
    coverage_hyperedges_by_sport: Counter[str] = Counter()
    providers_by_sport: dict[str, set[str]] = defaultdict(set)
    template_candidates_by_provider: Counter[str] = Counter()
    template_candidates_by_sport: Counter[str] = Counter()

    for template in collections.template_candidates:
        sport = _normalize_sport(template.get("sport"))
        observed = _observed_count(template)
        event_candidates_by_sport[sport] += observed
        template_candidates_by_sport[sport] += 1
        for provider in _providers_from_support(template):
            event_candidates_by_provider[provider] += observed
            template_candidates_by_provider[provider] += 1
            providers_by_sport[sport].add(provider)

    proof_by_id = {
        str(proof.get("proof_id")): proof
        for proof in collections.coverage_proofs
        if proof.get("proof_id")
    }
    for proof in collections.coverage_proofs:
        sport = _normalize_sport((proof.get("universe") or {}).get("sport"))
        coverage_proofs_by_sport[sport] += 1
        provider_scope = ((proof.get("coverage_set") or {}).get("provider_scope")) or ()
        for provider in provider_scope:
            normalized_provider = str(provider).upper()
            coverage_proofs_by_provider[normalized_provider] += 1
            providers_by_sport[sport].add(normalized_provider)

    for hyperedge in collections.coverage_hyperedges:
        hyperedge_proof = proof_by_id.get(str(hyperedge.get("coverage_proof_id") or ""))
        proof_payload = hyperedge_proof if isinstance(hyperedge_proof, dict) else {}
        universe_payload = proof_payload.get("universe") or {}
        sport = _normalize_sport(
            universe_payload.get("sport") if isinstance(universe_payload, dict) else None,
        )
        coverage_hyperedges_by_sport[sport] += 1
        for provider in hyperedge.get("provider_scope", ()) or ():
            coverage_hyperedges_by_provider[str(provider).upper()] += 1

    return CandidateCounters(
        event_candidates_by_provider=event_candidates_by_provider,
        event_candidates_by_sport=event_candidates_by_sport,
        coverage_proofs_by_provider=coverage_proofs_by_provider,
        coverage_proofs_by_sport=coverage_proofs_by_sport,
        coverage_hyperedges_by_provider=coverage_hyperedges_by_provider,
        coverage_hyperedges_by_sport=coverage_hyperedges_by_sport,
        providers_by_sport=providers_by_sport,
        template_candidates_by_provider=template_candidates_by_provider,
        template_candidates_by_sport=template_candidates_by_sport,
    )


def _event_candidate_counters(candidate_rules: list[dict[str, Any]]) -> CandidateCounters:
    event_candidates_by_provider: Counter[str] = Counter()
    event_candidates_by_sport: Counter[str] = Counter()
    providers_by_sport: dict[str, set[str]] = defaultdict(set)

    for rule in candidate_rules:
        sport = _normalize_sport(rule.get("sport"))
        event_candidates_by_sport[sport] += 1
        for provider in rule.get("venue_scope", []):
            normalized_provider = str(provider).upper()
            event_candidates_by_provider[normalized_provider] += 1
            providers_by_sport[sport].add(normalized_provider)

    return CandidateCounters(
        event_candidates_by_provider=event_candidates_by_provider,
        event_candidates_by_sport=event_candidates_by_sport,
        coverage_proofs_by_provider=Counter(),
        coverage_proofs_by_sport=Counter(),
        coverage_hyperedges_by_provider=Counter(),
        coverage_hyperedges_by_sport=Counter(),
        providers_by_sport=providers_by_sport,
        template_candidates_by_provider=Counter(),
        template_candidates_by_sport=Counter(),
    )


def _candidate_counters(collections: CacheCollections) -> CandidateCounters:
    if (
        collections.template_candidates
        or collections.coverage_proofs
        or collections.coverage_hyperedges
    ):
        return _template_candidate_counters(collections)
    return _event_candidate_counters(collections.candidate_rules)


def _promotion_counters(collections: CacheCollections) -> PromotionCounters:
    promoted_by_provider: Counter[str] = Counter()
    execution_safe_by_provider: Counter[str] = Counter()
    for template in collections.promoted_templates:
        for provider in _providers_from_support(template):
            promoted_by_provider[provider] += 1
            if _is_execution_safe_template(template):
                execution_safe_by_provider[provider] += 1

    return PromotionCounters(
        promoted_by_provider=promoted_by_provider,
        execution_safe_by_provider=execution_safe_by_provider,
        safety_tier_counts=Counter(
            str(item.get("safety_tier", "")) for item in collections.template_candidates
        ),
    )


def build_completion_report(
    cache_dir: Path,
    *,
    required_providers: tuple[str, ...],
    target_sports: tuple[str, ...],
    min_candidates: int,
    target_candidates: int,
) -> dict[str, Any]:
    required = tuple(provider.upper() for provider in required_providers)
    sports = tuple(_normalize_sport(sport) for sport in target_sports)

    collections = _load_cache_collections(cache_dir)
    selection_counts = _selection_counters(collections)
    candidate_counts = _candidate_counters(collections)
    promotion_counts = _promotion_counters(collections)
    providers = tuple(
        _provider_report(
            provider=provider,
            manifest_count=selection_counts.manifests_by_provider[provider],
            selection_count=selection_counts.selections_by_provider[provider],
            event_candidate_count=candidate_counts.event_candidates_by_provider[provider],
            coverage_proof_count=candidate_counts.coverage_proofs_by_provider[provider],
            coverage_hyperedge_count=candidate_counts.coverage_hyperedges_by_provider[provider],
            template_candidate_count=candidate_counts.template_candidates_by_provider[provider],
            promoted_template_count=promotion_counts.promoted_by_provider[provider],
            execution_safe_template_count=promotion_counts.execution_safe_by_provider[provider],
            sports=tuple(sorted(selection_counts.sports_by_provider[provider])),
        )
        for provider in required
    )
    sport_reports = tuple(
        _sport_report(
            sport=sport,
            selection_count=selection_counts.selections_by_sport[sport],
            event_candidate_count=candidate_counts.event_candidates_by_sport[sport],
            coverage_proof_count=candidate_counts.coverage_proofs_by_sport[sport],
            coverage_hyperedge_count=candidate_counts.coverage_hyperedges_by_sport[sport],
            template_candidate_count=candidate_counts.template_candidates_by_sport[sport],
            providers=tuple(sorted(candidate_counts.providers_by_sport[sport])),
            min_candidates=min_candidates,
            target_candidates=target_candidates,
        )
        for sport in sports
    )
    passed = all(item["passed"] for item in providers) and all(
        item["passed"] for item in sport_reports
    )
    return {
        "passed": passed,
        "required_providers": required,
        "target_sports": sports,
        "min_candidates": min_candidates,
        "target_candidates": target_candidates,
        "total_normalized_selections": len(collections.normalized_records),
        "total_event_candidates": sum(candidate_counts.event_candidates_by_sport.values())
        if collections.template_candidates
        else len(collections.candidate_rules),
        "total_coverage_proofs": len(collections.coverage_proofs),
        "total_coverage_hyperedges": len(collections.coverage_hyperedges),
        "total_semantic_candidates": (
            (
                sum(candidate_counts.event_candidates_by_sport.values())
                if collections.template_candidates
                else len(collections.candidate_rules)
            )
            + len(collections.coverage_proofs)
        ),
        "total_template_candidates": len(collections.template_candidates),
        "total_promoted_templates": len(collections.promoted_templates),
        "total_execution_safe_templates": sum(
            1
            for template in collections.promoted_templates
            if _is_execution_safe_template(template)
        ),
        "load_error_count": len(collections.load_errors),
        "load_errors": tuple(collections.load_errors[:20]),
        "safety_tier_counts": tuple(sorted(promotion_counts.safety_tier_counts.items())),
        "providers": providers,
        "sports": sport_reports,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--required-provider", action="append", default=[])
    parser.add_argument("--target-sport", action="append", default=[])
    parser.add_argument("--min-candidates", type=int, default=10)
    parser.add_argument("--target-candidates", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_completion_report(
        Path(args.cache_dir),
        required_providers=tuple(args.required_provider),
        target_sports=tuple(args.target_sport),
        min_candidates=args.min_candidates,
        target_candidates=args.target_candidates,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
