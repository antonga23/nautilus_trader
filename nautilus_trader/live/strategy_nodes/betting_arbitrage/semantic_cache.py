from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
import hashlib
import json
import os
import shlex
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
DEFAULT_SXBET_SPORTS = SEMANTIC_TARGET_SPORTS
DEFAULT_POLYMARKET_SPORTS = SEMANTIC_TARGET_SPORTS
SEMANTIC_CACHE_COMPATIBILITY_VERSION = "semantic-rule-cache:20260510:polymarket-runtime-coverage-v1"
SEMANTIC_CACHE_COMPATIBILITY_FILE = ".semantic-cache-version"
SEMANTIC_CACHE_SUMMARY_FILE = ".semantic-cache-summary.json"
SEMANTIC_CACHE_BOOTSTRAP_TIMINGS_FILE = ".semantic-cache-bootstrap-timings.json"
SEMANTIC_CACHE_SEED_DIR_ENV = "SEMANTIC_RULE_CACHE_SEED_DIR"
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
_DEFAULT_LOCAL_ENV_FILES = (
    Path(__file__).resolve().parents[4] / ".env.cloud-workspace.local",
    Path(__file__).resolve().parents[4] / ".env.local",
    Path(__file__).resolve().parents[4] / ".env",
)


@dataclass(frozen=True)
class SemanticCacheStatus:
    path: str | None
    source: str
    manifest_count: int
    promoted_template_count: int
    execution_safe_template_count: int
    same_venue_execution_eligible_template_count: int
    promoted_safety_tier_counts: dict[str, int] = field(default_factory=dict)
    promoted_market_family_counts: dict[str, int] = field(default_factory=dict)
    execution_safe_market_family_counts: dict[str, int] = field(default_factory=dict)
    same_venue_eligible_market_family_counts: dict[str, int] = field(default_factory=dict)
    strict_execution_blocker_counts: dict[str, int] = field(default_factory=dict)
    coverage_proof_count: int = 0
    coverage_hyperedge_count: int = 0
    compatibility_version: str | None = None
    compatibility_scope: str | None = None
    compatible: bool = True
    summary_reused: bool = False
    bootstrap_phase_timings_secs: dict[str, float] = field(default_factory=dict)
    provider_corpus_coverage: dict[str, object] = field(default_factory=dict)

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
class _SemanticCoverageCounts:
    proofs: int
    hyperedges: int


@dataclass(frozen=True)
class _SxbetCorpusScope:
    sport_keys: list[str] | None
    sport_ids: list[int] | None
    instrument_limit: int
    market_discovery_limit: int
    prefer_liquid_markets: bool
    liquidity_probe_limit: int
    min_two_sided_markets: int
    live_only: bool


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
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


def ensure_semantic_cache_ready(
    manifest: BettingArbitrageNodeManifest,
    *,
    logger: Logger | None = None,
) -> SemanticCacheStatus:
    _load_local_workspace_env()
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

    mode = manifest.semantic_rule_cache_mode
    default_root = (manifest.semantic_rule_cache_default_root or "").strip()

    if mode == "reuse":
        return _ensure_reuse_cache(path, manifest=manifest, logger=logger)

    if mode == "default" and default_root:
        return _ensure_default_mine(
            path,
            default_root,
            manifest=manifest,
            logger=logger,
        )

    if mode == "default" and logger is not None:
        logger.warning(
            "semantic_rule_cache_mode='default' without "
            "semantic_rule_cache_default_root; mining fresh instead",
        )

    # "fresh" (default): never reuse or seed — always re-mine from the live
    # venue corpus so a deploy reflects the current market, not a stale cache.
    status = _bootstrap_fresh(path, manifest=manifest, logger=logger)
    if default_root:
        _register_default_mine(path, default_root, manifest=manifest, logger=logger)
    return status


def _ensure_reuse_cache(
    cache_dir: Path,
    *,
    manifest: BettingArbitrageNodeManifest,
    logger: Logger | None,
) -> SemanticCacheStatus:
    status = semantic_cache_status(cache_dir, manifest=manifest)
    if status.ready and status.compatible:
        return status
    if status.ready and not status.compatible:
        _reset_semantic_cache_dir(cache_dir)

    seeded_status = _try_seed_semantic_cache(cache_dir, manifest=manifest, logger=logger)
    if seeded_status is not None:
        return seeded_status

    _run_bootstrap(manifest=manifest, cache_dir=cache_dir, logger=logger)
    _write_semantic_cache_compatibility(cache_dir, manifest=manifest)
    status = semantic_cache_status(cache_dir, source="bootstrapped", manifest=manifest)
    if not status.ready:
        raise RuntimeError(
            "Semantic cache bootstrap completed without a usable cache "
            f"(manifests={status.manifest_count}, "
            f"promoted_templates={status.promoted_template_count})",
        )
    return status


