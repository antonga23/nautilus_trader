#!/usr/bin/env python3
"""
Lightweight semantic-cache completion verifier for deployed strategy nodes.

This intentionally avoids importing Nautilus modules so the EC2 runtime verifier
can inspect the file-backed semantic cache without a repo virtualenv.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_INDEX_KEY = "betting:semantic_rules:index:manifests"
NORMALIZED_INDEX_KEY = "betting:semantic_rules:index:normalized"
CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:candidates"
TEMPLATE_CANDIDATE_INDEX_KEY = "betting:semantic_rules:index:template_candidates"
TEMPLATE_PROMOTED_INDEX_KEY = "betting:semantic_rules:index:template_promoted"

MANIFEST_PREFIX = "betting:semantic_rules:manifest"
NORMALIZED_PREFIX = "betting:semantic_rules:normalized"
CANDIDATE_PREFIX = "betting:semantic_rules:candidate"
TEMPLATE_CANDIDATE_PREFIX = "betting:semantic_rules:template:candidate"
TEMPLATE_PROMOTED_PREFIX = "betting:semantic_rules:template:promoted"


def _cache_file(cache_dir: Path, key: str) -> Path:
    key_index_path = cache_dir / "keys.json"
    if key_index_path.exists():
        key_index = json.loads(key_index_path.read_text(encoding="utf-8"))
        indexed_name = key_index.get(key)
        if indexed_name:
            return cache_dir / str(indexed_name)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.bin"


def _read_bytes(cache_dir: Path, key: str) -> bytes | None:
    path = _cache_file(cache_dir, key)
    if not path.exists():
        return None
    raw = path.read_bytes()
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def _read_json(cache_dir: Path, key: str) -> dict[str, Any] | None:
    raw = _read_bytes(cache_dir, key)
    if raw is None:
        return None
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def _read_index(cache_dir: Path, key: str) -> list[str]:
    payload = _read_json(cache_dir, key) or {}
    return [str(item) for item in payload.get("items", [])]


def _load_many(cache_dir: Path, index_key: str, prefix: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item_id in _read_index(cache_dir, index_key):
        payload = _read_json(cache_dir, f"{prefix}:{item_id}")
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


def _provider_report(
    *,
    provider: str,
    manifest_count: int,
    selection_count: int,
    event_candidate_count: int,
    template_candidate_count: int,
    promoted_template_count: int,
    execution_safe_template_count: int,
    sports: tuple[str, ...],
) -> dict[str, Any]:
    blockers: list[str] = []
    if manifest_count == 0:
        blockers.append("missing_manifest")
    if selection_count == 0:
        blockers.append("no_normalized_selections")
    if event_candidate_count == 0:
        blockers.append("no_event_candidates")
    return {
        "provider": provider,
        "manifest_count": manifest_count,
        "selection_count": selection_count,
        "event_candidate_count": event_candidate_count,
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
    template_candidate_count: int,
    providers: tuple[str, ...],
    min_candidates: int,
    target_candidates: int,
) -> dict[str, Any]:
    blockers: list[str] = []
    if selection_count == 0:
        blockers.append("no_normalized_selections")
    if event_candidate_count == 0:
        blockers.append("no_event_candidates")
    elif event_candidate_count < min_candidates:
        blockers.append("below_min_candidate_count")
    return {
        "sport": sport,
        "selection_count": selection_count,
        "event_candidate_count": event_candidate_count,
        "template_candidate_count": template_candidate_count,
        "provider_count": len(providers),
        "providers": providers,
        "min_candidates": min_candidates,
        "target_candidates": target_candidates,
        "blockers": tuple(blockers),
        "passed": not blockers,
        "target_reached": event_candidate_count >= target_candidates,
    }


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

    manifests = _load_many(cache_dir, MANIFEST_INDEX_KEY, MANIFEST_PREFIX)
    normalized_records = _load_many(cache_dir, NORMALIZED_INDEX_KEY, NORMALIZED_PREFIX)
    candidate_rules = _load_many(cache_dir, CANDIDATE_INDEX_KEY, CANDIDATE_PREFIX)
    template_candidates = _load_many(
        cache_dir,
        TEMPLATE_CANDIDATE_INDEX_KEY,
        TEMPLATE_CANDIDATE_PREFIX,
    )
    promoted_templates = _load_many(
        cache_dir,
        TEMPLATE_PROMOTED_INDEX_KEY,
        TEMPLATE_PROMOTED_PREFIX,
    )

    manifests_by_provider = Counter(str(item.get("provider", "")).upper() for item in manifests)
    selections_by_provider = Counter(
        str(item.get("provider", "")).upper() for item in normalized_records
    )
    selections_by_sport = Counter(
        _normalize_sport((item.get("selection") or {}).get("sport")) for item in normalized_records
    )
    sports_by_provider: dict[str, set[str]] = defaultdict(set)
    for record in normalized_records:
        sports_by_provider[str(record.get("provider", "")).upper()].add(
            _normalize_sport((record.get("selection") or {}).get("sport")),
        )

    event_candidates_by_provider: Counter[str] = Counter()
    event_candidates_by_sport: Counter[str] = Counter()
    providers_by_sport: dict[str, set[str]] = defaultdict(set)
    template_candidates_by_provider: Counter[str] = Counter()
    template_candidates_by_sport: Counter[str] = Counter()
    if template_candidates:
        for template in template_candidates:
            sport = _normalize_sport(template.get("sport"))
            observed = _observed_count(template)
            event_candidates_by_sport[sport] += observed
            template_candidates_by_sport[sport] += 1
            for provider in _providers_from_support(template):
                event_candidates_by_provider[provider] += observed
                template_candidates_by_provider[provider] += 1
                providers_by_sport[sport].add(provider)
    else:
        for rule in candidate_rules:
            sport = _normalize_sport(rule.get("sport"))
            event_candidates_by_sport[sport] += 1
            for provider in rule.get("venue_scope", []):
                normalized_provider = str(provider).upper()
                event_candidates_by_provider[normalized_provider] += 1
                providers_by_sport[sport].add(normalized_provider)

    promoted_by_provider: Counter[str] = Counter()
    execution_safe_by_provider: Counter[str] = Counter()
    for template in promoted_templates:
        for provider in _providers_from_support(template):
            promoted_by_provider[provider] += 1
            if bool(template.get("execution_safe")):
                execution_safe_by_provider[provider] += 1

    safety_tier_counts = Counter(str(item.get("safety_tier", "")) for item in template_candidates)
    providers = tuple(
        _provider_report(
            provider=provider,
            manifest_count=manifests_by_provider[provider],
            selection_count=selections_by_provider[provider],
            event_candidate_count=event_candidates_by_provider[provider],
            template_candidate_count=template_candidates_by_provider[provider],
            promoted_template_count=promoted_by_provider[provider],
            execution_safe_template_count=execution_safe_by_provider[provider],
            sports=tuple(sorted(sports_by_provider[provider])),
        )
        for provider in required
    )
    sport_reports = tuple(
        _sport_report(
            sport=sport,
            selection_count=selections_by_sport[sport],
            event_candidate_count=event_candidates_by_sport[sport],
            template_candidate_count=template_candidates_by_sport[sport],
            providers=tuple(sorted(providers_by_sport[sport])),
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
        "total_normalized_selections": len(normalized_records),
        "total_event_candidates": sum(event_candidates_by_sport.values())
        if template_candidates
        else len(candidate_rules),
        "total_template_candidates": len(template_candidates),
        "total_promoted_templates": len(promoted_templates),
        "total_execution_safe_templates": sum(
            1 for template in promoted_templates if bool(template.get("execution_safe"))
        ),
        "safety_tier_counts": tuple(sorted(safety_tier_counts.items())),
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
