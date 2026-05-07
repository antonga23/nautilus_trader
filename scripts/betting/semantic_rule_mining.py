#!/usr/bin/env python3
# ruff: noqa: E402
# skipcq
"""
Operator entrypoints for provider-backed semantic rule mining.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from typing import Any
from typing import TypedDict


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nautilus_trader.adapters.betting.semantics import HistoricalRuleValidator
from nautilus_trader.adapters.betting.semantics import LinearIssueSync
from nautilus_trader.adapters.betting.semantics import RuleMiner
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SnapshotIngestor
from nautilus_trader.adapters.betting.semantics import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import build_completion_report
from nautilus_trader.adapters.betting.semantics.secrets import load_aws_secret_payload
from nautilus_trader.adapters.betting.semantics.secrets import restore_gcp_service_account
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.cache import Cache
from nautilus_trader.cache.adapter import CachePostgresAdapter
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger


_DEFAULT_LOCAL_ENV_FILES = (
    REPO_ROOT / ".env.cloud-workspace.local",
    REPO_ROOT / ".env.local",
    REPO_ROOT / ".env",
)


class _BreakdownBucket(TypedDict):
    promoted_template_count: int
    execution_safe_template_count: int
    same_venue_execution_eligible_template_count: int
    safety_tier_counts: Counter[str]
    strict_execution_blocker_counts: Counter[str]
    strict_execution_caveat_counts: Counter[str]
    coverage_proof_count: int
    coverage_hyperedge_count: int
    coverage_blocker_counts: Counter[str]
    coverage_blocker_samples: dict[str, list[dict[str, object]]]


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return None
    value = value.strip()
    if value:
        try:
            parsed = shlex.split(value, comments=False, posix=True)
        except ValueError:
            parsed = [value]
        if len(parsed) == 1:
            value = parsed[0]
    return key, value


def _load_local_workspace_env() -> Path | None:
    for candidate in _DEFAULT_LOCAL_ENV_FILES:
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_assignment(line)
            if parsed is None:
                continue
            key, value = parsed
            os.environ.setdefault(key, value)
        return candidate
    return None


def _require_provider_credentials(provider: str) -> None:
    required_keys = {
        "cloudbet": ("CLOUDBET_API_KEY",),
        "sxbet": ("SXBET_API_KEY",),
        "polymarket": (
            "POLYMARKET_API_KEY",
            "POLYMARKET_API_SECRET",
            "POLYMARKET_PASSPHRASE",
            "POLYMARKET_PK",
            "POLYMARKET_FUNDER",
        ),
    }[provider]
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Missing required credentials for provider '{provider}': {joined}. "
            "Load the repo-local workspace env or export the variables explicitly.",
        )


def _build_cache(persist_cache: bool, cache_dir: str | None = None) -> Any:
    if cache_dir:
        return FileRuleCache(cache_dir)
    if persist_cache and all(
        os.getenv(name) for name in ("POSTGRES_HOST", "POSTGRES_PASSWORD", "POSTGRES_DATABASE")
    ):
        return Cache(database=CachePostgresAdapter())
    return Cache()


def _emit_phase_marker(phase: str, **payload: object) -> None:
    print(
        json.dumps(
            {
                "phase": phase,
                **payload,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )


def _serialize_manifest(manifest: RuleCorpusManifest) -> dict[str, object]:
    return {
        "manifest_id": manifest.manifest_id,
        "provider": manifest.provider,
        "fetched_at": manifest.fetched_at,
        "endpoint_version": manifest.endpoint_version,
        "sport_count": manifest.sport_count,
        "event_count": manifest.event_count,
        "selection_count": manifest.selection_count,
        "market_taxonomy_hash": manifest.market_taxonomy_hash,
        "source_refs": list(manifest.source_refs),
    }


def _write_fixture_bundle(
    store: RuleStore,
    manifest: RuleCorpusManifest,
    fixture_dir: Path,
) -> None:
    target_dir = fixture_dir / manifest.provider.lower() / manifest.manifest_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "manifest.json").write_text(
        json.dumps(_serialize_manifest(manifest), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    for index, snapshot_id in enumerate(manifest.source_refs, start=1):
        snapshot = store.load_snapshot(snapshot_id)
        if snapshot is None:
            continue
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", snapshot.endpoint).strip("_") or "snapshot"
        suffix = ".json" if snapshot.content_type == "application/json" else ".bin"
        (target_dir / f"{index:02d}_{slug}{suffix}").write_bytes(snapshot.payload)


def _maybe_linear_comment(body: str) -> None:
    issue_id = os.getenv("LINEAR_PARENT_ISSUE_ID")
    if not issue_id:
        return
    try:
        LinearIssueSync().create_comment(issue_id=issue_id, body=body)
    except Exception:
        return


async def _refresh_corpus(args: argparse.Namespace) -> None:
    env_path = _load_local_workspace_env()
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    ingestor = SnapshotIngestor(store)
    fixture_dir = Path(args.fixture_dir).resolve() if args.fixture_dir else None

    clock = LiveClock()
    logger = Logger(clock=clock, bypass=True)
    manifests: list[RuleCorpusManifest] = []

    if args.provider in {"cloudbet", "all"}:
        _emit_phase_marker("refresh_corpus_provider_start", provider="CLOUDBET")
        _require_provider_credentials("cloudbet")
        client = CloudbetClient(
            asyncio.get_running_loop(),
            logger,
            api_key=os.getenv("CLOUDBET_API_KEY") or "",
        )
        await client.connect()
        try:
            from_timestamp = args.from_timestamp or int(time.time())
            to_timestamp = args.to_timestamp or from_timestamp + args.initial_window_seconds
            manifests.append(
                await ingestor.refresh_cloudbet(
                    client,
                    sports=args.sports or None,
                    from_timestamp=from_timestamp,
                    to_timestamp=to_timestamp,
                    limit=args.limit,
                    adaptive_window=not args.no_adaptive_cloudbet_window,
                    max_window_seconds=args.max_window_days * 24 * 60 * 60,
                    min_events_per_sport=args.min_events_per_sport,
                    include_recent_past_on_sparse=args.include_past_on_sparse,
                    include_bets=args.include_bets and not args.skip_bets,
                    bet_page_size=args.bet_page_size,
                    bet_max_pages=args.bet_max_pages,
                    bet_from_date=args.bet_from_date,
                    bet_to_date=args.bet_to_date,
                    settled_bets_only=args.settled_bets,
                ),
            )
        finally:
            await client.disconnect()
        _emit_phase_marker("refresh_corpus_provider_done", provider="CLOUDBET")

    if args.provider in {"sxbet", "all"}:
        _emit_phase_marker("refresh_corpus_provider_start", provider="SXBET")
        _require_provider_credentials("sxbet")
        from_timestamp = args.from_timestamp
        to_timestamp = args.to_timestamp
        sxbet_client = SXBetHttpClient(api_key=os.getenv("SXBET_API_KEY"))
        await sxbet_client.connect()
        try:
            manifests.append(
                await ingestor.refresh_sxbet(
                    sxbet_client,
                    sports=args.sports or None,
                    sport_ids=args.sport_ids or None,
                    from_time=args.from_timestamp,
                    to_time=args.to_timestamp,
                    instrument_limit=args.instrument_limit,
                    market_discovery_limit=args.market_discovery_limit,
                    prefer_liquid_markets=args.prefer_liquid_markets,
                    liquidity_probe_limit=args.liquidity_probe_limit,
                    min_two_sided_markets=args.min_two_sided_markets,
                    live_only=args.live_only,
                ),
            )
        finally:
            await sxbet_client.disconnect()
        _emit_phase_marker("refresh_corpus_provider_done", provider="SXBET")

    if args.provider in {"polymarket", "all"}:
        _emit_phase_marker("refresh_corpus_provider_start", provider="POLYMARKET")
        _require_provider_credentials("polymarket")
        manifests.append(
            await ingestor.refresh_polymarket(
                sports=args.sports or None,
                limit=args.limit,
            ),
        )
        _emit_phase_marker("refresh_corpus_provider_done", provider="POLYMARKET")

    if fixture_dir is not None:
        for manifest in manifests:
            _write_fixture_bundle(store, manifest, fixture_dir)

    _maybe_linear_comment(
        "Semantic corpus refresh completed:\n\n"
        + (f"- env: `{env_path.name}`\n" if env_path is not None else "")
        + "\n".join(
            f"- `{manifest.provider}` `{manifest.manifest_id}` selections={manifest.selection_count}"
            for manifest in manifests
        ),
    )
    print(json.dumps([_serialize_manifest(manifest) for manifest in manifests], indent=2))


def _mine_candidates(args: argparse.Namespace) -> None:
    _emit_phase_marker("mine_candidates_start", provider=args.provider or "ALL")
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    miner = RuleMiner(store)
    provider = args.provider.upper() if args.provider else None
    rules = miner.mine_store(
        provider=provider,
        manifest_id=args.manifest_id,
        persist=True,
    )
    print(
        json.dumps(
            {
                "provider": provider,
                "manifest_id": args.manifest_id,
                "candidate_count": len(rules),
            },
            indent=2,
        ),
    )
    _maybe_linear_comment(
        "Semantic candidate mining completed:\n\n"
        f"- provider: `{provider or 'ALL'}`\n"
        f"- manifest: `{args.manifest_id or 'ALL'}`\n"
        f"- candidates: `{len(rules)}`",
    )
    _emit_phase_marker("mine_candidates_done", provider=provider or "ALL", count=len(rules))


def _generalize_templates(args: argparse.Namespace) -> None:
    _emit_phase_marker("generalize_templates_start", provider=args.provider or "ALL")
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    miner = RuleMiner(store)
    provider = args.provider.upper() if args.provider else None
    candidate_ids = store.list_candidate_ids()
    use_existing_candidates = args.skip_event_candidates or (
        bool(candidate_ids) and args.manifest_id is None
    )
    if use_existing_candidates:
        existing_rules = [
            rule
            for rule_id in candidate_ids
            if (rule := store.load_candidate(rule_id)) is not None
            and (provider is None or provider in rule.venue_scope)
        ]
        templates = miner.generalize(existing_rules, persist=True)
    else:
        templates = miner.mine_templates_from_store(
            provider=provider,
            manifest_id=args.manifest_id,
            persist=True,
            persist_event_candidates=True,
        )
    promotable = sum(1 for template in templates if template.support.catalog_promotable)
    execution_safe = sum(1 for template in templates if template.execution_safe)
    print(
        json.dumps(
            {
                "provider": provider,
                "manifest_id": args.manifest_id,
                "template_count": len(templates),
                "catalog_promotable_count": promotable,
                "execution_safe_count": execution_safe,
            },
            indent=2,
        ),
    )
    _maybe_linear_comment(
        "Semantic template generalization completed:\n\n"
        f"- provider: `{provider or 'ALL'}`\n"
        f"- manifest: `{args.manifest_id or 'ALL'}`\n"
        f"- templates: `{len(templates)}`\n"
        f"- catalog-promotable: `{promotable}`",
    )
    _emit_phase_marker(
        "generalize_templates_done",
        provider=provider or "ALL",
        count=len(templates),
        execution_safe=execution_safe,
    )


def _mine_coverage(args: argparse.Namespace) -> None:
    _emit_phase_marker("mine_coverage_start", provider=args.provider or "ALL")
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    miner = RuleMiner(store)
    provider = args.provider.upper() if args.provider else None
    proofs, hyperedges = miner.mine_coverage_from_store(
        provider=provider,
        manifest_id=args.manifest_id,
        persist=True,
    )
    blocker_counts: Counter[str] = Counter()
    for proof in proofs:
        for reason in proof.blocker_reasons:
            blocker_counts[reason] += 1
    payload = {
        "provider": provider,
        "manifest_id": args.manifest_id,
        "coverage_proof_count": len(proofs),
        "coverage_hyperedge_count": len(hyperedges),
        "complete_coverage_count": sum(1 for proof in proofs if proof.complete),
        "execution_safe_coverage_count": sum(1 for proof in proofs if proof.execution_safe),
        "same_venue_eligible_coverage_count": sum(
            1 for proof in proofs if proof.same_venue_execution_eligible
        ),
        "coverage_blocker_counts": dict(sorted(blocker_counts.items())),
        "coverage_blocker_samples": _coverage_blocker_samples(proofs),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    _maybe_linear_comment(
        "Semantic coverage mining completed:\n\n"
        f"- provider: `{provider or 'ALL'}`\n"
        f"- manifest: `{args.manifest_id or 'ALL'}`\n"
        f"- coverage proofs: `{len(proofs)}`\n"
        f"- hyperedges: `{len(hyperedges)}`\n"
        f"- execution-safe proofs: `{payload['execution_safe_coverage_count']}`",
    )
    _emit_phase_marker(
        "mine_coverage_done",
        provider=provider or "ALL",
        proof_count=len(proofs),
        hyperedge_count=len(hyperedges),
    )


def _validate_rules(args: argparse.Namespace) -> None:
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    validator = HistoricalRuleValidator(store)
    provider = args.provider.upper() if args.provider else None
    stats = validator.validate_store(
        provider=provider,
        manifest_id=args.manifest_id,
        persist=True,
    )
    print(
        json.dumps(
            {
                "provider": provider,
                "manifest_id": args.manifest_id,
                "validated_rule_count": len(stats),
            },
            indent=2,
        ),
    )
    _maybe_linear_comment(
        "Semantic validation completed:\n\n"
        f"- provider: `{provider or 'ALL'}`\n"
        f"- manifest: `{args.manifest_id or 'ALL'}`\n"
        f"- validated rules: `{len(stats)}`",
    )


def _promote_rules(args: argparse.Namespace) -> None:
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    allowlisted_scopes = {
        tuple(sorted(item.strip() for item in scope.split(",") if item.strip()))
        for scope in args.allowlisted_scope
    }
    policy = RulePromotionPolicy(allowlisted_venue_scopes=allowlisted_scopes)

    promoted = 0
    for rule_id in store.list_candidate_ids():
        rule = store.load_candidate(rule_id)
        stats = store.load_validation(rule_id)
        if rule is None:
            continue
        if policy.promote(store, rule, stats):
            promoted += 1

    print(json.dumps({"promoted_count": promoted}, indent=2))
    _maybe_linear_comment(
        f"Semantic promotion completed:\n\n- promoted rules: `{promoted}`",
    )


def _promote_templates(args: argparse.Namespace) -> None:
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    allowlisted_scopes = {
        tuple(sorted(item.strip().upper() for item in scope.split(",") if item.strip()))
        for scope in args.allowlisted_scope
    }
    policy = RulePromotionPolicy()

    promoted = 0
    considered = 0
    execution_safe = 0
    same_venue_execution_eligible = 0
    tier_counts: Counter[str] = Counter()
    for template_id in store.list_template_candidate_ids():
        template = store.load_template_candidate(template_id)
        if template is None:
            continue
        considered += 1
        provider_scope = tuple(sorted(template.support.providers))
        allowlisted = provider_scope in allowlisted_scopes or args.allowlisted
        promoted_template = policy.promote_template(
            store,
            template,
            allowlisted=allowlisted,
            venue_agnostic=args.venue_agnostic,
        )
        if promoted_template is None:
            continue
        promoted += 1
        tier_counts[promoted_template.safety_tier] += 1
        if promoted_template.execution_safe:
            execution_safe += 1
        if promoted_template.same_venue_execution_eligible:
            same_venue_execution_eligible += 1

    print(
        json.dumps(
            {
                "considered_template_count": considered,
                "promoted_template_count": promoted,
                "execution_safe_template_count": execution_safe,
                "same_venue_execution_eligible_template_count": same_venue_execution_eligible,
                "safety_tier_counts": dict(sorted(tier_counts.items())),
            },
            indent=2,
        ),
    )
    _maybe_linear_comment(
        "Semantic template promotion completed:\n\n"
        f"- considered templates: `{considered}`\n"
        f"- promoted templates: `{promoted}`\n"
        f"- execution-safe templates: `{execution_safe}`\n"
        f"- same-venue eligible templates: `{same_venue_execution_eligible}`",
    )


def _report_coverage(args: argparse.Namespace) -> None:
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    manifests = [
        manifest
        for manifest_id in store.list_manifest_ids()
        if (manifest := store.load_manifest(manifest_id)) is not None
    ]
    candidate_templates = [
        template
        for template_id in store.list_template_candidate_ids()
        if (template := store.load_template_candidate(template_id)) is not None
    ]
    promoted_templates = [
        template
        for template_id in store.list_promoted_template_ids()
        if (template := store.load_promoted_template(template_id)) is not None
    ]
    coverage_proofs = [
        proof
        for proof_id in store.list_coverage_proof_ids()
        if (proof := store.load_coverage_proof(proof_id)) is not None
    ]
    coverage_hyperedges = [
        hyperedge
        for hyperedge_id in store.list_coverage_hyperedge_ids()
        if (hyperedge := store.load_coverage_hyperedge(hyperedge_id)) is not None
    ]
    normalized_records = [
        record
        for record_id in store.list_normalized_ids()
        if (record := store.load_normalized_selection(record_id)) is not None
    ]
    sparse_sports: list[dict[str, object]] = []
    provider_coverage: dict[str, object] = {}
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if snapshot is None or not snapshot.endpoint.startswith("/semantic/coverage/"):
            continue
        payload = json.loads(snapshot.payload.decode("utf-8"))
        provider_coverage[snapshot.endpoint] = payload
        if snapshot.endpoint != "/semantic/coverage/cloudbet":
            continue
        for sport, report in payload.get("sports", {}).items():
            if report.get("sparse"):
                sparse_sports.append(
                    {
                        "sport": sport,
                        "event_count": report.get("event_count", 0),
                        "selection_count": report.get("selection_count", 0),
                    },
                )

    completion_kwargs = {
        "min_candidates": args.min_candidates,
        "target_candidates": args.target_candidates,
    }
    if args.required_provider:
        completion_kwargs["required_providers"] = tuple(args.required_provider)
    if args.target_sport:
        completion_kwargs["target_sports"] = tuple(args.target_sport)
    completion = build_completion_report(store, **completion_kwargs)
    report = {
        "manifest_count": len(manifests),
        "selection_count": sum(manifest.selection_count for manifest in manifests),
        "event_candidate_count": len(store.list_candidate_ids()),
        "candidate_template_count": len(candidate_templates),
        "catalog_promotable_template_count": sum(
            1 for template in candidate_templates if template.support.catalog_promotable
        ),
        "promoted_template_count": len(promoted_templates),
        "execution_safe_template_count": sum(
            1 for template in promoted_templates if template.execution_safe
        ),
        "same_venue_execution_eligible_template_count": sum(
            1 for template in promoted_templates if template.same_venue_execution_eligible
        ),
        "coverage_proof_count": len(coverage_proofs),
        "coverage_hyperedge_count": len(coverage_hyperedges),
        "coverage_complete_count": sum(1 for proof in coverage_proofs if proof.complete),
        "coverage_execution_safe_count": sum(
            1 for proof in coverage_proofs if proof.execution_safe
        ),
        "coverage_same_venue_eligible_count": sum(
            1 for proof in coverage_proofs if proof.same_venue_execution_eligible
        ),
        "coverage_blocker_counts": _coverage_blocker_counts(coverage_proofs),
        "coverage_blocker_samples": _coverage_blocker_samples(coverage_proofs),
        "coverage_proof_breakdown": _coverage_proof_breakdown(coverage_proofs),
        "candidate_safety_tier_counts": dict(completion.safety_tier_counts),
        "promoted_safety_tier_counts": dict(
            sorted(Counter(template.safety_tier for template in promoted_templates).items()),
        ),
        "provider_template_breakdown": _provider_template_breakdown(
            promoted_templates,
            coverage_proofs,
            coverage_hyperedges,
        ),
        "sport_template_breakdown": _sport_template_breakdown(
            promoted_templates,
            coverage_proofs,
            coverage_hyperedges,
        ),
        "provider_sport_template_breakdown": _provider_sport_template_breakdown(
            promoted_templates,
            coverage_proofs,
            coverage_hyperedges,
        ),
        "promoted_template_strictness": _promoted_template_strictness(promoted_templates),
        "normalized_market_coverage": _normalized_market_coverage(normalized_records),
        "template_coverage": _template_coverage(candidate_templates, promoted_templates),
        "provider_coverage": provider_coverage,
        "providers": [provider.__dict__ for provider in completion.providers],
        "sports": [sport.__dict__ for sport in completion.sports],
        "promotion_blockers": dict(completion.promotion_blockers),
        "sparse_sports": sparse_sports[: args.max_sparse_sports],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    _maybe_linear_comment(
        "Semantic coverage report:\n\n"
        f"- manifests: `{report['manifest_count']}`\n"
        f"- selections: `{report['selection_count']}`\n"
        f"- candidate templates: `{report['candidate_template_count']}`\n"
        f"- promoted templates: `{report['promoted_template_count']}`\n"
        f"- execution-safe templates: `{report['execution_safe_template_count']}`\n"
        f"- same-venue eligible templates: `{report['same_venue_execution_eligible_template_count']}`\n"
        f"- sparse sports sampled: `{len(sparse_sports)}`",
    )


def _normalized_market_coverage(records: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        selection = record.selection
        key = "|".join(
            [
                str(record.provider).upper(),
                str(selection.sport),
                str(selection.market_family),
                str(selection.selection),
            ],
        )
        counts[key] += 1
    return dict(sorted(counts.items()))


def _template_coverage(
    candidate_templates: list[Any],
    promoted_templates: list[Any],
) -> dict[str, object]:
    candidate_counts: Counter[str] = Counter()
    promoted_counts: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    blocker_samples: dict[str, list[dict[str, object]]] = {}

    for template in candidate_templates:
        key = _template_coverage_key(template)
        candidate_counts[key] += 1
        for blocker in _template_blocker_reasons(template):
            blockers[blocker] += 1
            samples = blocker_samples.setdefault(blocker, [])
            if len(samples) < 3:
                samples.append(_template_blocker_sample(template))

    for template in promoted_templates:
        promoted_counts[_template_coverage_key(template)] += 1

    return {
        "candidate_counts": dict(sorted(candidate_counts.items())),
        "promoted_counts": dict(sorted(promoted_counts.items())),
        "blocker_counts": dict(sorted(blockers.items())),
        "blocker_samples": dict(sorted(blocker_samples.items())),
    }


def _breakdown_bucket(
    breakdown: dict[str, _BreakdownBucket],
    key: str,
) -> _BreakdownBucket:
    return breakdown.setdefault(
        key,
        {
            "promoted_template_count": 0,
            "execution_safe_template_count": 0,
            "same_venue_execution_eligible_template_count": 0,
            "safety_tier_counts": Counter(),
            "strict_execution_blocker_counts": Counter(),
            "strict_execution_caveat_counts": Counter(),
            "coverage_proof_count": 0,
            "coverage_hyperedge_count": 0,
            "coverage_blocker_counts": Counter(),
            "coverage_blocker_samples": {},
        },
    )


def _finalize_breakdown(
    breakdown: dict[str, _BreakdownBucket],
) -> dict[str, dict[str, object]]:
    return {
        key: {
            **{
                field: value
                for field, value in payload.items()
                if field
                not in {
                    "safety_tier_counts",
                    "strict_execution_blocker_counts",
                    "strict_execution_caveat_counts",
                    "coverage_blocker_counts",
                    "coverage_blocker_samples",
                }
            },
            "safety_tier_counts": dict(sorted(payload["safety_tier_counts"].items())),
            "strict_execution_blocker_counts": dict(
                sorted(payload["strict_execution_blocker_counts"].items()),
            ),
            "strict_execution_caveat_counts": dict(
                sorted(payload["strict_execution_caveat_counts"].items()),
            ),
            "coverage_blocker_counts": dict(sorted(payload["coverage_blocker_counts"].items())),
            "coverage_blocker_samples": dict(sorted(payload["coverage_blocker_samples"].items())),
        }
        for key, payload in sorted(breakdown.items())
    }


def _report_sport_key(sport: str) -> str:
    normalized = str(sport).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "soccer/football": "soccer",
        "soccer_football": "soccer",
        "football": "american_football",
        "hockey": "ice_hockey",
    }
    return aliases.get(normalized, normalized)


def _accumulate_template_breakdown(
    bucket: _BreakdownBucket,
    template: Any,
) -> None:
    bucket["promoted_template_count"] += 1
    if template.execution_safe:
        bucket["execution_safe_template_count"] += 1
    if template.same_venue_execution_eligible:
        bucket["same_venue_execution_eligible_template_count"] += 1
    bucket["safety_tier_counts"][str(template.safety_tier)] += 1
    for blocker in _strict_execution_blockers(template):
        bucket["strict_execution_blocker_counts"][str(blocker)] += 1
    for caveat in getattr(template, "caveats", ()) or ():
        bucket["strict_execution_caveat_counts"][str(caveat)] += 1


def _accumulate_proof_breakdown(
    bucket: _BreakdownBucket,
    proof: Any,
    *,
    sample_limit_per_reason: int = 3,
) -> None:
    bucket["coverage_proof_count"] += 1
    sample = _coverage_proof_sample(proof)
    for blocker in proof.blocker_reasons:
        bucket["coverage_blocker_counts"][str(blocker)] += 1
        samples = bucket["coverage_blocker_samples"].setdefault(str(blocker), [])
        if len(samples) < sample_limit_per_reason:
            samples.append(sample)


def _provider_template_breakdown(
    promoted_templates: list[Any],
    coverage_proofs: list[Any],
    coverage_hyperedges: list[Any],
) -> dict[str, dict[str, object]]:
    breakdown: dict[str, _BreakdownBucket] = {}

    for template in promoted_templates:
        providers = tuple(str(provider).upper() for provider in template.support.providers)
        for provider in providers:
            _accumulate_template_breakdown(_breakdown_bucket(breakdown, provider), template)

    for proof in coverage_proofs:
        for provider in proof.coverage_set.provider_scope:
            _accumulate_proof_breakdown(_breakdown_bucket(breakdown, str(provider).upper()), proof)

    for hyperedge in coverage_hyperedges:
        for provider in hyperedge.provider_scope:
            bucket = _breakdown_bucket(breakdown, str(provider).upper())
            bucket["coverage_hyperedge_count"] += 1

    return _finalize_breakdown(breakdown)


def _sport_template_breakdown(
    promoted_templates: list[Any],
    coverage_proofs: list[Any],
    coverage_hyperedges: list[Any],
) -> dict[str, dict[str, object]]:
    breakdown: dict[str, _BreakdownBucket] = {}
    proof_sports = {
        str(proof.proof_id): _report_sport_key(str(proof.universe.sport))
        for proof in coverage_proofs
    }

    for template in promoted_templates:
        _accumulate_template_breakdown(
            _breakdown_bucket(breakdown, _report_sport_key(str(template.sport))),
            template,
        )

    for proof in coverage_proofs:
        _accumulate_proof_breakdown(
            _breakdown_bucket(breakdown, _report_sport_key(str(proof.universe.sport))),
            proof,
        )

    for hyperedge in coverage_hyperedges:
        sport = proof_sports.get(str(hyperedge.coverage_proof_id), "unknown")
        bucket = _breakdown_bucket(breakdown, sport)
        bucket["coverage_hyperedge_count"] += 1

    return _finalize_breakdown(breakdown)


def _provider_sport_template_breakdown(
    promoted_templates: list[Any],
    coverage_proofs: list[Any],
    coverage_hyperedges: list[Any],
) -> dict[str, dict[str, object]]:
    breakdown: dict[str, _BreakdownBucket] = {}
    proof_sports = {
        str(proof.proof_id): _report_sport_key(str(proof.universe.sport))
        for proof in coverage_proofs
    }

    for template in promoted_templates:
        sport = _report_sport_key(str(template.sport))
        providers = tuple(str(provider).upper() for provider in template.support.providers)
        for provider in providers:
            _accumulate_template_breakdown(
                _breakdown_bucket(breakdown, f"{provider}|{sport}"),
                template,
            )

    for proof in coverage_proofs:
        sport = _report_sport_key(str(proof.universe.sport))
        for provider in proof.coverage_set.provider_scope:
            _accumulate_proof_breakdown(
                _breakdown_bucket(breakdown, f"{str(provider).upper()}|{sport}"),
                proof,
            )

    for hyperedge in coverage_hyperedges:
        sport = proof_sports.get(str(hyperedge.coverage_proof_id), "unknown")
        for provider in hyperedge.provider_scope:
            bucket = _breakdown_bucket(breakdown, f"{str(provider).upper()}|{sport}")
            bucket["coverage_hyperedge_count"] += 1

    return _finalize_breakdown(breakdown)


def _template_blocker_reasons(template: Any) -> tuple[str, ...]:
    blockers: list[str] = []
    if template.relationship_type == "DANGEROUS_NON_EQUIVALENT":
        blockers.append("dangerous_non_equivalent")
    if template.safety_tier == "AUDIT_ONLY":
        blockers.append("audit_only")
    if template.has_unknown:
        blockers.append("unknown_settlement")
    if template.has_void:
        blockers.append("void_settlement")
    if template.has_partial:
        blockers.append("partial_settlement")
    blockers.extend(_catalog_support_blockers(template.support))
    return tuple(blockers)


def _template_blocker_sample(template: Any) -> dict[str, object]:
    return {
        "template_id": str(template.template_id),
        "providers": [str(provider).upper() for provider in template.support.providers],
        "sport": str(template.sport),
        "market_family_pair": [
            str(template.pattern_a.market_family),
            str(template.pattern_b.market_family),
        ],
        "selection_pair": [
            str(template.pattern_a.selection),
            str(template.pattern_b.selection),
        ],
        "params_a": [[str(key), str(value)] for key, value in template.pattern_a.params],
        "params_b": [[str(key), str(value)] for key, value in template.pattern_b.params],
        "relationship_type": str(template.relationship_type),
        "safety_tier": str(template.safety_tier),
        "caveats": [str(caveat) for caveat in template.caveats],
        "observed_count": int(template.support.observed_count),
        "event_count": int(template.support.event_count),
        "catalog_promotable": bool(template.support.catalog_promotable),
        "execution_safe": bool(template.execution_safe),
        "same_venue_execution_eligible": bool(template.same_venue_execution_eligible),
    }


def _template_coverage_key(template: Any) -> str:
    providers = ",".join(str(provider).upper() for provider in template.support.providers)
    return "|".join(
        [
            providers or "UNKNOWN",
            str(template.sport),
            f"{template.pattern_a.market_family}+{template.pattern_b.market_family}",
            f"{template.pattern_a.selection}+{template.pattern_b.selection}",
            str(template.relationship_type),
            str(template.safety_tier),
        ],
    )


def _promoted_template_strictness(promoted_templates: list[Any]) -> dict[str, object]:
    by_family_tier: Counter[str] = Counter()
    strict_blockers: Counter[str] = Counter()
    blocker_samples: dict[str, list[dict[str, object]]] = {}
    same_venue_breakdown: Counter[str] = Counter()
    execution_safe_breakdown: Counter[str] = Counter()
    caveat_counts: Counter[str] = Counter()

    for template in promoted_templates:
        breakdown_key = _template_coverage_key(template)
        by_family_tier[breakdown_key] += 1
        for caveat in getattr(template, "caveats", ()) or ():
            caveat_counts[str(caveat)] += 1
        if template.execution_safe:
            execution_safe_breakdown[breakdown_key] += 1
            continue
        if template.same_venue_execution_eligible:
            same_venue_breakdown[breakdown_key] += 1
        for blocker in _strict_execution_blockers(template):
            strict_blockers[blocker] += 1
            samples = blocker_samples.setdefault(blocker, [])
            if len(samples) < 3:
                samples.append(_template_blocker_sample(template))

    return {
        "by_family_tier": dict(sorted(by_family_tier.items())),
        "strict_execution_blocker_counts": dict(sorted(strict_blockers.items())),
        "strict_execution_blocker_samples": dict(sorted(blocker_samples.items())),
        "same_venue_eligible_breakdown": dict(sorted(same_venue_breakdown.items())),
        "execution_safe_breakdown": dict(sorted(execution_safe_breakdown.items())),
        "caveat_counts": dict(sorted(caveat_counts.items())),
    }


def _strict_execution_blockers(template: Any) -> tuple[str, ...]:
    blockers: list[str] = []
    if template.same_venue_execution_eligible:
        blockers.append("same_venue_risk_engine_elevation_required")
    if template.relationship_type != "COMPLEMENTARY_COVERAGE":
        blockers.append("non_complementary_relationship")
    if template.has_void:
        blockers.append("void_states_present")
    if template.has_partial:
        blockers.append("partial_settlement_present")
    if template.has_unknown:
        blockers.append("unknown_settlement_present")
    blockers.extend(_catalog_support_blockers(template.support))
    if not blockers and not template.execution_safe:
        blockers.extend(str(reason) for reason in getattr(template, "eligibility_reasons", ()))
    return tuple(sorted(set(blockers)))


def _catalog_support_blockers(support: Any) -> tuple[str, ...]:
    blockers: list[str] = []
    if support is None:
        return tuple(blockers)
    if getattr(support, "catalog_promotable", False):
        return tuple(blockers)
    if not getattr(support, "deterministic", True):
        blockers.append("nondeterministic_support")
    if getattr(support, "unknown_settlement_count", 0) > 0:
        blockers.append("support_unknown_settlement_present")
    if getattr(support, "observed_count", 0) < 10:
        blockers.append("observed_count_below_10")
    if getattr(support, "event_count", 0) < 3:
        blockers.append("event_count_below_3")
    if getattr(support, "mismatch_rate", 1.0) > 0.01:
        blockers.append("mismatch_rate_above_0_01")
    if getattr(support, "confidence", 0.0) < 0.99:
        blockers.append("confidence_below_0_99")
    if not blockers:
        blockers.append("catalog_support_below_gate")
    return tuple(sorted(set(blockers)))


def _coverage_blocker_counts(proofs: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for proof in proofs:
        for reason in proof.blocker_reasons:
            counts[str(reason)] += 1
    return dict(sorted(counts.items()))


def _coverage_blocker_samples(
    proofs: list[Any],
    *,
    limit_per_reason: int = 3,
) -> dict[str, list[dict[str, object]]]:
    samples: dict[str, list[dict[str, object]]] = {}
    for proof in proofs:
        sample = _coverage_proof_sample(proof)
        for reason in proof.blocker_reasons:
            bucket = samples.setdefault(str(reason), [])
            if len(bucket) < limit_per_reason:
                bucket.append(sample)
    return dict(sorted(samples.items()))


def _coverage_proof_sample(proof: Any) -> dict[str, object]:
    return {
        "proof_id": str(proof.proof_id),
        "sport": str(proof.universe.sport),
        "scope": str(proof.universe.scope),
        "provider_scope": [str(item) for item in proof.coverage_set.provider_scope],
        "market_families": [str(item) for item in proof.coverage_set.market_families],
        "relationship_type": str(proof.relationship_type),
        "safety_tier": str(proof.safety_tier),
        "complete": bool(proof.complete),
        "execution_safe": bool(proof.execution_safe),
        "same_venue_execution_eligible": bool(proof.same_venue_execution_eligible),
        "instrument_ids": [str(predicate.instrument_id) for predicate in proof.predicates],
        "gap_states": [
            {
                "state_id": str(gap.state_id),
                "reason": str(gap.reason),
                "detail": str(gap.detail),
            }
            for gap in proof.gaps[:5]
        ],
        "risk_states": [
            {
                "state_id": str(risk.state_id),
                "reason": str(risk.reason),
                "detail": str(risk.detail),
                "severity": str(risk.severity),
            }
            for risk in proof.risks[:5]
        ],
    }


def _coverage_proof_breakdown(proofs: list[Any]) -> dict[str, object]:
    by_provider_sport_family_tier: Counter[str] = Counter()
    blocker_by_sport: Counter[str] = Counter()
    gap_count_by_sport: Counter[str] = Counter()
    for proof in proofs:
        providers = ",".join(str(item).upper() for item in proof.coverage_set.provider_scope)
        families = "+".join(str(item) for item in proof.coverage_set.market_families)
        key = "|".join(
            [
                providers or "UNKNOWN",
                str(proof.universe.sport),
                families or "UNKNOWN",
                str(proof.relationship_type),
                str(proof.safety_tier),
            ],
        )
        by_provider_sport_family_tier[key] += 1
        for blocker in proof.blocker_reasons:
            blocker_by_sport[f"{proof.universe.sport}|{blocker}"] += 1
        if proof.gaps:
            gap_count_by_sport[str(proof.universe.sport)] += len(proof.gaps)
    return {
        "by_provider_sport_family_tier": dict(sorted(by_provider_sport_family_tier.items())),
        "blocker_by_sport": dict(sorted(blocker_by_sport.items())),
        "gap_count_by_sport": dict(sorted(gap_count_by_sport.items())),
    }


def _verify_completion(args: argparse.Namespace) -> None:
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    completion_kwargs = {
        "min_candidates": args.min_candidates,
        "target_candidates": args.target_candidates,
    }
    if args.required_provider:
        completion_kwargs["required_providers"] = tuple(args.required_provider)
    if args.target_sport:
        completion_kwargs["target_sports"] = tuple(args.target_sport)
    report = build_completion_report(store, **completion_kwargs)
    payload = report.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True))
    _maybe_linear_comment(
        "Semantic mining completion verification:\n\n"
        f"- passed: `{report.passed}`\n"
        f"- normalized selections: `{report.total_normalized_selections}`\n"
        f"- event candidates: `{report.total_event_candidates}`\n"
        f"- template candidates: `{report.total_template_candidates}`\n"
        f"- promoted templates: `{report.total_promoted_templates}`\n"
        f"- execution-safe templates: `{report.total_execution_safe_templates}`",
    )
    if not report.passed:
        raise SystemExit(1)


def _sync_linear(args: argparse.Namespace) -> None:
    sync = LinearIssueSync()
    created = sync.create_semantic_rule_ticket_set()
    if args.comment:
        sync.create_comment(issue_id=created["parent"], body=args.comment)
    print(json.dumps(created, indent=2, sort_keys=True))


def _restore_gcp_auth(args: argparse.Namespace) -> None:
    payload = load_aws_secret_payload(
        secret_id=args.secret_id,
        region=args.region,
    )
    path = restore_gcp_service_account(
        payload=payload,
        output_path=args.output_path,
        secret_key=args.secret_key,
    )
    print(json.dumps({"output_path": str(path)}, indent=2))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser("refresh-corpus", help="Refresh provider corpora")
    refresh.add_argument(
        "--provider",
        choices=("all", "cloudbet", "sxbet", "polymarket"),
        default="all",
    )
    refresh.add_argument("--sports", nargs="*", default=[])
    refresh.add_argument("--sport-ids", nargs="*", type=int, default=[])
    refresh.add_argument("--from-timestamp", type=int)
    refresh.add_argument("--to-timestamp", type=int)
    refresh.add_argument("--initial-window-seconds", type=int, default=24 * 60 * 60)
    refresh.add_argument("--no-adaptive-cloudbet-window", action="store_true")
    refresh.add_argument("--max-window-days", type=int, default=7)
    refresh.add_argument("--min-events-per-sport", type=int, default=1)
    refresh.add_argument("--include-past-on-sparse", action="store_true")
    refresh.add_argument("--limit", type=int, default=20)
    refresh.add_argument("--instrument-limit", type=int, default=1000)
    refresh.add_argument("--market-discovery-limit", type=int, default=1000)
    refresh.add_argument("--prefer-liquid-markets", action="store_true")
    refresh.add_argument("--liquidity-probe-limit", type=int, default=100)
    refresh.add_argument("--min-two-sided-markets", type=int, default=1)
    refresh.add_argument("--live-only", action="store_true")
    refresh.add_argument("--include-bets", action="store_true")
    refresh.add_argument("--skip-bets", action="store_true")
    refresh.add_argument("--bet-page-size", type=int, default=50)
    refresh.add_argument("--bet-max-pages", type=int, default=5)
    refresh.add_argument("--bet-from-date")
    refresh.add_argument("--bet-to-date")
    refresh.add_argument("--settled-bets", action="store_true")
    refresh.add_argument("--persist-cache", action="store_true")
    refresh.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))
    refresh.add_argument("--fixture-dir")

    mine = subparsers.add_parser(
        "mine-candidates",
        help="Mine candidate rules from normalized records",
    )
    mine.add_argument("--provider", choices=("cloudbet", "sxbet", "polymarket"))
    mine.add_argument("--manifest-id")
    mine.add_argument("--persist-cache", action="store_true")
    mine.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))

    generalize = subparsers.add_parser(
        "generalize-templates",
        help="Mine reusable semantic templates from normalized records",
    )
    generalize.add_argument("--provider", choices=("cloudbet", "sxbet", "polymarket"))
    generalize.add_argument("--manifest-id")
    generalize.add_argument("--skip-event-candidates", action="store_true")
    generalize.add_argument("--persist-cache", action="store_true")
    generalize.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))

    coverage = subparsers.add_parser(
        "mine-coverage",
        help="Mine generalized semantic coverage proofs and hyperedges",
    )
    coverage.add_argument("--provider", choices=("cloudbet", "sxbet", "polymarket"))
    coverage.add_argument("--manifest-id")
    coverage.add_argument("--persist-cache", action="store_true")
    coverage.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))

    validate = subparsers.add_parser(
        "validate",
        help="Validate candidate rules from persisted provider evidence",
    )
    validate.add_argument("--provider", choices=("cloudbet", "sxbet", "polymarket"))
    validate.add_argument("--manifest-id")
    validate.add_argument("--persist-cache", action="store_true")
    validate.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))

    promote = subparsers.add_parser("promote", help="Promote validated candidate rules")
    promote.add_argument("--persist-cache", action="store_true")
    promote.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))
    promote.add_argument("--allowlisted-scope", action="append", default=[])

    promote_templates = subparsers.add_parser(
        "promote-templates",
        help="Promote catalog-derived semantic templates",
    )
    promote_templates.add_argument("--persist-cache", action="store_true")
    promote_templates.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))
    promote_templates.add_argument("--allowlisted-scope", action="append", default=[])
    promote_templates.add_argument("--allowlisted", action="store_true")
    promote_templates.add_argument("--venue-agnostic", action="store_true")

    report = subparsers.add_parser("report-coverage", help="Report corpus/template coverage")
    report.add_argument("--persist-cache", action="store_true")
    report.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))
    report.add_argument("--max-sparse-sports", type=int, default=25)
    report.add_argument(
        "--required-provider",
        action="append",
        default=[],
    )
    report.add_argument(
        "--target-sport",
        action="append",
        default=[],
    )
    report.add_argument("--min-candidates", type=int, default=10)
    report.add_argument("--target-candidates", type=int, default=20)

    verify = subparsers.add_parser(
        "verify-completion",
        help="Fail unless semantic mining coverage gates pass",
    )
    verify.add_argument("--persist-cache", action="store_true")
    verify.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))
    verify.add_argument(
        "--required-provider",
        action="append",
        default=[],
    )
    verify.add_argument(
        "--target-sport",
        action="append",
        default=[],
    )
    verify.add_argument("--min-candidates", type=int, default=10)
    verify.add_argument("--target-candidates", type=int, default=20)

    linear = subparsers.add_parser("sync-linear", help="Create the Linear issue hierarchy")
    linear.add_argument("--comment")

    restore = subparsers.add_parser(
        "restore-gcp-auth",
        help="Restore GCP service-account JSON from AWS Secrets Manager",
    )
    restore.add_argument(
        "--secret-id",
        default=os.getenv("AGENT_SECRET_ID", "cloudbet-market-maker/credentials"),
    )
    restore.add_argument("--secret-key", default="GCP_SERVICE_ACCOUNT_JSON_B64")
    restore.add_argument(
        "--output-path",
        default=os.getenv(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "/srv/symphony/gcp-service-account.json",
        ),
    )
    restore.add_argument(
        "--region",
        default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
    )

    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    handlers = {
        "mine-candidates": _mine_candidates,
        "generalize-templates": _generalize_templates,
        "mine-coverage": _mine_coverage,
        "validate": _validate_rules,
        "promote": _promote_rules,
        "promote-templates": _promote_templates,
        "report-coverage": _report_coverage,
        "verify-completion": _verify_completion,
        "sync-linear": _sync_linear,
        "restore-gcp-auth": _restore_gcp_auth,
    }
    if args.command == "refresh-corpus":
        asyncio.run(_refresh_corpus(args))
        return
    handler = handlers.get(args.command)
    if handler is None:
        raise ValueError(f"Unhandled command {args.command}")
    handler(args)


if __name__ == "__main__":
    main()