def _ensure_default_mine(
    cache_dir: Path,
    default_root: str,
    *,
    manifest: BettingArbitrageNodeManifest,
    logger: Logger | None,
) -> SemanticCacheStatus:
    reused = _try_use_default_mine(
        cache_dir,
        default_root,
        manifest=manifest,
        max_age_hours=manifest.semantic_rule_cache_max_age_hours,
        logger=logger,
    )
    if reused is not None:
        return reused

    status = _bootstrap_fresh(cache_dir, manifest=manifest, logger=logger)
    _register_default_mine(cache_dir, default_root, manifest=manifest, logger=logger)
    return status


def _bootstrap_fresh(
    cache_dir: Path,
    *,
    manifest: BettingArbitrageNodeManifest,
    logger: Logger | None,
) -> SemanticCacheStatus:
    _reset_semantic_cache_dir(cache_dir)
    _run_bootstrap(manifest=manifest, cache_dir=cache_dir, logger=logger)
    _write_semantic_cache_compatibility(cache_dir, manifest=manifest)
    status = semantic_cache_status(cache_dir, source="bootstrapped", manifest=manifest)
    if not status.ready:
        raise RuntimeError(
            "Semantic cache bootstrap completed without a usable cache "
            f"(manifests={status.manifest_count}, "
            f"promoted_templates={status.promoted_template_count})",
        )
    return status


def _try_seed_semantic_cache(
    cache_dir: Path,
    *,
    manifest: BettingArbitrageNodeManifest,
    logger: Logger | None,
) -> SemanticCacheStatus | None:
    manifest_seed = str(getattr(manifest, "semantic_rule_cache_seed_dir", "") or "").strip()
    seed_value = manifest_seed or (os.getenv(SEMANTIC_CACHE_SEED_DIR_ENV) or "").strip()
    if not seed_value:
        return None
    seed_dir = Path(seed_value).expanduser()
    if not seed_dir.is_dir():
        if logger is not None:
            logger.warning(f"Semantic cache seed directory does not exist: {seed_dir}")
        return None
    if seed_dir.resolve() == cache_dir.resolve():
        return None

    seed_status = semantic_cache_status(seed_dir, manifest=manifest)
    scope_mismatch_accepted = _seed_compatibility_gate(
        seed_status,
        seed_dir,
        manifest=manifest,
        logger=logger,
    )
    if scope_mismatch_accepted is None:
        return None

    _reset_semantic_cache_dir(cache_dir)
    shutil.copytree(seed_dir, cache_dir, dirs_exist_ok=True)
    if scope_mismatch_accepted:
        _write_semantic_cache_compatibility(cache_dir, manifest=manifest)
    status = semantic_cache_status(cache_dir, source="seeded", manifest=manifest)
    if status.ready and status.compatible:
        if logger is not None:
            logger.info(
                "Seeded semantic cache from compatible source: "
                f"source={seed_dir} target={cache_dir}",
            )
        return status
    return None


def _seed_compatibility_gate(
    seed_status: SemanticCacheStatus,
    seed_dir: Path,
    *,
    manifest: BettingArbitrageNodeManifest,
    logger: Logger | None,
) -> bool | None:
    """
    Return ``False`` for a fully compatible seed, ``True`` for a scope-mismatched seed
    accepted via the manifest opt-in (compatibility version must still match), or
    ``None`` when the seed is rejected.
    """
    if seed_status.ready and seed_status.compatible:
        return False
    if _accept_scope_mismatched_seed(seed_status, manifest=manifest):
        if logger is not None:
            logger.warning(
                "Semantic seed scope mismatch accepted: "
                f"seed_scope={seed_status.compatibility_scope} "
                f"node_scope={_semantic_cache_scope_key(manifest)} path={seed_dir}",
            )
        return True
    if logger is not None:
        logger.warning(
            "Ignoring incompatible semantic cache seed: "
            f"path={seed_dir} ready={seed_status.ready} "
            f"compatible={seed_status.compatible}",
        )
    return None


