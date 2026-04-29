from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict
from dataclasses import dataclass
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
from nautilus_trader.adapters.betting.semantics import SnapshotIngestor
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import (
    BettingArbitrageNodeManifest,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import BettingVenueManifest


DEFAULT_CLOUDBET_SPORTS = ("soccer", "tennis", "basketball", "american_football")
SEMANTIC_CACHE_COMPATIBILITY_VERSION = "semantic-rule-cache:20260429:sxbet-current-sports-v1"
SEMANTIC_CACHE_COMPATIBILITY_FILE = ".semantic-cache-version"


@dataclass(frozen=True)
class SemanticCacheStatus:
    path: str | None
    source: str
    manifest_count: int
    promoted_template_count: int
    execution_safe_template_count: int
    same_venue_execution_eligible_template_count: int
    compatibility_version: str | None = None
    compatible: bool = True

    @property
    def ready(self) -> bool:
        return self.manifest_count > 0 and self.promoted_template_count > 0

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["ready"] = self.ready
        return payload


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
    status = semantic_cache_status(path)
    if status.ready and status.compatible:
        return status
    if status.ready and not status.compatible:
        _reset_semantic_cache_dir(path)

    _run_bootstrap(manifest=manifest, cache_dir=path, logger=logger)
    _write_semantic_cache_compatibility(path)
    status = semantic_cache_status(path, source="bootstrapped")
    if not status.ready:
        raise RuntimeError(
            "Semantic cache bootstrap completed without a usable cache "
            f"(manifests={status.manifest_count}, promoted_templates={status.promoted_template_count})",
        )
    return status


def semantic_cache_status(
    cache_dir: str | Path,
    *,
    source: str = "existing",
) -> SemanticCacheStatus:
    path = Path(cache_dir)
    store = RuleStore(FileRuleCache(path))
    manifests = store.list_manifest_ids()
    promoted_template_ids = store.list_promoted_template_ids()
    compatibility_version = _read_semantic_cache_compatibility(path)
    compatible = compatibility_version == SEMANTIC_CACHE_COMPATIBILITY_VERSION

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

    return SemanticCacheStatus(
        path=str(path),
        source=source,
        manifest_count=len(manifests),
        promoted_template_count=len(promoted_template_ids),
        execution_safe_template_count=execution_safe,
        same_venue_execution_eligible_template_count=same_venue_eligible,
        compatibility_version=compatibility_version,
        compatible=compatible,
    )


def _read_semantic_cache_compatibility(cache_dir: Path) -> str | None:
    marker = cache_dir / SEMANTIC_CACHE_COMPATIBILITY_FILE
    if not marker.exists():
        return None
    return marker.read_text(encoding="utf-8").strip() or None


def _write_semantic_cache_compatibility(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / SEMANTIC_CACHE_COMPATIBILITY_FILE).write_text(
        f"{SEMANTIC_CACHE_COMPATIBILITY_VERSION}\n",
        encoding="utf-8",
    )


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

    await _refresh_required_sxbet_corpus(
        manifest=manifest,
        venues=venues,
        ingestor=ingestor,
        logger=logger,
    )
    await _refresh_optional_cloudbet_corpus(
        manifest=manifest,
        ingestor=ingestor,
        logger=logger,
    )

    miner.mine_store(persist=True)
    templates = miner.mine_templates_from_store(persist=True, persist_event_candidates=False)
    for template in templates:
        promotion_policy.promote_template(store, template)


async def _refresh_required_sxbet_corpus(
    *,
    manifest: BettingArbitrageNodeManifest,
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

    now = int(time.time())
    from_time = now - 6 * 60 * 60 if live_only else now
    to_time = now + 6 * 60 * 60 if live_only else now + 7 * 24 * 60 * 60
    client = SXBetHttpClient(api_key=api_key, logger=logger)
    await client.connect()
    try:
        await ingestor.refresh_sxbet(
            client,
            sport_ids=sorted(sport_ids) or None,
            from_time=from_time,
            to_time=to_time,
            instrument_limit=instrument_limit,
            market_discovery_limit=market_discovery_limit,
            prefer_liquid_markets=prefer_liquid_markets,
            liquidity_probe_limit=liquidity_probe_limit,
            min_two_sided_markets=min_two_sided_markets,
        )
    finally:
        await client.disconnect()


async def _refresh_optional_cloudbet_corpus(
    *,
    manifest: BettingArbitrageNodeManifest,
    ingestor: SnapshotIngestor,
    logger: Logger | None,
) -> None:
    api_key = (os.getenv("CLOUDBET_API_KEY") or "").strip()
    if not api_key:
        return

    loop = asyncio.get_running_loop()
    cloudbet_logger = logger or Logger(clock=LiveClock(), bypass=True)
    client = CloudbetClient(loop=loop, logger=cloudbet_logger, api_key=api_key)
    await client.connect()
    try:
        now = int(time.time())
        await ingestor.refresh_cloudbet(
            client,
            sports=list(DEFAULT_CLOUDBET_SPORTS),
            from_timestamp=now,
            to_timestamp=now + 24 * 60 * 60,
            limit=20,
            adaptive_window=True,
            max_window_seconds=7 * 24 * 60 * 60,
            min_events_per_sport=1,
            include_recent_past_on_sparse=True,
            include_bets=False,
        )
    except Exception:
        if logger is not None:
            logger.warning(
                "Optional Cloudbet semantic enrichment failed; continuing with SXBET corpus only",
            )
    finally:
        await client.disconnect()


__all__ = [
    "SemanticCacheStatus",
    "ensure_semantic_cache_ready",
    "semantic_cache_status",
]
