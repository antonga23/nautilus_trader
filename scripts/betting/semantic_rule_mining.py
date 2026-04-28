#!/usr/bin/env python3
# ruff: noqa: E402
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
import sys
import time


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


def _build_cache(persist_cache: bool, cache_dir: str | None = None):
    if cache_dir:
        return FileRuleCache(cache_dir)
    if persist_cache and all(
        os.getenv(name) for name in ("POSTGRES_HOST", "POSTGRES_PASSWORD", "POSTGRES_DATABASE")
    ):
        return Cache(database=CachePostgresAdapter())
    return Cache()


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
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    ingestor = SnapshotIngestor(store)
    fixture_dir = Path(args.fixture_dir).resolve() if args.fixture_dir else None

    clock = LiveClock()
    logger = Logger(clock=clock, bypass=True)
    manifests: list[RuleCorpusManifest] = []

    if args.provider in {"cloudbet", "all"}:
        client = CloudbetClient(asyncio.get_running_loop(), logger)
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

    if args.provider in {"sxbet", "all"}:
        from_timestamp = args.from_timestamp
        to_timestamp = args.to_timestamp
        client = SXBetHttpClient(api_key=os.getenv("SXBET_API_KEY"))
        await client.connect()
        try:
            manifests.append(
                await ingestor.refresh_sxbet(
                    client,
                    sport_ids=args.sport_ids or None,
                    from_time=args.from_timestamp,
                    to_time=args.to_timestamp,
                    instrument_limit=args.instrument_limit,
                    market_discovery_limit=args.market_discovery_limit,
                ),
            )
        finally:
            await client.disconnect()

    if args.provider in {"polymarket", "all"}:
        manifests.append(
            await ingestor.refresh_polymarket(
                sports=args.sports or None,
                limit=args.limit,
            ),
        )

    if fixture_dir is not None:
        for manifest in manifests:
            _write_fixture_bundle(store, manifest, fixture_dir)

    _maybe_linear_comment(
        "Semantic corpus refresh completed:\n\n"
        + "\n".join(
            f"- `{manifest.provider}` `{manifest.manifest_id}` selections={manifest.selection_count}"
            for manifest in manifests
        ),
    )
    print(json.dumps([_serialize_manifest(manifest) for manifest in manifests], indent=2))


def _mine_candidates(args: argparse.Namespace) -> None:
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


def _generalize_templates(args: argparse.Namespace) -> None:
    cache = _build_cache(args.persist_cache, args.cache_dir)
    store = RuleStore(cache)
    miner = RuleMiner(store)
    provider = args.provider.upper() if args.provider else None
    if args.skip_event_candidates:
        existing_rules = [
            rule
            for rule_id in store.list_candidate_ids()
            if (rule := store.load_candidate(rule_id)) is not None
            and args.manifest_id is None
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
    sparse_sports: list[dict[str, object]] = []
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if snapshot is None or snapshot.endpoint != "/semantic/coverage/cloudbet":
            continue
        payload = json.loads(snapshot.payload.decode("utf-8"))
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
        "candidate_safety_tier_counts": dict(completion.safety_tier_counts),
        "promoted_safety_tier_counts": dict(
            sorted(Counter(template.safety_tier for template in promoted_templates).items()),
        ),
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
        "--provider", choices=("all", "cloudbet", "sxbet", "polymarket"), default="all"
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
    refresh.add_argument("--instrument-limit", type=int, default=250)
    refresh.add_argument("--market-discovery-limit", type=int, default=250)
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
        "mine-candidates", help="Mine candidate rules from normalized records"
    )
    mine.add_argument("--provider", choices=("cloudbet", "sxbet", "polymarket"))
    mine.add_argument("--manifest-id")
    mine.add_argument("--persist-cache", action="store_true")
    mine.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))

    generalize = subparsers.add_parser(
        "generalize-templates", help="Mine reusable semantic templates from normalized records"
    )
    generalize.add_argument("--provider", choices=("cloudbet", "sxbet", "polymarket"))
    generalize.add_argument("--manifest-id")
    generalize.add_argument("--skip-event-candidates", action="store_true")
    generalize.add_argument("--persist-cache", action="store_true")
    generalize.add_argument("--cache-dir", default=os.getenv("SEMANTIC_RULE_CACHE_DIR"))

    validate = subparsers.add_parser(
        "validate", help="Validate candidate rules from persisted provider evidence"
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
        "promote-templates", help="Promote catalog-derived semantic templates"
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
        "verify-completion", help="Fail unless semantic mining coverage gates pass"
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
        "restore-gcp-auth", help="Restore GCP service-account JSON from AWS Secrets Manager"
    )
    restore.add_argument(
        "--secret-id", default=os.getenv("AGENT_SECRET_ID", "cloudbet-market-maker/credentials")
    )
    restore.add_argument("--secret-key", default="GCP_SERVICE_ACCOUNT_JSON_B64")
    restore.add_argument(
        "--output-path",
        default=os.getenv(
            "GOOGLE_APPLICATION_CREDENTIALS", "/srv/symphony/gcp-service-account.json"
        ),
    )
    restore.add_argument(
        "--region", default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    )

    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    handlers = {
        "mine-candidates": _mine_candidates,
        "generalize-templates": _generalize_templates,
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
