#!/usr/bin/env python3
"""
Standing semantic-rule miner: accumulating master mine -> slim node seeds.

Runs inside the strategy-node docker image (see install.sh) and loops forever:

1. MINE — run the node's semantic-cache bootstrap engine against a persistent
   master cache dir. The master is NEVER reset: the store's indexes are
   append-dedup, so every cycle is additive and the master accumulates market
   types that are only listed on some days.
2. SLIM EXPORT — project the master down to exactly what a node's runtime
   reads: promoted templates (minus stale ones), their coverage proofs and
   hyperedges, corpus manifests, rewritten indexes, a fresh compatibility
   marker, and a regenerated summary. The export is acceptance-gated with the
   node's own readiness check before it can leave the miner.
3. DISTRIBUTE — swap the seed into every node dir's staging + seed dirs
   (complete-or-absent, never partial) and queue a reload command the node
   polls, so running nodes hot-swap without a restart.
4. TELEMETRY — log per-node semantic diagnostics deltas from status.json.

Errors in any stage are logged and never kill the loop; SIGTERM/SIGINT finish
the in-flight stage and exit cleanly (the master tolerates a hard kill: cache
writes are per-file atomic and the next cycle re-mines).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import importlib
import json
import logging
import os
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("miner")

DEFAULT_MASTER_DIR = "/opt/cloudbet/miner/master-cache"
DEFAULT_MANIFEST_PATH = "/opt/cloudbet/miner/mine-manifest.json"
DEFAULT_NODES_ROOT = "/opt/cloudbet/strategy-nodes"
DEFAULT_INTERVAL_HOURS = 6.0
DEFAULT_TEMPLATE_STALE_DAYS = 14.0
DEFAULT_MAX_DISK_GB = 10.0

NODE_MANIFEST_FILENAME = "manifest.runtime.json"
NODE_STATUS_FILENAME = "status.json"
NODE_STAGING_DIRNAME = "semantic-rule-cache-staging"
NODE_SEED_DIRNAME = "semantic-rule-cache-seed"
COMMANDS_DIRNAME = "commands"
RELOAD_COMMAND = "reload_semantic_cache"
# The node container mounts its host dir at /var/lib/nautilus-node, so the
# command payload always names the container's view of the staging dir,
# independent of how the miner itself is mounted.
NODE_CONTAINER_STAGING_DIR = "/var/lib/nautilus-node/semantic-rule-cache-staging"


class ExportNotReadyError(RuntimeError):
    """
    Raised when a slim export fails the node-readiness acceptance gate.
    """


@dataclass(frozen=True)
class MinerConfig:
    master_dir: Path
    manifest_path: Path
    nodes_root: Path
    interval_hours: float
    hot_swap: bool
    template_stale_days: float
    max_disk_gb: float
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MinerConfig:
        source: Mapping[str, str] = os.environ if env is None else env
        hot_swap_raw = source.get("MINER_HOT_SWAP", "1").strip().lower()
        return cls(
            master_dir=Path(source.get("MINER_MASTER_DIR", DEFAULT_MASTER_DIR)),
            manifest_path=Path(source.get("MINER_MANIFEST", DEFAULT_MANIFEST_PATH)),
            nodes_root=Path(source.get("MINER_NODES_ROOT", DEFAULT_NODES_ROOT)),
            interval_hours=_env_float(source, "MINER_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS),
            hot_swap=hot_swap_raw not in {"0", "false", "no", ""},
            template_stale_days=_env_float(
                source,
                "MINER_TEMPLATE_STALE_DAYS",
                DEFAULT_TEMPLATE_STALE_DAYS,
            ),
            max_disk_gb=_env_float(source, "MINER_MAX_DISK_GB", DEFAULT_MAX_DISK_GB),
            log_level=(source.get("MINER_LOG_LEVEL", "INFO").strip().upper() or "INFO"),
        )


@dataclass(frozen=True)
class SlimExportResult:
    promoted_template_count: int
    stale_template_count: int
    filtered_template_count: int
    coverage_proof_count: int
    coverage_hyperedge_count: int
    manifest_count: int


def _env_float(source: Mapping[str, str], key: str, default: float) -> float:
    raw = source.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r; using default %s", key, raw, default)
        return default


# nautilus_trader is imported lazily by module path so the service module stays
# importable (and its loop testable) without the trading stack, and so static
# checkers don't follow into the node runtime's import graph.
def _semantics_store() -> Any:
    return importlib.import_module("nautilus_trader.adapters.betting.semantics.store")


def _semantic_cache() -> Any:
    return importlib.import_module(
        "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache",
    )


def _node_builder() -> Any:
    return importlib.import_module(
        "nautilus_trader.live.strategy_nodes.betting_arbitrage.builder",
    )


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return total
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def mine_master(config: MinerConfig) -> dict[str, Any]:
    """
    Run one additive mine of the master cache and return cycle telemetry.
    """
    builder_mod = _node_builder()
    cache_mod = _semantic_cache()
    store_mod = _semantics_store()
    manifest = builder_mod.load_manifest(config.manifest_path)
    config.master_dir.mkdir(parents=True, exist_ok=True)
    # Additive by construction: the bootstrap appends into the same RuleStore
    # (indexes are append-dedup) and the miner never resets the master dir.
    asyncio.run(
        cache_mod._bootstrap_semantic_cache(
            manifest=manifest,
            cache_dir=config.master_dir,
            logger=None,
        ),
    )
    cache_mod._write_semantic_cache_compatibility(config.master_dir, manifest=manifest)
    store = store_mod.RuleStore(store_mod.FileRuleCache(config.master_dir))
    return {
        "corpus_record_count": len(store.list_normalized_ids()),
        "promoted_template_count": len(store.list_promoted_template_ids()),
        "phase_timings_secs": cache_mod._read_semantic_cache_bootstrap_timings(config.master_dir),
    }


def check_master_disk(config: MinerConfig) -> float:
    """
    Return the master cache size in GB, warning when it exceeds the guard.
    """
    size_gb = _dir_size_bytes(config.master_dir) / float(1024**3)
    if size_gb > config.max_disk_gb:
        logger.warning(
            "Master cache disk usage %.2f GB exceeds MINER_MAX_DISK_GB=%.2f "
            "(corpus is append-only; compaction is a manual follow-up)",
            size_gb,
            config.max_disk_gb,
        )
    return size_gb


def _write_index(cache: Any, key: str, items: Sequence[str]) -> None:
    raw = json.dumps({"items": list(items)}, sort_keys=True, separators=(",", ":"))
    cache.add(key, gzip.compress(raw.encode("utf-8"), compresslevel=1))


def _copy_raw(src_cache: Any, dst_cache: Any, key: str) -> bool:
    # Store values are already gzip-compressed; copying the raw bytes verbatim
    # avoids a decompress/re-serialize round-trip and keeps payloads identical.
    raw = src_cache.get(key)
    if raw is None:
        return False
    dst_cache.add(key, raw)
    return True


@dataclass(frozen=True)
class _ExportFilters:
    sports: set[str] | None
    tiers: set[str] | None
    cutoff: datetime | None

    def sport_ok(self, sport: Any) -> bool:
        return self.sports is None or str(sport).lower() in self.sports

    def tier_ok(self, tier: Any) -> bool:
        return self.tiers is None or str(tier) in self.tiers


def _copy_promoted_templates(
    src_cache: Any,
    src_store: Any,
    dst_cache: Any,
    filters: _ExportFilters,
) -> tuple[list[str], int, int]:
    rule_store_cls = type(src_store)
    kept_ids: list[str] = []
    stale_count = 0
    filtered_count = 0
    for template_id in src_store.list_promoted_template_ids():
        template = src_store.load_promoted_template(template_id)
        if template is None:
            continue
        if not filters.sport_ok(template.sport) or not filters.tier_ok(template.safety_tier):
            filtered_count += 1
            continue
        # Promoted templates are never demoted by the engine; the export-time
        # staleness filter is what keeps templates the venues stopped listing
        # out of node seeds. last_seen_at=None is kept (nothing to judge).
        last_seen = _parse_utc_timestamp(template.support.last_seen_at)
        if filters.cutoff is not None and last_seen is not None and last_seen < filters.cutoff:
            stale_count += 1
            continue
        if _copy_raw(src_cache, dst_cache, rule_store_cls.template_promoted_key(template_id)):
            kept_ids.append(template_id)
    _write_index(dst_cache, rule_store_cls.TEMPLATE_PROMOTED_INDEX_KEY, kept_ids)
    return kept_ids, stale_count, filtered_count


def _copy_coverage(
    src_cache: Any,
    src_store: Any,
    dst_cache: Any,
    filters: _ExportFilters,
) -> tuple[list[str], list[str]]:
    rule_store_cls = type(src_store)
    kept_proof_ids: list[str] = []
    for proof_id in src_store.list_coverage_proof_ids():
        proof = src_store.load_coverage_proof(proof_id)
        if proof is None:
            continue
        if not filters.sport_ok(proof.universe.sport) or not filters.tier_ok(proof.safety_tier):
            continue
        if _copy_raw(src_cache, dst_cache, rule_store_cls.coverage_proof_key(proof_id)):
            kept_proof_ids.append(proof_id)
    _write_index(dst_cache, rule_store_cls.COVERAGE_PROOF_INDEX_KEY, kept_proof_ids)

    kept_proof_set = set(kept_proof_ids)
    kept_hyperedge_ids: list[str] = []
    for hyperedge_id in src_store.list_coverage_hyperedge_ids():
        hyperedge = src_store.load_coverage_hyperedge(hyperedge_id)
        if hyperedge is None or hyperedge.coverage_proof_id not in kept_proof_set:
            continue
        if _copy_raw(src_cache, dst_cache, rule_store_cls.coverage_hyperedge_key(hyperedge_id)):
            kept_hyperedge_ids.append(hyperedge_id)
    _write_index(dst_cache, rule_store_cls.COVERAGE_HYPEREDGE_INDEX_KEY, kept_hyperedge_ids)
    return kept_proof_ids, kept_hyperedge_ids


def _copy_manifests(src_cache: Any, src_store: Any, dst_cache: Any) -> list[str]:
    rule_store_cls = type(src_store)
    kept_ids = [
        manifest_id
        for manifest_id in src_store.list_manifest_ids()
        if _copy_raw(src_cache, dst_cache, rule_store_cls.manifest_key(manifest_id))
    ]
    _write_index(dst_cache, rule_store_cls.MANIFEST_INDEX_KEY, kept_ids)
    return kept_ids


def export_slim_seed(
    master_dir: Path,
    out_dir: Path,
    *,
    stale_days: float | None = None,
    sports: Iterable[str] | None = None,
    tiers: Iterable[str] | None = None,
) -> SlimExportResult:
    """
    Project the master cache into a minimal node seed at ``out_dir``.

    Keeps only what the node runtime reads — promoted templates (support stats
    are embedded in the serialized template, so support sidecars are dropped),
    coverage proofs, hyperedges surviving their proof, and corpus manifests —
    with rewritten indexes, a fresh compatibility marker carrying the master's
    scope, and a regenerated summary. Raises :class:`ExportNotReadyError`
    unless the node's own readiness check accepts the result; a non-ready
    export must never be distributed.

    """
    store_mod = _semantics_store()
    cache_mod = _semantic_cache()
    src_cache = store_mod.FileRuleCache(master_dir)
    src_store = store_mod.RuleStore(src_cache)
    dst_cache = store_mod.FileRuleCache(out_dir)

    filters = _ExportFilters(
        sports=({item.strip().lower() for item in sports if item.strip()} if sports else None),
        tiers=({item.strip() for item in tiers if item.strip()} if tiers else None),
        cutoff=(datetime.now(UTC) - timedelta(days=stale_days) if stale_days is not None else None),
    )

    with dst_cache.bulk_writes():
        kept_template_ids, stale_count, filtered_count = _copy_promoted_templates(
            src_cache,
            src_store,
            dst_cache,
            filters,
        )
        kept_proof_ids, kept_hyperedge_ids = _copy_coverage(
            src_cache,
            src_store,
            dst_cache,
            filters,
        )
        kept_manifest_ids = _copy_manifests(src_cache, src_store, dst_cache)

    marker = {
        "version": cache_mod.SEMANTIC_CACHE_COMPATIBILITY_VERSION,
        "scope": cache_mod._read_semantic_cache_compatibility(master_dir).get("scope"),
    }
    (out_dir / cache_mod.SEMANTIC_CACHE_COMPATIBILITY_FILE).write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    # Acceptance gate: the node's own status check over the export (this also
    # regenerates the summary file, which was absent in the fresh out dir).
    status = cache_mod.semantic_cache_status(out_dir)
    if not status.ready:
        raise ExportNotReadyError(
            "Slim export failed the node readiness gate "
            f"(manifests={status.manifest_count}, "
            f"promoted_templates={status.promoted_template_count})",
        )
    return SlimExportResult(
        promoted_template_count=len(kept_template_ids),
        stale_template_count=stale_count,
        filtered_template_count=filtered_count,
        coverage_proof_count=len(kept_proof_ids),
        coverage_hyperedge_count=len(kept_hyperedge_ids),
        manifest_count=len(kept_manifest_ids),
    )


def _swap_directory(node_dir: Path, seed_dir: Path, dest_name: str, stamp: str) -> None:
    # os.rename cannot atomically replace a non-empty directory, so the previous
    # generation is renamed aside first. A crash between the two renames leaves
    # the destination absent — never partial — and the node's seed adoption gate
    # skips an absent/not-ready dir; the reload command is only written after
    # every swap succeeded. A failed swap-in restores the previous generation.
    dest = node_dir / dest_name
    tmp_dir = node_dir / f".miner-tmp-{dest_name}-{stamp}"
    old_dir = node_dir / f".miner-old-{dest_name}-{stamp}"
    try:
        shutil.copytree(seed_dir, tmp_dir)
        if dest.exists():
            os.rename(dest, old_dir)
        try:
            os.rename(tmp_dir, dest)
        except OSError:
            if old_dir.exists() and not dest.exists():
                os.rename(old_dir, dest)
            raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(old_dir, ignore_errors=True)


def _write_reload_command(node_dir: Path, stamp: str) -> Path:
    commands_dir = node_dir / COMMANDS_DIRNAME
    commands_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "command": RELOAD_COMMAND,
        "id": f"miner-{stamp}",
        "staging_dir": NODE_CONTAINER_STAGING_DIR,
    }
    path = commands_dir / f"miner-{stamp}-{RELOAD_COMMAND}.json"
    tmp_path = commands_dir / f".{path.name}.tmp"
    tmp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _node_dirs(nodes_root: Path) -> list[Path]:
    if not nodes_root.is_dir():
        return []
    return sorted(
        child
        for child in nodes_root.iterdir()
        if child.is_dir() and (child / NODE_MANIFEST_FILENAME).is_file()
    )


def distribute_seed(config: MinerConfig, seed_dir: Path) -> list[str]:
    """
    Swap the seed into every node dir; one failing node never blocks the rest.
    """
    node_dirs = _node_dirs(config.nodes_root)
    if not node_dirs:
        logger.info("No node dirs under %s; nothing to distribute", config.nodes_root)
        return []
    distributed: list[str] = []
    stamp = _utc_stamp()
    for node_dir in node_dirs:
        try:
            _swap_directory(node_dir, seed_dir, NODE_STAGING_DIRNAME, stamp)
            _swap_directory(node_dir, seed_dir, NODE_SEED_DIRNAME, stamp)
            if config.hot_swap:
                _write_reload_command(node_dir, stamp)
            distributed.append(node_dir.name)
        except OSError:
            logger.exception("Seed distribution to node %s failed; continuing", node_dir.name)
    return distributed


def read_node_semantic_diagnostics(node_dir: Path) -> dict[str, float] | None:
    status_path = node_dir / NODE_STATUS_FILENAME
    if not status_path.is_file():
        return None
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    diagnostics = payload.get("semanticDiagnostics") if isinstance(payload, dict) else None
    if not isinstance(diagnostics, dict):
        return None
    result: dict[str, float] = {}
    for key in ("unsupportedProviderPatternCount", "supportedProviderCoverageRatio"):
        value = diagnostics.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[key] = float(value)
    return result or None


class Miner:
    """
    Cycle orchestrator: mine, slim-export, distribute, telemetry.
    """

    def __init__(self, config: MinerConfig) -> None:
        self._config = config
        self._stop = threading.Event()
        self._previous_diagnostics: dict[str, dict[str, float]] = {}

    @property
    def config(self) -> MinerConfig:
        return self._config

    def request_stop(self) -> None:
        self._stop.set()

    def handle_signal(self, signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; finishing the in-flight stage and exiting", signum)
        self.request_stop()

    def run_forever(self) -> None:
        interval_secs = max(1.0, self._config.interval_hours * 3600.0)
        while not self._stop.is_set():
            self.run_cycle()
            if self._stop.wait(interval_secs):
                break
        logger.info("Miner stopped")

    def run_cycle(self) -> None:
        started = time.monotonic()
        try:
            telemetry = mine_master(self._config)
            logger.info(
                "Mine complete: corpus_records=%s promoted_templates=%s phase_timings=%s",
                telemetry["corpus_record_count"],
                telemetry["promoted_template_count"],
                json.dumps(telemetry["phase_timings_secs"], sort_keys=True),
            )
        except Exception:
            # The master is still valid from previous cycles, so export and
            # distribution proceed even when this mine failed (venue outage).
            logger.exception("Mine stage failed; continuing from the existing master")
        check_master_disk(self._config)

        scratch_root = Path(tempfile.mkdtemp(prefix="miner-slim-seed-"))
        try:
            seed_dir = scratch_root / "seed"
            export_result: SlimExportResult | None = None
            try:
                export_result = export_slim_seed(
                    self._config.master_dir,
                    seed_dir,
                    stale_days=self._config.template_stale_days,
                )
                logger.info("Slim export complete: %s", export_result)
            except Exception:
                logger.exception("Slim export failed; skipping distribution this cycle")
            if export_result is not None:
                try:
                    distributed = distribute_seed(self._config, seed_dir)
                    logger.info(
                        "Distributed slim seed to %d node(s): %s",
                        len(distributed),
                        ", ".join(distributed) or "-",
                    )
                except Exception:
                    logger.exception("Seed distribution failed")
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)

        try:
            self._log_node_diagnostics()
        except Exception:
            logger.exception("Telemetry stage failed")
        logger.info("Cycle finished in %.1fs", time.monotonic() - started)

    def _log_node_diagnostics(self) -> None:
        for node_dir in _node_dirs(self._config.nodes_root):
            diagnostics = read_node_semantic_diagnostics(node_dir)
            if diagnostics is None:
                continue
            previous = self._previous_diagnostics.get(node_dir.name, {})
            unsupported = diagnostics.get("unsupportedProviderPatternCount")
            previous_unsupported = previous.get("unsupportedProviderPatternCount")
            delta = (
                unsupported - previous_unsupported
                if unsupported is not None and previous_unsupported is not None
                else None
            )
            logger.info(
                "Node %s semantic diagnostics: unsupported_provider_patterns=%s "
                "(delta=%s) supported_coverage_ratio=%s",
                node_dir.name,
                unsupported,
                delta,
                diagnostics.get("supportedProviderCoverageRatio"),
            )
            self._previous_diagnostics[node_dir.name] = diagnostics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standing semantic-rule miner service")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args(argv)

    config = MinerConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "Starting miner: master=%s manifest=%s nodes_root=%s interval_hours=%s "
        "hot_swap=%s stale_days=%s max_disk_gb=%s",
        config.master_dir,
        config.manifest_path,
        config.nodes_root,
        config.interval_hours,
        config.hot_swap,
        config.template_stale_days,
        config.max_disk_gb,
    )
    miner = Miner(config)
    signal.signal(signal.SIGTERM, miner.handle_signal)
    signal.signal(signal.SIGINT, miner.handle_signal)
    if args.once:
        miner.run_cycle()
        return 0
    miner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