def _accept_scope_mismatched_seed(
    seed_status: SemanticCacheStatus,
    *,
    manifest: BettingArbitrageNodeManifest,
) -> bool:
    if not bool(getattr(manifest, "semantic_rule_cache_seed_allow_scope_mismatch", False)):
        return False
    return (
        seed_status.ready
        and seed_status.compatibility_version == SEMANTIC_CACHE_COMPATIBILITY_VERSION
    )


def _default_mine_dir(default_root: str, manifest: BettingArbitrageNodeManifest) -> Path:
    scope_key = _semantic_cache_scope_key(manifest) or "unscoped"
    return Path(default_root).expanduser() / scope_key


def _register_default_mine(
    cache_dir: Path,
    default_root: str,
    *,
    manifest: BettingArbitrageNodeManifest,
    logger: Logger | None,
) -> None:
    registry_dir = _default_mine_dir(default_root, manifest)
    if registry_dir.resolve() == cache_dir.resolve():
        return
    _reset_semantic_cache_dir(registry_dir)
    shutil.copytree(cache_dir, registry_dir, dirs_exist_ok=True)
    if logger is not None:
        logger.info(
            "Registered default semantic mine for config signature: "
            f"source={cache_dir} target={registry_dir}",
        )


def _default_mine_age_secs(registry_dir: Path) -> float | None:
    summary = _read_semantic_cache_summary(registry_dir)
    generated_at = summary.get("generated_at_unix_secs") if summary is not None else None
    if isinstance(generated_at, int | float):
        return max(0.0, time.time() - float(generated_at))
    marker = registry_dir / SEMANTIC_CACHE_COMPATIBILITY_FILE
    if marker.exists():
        return max(0.0, time.time() - os.path.getmtime(marker))
    return None


def _try_use_default_mine(
    cache_dir: Path,
    default_root: str,
    *,
    manifest: BettingArbitrageNodeManifest,
    max_age_hours: float | None,
    logger: Logger | None,
) -> SemanticCacheStatus | None:
    registry_dir = _default_mine_dir(default_root, manifest)
    if not registry_dir.is_dir():
        return None
    if registry_dir.resolve() == cache_dir.resolve():
        return None

    registry_status = semantic_cache_status(registry_dir, manifest=manifest)
    if not registry_status.ready or not registry_status.compatible:
        if logger is not None:
            logger.warning(
                "Ignoring unusable default semantic mine: "
                f"path={registry_dir} ready={registry_status.ready} "
                f"compatible={registry_status.compatible}",
            )
        return None

    if max_age_hours is not None:
        age_secs = _default_mine_age_secs(registry_dir)
        if age_secs is not None and age_secs > max_age_hours * 3600.0:
            if logger is not None:
                logger.info(
                    "Ignoring stale default semantic mine: "
                    f"path={registry_dir} age_secs={age_secs:.0f} "
                    f"max_age_hours={max_age_hours}",
                )
            return None

    _reset_semantic_cache_dir(cache_dir)
    shutil.copytree(registry_dir, cache_dir, dirs_exist_ok=True)
    status = semantic_cache_status(cache_dir, source="default-mine", manifest=manifest)
    if status.ready and status.compatible:
        if logger is not None:
            logger.info(
                "Reused default semantic mine for config signature: "
                f"source={registry_dir} target={cache_dir}",
            )
        return status
    return None


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

    manifest_ids = store.list_manifest_ids()
    promoted_template_ids = store.list_promoted_template_ids()
    proof_ids = store.list_coverage_proof_ids() if hasattr(store, "list_coverage_proof_ids") else []
    hyperedge_ids = (
        store.list_coverage_hyperedge_ids() if hasattr(store, "list_coverage_hyperedge_ids") else []
    )
    summary_signatures = {
        "manifest_index_signature": _semantic_cache_index_signature(manifest_ids),
        "promoted_template_index_signature": _semantic_cache_index_signature(promoted_template_ids),
        "coverage_proof_index_signature": _semantic_cache_index_signature(proof_ids),
        "coverage_hyperedge_index_signature": _semantic_cache_index_signature(hyperedge_ids),
    }
    summary_counts = _semantic_summary_counts(
        _read_semantic_cache_summary(path),
        compatibility_version=compatibility_version,
        compatibility_scope=compatibility_scope,
        manifest_count=len(manifest_ids),
        promoted_template_count=len(promoted_template_ids),
        coverage_proof_count=len(proof_ids),
        coverage_hyperedge_count=len(hyperedge_ids),
        signatures=summary_signatures,
    )
    summary_reused = summary_counts is not None
    if summary_counts is None:
        template_counts, strictness = _semantic_template_analysis(
            store,
            promoted_template_ids=promoted_template_ids,
        )
        _write_semantic_cache_summary(
            path,
            compatibility_version=compatibility_version,
            compatibility_scope=compatibility_scope,
            manifest_count=len(manifest_ids),
            template_counts=template_counts,
            strictness=strictness,
            coverage_counts=_SemanticCoverageCounts(
                proofs=len(proof_ids),
                hyperedges=len(hyperedge_ids),
            ),
            signatures=summary_signatures,
        )
    else:
        template_counts, strictness = summary_counts
    bootstrap_phase_timings = _read_semantic_cache_bootstrap_timings(path)
    provider_corpus_coverage = _semantic_provider_corpus_coverage(store)

    return SemanticCacheStatus(
        path=str(path),
        source=source,
        manifest_count=len(manifest_ids),
        promoted_template_count=template_counts.promoted,
        execution_safe_template_count=template_counts.execution_safe,
        same_venue_execution_eligible_template_count=template_counts.same_venue_eligible,
        promoted_safety_tier_counts=strictness["promoted_safety_tier_counts"],
        promoted_market_family_counts=strictness["promoted_market_family_counts"],
        execution_safe_market_family_counts=strictness["execution_safe_market_family_counts"],
        same_venue_eligible_market_family_counts=strictness[
            "same_venue_eligible_market_family_counts"
        ],
        strict_execution_blocker_counts=strictness["strict_execution_blocker_counts"],
        coverage_proof_count=len(proof_ids),
        coverage_hyperedge_count=len(hyperedge_ids),
        compatibility_version=compatibility_version,
        compatibility_scope=compatibility_scope,
        compatible=compatible,
        summary_reused=summary_reused,
        bootstrap_phase_timings_secs=bootstrap_phase_timings,
        provider_corpus_coverage=provider_corpus_coverage,
    )


