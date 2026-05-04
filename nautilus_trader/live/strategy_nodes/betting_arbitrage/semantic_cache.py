from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict
from dataclasses import dataclass
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleMiner
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SEMANTIC_TARGET_SPORTS
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import SnapshotIngestor
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import (
    BettingArbitrageNodeManifest,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import BettingVenueManifest


DEFAULT_CLOUDBET_SPORTS = SEMANTIC_TARGET_SPORTS
DEFAULT_POLYMARKET_SPORTS = SEMANTIC_TARGET_SPORTS
SEMANTIC_CACHE_COMPATIBILITY_VERSION = "semantic-rule-cache:20260504:six-sport-v1"
SEMANTIC_CACHE_COMPATIBILITY_FILE = ".semantic-cache-version"
PORTABLE_POLYMARKET_MARKET_FAMILIES = frozenset(
    {
        "MATCH_ODDS",
        "DOUBLE_CHANCE",
        "WINNER",
        "TOTALS",
        "TEAM_TOTALS",
        "POINT_SPREAD",
        "ASIAN_HANDICAP",
    },
)


@dataclass(frozen=True)
class SemanticCacheStatus:
    path: str | None
    source: str
    manifest_count: int
    promoted_template_count: int
    execution_safe_template_count: int
    same_venue_execution_eligible_template_count: int
    compatibility_version: str | None = None
    compatibility_scope: str | None = None
    compatible: bool = True

    @property
    def ready(self) -> bool:
        return self.manifest_count > 0 and self.promoted_template_count > 0

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["ready"] = self.ready
        return payload


@dataclass(frozen=True)
class _SemanticTemplateCounts:
    promoted: int
    execution_safe: int
    same_venue_eligible: int


@dataclass(frozen=True)
class _SxbetCorpusScope:
    sport_ids: list[int] | None
    instrument_limit: int
    market_discovery_limit: int
    prefer_liquid_markets: bool
    liquidity_probe_limit: int
    min_two_sided_markets: int
    live_only: bool


def ensure_semantic_cache_ready(
    manifest: BettingArbitrageNodeManifest,
    *,
    logger: Logger | None = None,
) -> SemanticCacheStatus:
    cache_dir = manifest.semantic_rule_cache_dir
    if not cache_dir:
        return SemanticCacheStatus(
            path=None,
            source="disabled",
            manifest_count=0,
            promoted_template_count=0,
            execution_safe_template_count=0,
            same_venue_execution_eligible_template_count=0,
            compatibility_version=SEMANTIC_CACHE_COMPATIBILITY_VERSION,
            compatible=True,
        )

    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    status = semantic_cache_status(path, manifest=manifest)
    if status.ready and status.compatible:
        return status
    if status.ready and not status.compatible:
        _reset_semantic_cache_dir(path)

    _run_bootstrap(manifest=manifest, cache_dir=path, logger=logger)
    _write_semantic_cache_compatibility(path, manifest=manifest)
    status = semantic_cache_status(path, source="bootstrapped", manifest=manifest)
    if not status.ready:
        raise RuntimeError(
            "Semantic cache bootstrap completed without a usable cache "
            f"(manifests={status.manifest_count}, "
            f"promoted_templates={status.promoted_template_count})",
        )
    return status


def semantic_cache_status(
    cache_dir: str | Path,
    *,
    source: str = "existing",
    manifest: BettingArbitrageNodeManifest | None = None,
) -> SemanticCacheStatus:
    path = Path(cache_dir)
    store = RuleStore(FileRuleCache(path))
    compatibility = _read_semantic_cache_compatibility(path)
    compatibility_version = compatibility.get("version")
    compatibility_scope = compatibility.get("scope")
    expected_scope = _semantic_cache_scope_key(manifest) if manifest is not None else None
    compatible = compatibility_version == SEMANTIC_CACHE_COMPATIBILITY_VERSION and (
        expected_scope is None or compatibility_scope == expected_scope
    )

    template_counts = _semantic_template_counts(store)

    return SemanticCacheStatus(
        path=str(path),
        source=source,
        manifest_count=len(store.list_manifest_ids()),
        promoted_template_count=template_counts.promoted,
        execution_safe_template_count=template_counts.execution_safe,
        same_venue_execution_eligible_template_count=template_counts.same_venue_eligible,
        compatibility_version=compatibility_version,
        compatibility_scope=compatibility_scope,
        compatible=compatible,
    )


def _semantic_template_counts(store: RuleStore) -> _SemanticTemplateCounts:
    promoted_template_ids = store.list_promoted_template_ids()
    execution_safe = 0
    same_venue_eligible = 0
    for template_id in promoted_template_ids:
        template = store.load_promoted_template(template_id)
        if template is None:
            continue
        if template.safety_tier == SafetyTier.EXECUTION_SAFE.value:
            execution_safe += 1
        if template.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value:
            same_venue_eligible += 1
    return _SemanticTemplateCounts(
        promoted=len(promoted_template_ids),
        execution_safe=execution_safe,
        same_venue_eligible=same_venue_eligible,
    )


def _read_semantic_cache_compatibility(cache_dir: Path) -> dict[str, str | None]:
    marker = cache_dir / SEMANTIC_CACHE_COMPATIBILITY_FILE
    if not marker.exists():
        return {"version": None, "scope": None}
    raw = marker.read_text(encoding="utf-8").strip()
    if not raw:
        return {"version": None, "scope": None}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"version": raw, "scope": None}
    if not isinstance(payload, dict):
        return {"version": None, "scope": None}
    version = payload.get("version")
    scope = payload.get("scope")
    return {
        "version": str(version) if version is not None else None,
        "scope": str(scope) if scope is not None else None,
    }