def _semantic_template_analysis(
    store: RuleStore,
    *,
    promoted_template_ids: list[str] | None = None,
) -> tuple[_SemanticTemplateCounts, dict[str, dict[str, int]]]:
    promoted_template_ids = promoted_template_ids or store.list_promoted_template_ids()
    execution_safe = 0
    same_venue_eligible = 0
    tier_counts: dict[str, int] = {}
    market_family_counts: dict[str, int] = {}
    execution_safe_family_counts: dict[str, int] = {}
    same_venue_family_counts: dict[str, int] = {}
    strict_blockers: dict[str, int] = {}
    for template_id in promoted_template_ids:
        template = store.load_promoted_template(template_id)
        if template is None:
            continue
        family_pair = _semantic_template_family_pair(template)
        market_family_counts[family_pair] = market_family_counts.get(family_pair, 0) + 1
        if template.safety_tier == SafetyTier.EXECUTION_SAFE.value:
            execution_safe += 1
            execution_safe_family_counts[family_pair] = (
                execution_safe_family_counts.get(family_pair, 0) + 1
            )
        if template.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value:
            same_venue_eligible += 1
            same_venue_family_counts[family_pair] = (
                same_venue_family_counts.get(
                    family_pair,
                    0,
                )
                + 1
            )
        tier_counts[template.safety_tier] = tier_counts.get(template.safety_tier, 0) + 1
        if not template.execution_safe:
            for blocker in _strict_execution_blockers(template):
                strict_blockers[blocker] = strict_blockers.get(blocker, 0) + 1
    return (
        _SemanticTemplateCounts(
            promoted=len(promoted_template_ids),
            execution_safe=execution_safe,
            same_venue_eligible=same_venue_eligible,
        ),
        {
            "promoted_safety_tier_counts": dict(sorted(tier_counts.items())),
            "promoted_market_family_counts": dict(sorted(market_family_counts.items())),
            "execution_safe_market_family_counts": dict(
                sorted(execution_safe_family_counts.items()),
            ),
            "same_venue_eligible_market_family_counts": dict(
                sorted(same_venue_family_counts.items()),
            ),
            "strict_execution_blocker_counts": dict(sorted(strict_blockers.items())),
        },
    )


def _semantic_template_family_pair(template: SemanticRuleTemplate) -> str:
    pattern_a = getattr(template, "pattern_a", None)
    pattern_b = getattr(template, "pattern_b", None)
    family_a = str(getattr(pattern_a, "market_family", "") or "UNKNOWN")
    family_b = str(getattr(pattern_b, "market_family", "") or "UNKNOWN")
    return " + ".join(sorted((family_a, family_b)))


def _semantic_coverage_counts(store: RuleStore) -> _SemanticCoverageCounts:
    proof_ids = store.list_coverage_proof_ids() if hasattr(store, "list_coverage_proof_ids") else []
    hyperedge_ids = (
        store.list_coverage_hyperedge_ids() if hasattr(store, "list_coverage_hyperedge_ids") else []
    )
    return _SemanticCoverageCounts(proofs=len(proof_ids), hyperedges=len(hyperedge_ids))


def _semantic_provider_corpus_coverage(store: RuleStore) -> dict[str, object]:
    list_snapshot_ids = getattr(store, "list_snapshot_ids", None)
    load_snapshot = getattr(store, "load_snapshot", None)
    if not callable(list_snapshot_ids) or not callable(load_snapshot):
        return {}

    latest_by_provider: dict[str, tuple[str, dict[str, object]]] = {}
    for snapshot_id in list_snapshot_ids():
        snapshot = load_snapshot(snapshot_id)
        if snapshot is None or not str(snapshot.endpoint).startswith("/semantic/coverage/"):
            continue
        try:
            payload = json.loads(snapshot.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        provider = str(payload.get("provider") or snapshot.provider or "").upper()
        if not provider:
            continue
        fetched_at = str(snapshot.fetched_at or "")
        previous = latest_by_provider.get(provider)
        if previous is None or fetched_at >= previous[0]:
            latest_by_provider[provider] = (fetched_at, payload)

    return {
        provider: _semantic_provider_coverage_summary(payload, fetched_at=fetched_at)
        for provider, (fetched_at, payload) in sorted(latest_by_provider.items())
    }


def _semantic_provider_coverage_summary(
    payload: dict[str, object],
    *,
    fetched_at: str,
) -> dict[str, object]:
    sports_payload = payload.get("sports")
    sports = sports_payload if isinstance(sports_payload, dict) else {}
    requested_sports = _semantic_coverage_str_list(payload.get("requested_sports"))
    resolved_sports = _semantic_coverage_str_list(payload.get("resolved_sports"))
    unresolved_requested_sports = _semantic_coverage_str_list(
        payload.get("unresolved_requested_sports"),
    )
    sport_summaries: dict[str, object] = {}
    blocker_counts: dict[str, int] = {}
    sparse_sports: list[str] = []
    zero_selection_sports: list[str] = []
    total_selection_count = 0
    total_event_count = 0
    total_market_count = 0

    for sport, raw_report in sorted(sports.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_report, dict):
            continue
        sport_key = str(sport)
        selection_count = _semantic_coverage_int(raw_report.get("selection_count"))
        event_count = _semantic_coverage_int(raw_report.get("event_count"))
        market_count = _semantic_coverage_int(raw_report.get("market_count"))
        attempts = raw_report.get("attempts")
        attempt_count = len(attempts) if isinstance(attempts, list) else 0
        blocker = raw_report.get("blocker")
        blocker_name = str(blocker) if blocker else ""
        sparse = bool(raw_report.get("sparse", False))

        total_selection_count += selection_count
        total_event_count += event_count
        total_market_count += market_count
        if blocker_name:
            blocker_counts[blocker_name] = blocker_counts.get(blocker_name, 0) + 1
        if sparse:
            sparse_sports.append(sport_key)
        if selection_count <= 0:
            zero_selection_sports.append(sport_key)

        sport_summaries[sport_key] = {
            "selection_count": selection_count,
            "event_count": event_count,
            "market_count": market_count,
            "attempt_count": attempt_count,
            "blocker": blocker_name or None,
            "sparse": sparse,
        }

    return {
        "fetched_at": fetched_at,
        "sport_count": len(sport_summaries),
        "sports_with_selections": sum(
            1
            for report in sport_summaries.values()
            if isinstance(report, dict) and int(report.get("selection_count", 0)) > 0
        ),
        "total_selection_count": total_selection_count,
        "total_event_count": total_event_count,
        "total_market_count": total_market_count,
        "coverage_mode": str(payload.get("coverage_mode") or ""),
        "live_only": bool(payload.get("live_only", False)),
        "prefer_liquid_markets": bool(payload.get("prefer_liquid_markets", False)),
        "requested_sports": requested_sports,
        "resolved_sports": resolved_sports,
        "unresolved_requested_sports": unresolved_requested_sports,
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "sparse_sports": sparse_sports,
        "zero_selection_sports": zero_selection_sports,
        "sports": sport_summaries,
    }


def _semantic_coverage_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    return 0


def _semantic_coverage_str_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return sorted({str(item) for item in value if str(item).strip()})


def _semantic_cache_index_signature(ids: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for item in sorted(str(item) for item in ids):
        digest.update(item.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _read_semantic_cache_summary(cache_dir: Path) -> dict[str, object] | None:
    summary_path = cache_dir / SEMANTIC_CACHE_SUMMARY_FILE
    if not summary_path.exists():
        return None
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_semantic_cache_bootstrap_timings(cache_dir: Path) -> dict[str, float]:
    timings_path = cache_dir / SEMANTIC_CACHE_BOOTSTRAP_TIMINGS_FILE
    if not timings_path.exists():
        return {}
    try:
        payload = json.loads(timings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    timings = payload.get("phase_timings_secs")
    if not isinstance(timings, dict):
        return {}
    normalized: dict[str, float] = {}
    for key, value in timings.items():
        try:
            normalized[str(key)] = round(max(0.0, float(value)), 6)
        except (TypeError, ValueError):
            continue
    return dict(sorted(normalized.items()))


def _write_semantic_cache_bootstrap_timings(
    cache_dir: Path,
    *,
    phase_timings_secs: dict[str, float],
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase_timings_secs": {
            key: round(max(0.0, float(value)), 6)
            for key, value in sorted(phase_timings_secs.items())
        },
        "generated_at_unix_secs": time.time(),
    }
    (cache_dir / SEMANTIC_CACHE_BOOTSTRAP_TIMINGS_FILE).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _semantic_summary_counts(
    summary: dict[str, object] | None,
    *,
    compatibility_version: str | None,
    compatibility_scope: str | None,
    manifest_count: int,
    promoted_template_count: int,
    coverage_proof_count: int,
    coverage_hyperedge_count: int,
    signatures: dict[str, str],
) -> tuple[_SemanticTemplateCounts, dict[str, dict[str, int]]] | None:
    if summary is None:
        return None
    if summary.get("compatibility_version") != compatibility_version:
        return None
    if summary.get("compatibility_scope") != compatibility_scope:
        return None
    expected_counts = {
        "manifest_count": manifest_count,
        "promoted_template_count": promoted_template_count,
        "coverage_proof_count": coverage_proof_count,
        "coverage_hyperedge_count": coverage_hyperedge_count,
    }
    for count_key, expected_count in expected_counts.items():
        if _semantic_summary_int(summary.get(count_key), default=-1) != expected_count:
            return None
    for signature_key, expected_signature in signatures.items():
        if summary.get(signature_key) != expected_signature:
            return None

    safety_tier_counts = summary.get("promoted_safety_tier_counts")
    market_family_counts = summary.get("promoted_market_family_counts")
    execution_safe_family_counts = summary.get("execution_safe_market_family_counts")
    same_venue_family_counts = summary.get("same_venue_eligible_market_family_counts")
    strict_blocker_counts = summary.get("strict_execution_blocker_counts")
    if (
        not isinstance(safety_tier_counts, dict)
        or not isinstance(market_family_counts, dict)
        or not isinstance(execution_safe_family_counts, dict)
        or not isinstance(same_venue_family_counts, dict)
        or not isinstance(strict_blocker_counts, dict)
    ):
        return None

    return (
        _SemanticTemplateCounts(
            promoted=promoted_template_count,
            execution_safe=max(
                0,
                _semantic_summary_int(summary.get("execution_safe_template_count")),
            ),
            same_venue_eligible=max(
                0,
                _semantic_summary_int(
                    summary.get("same_venue_execution_eligible_template_count"),
                ),
            ),
        ),
        {
            "promoted_safety_tier_counts": {
                str(key): int(value) for key, value in safety_tier_counts.items()
            },
            "promoted_market_family_counts": {
                str(key): int(value) for key, value in market_family_counts.items()
            },
            "execution_safe_market_family_counts": {
                str(key): int(value) for key, value in execution_safe_family_counts.items()
            },
            "same_venue_eligible_market_family_counts": {
                str(key): int(value) for key, value in same_venue_family_counts.items()
            },
            "strict_execution_blocker_counts": {
                str(key): int(value) for key, value in strict_blocker_counts.items()
            },
        },
    )


def _semantic_summary_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _write_semantic_cache_summary(
    cache_dir: Path,
    *,
    compatibility_version: str | None,
    compatibility_scope: str | None,
    manifest_count: int,
    template_counts: _SemanticTemplateCounts,
    strictness: dict[str, dict[str, int]],
    coverage_counts: _SemanticCoverageCounts,
    signatures: dict[str, str],
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "compatibility_version": compatibility_version,
        "compatibility_scope": compatibility_scope,
        "manifest_count": manifest_count,
        "promoted_template_count": template_counts.promoted,
        "execution_safe_template_count": template_counts.execution_safe,
        "same_venue_execution_eligible_template_count": template_counts.same_venue_eligible,
        "coverage_proof_count": coverage_counts.proofs,
        "coverage_hyperedge_count": coverage_counts.hyperedges,
        "promoted_safety_tier_counts": strictness["promoted_safety_tier_counts"],
        "promoted_market_family_counts": strictness["promoted_market_family_counts"],
        "execution_safe_market_family_counts": strictness["execution_safe_market_family_counts"],
        "same_venue_eligible_market_family_counts": strictness[
            "same_venue_eligible_market_family_counts"
        ],
        "strict_execution_blocker_counts": strictness["strict_execution_blocker_counts"],
        **signatures,
        "generated_at_unix_secs": time.time(),
    }
    (cache_dir / SEMANTIC_CACHE_SUMMARY_FILE).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _strict_execution_blockers(template: object) -> tuple[str, ...]:
    blockers: list[str] = []
    relationship_type = str(getattr(template, "relationship_type", "") or "")
    same_venue_execution_eligible = bool(
        getattr(template, "same_venue_execution_eligible", False),
    )
    has_void = bool(getattr(template, "has_void", False))
    has_partial = bool(getattr(template, "has_partial", False))
    has_unknown = bool(getattr(template, "has_unknown", False))
    execution_safe = bool(getattr(template, "execution_safe", False))
    support = getattr(template, "support", None)
    eligibility_reasons = tuple(getattr(template, "eligibility_reasons", ()) or ())

    if same_venue_execution_eligible:
        blockers.append("same_venue_risk_engine_elevation_required")
    if relationship_type != "COMPLEMENTARY_COVERAGE":
        blockers.append("non_complementary_relationship")
    if has_void:
        blockers.append("void_states_present")
    if has_partial:
        blockers.append("partial_settlement_present")
    if has_unknown:
        blockers.append("unknown_settlement_present")
    blockers.extend(_catalog_support_blockers(support))
    if not blockers and not execution_safe:
        blockers.extend(str(reason) for reason in eligibility_reasons)
    return tuple(sorted(set(blockers)))


def _catalog_support_blockers(support: object | None) -> tuple[str, ...]:
    blockers: list[str] = []
    if support is None:
        return tuple(blockers)
    if bool(getattr(support, "catalog_promotable", False)):
        return tuple(blockers)
    if not bool(getattr(support, "deterministic", True)):
        blockers.append("nondeterministic_support")
    if int(getattr(support, "unknown_settlement_count", 0)) > 0:
        blockers.append("support_unknown_settlement_present")
    if int(getattr(support, "observed_count", 0)) < 10:
        blockers.append("observed_count_below_10")
    if int(getattr(support, "event_count", 0)) < 3:
        blockers.append("event_count_below_3")
    if float(getattr(support, "mismatch_rate", 1.0)) > 0.01:
        blockers.append("mismatch_rate_above_0_01")
    if float(getattr(support, "confidence", 0.0)) < 0.99:
        blockers.append("confidence_below_0_99")
    if not blockers:
        blockers.append("catalog_support_below_gate")
    return tuple(sorted(set(blockers)))


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
                "sport_keys": _semantic_cache_scope_sport_keys(venue),
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


def _semantic_cache_scope_sport_keys(venue: BettingVenueManifest) -> list[str] | str:
    if venue.sport_keys:
        return sorted(venue.sport_keys)
    if venue.sport_ids:
        return "sport_ids"
    venue_name = venue.venue.upper()
    if venue_name == "CLOUDBET":
        return list(DEFAULT_CLOUDBET_SPORTS)
    if venue_name == "SXBET":
        return list(DEFAULT_SXBET_SPORTS)
    if venue_name == "POLYMARKET":
        return list(DEFAULT_POLYMARKET_SPORTS)
    return "default"


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
    phase_timings_secs: dict[str, float] = {}

    defer_index_writes = getattr(store, "defer_index_writes", None)
    index_context = defer_index_writes() if callable(defer_index_writes) else nullcontext()
    bulk_writes = getattr(store, "bulk_writes", None)
    bulk_context = bulk_writes() if callable(bulk_writes) else nullcontext()
    total_started = time.perf_counter()
    with bulk_context, index_context:
        await _timed_async_phase(
            "refresh_sxbet_corpus",
            phase_timings_secs,
            _refresh_required_sxbet_corpus(venues=venues, ingestor=ingestor, logger=logger),
        )
        await _timed_async_phase(
            "refresh_cloudbet_corpus",
            phase_timings_secs,
            _refresh_cloudbet_corpus(
                manifest=manifest,
                venues=venues,
                ingestor=ingestor,
                logger=logger,
            ),
        )
        await _timed_async_phase(
            "refresh_polymarket_corpus",
            phase_timings_secs,
            _refresh_polymarket_corpus(
                venues=venues,
                ingestor=ingestor,
                logger=logger,
            ),
        )

        _timed_sync_phase(
            "mine_event_candidates",
            phase_timings_secs,
            miner.mine_store,
            persist=True,
        )
        templates = _timed_sync_phase(
            "generalize_templates",
            phase_timings_secs,
            miner.mine_templates_from_store,
            persist=True,
            persist_event_candidates=False,
        )
        _timed_sync_phase(
            "mine_coverage",
            phase_timings_secs,
            miner.mine_coverage_from_store,
            persist=True,
        )

        def _promote_templates() -> None:
            for template in templates:
                portable_polymarket = _is_portable_polymarket_template(template)
                promotion_policy.promote_template(
                    store,
                    template,
                    allowlisted=portable_polymarket,
                    venue_agnostic=portable_polymarket,
                )

        _timed_sync_phase("promote_templates", phase_timings_secs, _promote_templates)
    phase_timings_secs["total"] = time.perf_counter() - total_started
    _write_semantic_cache_bootstrap_timings(
        cache_dir,
        phase_timings_secs=phase_timings_secs,
    )


async def _timed_async_phase(
    name: str,
    phase_timings_secs: dict[str, float],
    awaitable,
):
    started = time.perf_counter()
    try:
        return await awaitable
    finally:
        phase_timings_secs[name] = time.perf_counter() - started


def _timed_sync_phase(
    name: str,
    phase_timings_secs: dict[str, float],
    func,
    *args,
    **kwargs,
):
    started = time.perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        phase_timings_secs[name] = time.perf_counter() - started


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
            sports=scope.sport_keys,
            sport_ids=scope.sport_ids,
            from_time=from_time,
            to_time=to_time,
            instrument_limit=scope.instrument_limit,
            market_discovery_limit=scope.market_discovery_limit,
            prefer_liquid_markets=scope.prefer_liquid_markets,
            liquidity_probe_limit=scope.liquidity_probe_limit,
            min_two_sided_markets=scope.min_two_sided_markets,
            live_only=scope.live_only,
        )
    finally:
        await client.disconnect()


def _sxbet_corpus_scope(sxbet_venues: Iterable[BettingVenueManifest]) -> _SxbetCorpusScope:
    sport_keys: set[str] = set()
    sport_ids: set[int] = set()
    instrument_limit = 250
    market_discovery_limit = 250
    prefer_liquid_markets = False
    liquidity_probe_limit = 100
    min_two_sided_markets = 1
    live_only = False
    for venue in sxbet_venues:
        if venue.sport_keys:
            sport_keys.update(key.strip().lower() for key in venue.sport_keys if key.strip())
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

    resolved_sport_keys = (
        sorted(sport_keys) if sport_keys else (None if sport_ids else list(DEFAULT_SXBET_SPORTS))
    )
    target_sport_count = max(len(sport_ids), len(resolved_sport_keys or ()), 1)
    instrument_limit = max(instrument_limit, target_sport_count * 80)
    market_discovery_limit = max(market_discovery_limit, target_sport_count * 120)

    return _SxbetCorpusScope(
        sport_keys=resolved_sport_keys,
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