def _write_semantic_cache_compatibility(
    cache_dir: Path,
    manifest: BettingArbitrageNodeManifest | None = None,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SEMANTIC_CACHE_COMPATIBILITY_VERSION,
        "scope": _semantic_cache_scope_key(manifest) if manifest is not None else None,
    }
    (cache_dir / SEMANTIC_CACHE_COMPATIBILITY_FILE).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _semantic_cache_scope_key(manifest: BettingArbitrageNodeManifest | None) -> str | None:
    if manifest is None:
        return None
    venues: list[dict[str, object]] = []
    enabled_venues = (item for item in manifest.venues if item.enabled)
    for venue in sorted(enabled_venues, key=lambda item: item.venue):
        venues.append(
            {
                "venue": venue.venue,
                "sport_keys": sorted(venue.sport_keys) if venue.sport_keys else "default",
                "sport_ids": sorted(venue.sport_ids) if venue.sport_ids else "all",
                "league_ids": sorted(venue.league_ids) if venue.league_ids else "all",
                "live_only": bool(venue.live_only),
                "instrument_load_limit": venue.instrument_load_limit,
                "market_discovery_limit": venue.market_discovery_limit,
                "prefer_liquid_markets": bool(venue.prefer_liquid_markets),
                "liquidity_probe_limit": venue.liquidity_probe_limit,
                "min_two_sided_markets": venue.min_two_sided_markets,
            },
        )
    payload = {"providers": venues}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _reset_semantic_cache_dir(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for child in cache_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _run_bootstrap(
    *,
    manifest: BettingArbitrageNodeManifest,
    cache_dir: Path,
    logger: Logger | None,
) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(
            _bootstrap_semantic_cache(
                manifest=manifest,
                cache_dir=cache_dir,
                logger=logger,
            ),
        )
        return

    error: list[BaseException] = []

    def _bootstrap_in_thread() -> None:
        try:
            asyncio.run(
                _bootstrap_semantic_cache(
                    manifest=manifest,
                    cache_dir=cache_dir,
                    logger=logger,
                ),
            )
        except BaseException as e:  # pragma: no cover - surfaced below
            error.append(e)

    thread = threading.Thread(target=_bootstrap_in_thread, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]


async def _bootstrap_semantic_cache(
    *,
    manifest: BettingArbitrageNodeManifest,
    cache_dir: Path,
    logger: Logger | None,
) -> None:
    store = RuleStore(FileRuleCache(cache_dir))
    ingestor = SnapshotIngestor(store)
    miner = RuleMiner(store)
    promotion_policy = RulePromotionPolicy()
    venues = [venue for venue in manifest.venues if venue.enabled]

    await _refresh_required_sxbet_corpus(venues=venues, ingestor=ingestor, logger=logger)
    await _refresh_cloudbet_corpus(
        manifest=manifest,
        venues=venues,
        ingestor=ingestor,
        logger=logger,
    )
    await _refresh_polymarket_corpus(
        venues=venues,
        ingestor=ingestor,
        logger=logger,
    )

    miner.mine_store(persist=True)
    templates = miner.mine_templates_from_store(persist=True, persist_event_candidates=False)
    for template in templates:
        portable_polymarket = _is_portable_polymarket_template(template)
        promotion_policy.promote_template(
            store,
            template,
            allowlisted=portable_polymarket,
            venue_agnostic=portable_polymarket,
        )


def _is_portable_polymarket_template(template: object) -> bool:
    """
    Return whether a Polymarket sports template is safe to apply cross-venue.

    This does not weaken settlement safety: only deterministic, full-time,
    no-void/no-partial/no-unknown complementary coverage across canonical sports
    families is allowed to become venue-agnostic.

    """
    if not isinstance(template, SemanticRuleTemplate):
        return False
    providers = {provider.upper() for provider in template.support.providers}
    resolution_values = {
        value
        for _, value in tuple(template.pattern_a.resolution_policy)
        + tuple(
            template.pattern_b.resolution_policy,
        )
    }
    return (
        "POLYMARKET" in providers
        and template.relationship_type == "COMPLEMENTARY_COVERAGE"
        and not template.has_void
        and not template.has_partial
        and not template.has_unknown
        and template.support.catalog_promotable
        and template.pattern_a.scope == "full_time"
        and template.pattern_b.scope == "full_time"
        and template.pattern_a.sport == template.pattern_b.sport
        and template.pattern_a.market_family in PORTABLE_POLYMARKET_MARKET_FAMILIES
        and template.pattern_b.market_family in PORTABLE_POLYMARKET_MARKET_FAMILIES
        and not (resolution_values & {"50_50", "unknown"})
    )


async def _refresh_required_sxbet_corpus(
    *,
    venues: Iterable[BettingVenueManifest],
    ingestor: SnapshotIngestor,
    logger: Logger | None,
) -> None:
    sxbet_venues = [venue for venue in venues if venue.venue == "SXBET"]
    if not sxbet_venues:
        return

    api_key = (os.getenv("SXBET_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("SXBET_API_KEY is required for semantic cache bootstrap")

    scope = _sxbet_corpus_scope(sxbet_venues)
    from_time, to_time = _sxbet_time_window(scope.live_only)
    client = SXBetHttpClient(api_key=api_key, logger=logger)
    await client.connect()
    try:
        await ingestor.refresh_sxbet(
            client,
            sport_ids=scope.sport_ids,
            from_time=from_time,
            to_time=to_time,
            instrument_limit=scope.instrument_limit,
            market_discovery_limit=scope.market_discovery_limit,
            prefer_liquid_markets=scope.prefer_liquid_markets,
            liquidity_probe_limit=scope.liquidity_probe_limit,
            min_two_sided_markets=scope.min_two_sided_markets,
        )
    finally:
        await client.disconnect()


def _sxbet_corpus_scope(sxbet_venues: Iterable[BettingVenueManifest]) -> _SxbetCorpusScope:
    sport_ids: set[int] = set()
    instrument_limit = 250
    market_discovery_limit = 250
    prefer_liquid_markets = False
    liquidity_probe_limit = 100
    min_two_sided_markets = 1
    live_only = False
    for venue in sxbet_venues:
        if venue.sport_ids:
            sport_ids.update(int(value) for value in venue.sport_ids)
        if venue.instrument_load_limit is not None:
            instrument_limit = max(instrument_limit, int(venue.instrument_load_limit))
        if venue.market_discovery_limit is not None:
            market_discovery_limit = max(market_discovery_limit, int(venue.market_discovery_limit))
        prefer_liquid_markets = prefer_liquid_markets or bool(venue.prefer_liquid_markets)
        liquidity_probe_limit = max(liquidity_probe_limit, int(venue.liquidity_probe_limit))
        min_two_sided_markets = max(min_two_sided_markets, int(venue.min_two_sided_markets))
        live_only = live_only or venue.live_only

    return _SxbetCorpusScope(
        sport_ids=sorted(sport_ids) or None,
        instrument_limit=instrument_limit,
        market_discovery_limit=market_discovery_limit,
        prefer_liquid_markets=prefer_liquid_markets,
        liquidity_probe_limit=liquidity_probe_limit,
        min_two_sided_markets=min_two_sided_markets,
        live_only=live_only,
    )


def _sxbet_time_window(live_only: bool) -> tuple[int, int]:
    now = int(time.time())
    from_time = now - 6 * 60 * 60 if live_only else now
    to_time = now + 6 * 60 * 60 if live_only else now + 7 * 24 * 60 * 60
    return from_time, to_time


async def _refresh_cloudbet_corpus(
    *,
    manifest: BettingArbitrageNodeManifest | None,
    venues: Iterable[BettingVenueManifest],
    ingestor: SnapshotIngestor,
    logger: Logger | None,
) -> None:
    active_venues = list(venues)
    if not active_venues:
        active_venues = [venue for venue in manifest.venues if venue.enabled] if manifest else []
    cloudbet_venues = [venue for venue in active_venues if venue.venue == "CLOUDBET"]
    api_key = (os.getenv("CLOUDBET_API_KEY") or "").strip()
    if not api_key:
        if cloudbet_venues:
            raise RuntimeError("CLOUDBET_API_KEY is required for Cloudbet semantic cache bootstrap")
        return

    sports = _cloudbet_sports_for_venues(cloudbet_venues)
    event_limit = _cloudbet_event_limit_for_venues(cloudbet_venues)
    window_seconds = _cloudbet_window_seconds_for_venues(cloudbet_venues)
    loop = asyncio.get_running_loop()
    cloudbet_logger = logger or Logger(clock=LiveClock(), bypass=True)
    client = CloudbetClient(loop=loop, logger=cloudbet_logger, api_key=api_key)
    await client.connect()
    try:
        now = int(time.time())
        await ingestor.refresh_cloudbet(
            client,
            sports=sports,
            from_timestamp=now,
            to_timestamp=now + min(24 * 60 * 60, window_seconds),
            limit=event_limit,
            adaptive_window=True,
            max_window_seconds=window_seconds,
            min_events_per_sport=1,
            include_recent_past_on_sparse=True,
            include_bets=False,
        )
    except Exception:
        if cloudbet_venues:
            raise
        if logger is not None:
            logger.warning(
                "Optional Cloudbet semantic enrichment failed; continuing with SXBET corpus only",
            )
    finally:
        await client.disconnect()


async def _refresh_optional_cloudbet_corpus(
    *,
    manifest: BettingArbitrageNodeManifest,
    ingestor: SnapshotIngestor,
    logger: Logger | None,
) -> None:
    await _refresh_cloudbet_corpus(
        manifest=manifest,
        venues=(),
        ingestor=ingestor,
        logger=logger,
    )


async def _refresh_polymarket_corpus(
    *,
    venues: Iterable[BettingVenueManifest],
    ingestor: SnapshotIngestor,
    logger: Logger | None,
) -> None:
    active_venues = list(venues)
    polymarket_venues = [venue for venue in active_venues if venue.venue == "POLYMARKET"]
    if not polymarket_venues:
        return

    sports = _polymarket_sports_for_venues(polymarket_venues)
    limit = _polymarket_limit_for_venues(polymarket_venues)
    if logger is not None:
        logger.info(
            f"Refreshing Polymarket sports semantic corpus sports={sports} limit={limit}",
        )
    await ingestor.refresh_polymarket(
        sports=sports,
        limit=limit,
    )


def _cloudbet_sports_for_venues(venues: Iterable[BettingVenueManifest]) -> list[str]:
    sports: set[str] = set()
    for venue in venues:
        if venue.sport_keys:
            sports.update(key.strip().lower() for key in venue.sport_keys if key.strip())
    return sorted(sports) or list(DEFAULT_CLOUDBET_SPORTS)


def _polymarket_sports_for_venues(venues: Iterable[BettingVenueManifest]) -> list[str]:
    sports: set[str] = set()
    for venue in venues:
        if venue.sport_keys:
            sports.update(key.strip().lower() for key in venue.sport_keys if key.strip())
    return sorted(sports) or list(DEFAULT_POLYMARKET_SPORTS)


def _cloudbet_event_limit_for_venues(venues: Iterable[BettingVenueManifest]) -> int:
    limit = 20
    for venue in venues:
        if venue.instrument_load_limit is not None:
            limit = max(limit, int(venue.instrument_load_limit))
        if venue.market_discovery_limit is not None:
            limit = max(limit, int(venue.market_discovery_limit))
    return limit


def _polymarket_limit_for_venues(venues: Iterable[BettingVenueManifest]) -> int:
    limit = 80
    for venue in venues:
        if venue.instrument_load_limit is not None:
            limit = max(limit, int(venue.instrument_load_limit))
        if venue.market_discovery_limit is not None:
            limit = max(limit, int(venue.market_discovery_limit))
    return limit


def _cloudbet_window_seconds_for_venues(venues: Iterable[BettingVenueManifest]) -> int:
    for venue in venues:
        if venue.live_only:
            return 6 * 60 * 60
    return 7 * 24 * 60 * 60


__all__ = [
    "SemanticCacheStatus",
    "ensure_semantic_cache_ready",
    "semantic_cache_status",
]
