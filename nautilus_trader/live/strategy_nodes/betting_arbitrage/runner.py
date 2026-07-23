from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

from nautilus_trader.adapters.betting.fixture_identity import DEFAULT_FIXTURE_IDENTITY_RESOLVER
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.semantics import ARB_MARGIN_RELATIONSHIP_TYPES
from nautilus_trader.adapters.betting.semantics import CoverageBlockerReason
from nautilus_trader.adapters.betting.semantics import MarketNormalizer
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import build_trading_node_config
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import default_render_paths
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import load_manifest
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import (
    manifest_execution_readiness,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import write_manifest_snapshot
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import write_rendered_node_config
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    SEMANTIC_CACHE_COMPATIBILITY_FILE,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    SemanticCacheStatus,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    ensure_semantic_cache_ready,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    semantic_cache_status,
)


logger = logging.getLogger(__name__)


ZERO_PAIR_SAMPLE_NODE_LIMIT = 160

# A single book's own complementary two-way market always overrounds (its implied
# probabilities sum to 1 + vig > 1); it is arithmetically impossible for one book to
# underround its own complement. So a SAME-VENUE pair whose devig book overround falls
# meaningfully below 1 is a data error (e.g. a wrongly signed handicap leg), not a locked
# arbitrage — flag it as suspect instead of minting an executable edge. A genuine
# CROSS-venue underround is two independent books disagreeing and stays a real arb, so
# the guard is same-venue only. The tolerance absorbs benign rounding/quote-timing
# noise around a fair (overround == 1) book.
SAME_VENUE_UNDERROUND_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class RunnerContext:
    manifest: Any
    manifest_snapshot: Path
    rendered_config_path: Path
    status_path: Path
    heartbeat_path: Path
    semantic_cache: dict[str, object] | None = None


@dataclass
class ProbeProfitabilityCounters:
    quoted_edges: int = 0
    positive_execution: int = 0
    positive_same_venue: int = 0
    threshold_execution: int = 0
    threshold_same_venue: int = 0
    positive_execution_skewed: int = 0
    positive_same_venue_skewed: int = 0
    threshold_execution_skewed: int = 0
    threshold_same_venue_skewed: int = 0
    margin_bands: Counter[str] = field(default_factory=Counter)
    rag_bands: Counter[str] = field(default_factory=Counter)
    rejection_buckets: Counter[str] = field(default_factory=Counter)
    timing_flags: Counter[str] = field(default_factory=Counter)
    freshness_profiles: Counter[str] = field(default_factory=Counter)
    semantic_blocked_reasons: Counter[str] = field(default_factory=Counter)
    semantic_blocked_relationships: Counter[str] = field(default_factory=Counter)
    venue_pairs: dict[str, Counter[str]] = field(default_factory=dict)
    market_families: dict[str, Counter[str]] = field(default_factory=dict)
    blocker_samples: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    venue_quote_counts: Counter[str] = field(default_factory=Counter)
    venue_max_quote_age_secs: dict[str, float] = field(default_factory=dict)
    venue_max_fetch_latency_secs: dict[str, float] = field(default_factory=dict)
    quote_age_samples_secs: list[float] = field(default_factory=list)
    quote_age_samples_by_venue_secs: dict[str, list[float]] = field(default_factory=dict)
    fetch_latency_samples_secs: list[float] = field(default_factory=list)
    pair_skew_samples_secs: list[float] = field(default_factory=list)
    pair_skew_samples_by_venue_pair: dict[str, list[float]] = field(default_factory=dict)
    # Venues whose quotes arrive via a change-driven realtime stream (value = stream
    # currently healthy). A quiet market's last quote on such a venue is legitimately
    # old but still current, so wall-clock quote age is not a staleness signal there.
    stream_healthy_venues: dict[str, bool] = field(default_factory=dict)
    stream_dormant_leg_count: int = 0
    live_quote_age_slo_secs: float = 5.0
    live_quote_age_observations: int = 0
    live_quote_age_violations: int = 0
    live_fetch_latency_observations: int = 0
    live_fetch_latency_violations: int = 0
    live_fetch_latency_threshold_min_secs: float | None = None
    live_fetch_latency_threshold_max_secs: float | None = None
    live_pair_skew_observations: int = 0
    live_pair_skew_violations: int = 0
    live_pair_skew_threshold_min_secs: float | None = None
    live_pair_skew_threshold_max_secs: float | None = None
    same_venue_dry_run_passes: int = 0
    same_venue_dry_run_failures: int = 0
    same_venue_dry_run_failure_reasons: Counter[str] = field(default_factory=Counter)
    fee_adjusted_edges: int = 0
    fee_drag_samples: list[float] = field(default_factory=list)
    fee_impact_buckets: Counter[str] = field(default_factory=Counter)
    devig_evaluated_edges: int = 0
    devig_complete_books: int = 0
    devig_incomplete_books: int = 0
    devig_method_counts: Counter[str] = field(default_factory=Counter)
    devig_method_reason_counts: Counter[str] = field(default_factory=Counter)
    devig_convergence_counts: Counter[str] = field(default_factory=Counter)
    devig_value_buckets: Counter[str] = field(default_factory=Counter)
    overround_samples: list[float] = field(default_factory=list)
    vig_samples: list[float] = field(default_factory=list)
    gross_value_edge_samples: list[float] = field(default_factory=list)
    fee_adjusted_value_edge_samples: list[float] = field(default_factory=list)
    candidate_decision_latency_ns: list[int] = field(default_factory=list)
    samples: list[tuple[Decimal, dict[str, object]]] = field(default_factory=list)
    skewed_samples: list[tuple[Decimal, dict[str, object]]] = field(default_factory=list)
    negative_samples: list[tuple[Decimal, dict[str, object]]] = field(default_factory=list)
    value_samples: list[tuple[Decimal, dict[str, object]]] = field(default_factory=list)
    vig_erased_samples: list[tuple[Decimal, dict[str, object]]] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        self.samples.sort(key=lambda item: item[0], reverse=True)
        self.skewed_samples.sort(key=lambda item: item[0], reverse=True)
        self.negative_samples.sort(key=lambda item: item[0], reverse=True)
        self.value_samples.sort(key=lambda item: item[0], reverse=True)
        self.vig_erased_samples.sort(key=lambda item: item[0], reverse=True)
        return {
            "quoted_edges": self.quoted_edges,
            "positive_execution": self.positive_execution,
            "positive_same_venue": self.positive_same_venue,
            "threshold_execution": self.threshold_execution,
            "threshold_same_venue": self.threshold_same_venue,
            "positive_execution_skewed": self.positive_execution_skewed,
            "positive_same_venue_skewed": self.positive_same_venue_skewed,
            "threshold_execution_skewed": self.threshold_execution_skewed,
            "threshold_same_venue_skewed": self.threshold_same_venue_skewed,
            "margin_bands": dict(self.margin_bands),
            "rag_bands": dict(self.rag_bands),
            "rejection_buckets": dict(self.rejection_buckets),
            "timing_flags": dict(self.timing_flags),
            "freshness_profiles": dict(self.freshness_profiles),
            "semantic_blocked_reasons": dict(self.semantic_blocked_reasons),
            "semantic_blocked_relationships": dict(self.semantic_blocked_relationships),
            "venue_quote_health": {
                venue: {
                    "quoted_observations": self.venue_quote_counts.get(venue, 0),
                    "max_quote_age_secs": round(
                        self.venue_max_quote_age_secs.get(venue, 0.0),
                        6,
                    ),
                    "max_fetch_latency_secs": round(
                        self.venue_max_fetch_latency_secs.get(venue, 0.0),
                        6,
                    ),
                }
                for venue in sorted(self.venue_quote_counts)
            },
            "latency_histograms": {
                "quote_age_secs": _percentile_payload(self.quote_age_samples_secs),
                "fetch_latency_secs": _percentile_payload(self.fetch_latency_samples_secs),
                "pair_skew_secs": _percentile_payload(self.pair_skew_samples_secs),
            },
            "quote_age_by_venue": {
                venue: _venue_quote_age_payload(samples)
                for venue, samples in sorted(self.quote_age_samples_by_venue_secs.items())
            },
            "stream_dormant_leg_count": self.stream_dormant_leg_count,
            "pair_skew_by_venue_pair": {
                venue_pair: _percentile_payload(samples)
                for venue_pair, samples in sorted(self.pair_skew_samples_by_venue_pair.items())
            },
            "live_quote_age_slo": {
                "max_quote_age_secs": self.live_quote_age_slo_secs,
                "observations": self.live_quote_age_observations,
                "violations": self.live_quote_age_violations,
            },
            "live_timing_slo": {
                "quote_age": {
                    "threshold_secs": self.live_quote_age_slo_secs,
                    "observations": self.live_quote_age_observations,
                    "violations": self.live_quote_age_violations,
                },
                "fetch_latency": {
                    "threshold_mode": "per_candidate",
                    "min_threshold_secs": _rounded_or_none(
                        self.live_fetch_latency_threshold_min_secs,
                    ),
                    "max_threshold_secs": _rounded_or_none(
                        self.live_fetch_latency_threshold_max_secs,
                    ),
                    "observations": self.live_fetch_latency_observations,
                    "violations": self.live_fetch_latency_violations,
                },
                "pair_skew": {
                    "threshold_mode": "per_candidate",
                    "min_threshold_secs": _rounded_or_none(
                        self.live_pair_skew_threshold_min_secs,
                    ),
                    "max_threshold_secs": _rounded_or_none(
                        self.live_pair_skew_threshold_max_secs,
                    ),
                    "observations": self.live_pair_skew_observations,
                    "violations": self.live_pair_skew_violations,
                },
            },
            "same_venue_dry_run": {
                "passes": self.same_venue_dry_run_passes,
                "failures": self.same_venue_dry_run_failures,
                "failure_reasons": dict(self.same_venue_dry_run_failure_reasons),
            },
            "fee_adjustment": {
                "evaluated_edges": self.fee_adjusted_edges,
                "fee_drag_margin": _percentile_payload(self.fee_drag_samples),
                "impact_buckets": dict(self.fee_impact_buckets),
            },
            "devig_diagnostics": {
                "evaluated_edges": self.devig_evaluated_edges,
                "complete_books": self.devig_complete_books,
                "incomplete_books": self.devig_incomplete_books,
                "method_counts": dict(self.devig_method_counts),
                "method_reason_counts": dict(self.devig_method_reason_counts),
                "convergence_counts": dict(self.devig_convergence_counts),
                "value_buckets": dict(self.devig_value_buckets),
                "overround": _percentile_payload(self.overround_samples),
                "vig": _percentile_payload(self.vig_samples),
                "gross_value_edge": _percentile_payload(self.gross_value_edge_samples),
                "fee_adjusted_value_edge": _percentile_payload(
                    self.fee_adjusted_value_edge_samples,
                ),
            },
            "candidate_decision_latency": _latency_ns_payload(
                self.candidate_decision_latency_ns,
            ),
            "venue_pairs": {
                key: dict(counter)
                for key, counter in sorted(self.venue_pairs.items(), key=lambda item: item[0])
            },
            "market_families": {
                key: dict(counter)
                for key, counter in sorted(self.market_families.items(), key=lambda item: item[0])
            },
            "blocker_samples": {
                key: samples[:5]
                for key, samples in sorted(self.blocker_samples.items(), key=lambda item: item[0])
            },
            "sample_candidates": [payload for _, payload in self.samples[:10]],
            "skewed_sample_candidates": [payload for _, payload in self.skewed_samples[:5]],
            "negative_near_misses": [payload for _, payload in self.negative_samples[:10]],
            "value_edge_candidates": [payload for _, payload in self.value_samples[:10]],
            "vig_erased_candidates": [payload for _, payload in self.vig_erased_samples[:10]],
        }


def _venue_quote_age_payload(samples: list[float]) -> dict[str, float | int]:
    payload = _percentile_payload(samples)
    return {
        "observations": payload["count"],
        "p50": payload["p50"],
        "p95": payload["p95"],
        "max": payload["max"],
    }


def _percentile_payload(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "p50": round(_percentile(ordered, 0.50), 6),
        "p95": round(_percentile(ordered, 0.95), 6),
        "p99": round(_percentile(ordered, 0.99), 6),
        "max": round(ordered[-1], 6),
    }


def _latency_ns_payload(samples: list[int]) -> dict[str, float | int]:
    if not samples:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}
    ordered_ms = sorted(sample / 1_000_000 for sample in samples)
    return {
        "count": len(ordered_ms),
        "p50_ms": round(_percentile(ordered_ms, 0.50), 6),
        "p95_ms": round(_percentile(ordered_ms, 0.95), 6),
        "p99_ms": round(_percentile(ordered_ms, 0.99), 6),
        "max_ms": round(ordered_ms[-1], 6),
    }


def _percentile(ordered_samples: list[float], percentile: float) -> float:
    index = max(0, min(len(ordered_samples) - 1, int((len(ordered_samples) - 1) * percentile)))
    return ordered_samples[index]


def _rounded_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _min_threshold(current: float | None, candidate: float) -> float | None:
    if candidate <= 0:
        return current
    if current is None:
        return candidate
    return min(current, candidate)


def _max_threshold(current: float | None, candidate: float) -> float | None:
    if candidate <= 0:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


def _stream_healthy_venues(provider_quote_poll_stats: object) -> dict[str, bool]:
    if not isinstance(provider_quote_poll_stats, dict):
        return {}
    venues: dict[str, bool] = {}
    for venue, payload in provider_quote_poll_stats.items():
        if not isinstance(payload, dict):
            continue
        if str(payload.get("source") or "") != "realtime_stream":
            continue
        venues[str(venue).strip().upper()] = bool(payload.get("stream_connected"))
    return venues


def _record_live_timing_slo(
    counters: ProbeProfitabilityCounters,
    *,
    quote_age_a_secs: float,
    quote_age_b_secs: float,
    fetch_latency_a_secs: float,
    fetch_latency_b_secs: float,
    quote_delta_secs: float,
    max_fetch_latency_secs: float,
    max_pair_skew_secs: float,
    stream_dormant_leg_a: bool = False,
    stream_dormant_leg_b: bool = False,
) -> None:
    # Dormant healthy-stream legs are excluded from the wall-clock quote-age and
    # pair-skew SLO accounting: their last quote is old only because the stream pushes
    # on book change. Fetch latency stays fully counted — it measures the transport,
    # not quote recency.
    if not stream_dormant_leg_a:
        counters.live_quote_age_observations += 1
        if quote_age_a_secs > counters.live_quote_age_slo_secs:
            counters.live_quote_age_violations += 1
    if not stream_dormant_leg_b:
        counters.live_quote_age_observations += 1
        if quote_age_b_secs > counters.live_quote_age_slo_secs:
            counters.live_quote_age_violations += 1
    counters.live_fetch_latency_observations += 2
    if max_fetch_latency_secs > 0 and fetch_latency_a_secs > max_fetch_latency_secs:
        counters.live_fetch_latency_violations += 1
    if max_fetch_latency_secs > 0 and fetch_latency_b_secs > max_fetch_latency_secs:
        counters.live_fetch_latency_violations += 1
    if not (stream_dormant_leg_a or stream_dormant_leg_b):
        counters.live_pair_skew_observations += 1
        if max_pair_skew_secs > 0 and quote_delta_secs > max_pair_skew_secs:
            counters.live_pair_skew_violations += 1
    counters.live_fetch_latency_threshold_min_secs = _min_threshold(
        counters.live_fetch_latency_threshold_min_secs,
        max_fetch_latency_secs,
    )
    counters.live_fetch_latency_threshold_max_secs = _max_threshold(
        counters.live_fetch_latency_threshold_max_secs,
        max_fetch_latency_secs,
    )
    counters.live_pair_skew_threshold_min_secs = _min_threshold(
        counters.live_pair_skew_threshold_min_secs,
        max_pair_skew_secs,
    )
    counters.live_pair_skew_threshold_max_secs = _max_threshold(
        counters.live_pair_skew_threshold_max_secs,
        max_pair_skew_secs,
    )


class RuntimeProbeCoverageError(RuntimeError):
    def __init__(self, message: str, payload: dict[str, object]) -> None:
        super().__init__(message)
        self.payload = payload


class HeartbeatWriter(threading.Thread):
    def __init__(
        self,
        heartbeat_path: Path,
        node_id: str,
        interval_secs: float,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self._heartbeat_path = heartbeat_path
        self._node_id = node_id
        self._interval_secs = interval_secs
        self._stop_event = stop_event

    def run(self) -> None:
        while not self._stop_event.wait(self._interval_secs):
            _write_json(
                self._heartbeat_path,
                {
                    "nodeId": self._node_id,
                    "status": "alive",
                    "at": _utc_now(),
                },
            )


class RuntimeProbeStatusWriter(threading.Thread):
    def __init__(
        self,
        status_path: Path,
        *,
        manifest,
        strategy,
        semantic_cache: dict[str, object] | None,
        manifest_snapshot: Path,
        rendered_config_path: Path,
        heartbeat_path: Path,
        interval_secs: float,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self._status_path = status_path
        self._manifest = manifest
        self._strategy = strategy
        self._semantic_cache = semantic_cache
        self._manifest_snapshot = manifest_snapshot
        self._rendered_config_path = rendered_config_path
        self._heartbeat_path = heartbeat_path
        self._interval_secs = interval_secs
        self._stop_event = stop_event
        self._started_at = time.monotonic()
        self._diagnostics_throttle = _RuntimeProbeDiagnosticsThrottle(
            getattr(manifest, "semantic_diagnostics_interval_secs", 90.0),
        )
        cache_dir = getattr(manifest, "semantic_rule_cache_dir", None)
        self._semantic_cache_dir = Path(cache_dir) if cache_dir else None
        # Once-at-startup capture goes stale the moment a hot swap re-mines the cache
        # under this RUNNING node. Track the compatibility marker's mtime and recompute
        # the block when it changes so the status stays truthful (reloadedAt + new counts).
        self._semantic_cache_version_mtime = self._read_semantic_cache_version_mtime()

    def _semantic_cache_version_path(self) -> Path | None:
        if self._semantic_cache_dir is None:
            return None
        return self._semantic_cache_dir / SEMANTIC_CACHE_COMPATIBILITY_FILE

    def _read_semantic_cache_version_mtime(self) -> float | None:
        marker = self._semantic_cache_version_path()
        if marker is None:
            return None
        try:
            return marker.stat().st_mtime
        except OSError:
            return None

    def _maybe_refresh_semantic_cache(self) -> None:
        if self._semantic_cache_dir is None:
            return
        mtime = self._read_semantic_cache_version_mtime()
        if mtime is None or mtime == self._semantic_cache_version_mtime:
            return
        self._semantic_cache_version_mtime = mtime
        status = semantic_cache_status(self._semantic_cache_dir, manifest=self._manifest)
        payload = _semantic_cache_payload(status)
        if payload is not None:
            payload["reloadedAt"] = _utc_now()
        self._semantic_cache = payload

    def run(self) -> None:
        min_profit_margin = Decimal(str(self._manifest.strategy.min_profit_margin))
        while not self._stop_event.wait(self._interval_secs):
            # A daemon thread dies silently on any unhandled exception, which would freeze
            # status.json at its last snapshot while the node keeps running. Keep the writer
            # alive across transient collection/write errors so the probe stays fresh.
            try:
                self._maybe_refresh_semantic_cache()
                runtime_probe = _collect_runtime_probe_payload(
                    self._strategy,
                    min_profit_margin=min_profit_margin,
                    elapsed_seconds=time.monotonic() - self._started_at,
                    diagnostics=self._diagnostics_throttle,
                )
                _write_status(
                    self._status_path,
                    manifest=self._manifest,
                    status="running",
                    semantic_cache=self._semantic_cache,
                    manifest_snapshot=self._manifest_snapshot,
                    rendered_config_path=self._rendered_config_path,
                    heartbeat_path=self._heartbeat_path,
                    runtime_probe=runtime_probe,
                    updatedAt=_utc_now(),
                )
            except Exception:
                logger.exception(
                    "Runtime probe status write failed; keeping writer alive for next cycle",
                )


def _safe_shutdown_node(node) -> None:
    # Nautilus's TradingNode / DataClient refuse ``dispose`` while state==RUNNING
    # ("InvalidStateTrigger('RUNNING -> DISPOSE')"), which surfaces as a pyo3
    # abort during interpreter shutdown. Stop first when the node is still
    # running, then dispose; log-and-continue on either failure so we always
    # reach the dispose step.
    is_running_method = getattr(node, "is_running", None)
    try:
        still_running = bool(is_running_method()) if callable(is_running_method) else False
    except Exception:
        logger.exception("probe cleanup: node.is_running() failed; assuming running")
        still_running = True
    if still_running:
        try:
            node.stop()
        except Exception:
            logger.exception("probe cleanup: node.stop() failed")
    try:
        node.dispose()
    except Exception:
        logger.exception("probe cleanup: node.dispose() failed")


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    context = _load_runner_context(args)

    try:
        context = replace(context, semantic_cache=_ensure_semantic_cache(context.manifest))
        pre_node_result = _handle_pre_node_command(args, context)
        if pre_node_result is not None:
            return pre_node_result
        config = _write_node_config(context)
    except Exception as exc:
        _write_pre_node_failure(args, context, exc)
        raise

    from nautilus_trader.live.node import TradingNode

    node = TradingNode(config=config)
    try:
        _build_node(node, context)
        return _handle_built_node_command(args, node, context)
    except Exception:
        _safe_shutdown_node(node)
        raise
    finally:
        if args.command == "run" and args.no_start:
            _safe_shutdown_node(node)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Betting arbitrage trading-node runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate-manifest", help="Validate a node manifest")
    validate_parser.add_argument("--manifest", required=True)

    render_parser = subparsers.add_parser(
        "render-node-config",
        help="Render TradingNodeConfig JSON",
    )
    render_parser.add_argument("--manifest", required=True)
    render_parser.add_argument("--output")

    run_parser = subparsers.add_parser("run", help="Build and run a trading node from a manifest")
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("--no-start", action="store_true")

    probe_parser = subparsers.add_parser(
        "probe-runtime",
        help="Run a validation-mode node briefly and report semantic match coverage",
    )
    probe_parser.add_argument("--manifest", required=True)
    probe_parser.add_argument("--timeout-seconds", type=float, default=180.0)
    probe_parser.add_argument("--poll-interval-secs", type=float, default=5.0)
    probe_parser.add_argument("--min-connected-nodes", type=int, default=2)
    probe_parser.add_argument("--min-match-instruments", type=int, default=2)
    probe_parser.add_argument("--min-quoted-match-instruments", type=int, default=0)
    probe_parser.add_argument("--min-quoted-edges", type=int, default=0)
    probe_parser.add_argument("--min-positive-margin-candidates", type=int, default=0)
    probe_parser.add_argument("--min-cross-venue-candidates", type=int, default=0)
    probe_parser.add_argument(
        "--require-cross-venue-candidates-or-blockers",
        action="store_true",
        help=(
            "Accept either cross-venue candidates or explicit cross-venue blocker "
            "examples in the runtime probe."
        ),
    )
    probe_parser.add_argument(
        "--min-quoted-node-count",
        action="append",
        default=[],
        metavar="VENUE:COUNT",
        help="Require at least COUNT quoted nodes for VENUE. May be repeated.",
    )
    probe_parser.add_argument(
        "--allow-subscription-fallback",
        action="store_true",
        help=(
            "Accept an unmet per-venue quoted-node minimum when that venue has at "
            "least as many quote subscriptions and the quote observation state "
            "attributes the gap to markets not quoting yet; quoted-book coverage "
            "checks are waived while connection, instrument, and topology checks "
            "stay strict."
        ),
    )
    probe_parser.add_argument("--require-rust-semantic-topology", action="store_true")

    return parser


def _load_runner_context(args) -> RunnerContext:
    manifest = load_manifest(args.manifest)
    rendered_paths = default_render_paths(manifest)
    manifest_snapshot = rendered_paths["manifest"]
    rendered_config_path = (
        Path(manifest.rendered_config_path)
        if manifest.rendered_config_path
        else rendered_paths["rendered_config"]
    )
    status_path = Path(manifest.status_path) if manifest.status_path else rendered_paths["status"]
    heartbeat_path = (
        Path(manifest.heartbeat_path) if manifest.heartbeat_path else rendered_paths["heartbeat"]
    )
    return RunnerContext(
        manifest=manifest,
        manifest_snapshot=manifest_snapshot,
        rendered_config_path=rendered_config_path,
        status_path=status_path,
        heartbeat_path=heartbeat_path,
    )


def _write_node_config(context: RunnerContext):
    config = build_trading_node_config(context.manifest)
    write_manifest_snapshot(context.manifest, context.manifest_snapshot)
    write_rendered_node_config(config, context.rendered_config_path)
    return config


def _handle_pre_node_command(args, context: RunnerContext) -> int | None:
    if args.command == "validate-manifest":
        _write_node_config(context)
        _write_status(
            context.status_path,
            manifest=context.manifest,
            status="validated",
            semantic_cache=context.semantic_cache,
            manifest_snapshot=context.manifest_snapshot,
            rendered_config_path=context.rendered_config_path,
            validatedAt=_utc_now(),
        )
        return 0

    if args.command == "render-node-config":
        config = _write_node_config(context)
        if args.output:
            write_rendered_node_config(config, args.output)
        else:
            print(context.rendered_config_path.read_text())
        return 0

    return None


def _write_pre_node_failure(args, context: RunnerContext, error: Exception) -> None:
    if args.command == "render-node-config":
        return
    _write_status(
        context.status_path,
        manifest=context.manifest,
        status="failed",
        semantic_cache=context.semantic_cache,
        manifest_snapshot=context.manifest_snapshot,
        rendered_config_path=context.rendered_config_path,
        failedAt=_utc_now(),
        error=error,
    )


def _build_node(node, context: RunnerContext) -> None:
    _write_status(
        context.status_path,
        manifest=context.manifest,
        status="building",
        semantic_cache=context.semantic_cache,
        manifest_snapshot=context.manifest_snapshot,
        rendered_config_path=context.rendered_config_path,
        at=_utc_now(),
    )
    node.build()
    _write_status(
        context.status_path,
        manifest=context.manifest,
        status="built",
        semantic_cache=context.semantic_cache,
        manifest_snapshot=context.manifest_snapshot,
        rendered_config_path=context.rendered_config_path,
        at=_utc_now(),
    )


def _handle_built_node_command(args, node, context: RunnerContext) -> int:
    if args.command == "run" and args.no_start:
        return 0

    if args.command == "probe-runtime":
        return _handle_probe_runtime_command(args, node, context)

    return _run_node(node, context)


def _handle_probe_runtime_command(args, node, context: RunnerContext) -> int:
    try:
        payload = _probe_runtime(
            node=node,
            manifest=context.manifest,
            status_path=context.status_path,
            heartbeat_path=context.heartbeat_path,
            manifest_snapshot=context.manifest_snapshot,
            rendered_config_path=context.rendered_config_path,
            semantic_cache=context.semantic_cache,
            timeout_seconds=float(args.timeout_seconds),
            poll_interval_secs=float(args.poll_interval_secs),
            min_connected_nodes=int(args.min_connected_nodes),
            min_match_instruments=int(args.min_match_instruments),
            min_quoted_match_instruments=int(args.min_quoted_match_instruments),
            min_quoted_edges=int(args.min_quoted_edges),
            min_positive_margin_candidates=int(args.min_positive_margin_candidates),
            min_cross_venue_candidates=int(args.min_cross_venue_candidates),
            require_cross_venue_candidates_or_blockers=bool(
                args.require_cross_venue_candidates_or_blockers,
            ),
            min_quoted_node_counts=_parse_venue_count_requirements(
                args.min_quoted_node_count,
            ),
            require_rust_semantic_topology=bool(args.require_rust_semantic_topology),
            allow_subscription_fallback=bool(args.allow_subscription_fallback),
        )
        _write_status(
            context.status_path,
            manifest=context.manifest,
            status="probed",
            semantic_cache=context.semantic_cache,
            manifest_snapshot=context.manifest_snapshot,
            rendered_config_path=context.rendered_config_path,
            heartbeat_path=context.heartbeat_path,
            completedAt=_utc_now(),
            runtime_probe=payload,
        )
        return 0
    except Exception as exc:
        _write_status(
            context.status_path,
            manifest=context.manifest,
            status="failed",
            semantic_cache=context.semantic_cache,
            manifest_snapshot=context.manifest_snapshot,
            rendered_config_path=context.rendered_config_path,
            heartbeat_path=context.heartbeat_path,
            runtime_probe=getattr(exc, "payload", None),
            failedAt=_utc_now(),
            error=exc,
        )
        raise
    finally:
        _safe_shutdown_node(node)


def _run_node(node, context: RunnerContext) -> int:
    heartbeat_stop = threading.Event()
    heartbeat_writer = HeartbeatWriter(
        heartbeat_path=context.heartbeat_path,
        node_id=context.manifest.node_id,
        interval_secs=context.manifest.heartbeat_interval_secs,
        stop_event=heartbeat_stop,
    )
    runtime_probe_stop: threading.Event | None = None
    runtime_probe_writer: RuntimeProbeStatusWriter | None = None
    if hasattr(node, "trader"):
        runtime_probe_stop = threading.Event()
        runtime_probe_writer = RuntimeProbeStatusWriter(
            status_path=context.status_path,
            manifest=context.manifest,
            strategy=_resolve_betting_strategy(node),
            semantic_cache=context.semantic_cache,
            manifest_snapshot=context.manifest_snapshot,
            rendered_config_path=context.rendered_config_path,
            heartbeat_path=context.heartbeat_path,
            interval_secs=max(1.0, context.manifest.heartbeat_interval_secs),
            stop_event=runtime_probe_stop,
        )

    heartbeat_writer.start()
    _write_status(
        context.status_path,
        manifest=context.manifest,
        status="running",
        semantic_cache=context.semantic_cache,
        manifest_snapshot=context.manifest_snapshot,
        rendered_config_path=context.rendered_config_path,
        heartbeat_path=context.heartbeat_path,
        startedAt=_utc_now(),
    )
    if runtime_probe_writer is not None:
        runtime_probe_writer.start()

    try:
        node.run()
        _write_status(
            context.status_path,
            manifest=context.manifest,
            status="completed",
            semantic_cache=context.semantic_cache,
            manifest_snapshot=context.manifest_snapshot,
            rendered_config_path=context.rendered_config_path,
            heartbeat_path=context.heartbeat_path,
            completedAt=_utc_now(),
        )
        return 0
    except Exception as exc:
        _write_status(
            context.status_path,
            manifest=context.manifest,
            status="failed",
            semantic_cache=context.semantic_cache,
            manifest_snapshot=context.manifest_snapshot,
            rendered_config_path=context.rendered_config_path,
            heartbeat_path=context.heartbeat_path,
            failedAt=_utc_now(),
            error=exc,
        )
        raise
    finally:
        heartbeat_stop.set()
        if runtime_probe_stop is not None:
            runtime_probe_stop.set()
        _safe_shutdown_node(node)


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = _sanitize_json_value(payload)
    path.write_text(json.dumps(sanitized, indent=2) + "\n", encoding="utf8")


def _write_status(
    path: Path,
    *,
    manifest,
    status: str,
    semantic_cache: dict[str, object] | None,
    manifest_snapshot: Path,
    rendered_config_path: Path,
    heartbeat_path: Path | None = None,
    runtime_probe: dict[str, object] | None = None,
    error: Exception | str | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "nodeId": manifest.node_id,
        "status": status,
        "manifestPath": str(manifest_snapshot),
        "renderedConfigPath": str(rendered_config_path),
        "semanticCache": semantic_cache,
        "executionReadiness": manifest_execution_readiness(manifest),
    }
    if heartbeat_path is not None:
        payload["heartbeatPath"] = str(heartbeat_path)
    if runtime_probe is not None:
        payload["runtimeProbe"] = runtime_probe
    if error is not None:
        payload["error"] = repr(error) if isinstance(error, Exception) else str(error)
    payload.update(extra)
    _write_json(path, payload)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_semantic_cache(manifest) -> dict[str, object] | None:
    status = ensure_semantic_cache_ready(manifest)
    return _semantic_cache_payload(status)


def _semantic_cache_payload(status: SemanticCacheStatus | None) -> dict[str, object] | None:
    if status is None:
        return None
    payload = status.to_dict()
    return {
        "path": payload["path"],
        "source": payload["source"],
        "ready": payload["ready"],
        "manifestCount": payload["manifest_count"],
        "promotedTemplateCount": payload["promoted_template_count"],
        "executionSafeTemplateCount": payload["execution_safe_template_count"],
        "sameVenueExecutionEligibleTemplateCount": (
            payload["same_venue_execution_eligible_template_count"]
        ),
        "promotedSafetyTierCounts": payload.get("promoted_safety_tier_counts", {}),
        "promotedMarketFamilyCounts": payload.get("promoted_market_family_counts", {}),
        "executionSafeMarketFamilyCounts": payload.get(
            "execution_safe_market_family_counts",
            {},
        ),
        "sameVenueEligibleMarketFamilyCounts": payload.get(
            "same_venue_eligible_market_family_counts",
            {},
        ),
        "strictExecutionBlockerCounts": payload.get("strict_execution_blocker_counts", {}),
        "coverageProofCount": payload["coverage_proof_count"],
        "coverageHyperedgeCount": payload["coverage_hyperedge_count"],
        "compatibilityVersion": payload.get("compatibility_version"),
        "compatibilityScope": payload.get("compatibility_scope"),
        "compatible": payload.get("compatible", True),
        "summaryReused": payload.get("summary_reused", False),
        "bootstrapPhaseTimingsSeconds": payload.get("bootstrap_phase_timings_secs", {}),
        "providerCorpusCoverage": payload.get("provider_corpus_coverage", {}),
    }


def _probe_runtime(
    *,
    node,
    manifest,
    status_path: Path,
    heartbeat_path: Path,
    manifest_snapshot: Path,
    rendered_config_path: Path,
    semantic_cache: dict[str, object] | None,
    timeout_seconds: float,
    poll_interval_secs: float,
    min_connected_nodes: int,
    min_match_instruments: int,
    min_quoted_match_instruments: int,
    min_quoted_edges: int,
    min_positive_margin_candidates: int,
    min_cross_venue_candidates: int,
    require_cross_venue_candidates_or_blockers: bool,
    min_quoted_node_counts: dict[str, int],
    require_rust_semantic_topology: bool,
    allow_subscription_fallback: bool = False,
) -> dict[str, object]:
    if not manifest.validation_mode:
        raise RuntimeError("probe-runtime requires validation_mode=true")

    heartbeat_stop = threading.Event()
    heartbeat_writer = HeartbeatWriter(
        heartbeat_path=heartbeat_path,
        node_id=manifest.node_id,
        interval_secs=manifest.heartbeat_interval_secs,
        stop_event=heartbeat_stop,
    )
    heartbeat_writer.start()

    strategy = _resolve_betting_strategy(node)
    run_error: list[BaseException] = []

    def _run_node() -> None:
        try:
            node.run(raise_exception=True)
        except BaseException as e:  # pragma: no cover - surfaced below
            run_error.append(e)

    run_thread = threading.Thread(target=_run_node, daemon=True)
    run_thread.start()

    started_at = time.monotonic()
    min_profit_margin = Decimal(str(manifest.strategy.min_profit_margin))
    latest_payload: dict[str, object] = _collect_runtime_probe_payload(
        strategy,
        min_profit_margin=min_profit_margin,
        elapsed_seconds=0.0,
    )
    latest_payload["nodeLifecycle"] = _probe_node_lifecycle_state(
        node,
        run_thread=run_thread,
        run_error=run_error,
    )

    try:
        while time.monotonic() - started_at < timeout_seconds:
            latest_payload = _collect_runtime_probe_payload(
                strategy,
                min_profit_margin=min_profit_margin,
                elapsed_seconds=time.monotonic() - started_at,
            )
            latest_payload["nodeLifecycle"] = _probe_node_lifecycle_state(
                node,
                run_thread=run_thread,
                run_error=run_error,
            )
            _write_status(
                status_path,
                manifest=manifest,
                status="probing",
                semantic_cache=semantic_cache,
                manifest_snapshot=manifest_snapshot,
                rendered_config_path=rendered_config_path,
                heartbeat_path=heartbeat_path,
                runtime_probe=latest_payload,
                startedAt=_utc_now(),
            )
            if _runtime_probe_satisfied(
                latest_payload,
                min_connected_nodes=min_connected_nodes,
                min_match_instruments=min_match_instruments,
                min_quoted_match_instruments=min_quoted_match_instruments,
                min_quoted_edges=min_quoted_edges,
                min_positive_margin_candidates=min_positive_margin_candidates,
                min_cross_venue_candidates=min_cross_venue_candidates,
                require_cross_venue_candidates_or_blockers=(
                    require_cross_venue_candidates_or_blockers
                ),
                min_quoted_node_counts=min_quoted_node_counts,
                require_rust_semantic_topology=require_rust_semantic_topology,
                allow_subscription_fallback=allow_subscription_fallback,
            ):
                return latest_payload
            if run_error:
                raise run_error[0]
            if not run_thread.is_alive():
                break
            time.sleep(poll_interval_secs)
    finally:
        heartbeat_stop.set()
        node.stop()
        run_thread.join(
            timeout=max(
                5.0,
                float(manifest.timeout_shutdown) + float(manifest.timeout_disconnection),
            ),
        )

    if run_error:
        raise run_error[0]

    with suppress(Exception):
        _write_status(
            status_path,
            manifest=manifest,
            status="probe_failed",
            semantic_cache=semantic_cache,
            manifest_snapshot=manifest_snapshot,
            rendered_config_path=rendered_config_path,
            heartbeat_path=heartbeat_path,
            runtime_probe=latest_payload,
            startedAt=_utc_now(),
            failedAt=_utc_now(),
        )

    semantic_diagnostics = latest_payload.get("semanticDiagnostics", {})
    candidate_quality = latest_payload.get("candidateQuality", {})
    quote_observation = latest_payload.get("quoteObservationState", {})
    node_lifecycle = latest_payload.get("nodeLifecycle", {})
    diagnostics_json = json.dumps(
        semantic_diagnostics,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )[:4000]
    candidate_quality_json = json.dumps(
        candidate_quality,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )[:4000]
    quote_observation_json = json.dumps(
        quote_observation,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )[:2000]
    node_lifecycle_json = json.dumps(
        node_lifecycle,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )[:1200]
    raise RuntimeProbeCoverageError(
        "Runtime probe did not observe the required semantic coverage "
        f"(connected_nodes={latest_payload['connectedNodes']}, "
        f"semantic_match_instruments={latest_payload['semanticMatchInstruments']}, "
        f"quoted_semantic_match_instruments={latest_payload['quotedSemanticMatchInstruments']}, "
        f"quoted_edges={latest_payload['quotedEdges']}, "
        f"positive_margin_candidates={latest_payload['positiveMarginCandidates']['total']}, "
        f"cross_venue_candidate_count="
        f"{(latest_payload.get('venueCoverage') or {}).get('crossVenueCandidateCount', 0)}, "
        f"graph_engine={latest_payload.get('graphEngine')}, "
        f"topology_source={latest_payload.get('topologySource')}, "
        f"semantic_template_count={latest_payload.get('semanticTemplateCount')}, "
        f"quote_observation={quote_observation_json}, "
        f"node_lifecycle={node_lifecycle_json}, "
        f"venue_coverage={json.dumps(latest_payload.get('venueCoverage') or {}, sort_keys=True, default=str)[:2000]}, "
        f"semantic_diagnostics={diagnostics_json}, "
        f"candidate_quality={candidate_quality_json})",
        latest_payload,
    )


def _resolve_betting_strategy(node):
    from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy

    for strategy in node.trader.strategies():
        if isinstance(strategy, BettingArbitrageStrategy):
            return strategy
    raise RuntimeError("Trading node did not register a BettingArbitrageStrategy")


def _probe_node_lifecycle_state(
    node: object,
    *,
    run_thread: threading.Thread,
    run_error: Sequence[BaseException],
) -> dict[str, object]:
    kernel = getattr(node, "kernel", None)
    data_engine = getattr(kernel, "data_engine", None)
    exec_engine = getattr(kernel, "exec_engine", None)
    trader = getattr(kernel, "trader", None)
    loop = getattr(kernel, "loop", None)
    return {
        "kernelRunning": bool(_safe_call(kernel, "is_running")),
        "kernelStopping": bool(getattr(kernel, "_is_stopping", False)),
        "traderRunning": bool(getattr(trader, "is_running", False)),
        "runThreadAlive": run_thread.is_alive(),
        "runErrorCount": len(run_error),
        "dataEngineDisconnected": _safe_call(data_engine, "check_disconnected"),
        "execEngineDisconnected": _safe_call(exec_engine, "check_disconnected"),
        "loopRunning": bool(_safe_call(loop, "is_running")),
    }


def _safe_call(target: object | None, method_name: str) -> object:
    if target is None:
        return None
    method = getattr(target, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


class _RuntimeProbeDiagnosticsThrottle:
    """
    Rate-limit the O(graph) probe sections.

    The status writer runs every ``heartbeat_interval_secs`` (default 5s), but the
    graph snapshot copy and every pass over it (edge profitability, venue-pair
    coverage, resolution horizon, semantic and coverage diagnostics) do work
    proportional to the whole graph. On a large multivenue graph one pass runs for
    far longer than a heartbeat, so recomputing every cycle holds the GIL
    back-to-back and starves the asyncio venue quote-poll loops. The
    trading-relevant counters keep refreshing every heartbeat from O(1) strategy
    stats; the heavy sections refresh at most once per ``interval_secs`` and are
    carried forward in between, marked with the time they were computed, which
    releases the GIL so the quote path keeps running regardless of graph size.

    """

    def __init__(self, interval_secs: float, *, clock=time.monotonic) -> None:
        self._interval_secs = max(0.0, float(interval_secs))
        self._clock = clock
        self._last_computed_at: float | None = None
        self._heavy_sections: dict[str, Any] = {}
        self._computed_at: str | None = None

    @property
    def interval_secs(self) -> float:
        return self._interval_secs

    @property
    def heavy_sections(self) -> dict[str, Any]:
        return self._heavy_sections

    @property
    def computed_at(self) -> str | None:
        return self._computed_at

    def should_recompute(self) -> bool:
        # Always compute on the first cycle so the sections are never empty.
        if self._last_computed_at is None or not self._heavy_sections:
            return True
        return (self._clock() - self._last_computed_at) >= self._interval_secs

    def store(self, heavy_sections: dict[str, Any], *, computed_at: str) -> None:
        self._heavy_sections = heavy_sections
        self._computed_at = computed_at
        self._last_computed_at = self._clock()


def _collect_runtime_probe_payload(
    strategy,
    *,
    min_profit_margin: Decimal,
    elapsed_seconds: float,
    diagnostics: _RuntimeProbeDiagnosticsThrottle | None = None,
) -> dict[str, object]:
    stats = strategy.get_stats()
    # The graph snapshot copy and every O(nodes)/O(edges) pass over it are the probe
    # hotspots; throttle them via ``diagnostics`` when supplied (the live status
    # writer), otherwise compute them fresh every call (validation probe and direct
    # callers). Everything else below reads O(1) strategy stats so heartbeat-critical
    # fields stay fresh every cycle.
    reused = diagnostics is not None and not diagnostics.should_recompute()
    if reused:
        heavy = diagnostics.heavy_sections
        computed_at = diagnostics.computed_at
    else:
        heavy = _collect_probe_heavy_sections(
            strategy,
            stats=stats,
            min_profit_margin=min_profit_margin,
        )
        computed_at = _utc_now()
        if diagnostics is not None:
            diagnostics.store(heavy, computed_at=computed_at)
    return {
        "elapsedSeconds": round(elapsed_seconds, 2),
        "minProfitMargin": str(min_profit_margin),
        "subscribedInstruments": stats["subscribed_instruments"],
        "graphNodes": stats["opportunity_graph_nodes"],
        "graphEdges": stats["opportunity_graph_edges"],
        "graphQuoteStates": stats["opportunity_graph_quote_states"],
        "connectedNodes": stats["opportunity_graph_connected_nodes"],
        "graphEngine": "rust" if stats.get("opportunity_graph_rust_enabled") else "python",
        "topologySource": stats.get("opportunity_graph_topology_source", "unknown"),
        "semanticTemplateCount": stats.get("opportunity_graph_semantic_template_count", 0),
        "coverageProofCount": stats.get("opportunity_graph_coverage_proof_count", 0),
        "coverageHyperedgeCount": stats.get("opportunity_graph_coverage_hyperedge_count", 0),
        "coverageDiagnostics": stats.get("opportunity_graph_coverage_summary", {}),
        "feePolicy": {
            "venueTakerFeeRates": stats.get("venue_taker_fee_rates", {}),
            "venueMakerRebateRates": stats.get("venue_maker_rebate_rates", {}),
            "venueWinningProfitFeeRates": stats.get("venue_winning_profit_fee_rates", {}),
            "venueBasketRebateRates": stats.get("venue_basket_rebate_rates", {}),
            "venueBasketBoostRates": stats.get("venue_basket_boost_rates", {}),
            "devigEnabled": stats.get("devig_enabled", False),
            "devigMethod": stats.get("devig_method", "auto"),
            "devigReferenceVenues": stats.get("devig_reference_venues", []),
            "valueDiagnosticsEnabled": stats.get("value_diagnostics_enabled", False),
            "valueExecutionEnabled": stats.get("value_execution_enabled", False),
            "minValueEdge": stats.get("min_value_edge", "0"),
        },
        "liveExecution": stats.get("live_execution", {}),
        "executionApprovals": stats.get("execution_approvals", {}),
        **heavy["sections"],
        "instrumentRefresh": _instrument_refresh_payload(stats),
        "arbPositionPnl": stats.get("arb_position_tracker", {}),
        "strategyStats": stats,
        "latencyDiagnostics": _runtime_latency_diagnostics(stats, heavy["profitability"]),
        "providerQuotePollStats": stats.get("provider_quote_poll_stats", {}),
        "quoteObservationState": _probe_quote_observation_state(
            stats,
            heavy["venueCoverage"],
        ),
        "fxPolicy": stats.get("fx_policy", {}),
        "heavySections": {
            "computedAt": computed_at,
            "reused": reused,
            "refreshIntervalSecs": diagnostics.interval_secs if diagnostics is not None else 0.0,
        },
    }


def _collect_probe_heavy_sections(
    strategy,
    *,
    stats: dict[str, object],
    min_profit_margin: Decimal,
) -> dict[str, Any]:
    graph = strategy.opportunity_graph
    snapshot = _snapshot_probe_graph_state(graph)
    semantic_diagnostics = _semantic_probe_diagnostics(
        graph,
        nodes=snapshot["nodes"] if snapshot is not None else None,
    )
    if snapshot is None:
        venue_coverage = _venue_pair_coverage(
            strategy,
            edges=[],
            nodes={},
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
            provider_quote_poll_stats=stats.get("provider_quote_poll_stats", {}),
        )
        return {
            "profitability": _empty_candidate_quality_payload(),
            "venueCoverage": venue_coverage,
            "sections": {
                "semanticMatchInstruments": 0,
                "quotedSemanticMatchInstruments": 0,
                "executionSafeEdges": 0,
                "sameVenueExecutionEligibleEdges": 0,
                "quotedEdges": 0,
                "positiveMarginCandidates": {
                    "executionSafe": 0,
                    "sameVenueExecutionEligible": 0,
                    "total": 0,
                },
                "thresholdMarginCandidates": {
                    "executionSafe": 0,
                    "sameVenueExecutionEligible": 0,
                    "total": 0,
                },
                "skewedPositiveMarginCandidates": {
                    "executionSafe": 0,
                    "sameVenueExecutionEligible": 0,
                    "total": 0,
                },
                "candidateQuality": _empty_candidate_quality_payload(),
                "semanticDiagnostics": semantic_diagnostics,
                "venueCoverage": venue_coverage,
                "resolutionHorizon": _resolution_horizon_payload(
                    stats,
                    nodes={},
                    quotes={},
                    edges=[],
                ),
                "sampleCandidates": [],
                "negativeNearMisses": [],
            },
        }
    edges = snapshot["edges"]
    nodes = snapshot["nodes"]
    quotes = snapshot["quotes"]
    matched_node_ids = snapshot["matched_node_ids"]
    execution_safe_edges = 0
    same_venue_eligible_edges = 0
    for edge in edges:
        execution_safe_edges += bool(edge.execution_safe)
        same_venue_eligible_edges += bool(edge.same_venue_execution_eligible)
    profitability = _probe_edge_profitability(
        strategy,
        edges=edges,
        nodes=nodes,
        quotes=quotes,
        min_profit_margin=min_profit_margin,
        provider_quote_poll_stats=stats.get("provider_quote_poll_stats", {}),
    )
    coverage_book_devig = _probe_coverage_book_devig_diagnostics(
        strategy,
        coverage_diagnostics=stats.get("opportunity_graph_coverage_summary", {}),
        nodes=nodes,
        quotes=quotes,
        min_profit_margin=min_profit_margin,
    )
    venue_coverage = _venue_pair_coverage(
        strategy,
        edges=edges,
        nodes=nodes,
        quotes=quotes,
        matched_node_ids=matched_node_ids,
        candidate_venue_pairs=profitability["venue_pairs"],
        provider_quote_poll_stats=stats.get("provider_quote_poll_stats", {}),
    )

    sections = {
        "semanticMatchInstruments": len(matched_node_ids),
        "quotedSemanticMatchInstruments": sum(
            1 for node_id in matched_node_ids if node_id in quotes
        ),
        "executionSafeEdges": execution_safe_edges,
        "sameVenueExecutionEligibleEdges": same_venue_eligible_edges,
        "quotedEdges": profitability["quoted_edges"],
        "positiveMarginCandidates": {
            "executionSafe": profitability["positive_execution"],
            "sameVenueExecutionEligible": profitability["positive_same_venue"],
            "total": profitability["positive_execution"] + profitability["positive_same_venue"],
        },
        "thresholdMarginCandidates": {
            "executionSafe": profitability["threshold_execution"],
            "sameVenueExecutionEligible": profitability["threshold_same_venue"],
            "total": profitability["threshold_execution"] + profitability["threshold_same_venue"],
        },
        "skewedPositiveMarginCandidates": {
            "executionSafe": profitability["positive_execution_skewed"],
            "sameVenueExecutionEligible": profitability["positive_same_venue_skewed"],
            "total": (
                profitability["positive_execution_skewed"]
                + profitability["positive_same_venue_skewed"]
            ),
        },
        "candidateQuality": {
            "quotedEdges": profitability["quoted_edges"],
            "marginBands": profitability["margin_bands"],
            "ragBands": profitability["rag_bands"],
            "rejectionBuckets": profitability["rejection_buckets"],
            "timingFlags": profitability["timing_flags"],
            "freshnessProfiles": profitability["freshness_profiles"],
            "semanticBlockedReasons": profitability["semantic_blocked_reasons"],
            "semanticBlockedRelationships": profitability["semantic_blocked_relationships"],
            "blockerSamples": profitability["blocker_samples"],
            "venueQuoteHealth": profitability["venue_quote_health"],
            "latencyHistograms": {
                "quoteAgeSeconds": profitability["latency_histograms"]["quote_age_secs"],
                "fetchLatencySeconds": profitability["latency_histograms"]["fetch_latency_secs"],
                "pairSkewSeconds": profitability["latency_histograms"]["pair_skew_secs"],
            },
            "pairSkewByVenuePair": profitability["pair_skew_by_venue_pair"],
            "liveQuoteAgeSlo": {
                "maxQuoteAgeSeconds": profitability["live_quote_age_slo"]["max_quote_age_secs"],
                "observations": profitability["live_quote_age_slo"]["observations"],
                "violations": profitability["live_quote_age_slo"]["violations"],
            },
            "liveTimingSlo": {
                "quoteAge": {
                    "thresholdSeconds": profitability["live_timing_slo"]["quote_age"][
                        "threshold_secs"
                    ],
                    "observations": profitability["live_timing_slo"]["quote_age"]["observations"],
                    "violations": profitability["live_timing_slo"]["quote_age"]["violations"],
                },
                "fetchLatency": {
                    "thresholdMode": profitability["live_timing_slo"]["fetch_latency"][
                        "threshold_mode"
                    ],
                    "minThresholdSeconds": profitability["live_timing_slo"]["fetch_latency"][
                        "min_threshold_secs"
                    ],
                    "maxThresholdSeconds": profitability["live_timing_slo"]["fetch_latency"][
                        "max_threshold_secs"
                    ],
                    "observations": profitability["live_timing_slo"]["fetch_latency"][
                        "observations"
                    ],
                    "violations": profitability["live_timing_slo"]["fetch_latency"]["violations"],
                },
                "pairSkew": {
                    "thresholdMode": profitability["live_timing_slo"]["pair_skew"][
                        "threshold_mode"
                    ],
                    "minThresholdSeconds": profitability["live_timing_slo"]["pair_skew"][
                        "min_threshold_secs"
                    ],
                    "maxThresholdSeconds": profitability["live_timing_slo"]["pair_skew"][
                        "max_threshold_secs"
                    ],
                    "observations": profitability["live_timing_slo"]["pair_skew"]["observations"],
                    "violations": profitability["live_timing_slo"]["pair_skew"]["violations"],
                },
            },
            "sameVenueDryRun": {
                "passes": profitability["same_venue_dry_run"]["passes"],
                "failures": profitability["same_venue_dry_run"]["failures"],
                "failureReasons": profitability["same_venue_dry_run"]["failure_reasons"],
            },
            "feeAdjustment": {
                "evaluatedEdges": profitability["fee_adjustment"]["evaluated_edges"],
                "feeDragMargin": profitability["fee_adjustment"]["fee_drag_margin"],
                "impactBuckets": profitability["fee_adjustment"].get("impact_buckets", {}),
            },
            "devigDiagnostics": {
                "evaluatedEdges": profitability["devig_diagnostics"]["evaluated_edges"],
                "completeBooks": profitability["devig_diagnostics"]["complete_books"],
                "incompleteBooks": profitability["devig_diagnostics"]["incomplete_books"],
                "methodCounts": profitability["devig_diagnostics"]["method_counts"],
                "methodReasonCounts": profitability["devig_diagnostics"]["method_reason_counts"],
                "convergenceCounts": profitability["devig_diagnostics"]["convergence_counts"],
                "valueBuckets": profitability["devig_diagnostics"]["value_buckets"],
                "overround": profitability["devig_diagnostics"]["overround"],
                "vig": profitability["devig_diagnostics"]["vig"],
                "grossValueEdge": profitability["devig_diagnostics"]["gross_value_edge"],
                "feeAdjustedValueEdge": profitability["devig_diagnostics"][
                    "fee_adjusted_value_edge"
                ],
            },
            "coverageBookDevigDiagnostics": coverage_book_devig,
            "venuePairs": profitability["venue_pairs"],
            "marketFamilies": profitability["market_families"],
            "zeroCandidateVenuePairSamples": venue_coverage["zeroCandidateVenuePairs"],
            "zeroCandidateBlockerCounts": venue_coverage["zeroCandidateBlockerCounts"],
            "zeroCandidateFixtureProofBlockerCounts": venue_coverage[
                "zeroCandidateFixtureProofBlockerCounts"
            ],
            "topPositiveCandidates": profitability["sample_candidates"],
            "topSkewedPositiveCandidates": profitability["skewed_sample_candidates"],
            "topNegativeNearMisses": profitability["negative_near_misses"],
            "topValueEdgeCandidates": profitability["value_edge_candidates"],
            "topVigErasedCandidates": profitability["vig_erased_candidates"],
        },
        "semanticDiagnostics": semantic_diagnostics,
        "venueCoverage": venue_coverage,
        "resolutionHorizon": _resolution_horizon_payload(
            stats,
            nodes=nodes,
            quotes=quotes,
            edges=edges,
        ),
        "sampleCandidates": profitability["sample_candidates"],
        "negativeNearMisses": profitability["negative_near_misses"],
    }
    return {
        "profitability": profitability,
        "venueCoverage": venue_coverage,
        "sections": sections,
    }


def _instrument_refresh_payload(stats: dict[str, object]) -> dict[str, object]:
    latency_diagnostics = stats.get("latency_diagnostics") or {}
    if not isinstance(latency_diagnostics, dict):
        latency_diagnostics = {}
    return {
        "requests": int(stats.get("instrument_refresh_requests") or 0),
        "failures": int(stats.get("instrument_refresh_failures") or 0),
        "added": int(stats.get("instrument_refresh_added") or 0),
        "removed": int(stats.get("instrument_refresh_removed") or 0),
        "delistedRemoved": int(stats.get("instrument_refresh_delisted_removed") or 0),
        "reconciles": int(stats.get("instrument_refresh_reconciles") or 0),
        "graphRebuilds": int(stats.get("instrument_refresh_graph_rebuilds") or 0),
        "staleQuoteTriggers": int(stats.get("instrument_refresh_stale_triggers") or 0),
        "quoteUnsubscribeRequests": int(stats.get("quote_unsubscribe_requests") or 0),
        "venues": stats.get("instrument_refresh_by_venue", {}),
        "reconcileLatency": latency_diagnostics.get("instrument_refresh_reconcile", {}),
    }


def _resolution_horizon_payload(
    stats: dict[str, object],
    *,
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    edges: list[object],
) -> dict[str, object]:
    horizon_hours = stats.get("max_resolution_horizon_hours")
    if horizon_hours is None:
        return _empty_resolution_horizon_payload()
    try:
        horizon = timedelta(hours=float(horizon_hours))
    except (TypeError, ValueError):
        horizon = timedelta(hours=48)
    now = datetime.now(tz=UTC)
    state_by_node: dict[Any, str] = {}
    samples: dict[str, list[str]] = {
        "inside": [],
        "recent_past": [],
        "outside": [],
        "stale_past": [],
        "unknown": [],
    }
    event_keys_by_state: dict[str, set[str]] = {
        "inside": set(),
        "recent_past": set(),
        "outside": set(),
        "stale_past": set(),
        "unknown": set(),
    }
    for node_id, node in nodes.items():
        instrument = getattr(node, "instrument", None)
        state = _resolution_horizon_state(instrument, now=now, horizon=horizon)
        state_by_node[node_id] = state
        event_key = _canonical_probe_event_key_no_time(node)
        if event_key:
            event_keys_by_state[state].add(event_key)
            if len(samples[state]) < 5:
                samples[state].append(event_key)
    quoted_candidates_inside = 0
    blocked_due_horizon = 0
    for edge in edges:
        source = getattr(edge, "source_node_id", None)
        target = getattr(edge, "target_node_id", None)
        if source not in quotes or target not in quotes:
            continue
        states = {state_by_node.get(source, "unknown"), state_by_node.get(target, "unknown")}
        if states <= {"inside", "recent_past"}:
            quoted_candidates_inside += 1
        elif "outside" in states or "stale_past" in states:
            blocked_due_horizon += 1
    return {
        "enabled": True,
        "maxResolutionHorizonHours": float(horizon_hours),
        "eventsInsideHorizon": len(event_keys_by_state["inside"]),
        "recentPastEvents": len(event_keys_by_state["recent_past"]),
        "eventsOutsideHorizon": len(event_keys_by_state["outside"]),
        "stalePastEvents": len(event_keys_by_state["stale_past"]),
        "unknownResolutionEvents": len(event_keys_by_state["unknown"]),
        "quotedCandidatesInsideHorizon": quoted_candidates_inside,
        "blockedCandidatesDueHorizon": blocked_due_horizon,
        "insideHorizonEventSamples": samples["inside"],
        "recentPastEventSamples": samples["recent_past"],
        "outsideHorizonEventSamples": samples["outside"],
        "stalePastEventSamples": samples["stale_past"],
        "unknownResolutionEventSamples": samples["unknown"],
    }


def _empty_resolution_horizon_payload() -> dict[str, object]:
    return {
        "enabled": False,
        "maxResolutionHorizonHours": None,
        "eventsInsideHorizon": 0,
        "recentPastEvents": 0,
        "eventsOutsideHorizon": 0,
        "stalePastEvents": 0,
        "unknownResolutionEvents": 0,
        "quotedCandidatesInsideHorizon": 0,
        "blockedCandidatesDueHorizon": 0,
        "insideHorizonEventSamples": [],
        "recentPastEventSamples": [],
        "outsideHorizonEventSamples": [],
        "stalePastEventSamples": [],
        "unknownResolutionEventSamples": [],
    }


def _resolution_horizon_state(
    instrument: object | None,
    *,
    now: datetime,
    horizon: timedelta,
) -> str:
    start_time = _probe_parsed_start_time(instrument)
    if start_time is None:
        return "unknown"
    stale_grace = timedelta(hours=6)
    if start_time < now - stale_grace:
        return "stale_past"
    if start_time < now:
        return "recent_past"
    return "inside" if start_time <= now + horizon else "outside"


def _probe_parsed_start_time(instrument: object | None) -> datetime | None:
    parser = getattr(instrument, "parsed_start_time", None)
    if callable(parser):
        try:
            parsed = parser()
        except (AttributeError, TypeError, ValueError):
            parsed = None
        if isinstance(parsed, datetime):
            return (
                parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            )
    start_time = getattr(instrument, "start_time", None)
    if not start_time:
        return None
    try:
        parsed = datetime.fromisoformat(str(start_time))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _runtime_latency_diagnostics(
    stats: dict[str, object],
    profitability: dict[str, object],
) -> dict[str, object]:
    diagnostics = dict(stats.get("latency_diagnostics") or {})
    candidate_decision = diagnostics.get("candidate_decision")
    candidate_decision_count = (
        int(candidate_decision.get("count") or 0) if isinstance(candidate_decision, dict) else 0
    )
    probe_candidate_decision = profitability.get("candidate_decision_latency")
    diagnostics["candidate_decision_source"] = "strategy"
    if candidate_decision_count == 0 and isinstance(probe_candidate_decision, dict):
        diagnostics["candidate_decision"] = probe_candidate_decision
        diagnostics["candidate_decision_source"] = "runtime_probe"
    diagnostics["runtime_probe_candidate_decision"] = (
        probe_candidate_decision if isinstance(probe_candidate_decision, dict) else {}
    )
    quote_age_by_venue = profitability.get("quote_age_by_venue")
    diagnostics["quoteAgeByVenue"] = (
        quote_age_by_venue if isinstance(quote_age_by_venue, dict) else {}
    )
    diagnostics["streamDormantLegCount"] = int(
        profitability.get("stream_dormant_leg_count") or 0,
    )
    diagnostics["sloStatus"] = _runtime_latency_slo_status(diagnostics, profitability)
    diagnostics["diagnosticWarnings"] = _runtime_latency_diagnostic_warnings(
        diagnostics,
        profitability,
    )
    return diagnostics


def _runtime_latency_slo_status(
    diagnostics: dict[str, object],
    profitability: dict[str, object],
) -> dict[str, object]:
    live_timing = profitability.get("live_timing_slo")
    live_timing = live_timing if isinstance(live_timing, dict) else {}
    histograms = profitability.get("latency_histograms")
    histograms = histograms if isinstance(histograms, dict) else {}
    quote_age = _runtime_slo_section_status(
        live_timing.get("quote_age"),
        threshold_key="threshold_secs",
        fallback=_runtime_histogram_slo_status(
            histograms.get("quote_age_secs"),
            threshold_seconds=5.0,
        ),
    )
    fetch_latency = _runtime_slo_section_status(
        live_timing.get("fetch_latency"),
        fallback=_runtime_histogram_slo_status(
            histograms.get("fetch_latency_secs"),
            threshold_seconds=5.0,
            ms_histogram=diagnostics.get("quote_fetch_latency"),
        ),
    )
    pair_skew = _runtime_slo_section_status(
        live_timing.get("pair_skew"),
        fallback=_runtime_histogram_slo_status(
            histograms.get("pair_skew_secs"),
            threshold_seconds=1.0,
        ),
    )
    stages = {
        "quoteReceiveObserved": _latency_count(diagnostics.get("quote_event_to_strategy")) > 0
        or _latency_count(diagnostics.get("quote_publish_to_strategy")) > 0,
        "graphScanObserved": _latency_count(diagnostics.get("graph_scan")) > 0,
        "candidateDecisionObserved": _latency_count(diagnostics.get("candidate_decision")) > 0,
        "providerLatencyObserved": _latency_count(
            (histograms.get("fetch_latency_secs") if isinstance(histograms, dict) else {}),
        )
        > 0
        or _latency_count(diagnostics.get("quote_fetch_latency")) > 0,
        "candidateDecisionSource": diagnostics.get("candidate_decision_source"),
    }
    statuses = [
        str(quote_age.get("status") or "unknown"),
        str(fetch_latency.get("status") or "unknown"),
        str(pair_skew.get("status") or "unknown"),
    ]
    if any(status == "warn" for status in statuses):
        overall = "warn"
    elif any(status == "pass" for status in statuses):
        overall = "pass"
    else:
        overall = "unknown"
    missing_stages = [
        label
        for label, observed in (
            ("quote_receive", stages["quoteReceiveObserved"]),
            ("graph_scan", stages["graphScanObserved"]),
            ("candidate_decision", stages["candidateDecisionObserved"]),
            ("provider_latency", stages["providerLatencyObserved"]),
        )
        if not observed
    ]
    return {
        "overall": overall,
        "quoteAge": quote_age,
        "fetchLatency": fetch_latency,
        "pairSkew": pair_skew,
        "strategyLatency": stages,
        "missingStages": missing_stages,
    }


def _runtime_slo_section_status(
    section: object,
    *,
    threshold_key: str = "max_threshold_secs",
    fallback: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = section if isinstance(section, dict) else {}
    observations = int(payload.get("observations") or 0)
    if observations <= 0 and fallback and int(fallback.get("observations") or 0) > 0:
        return fallback
    violations = int(payload.get("violations") or 0)
    if observations <= 0:
        status = "unknown"
    elif violations > 0:
        status = "warn"
    else:
        status = "pass"
    threshold = payload.get(threshold_key)
    return {
        "status": status,
        "observations": observations,
        "violations": violations,
        "violationRate": round((violations / observations) if observations else 0.0, 6),
        "thresholdSeconds": threshold,
        "minThresholdSeconds": payload.get("min_threshold_secs"),
        "maxThresholdSeconds": payload.get("max_threshold_secs"),
        "thresholdMode": payload.get("threshold_mode"),
    }


def _runtime_histogram_slo_status(
    histogram: object,
    *,
    threshold_seconds: float,
    ms_histogram: object | None = None,
) -> dict[str, object]:
    payload = histogram if isinstance(histogram, dict) else {}
    observations = int(payload.get("count") or 0)
    p95 = float(payload.get("p95") or 0.0)
    max_value = float(payload.get("max") or 0.0)
    if observations <= 0 and isinstance(ms_histogram, dict):
        observations = int(ms_histogram.get("count") or 0)
        p95 = float(ms_histogram.get("p95_ms") or 0.0) / 1000.0
        max_value = float(ms_histogram.get("max_ms") or 0.0) / 1000.0
    violations = observations if observations > 0 and p95 > threshold_seconds else 0
    status = "unknown" if observations <= 0 else "warn" if violations else "pass"
    return {
        "status": status,
        "observations": observations,
        "violations": violations,
        "violationRate": 1.0 if violations else 0.0,
        "thresholdSeconds": threshold_seconds,
        "minThresholdSeconds": None,
        "maxThresholdSeconds": None,
        "thresholdMode": "histogram_p95",
        "p95Seconds": p95,
        "maxObservedSeconds": max_value,
        "outlierMaxExceeded": observations > 0 and max_value > threshold_seconds,
    }


def _runtime_latency_diagnostic_warnings(
    diagnostics: dict[str, object],
    profitability: dict[str, object],
) -> list[str]:
    quoted_edges = int(profitability.get("quoted_edges") or 0)
    positive = int(profitability.get("positive_execution") or 0) + int(
        profitability.get("positive_same_venue") or 0,
    )
    threshold = int(profitability.get("threshold_execution") or 0) + int(
        profitability.get("threshold_same_venue") or 0,
    )
    has_activity = quoted_edges > 0 or positive > 0 or threshold > 0
    if not has_activity:
        return []
    warnings: list[str] = []
    if (
        _latency_count(diagnostics.get("quote_event_to_strategy")) == 0
        and _latency_count(
            diagnostics.get("quote_publish_to_strategy"),
        )
        == 0
    ):
        warnings.append("missing_quote_receive_latency")
    if _latency_count(diagnostics.get("graph_scan")) == 0:
        warnings.append("missing_graph_scan_latency")
    if _latency_count(diagnostics.get("candidate_decision")) == 0:
        warnings.append("missing_candidate_decision_latency")
    histograms = profitability.get("latency_histograms")
    histograms = histograms if isinstance(histograms, dict) else {}
    if (
        _latency_count(histograms.get("fetch_latency_secs")) == 0
        and _latency_count(diagnostics.get("quote_fetch_latency")) == 0
    ):
        warnings.append("missing_provider_latency")
    return warnings


def _latency_count(value: object) -> int:
    return int(value.get("count") or 0) if isinstance(value, dict) else 0


def _runtime_probe_satisfied(
    payload: dict[str, object],
    *,
    min_connected_nodes: int,
    min_match_instruments: int,
    min_quoted_match_instruments: int = 0,
    min_quoted_edges: int = 0,
    min_positive_margin_candidates: int = 0,
    min_cross_venue_candidates: int = 0,
    require_cross_venue_candidates_or_blockers: bool = False,
    min_quoted_node_counts: dict[str, int] | None = None,
    require_rust_semantic_topology: bool = False,
    allow_subscription_fallback: bool = False,
) -> bool:
    positive_candidates = payload["positiveMarginCandidates"]["total"]
    venue_coverage = payload.get("venueCoverage") or {}
    if not isinstance(venue_coverage, dict):
        venue_coverage = {}
    quoted_node_counts = venue_coverage.get("quotedNodeCounts") or {}
    if not isinstance(quoted_node_counts, dict):
        quoted_node_counts = {}
    venue_quoted_ok = all(
        int(quoted_node_counts.get(venue, 0) or 0) >= minimum
        for venue, minimum in (min_quoted_node_counts or {}).items()
    )
    cross_venue_candidate_count = int(
        venue_coverage.get("crossVenueCandidateCount") or 0,
    )
    if require_cross_venue_candidates_or_blockers:
        cross_venue_ok = cross_venue_candidate_count >= max(
            1,
            min_cross_venue_candidates,
        ) or _has_cross_venue_blocker(venue_coverage, payload)
    else:
        cross_venue_ok = cross_venue_candidate_count >= min_cross_venue_candidates
    rust_semantic_topology_ok = (
        payload.get("graphEngine") == "rust"
        and payload.get("topologySource") == "rust_semantic"
        and int(payload.get("semanticTemplateCount") or 0) > 0
    )
    wiring_ok = (
        payload["connectedNodes"] >= min_connected_nodes
        and payload["semanticMatchInstruments"] >= min_match_instruments
        and (rust_semantic_topology_ok or not require_rust_semantic_topology)
    )
    quoted_flow_ok = (
        payload["quotedSemanticMatchInstruments"] >= min_quoted_match_instruments
        and payload["quotedEdges"] >= min_quoted_edges
        and positive_candidates >= min_positive_margin_candidates
        and cross_venue_ok
        and venue_quoted_ok
    )
    if wiring_ok and quoted_flow_ok:
        return True
    if not (allow_subscription_fallback and wiring_ok):
        return False
    fallback_venues = _subscription_fallback_venues(
        payload,
        min_quoted_node_counts=min_quoted_node_counts or {},
    )
    if not fallback_venues:
        return False
    quote_observation = payload.get("quoteObservationState")
    quote_observation_status = (
        str(quote_observation.get("status") or "") if isinstance(quote_observation, dict) else ""
    )
    payload["subscriptionFallback"] = {
        "engaged": True,
        "venues": fallback_venues,
        "quoteObservationStatus": quote_observation_status,
    }
    logger.warning(
        "Runtime probe passed via subscription fallback: quoted-node minimums unmet "
        "for %s but quote subscriptions cover the minimums "
        "(quote_observation_status=%s); quoted-book coverage checks were waived",
        ",".join(fallback_venues),
        quote_observation_status,
    )
    return True


_SUBSCRIPTION_FALLBACK_QUOTE_STATUSES = frozenset(
    {"subscribed_but_no_quotes", "partial_subscription_quote_gap"},
)


def _subscription_fallback_venues(
    payload: dict[str, object],
    *,
    min_quoted_node_counts: dict[str, int],
) -> list[str]:
    venue_coverage = payload.get("venueCoverage") or {}
    if not isinstance(venue_coverage, dict):
        return []
    quote_observation = payload.get("quoteObservationState") or {}
    if not isinstance(quote_observation, dict):
        return []
    if str(quote_observation.get("status") or "") not in _SUBSCRIPTION_FALLBACK_QUOTE_STATUSES:
        return []
    quoted_node_counts = _int_mapping(venue_coverage.get("quotedNodeCounts"))
    subscription_counts = _int_mapping(venue_coverage.get("quoteSubscriptionCounts"))
    fallback_venues: list[str] = []
    for venue, minimum in sorted(min_quoted_node_counts.items()):
        if quoted_node_counts.get(venue, 0) >= minimum:
            continue
        if subscription_counts.get(venue, 0) < minimum:
            return []
        fallback_venues.append(venue)
    return fallback_venues


def _has_cross_venue_blocker(
    venue_coverage: dict[str, object],
    payload: dict[str, object],
) -> bool:
    reports = list(venue_coverage.get("zeroCandidateVenuePairs") or [])
    candidate_quality = payload.get("candidateQuality") or {}
    if isinstance(candidate_quality, dict):
        reports.extend(candidate_quality.get("zeroCandidateVenuePairSamples") or [])
    for report in reports:
        if not isinstance(report, dict):
            continue
        pair = str(report.get("venuePair") or "")
        if "->" not in pair:
            continue
        source, target = pair.split("->", maxsplit=1)
        if source == target:
            continue
        blocker = str(report.get("blockerReason") or report.get("reason") or "")
        if blocker and blocker != "missing_instruments":
            return True
    return False


def _parse_venue_count_requirements(values: list[str] | tuple[str, ...]) -> dict[str, int]:
    requirements: dict[str, int] = {}
    for raw_value in values:
        if not raw_value:
            continue
        if ":" not in raw_value:
            msg = f"Venue count requirement must use VENUE:COUNT, got {raw_value!r}"
            raise ValueError(msg)
        raw_venue, raw_count = raw_value.split(":", maxsplit=1)
        venue = raw_venue.strip().upper()
        if not venue:
            msg = f"Venue count requirement has empty venue: {raw_value!r}"
            raise ValueError(msg)
        try:
            count = int(raw_count)
        except ValueError as exc:
            msg = f"Venue count requirement has invalid count: {raw_value!r}"
            raise ValueError(msg) from exc
        if count < 0:
            msg = f"Venue count requirement count must be non-negative: {raw_value!r}"
            raise ValueError(msg)
        requirements[venue] = count
    return requirements


def _empty_candidate_quality_payload() -> dict[str, object]:
    return {
        "quotedEdges": 0,
        "marginBands": {},
        "ragBands": {},
        "rejectionBuckets": {},
        "timingFlags": {},
        "freshnessProfiles": {},
        "semanticBlockedReasons": {},
        "semanticBlockedRelationships": {},
        "blockerSamples": {},
        "venueQuoteHealth": {},
        "latencyHistograms": {
            "quoteAgeSeconds": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
            "fetchLatencySeconds": {
                "count": 0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "max": 0.0,
            },
            "pairSkewSeconds": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
        },
        "liveQuoteAgeSlo": {
            "maxQuoteAgeSeconds": 5.0,
            "observations": 0,
            "violations": 0,
        },
        "liveTimingSlo": {
            "quoteAge": {
                "thresholdSeconds": 5.0,
                "observations": 0,
                "violations": 0,
            },
            "fetchLatency": {
                "thresholdMode": "per_candidate",
                "minThresholdSeconds": None,
                "maxThresholdSeconds": None,
                "observations": 0,
                "violations": 0,
            },
            "pairSkew": {
                "thresholdMode": "per_candidate",
                "minThresholdSeconds": None,
                "maxThresholdSeconds": None,
                "observations": 0,
                "violations": 0,
            },
        },
        "sameVenueDryRun": {
            "passes": 0,
            "failures": 0,
            "failureReasons": {},
        },
        "feeAdjustment": {
            "evaluatedEdges": 0,
            "feeDragMargin": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
            "impactBuckets": {},
        },
        "devigDiagnostics": {
            "evaluatedEdges": 0,
            "completeBooks": 0,
            "incompleteBooks": 0,
            "methodCounts": {},
            "methodReasonCounts": {},
            "convergenceCounts": {},
            "valueBuckets": {},
            "overround": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
            "vig": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
            "grossValueEdge": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
            "feeAdjustedValueEdge": {
                "count": 0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "max": 0.0,
            },
        },
        "venuePairs": {},
        "marketFamilies": {},
        "zeroCandidateVenuePairSamples": [],
        "zeroCandidateBlockerCounts": {},
        "zeroCandidateFixtureProofBlockerCounts": {},
        "topPositiveCandidates": [],
        "topSkewedPositiveCandidates": [],
        "topNegativeNearMisses": [],
        "topValueEdgeCandidates": [],
        "topVigErasedCandidates": [],
        "coverageBookDevigDiagnostics": _empty_coverage_book_devig_payload(),
    }


def _snapshot_probe_graph_state(graph, *, attempts: int = 5) -> dict[str, object] | None:
    # The probe thread copies the live graph dicts while the strategy thread mutates them
    # during rebuilds. On a large, actively-refreshing graph that races into
    # "RuntimeError: dictionary changed size during iteration", which previously collapsed
    # to an empty snapshot (edges=0) and made a healthy node read as idle to the
    # runtime-verify gate. The race is transient, so retry a few times before giving up.
    for attempt in range(1, attempts + 1):
        try:
            return {
                "edges": list(graph.edges_by_id.values()),
                "nodes": dict(graph.nodes_by_id),
                "quotes": dict(graph.quotes_by_node_id),
                "matched_node_ids": {
                    node_id for node_id, edge_ids in graph.edge_ids_by_node_id.items() if edge_ids
                },
            }
        except RuntimeError:
            if attempt >= attempts:
                logger.warning(
                    "Runtime probe graph snapshot failed after %d attempts "
                    "(graph mutating concurrently); emitting empty snapshot",
                    attempts,
                )
                return None
            time.sleep(0.05)
    return None


def _venue_pair_coverage(
    strategy,
    *,
    edges: Any,
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    matched_node_ids: set[Any],
    candidate_venue_pairs: Any,
    provider_quote_poll_stats: object = None,
) -> dict[str, object]:
    venues = _enabled_probe_venues(strategy, nodes)
    node_counts: Counter[str] = Counter()
    quoted_node_counts: Counter[str] = Counter()
    matched_node_counts: Counter[str] = Counter()
    quoted_matched_node_counts: Counter[str] = Counter()
    quote_subscription_counts = _quote_subscription_counts_by_venue(strategy)
    edge_counts: Counter[str] = Counter()
    quoted_edge_counts: Counter[str] = Counter()
    unquoted_semantic_samples: dict[str, list[dict[str, object]]] = {}

    for node_id, node in nodes.items():
        venue = _probe_node_venue(node)
        if venue is None:
            continue
        node_counts[venue] += 1
        if node_id in quotes:
            quoted_node_counts[venue] += 1
        if node_id in matched_node_ids:
            matched_node_counts[venue] += 1
            if node_id in quotes:
                quoted_matched_node_counts[venue] += 1
            else:
                samples = unquoted_semantic_samples.setdefault(venue, [])
                if len(samples) < 5:
                    samples.append(
                        {
                            "instrumentId": str(getattr(node.instrument, "id", "")),
                            "eventKey": _probe_event_key_no_time(node),
                            "pattern": _probe_pattern_payload(node),
                        },
                    )

    for edge in edges:
        source_node = nodes.get(edge.source_node_id)
        target_node = nodes.get(edge.target_node_id)
        source_venue = _probe_node_venue(source_node)
        target_venue = _probe_node_venue(target_node)
        if source_venue is None or target_venue is None:
            continue
        pair = f"{source_venue}->{target_venue}"
        edge_counts[pair] += 1
        if _quoted_probe_edge(edge, nodes, quotes) is not None:
            quoted_edge_counts[pair] += 1

    all_pairs = [
        pair
        for source in venues
        for target in venues
        if _venue_pair_matches_execution_venue_mode(strategy, pair := f"{source}->{target}")
    ]
    candidate_counts = _venue_pair_candidate_counts(candidate_venue_pairs)
    zero_pairs = [
        _zero_venue_pair_report(
            pair,
            strategy=strategy,
            nodes=nodes,
            quotes=quotes,
            node_counts=node_counts,
            edge_counts=edge_counts,
            quoted_edge_counts=quoted_edge_counts,
            candidate_count=candidate_counts.get(pair, 0),
        )
        for pair in all_pairs
        if candidate_counts.get(pair, 0) == 0
    ]
    zero_pair_blocker_counts = Counter(
        str(report.get("blockerReason") or report.get("reason") or "unknown")
        for report in zero_pairs
        if isinstance(report, dict)
    )
    zero_pair_fixture_proof_blocker_counts = _zero_pair_fixture_proof_blocker_counts(zero_pairs)
    cross_venue_candidate_count = sum(
        count for pair, count in candidate_counts.items() if _is_cross_venue_pair(pair)
    )
    quote_subscription_gap_counts = {
        venue: max(
            int(quote_subscription_counts.get(venue, 0) or 0)
            - int(quoted_node_counts.get(venue, 0) or 0),
            0,
        )
        for venue in venues
    }
    config = getattr(strategy, "_config", None)
    quote_subscription_limits = {
        str(venue).upper(): int(limit)
        for venue, limit in (
            getattr(config, "semantic_quote_subscription_limit_by_venue", {}) or {}
        ).items()
    }
    quote_subscription_limit_exceeded_counts = {
        venue: max(
            int(quote_subscription_counts.get(venue, 0) or 0)
            - int(quote_subscription_limits.get(venue, 0) or 0),
            0,
        )
        for venue in venues
        if quote_subscription_limits.get(venue) is not None
        and int(quote_subscription_counts.get(venue, 0) or 0)
        > int(quote_subscription_limits.get(venue, 0) or 0)
    }
    provider_subscribed_counts = {
        str(venue).upper(): int(poll_stats.get("subscribed_instrument_count") or 0)
        for venue, poll_stats in (
            provider_quote_poll_stats if isinstance(provider_quote_poll_stats, dict) else {}
        ).items()
        if isinstance(poll_stats, dict)
    }
    unquoted_semantic_match_counts = {
        venue: max(
            int(matched_node_counts.get(venue, 0) or 0)
            - int(quoted_matched_node_counts.get(venue, 0) or 0),
            0,
        )
        for venue in venues
    }
    cross_venue_quote_readiness = _cross_venue_quote_readiness(
        all_pairs=all_pairs,
        nodes=nodes,
        quotes=quotes,
        node_counts=node_counts,
        edge_counts=edge_counts,
        quoted_edge_counts=quoted_edge_counts,
        candidate_counts=candidate_counts,
    )

    return {
        "enabledVenues": venues,
        "nodeCounts": {venue: node_counts.get(venue, 0) for venue in venues},
        "eventKeyCounts": {
            venue: len(_event_keys_for_venue(nodes, venue) - {""}) for venue in venues
        },
        "eventSportCounts": {
            venue: dict(sorted(_event_sport_counts(_event_keys_for_venue(nodes, venue)).items()))
            for venue in venues
        },
        "quoteSubscriptionCounts": {
            venue: quote_subscription_counts.get(venue, 0) for venue in venues
        },
        "quoteSubscriptionLimits": {
            venue: quote_subscription_limits.get(venue)
            for venue in venues
            if quote_subscription_limits.get(venue) is not None
        },
        "quoteSubscriptionLimitExceededCounts": quote_subscription_limit_exceeded_counts,
        "providerSubscribedCounts": {
            venue: provider_subscribed_counts[venue]
            for venue in venues
            if venue in provider_subscribed_counts
        },
        "providerVsStrategySubscriptionDrift": {
            venue: provider_subscribed_counts[venue]
            - int(quote_subscription_counts.get(venue, 0) or 0)
            for venue in venues
            if venue in provider_subscribed_counts
        },
        "quoteSubscriptionGapCounts": quote_subscription_gap_counts,
        "venuesWithSubscriptionQuoteGap": [
            venue for venue in venues if quote_subscription_gap_counts.get(venue, 0) > 0
        ],
        "quotedNodeCounts": {venue: quoted_node_counts.get(venue, 0) for venue in venues},
        "semanticMatchedNodeCounts": {venue: matched_node_counts.get(venue, 0) for venue in venues},
        "quotedSemanticMatchedNodeCounts": {
            venue: quoted_matched_node_counts.get(venue, 0) for venue in venues
        },
        "unquotedSemanticMatchedNodeCounts": unquoted_semantic_match_counts,
        "unquotedSemanticMatchedNodeSamples": {
            venue: unquoted_semantic_samples.get(venue, []) for venue in venues
        },
        "edgeCounts": {pair: edge_counts.get(pair, 0) for pair in all_pairs},
        "quotedEdgeCounts": {pair: quoted_edge_counts.get(pair, 0) for pair in all_pairs},
        "candidateCounts": {pair: candidate_counts.get(pair, 0) for pair in all_pairs},
        "crossVenueCandidateCount": cross_venue_candidate_count,
        "zeroCandidateBlockerCounts": dict(sorted(zero_pair_blocker_counts.items())),
        "zeroCandidateFixtureProofBlockerCounts": dict(
            sorted(zero_pair_fixture_proof_blocker_counts.items()),
        ),
        "crossVenuePairsWithCandidates": [
            pair
            for pair in all_pairs
            if _is_cross_venue_pair(pair) and candidate_counts.get(pair, 0) > 0
        ],
        "zeroCandidateVenuePairs": zero_pairs,
        "crossVenueQuoteReadiness": cross_venue_quote_readiness,
    }


def _probe_quote_observation_state(
    stats: dict[str, object],
    venue_coverage: dict[str, object],
) -> dict[str, object]:
    quote_subscription_counts = _int_mapping(venue_coverage.get("quoteSubscriptionCounts"))
    quoted_node_counts = _int_mapping(venue_coverage.get("quotedNodeCounts"))
    quote_gap_counts = _int_mapping(venue_coverage.get("quoteSubscriptionGapCounts"))
    quoted_semantic_counts = _int_mapping(venue_coverage.get("quotedSemanticMatchedNodeCounts"))
    unquoted_semantic_counts = _int_mapping(venue_coverage.get("unquotedSemanticMatchedNodeCounts"))
    total_subscriptions = sum(quote_subscription_counts.values())
    total_quoted_nodes = sum(quoted_node_counts.values())
    total_quote_gaps = sum(quote_gap_counts.values())
    total_quoted_semantic_nodes = sum(quoted_semantic_counts.values())

    if total_subscriptions <= 0:
        status = "no_quote_subscriptions"
        health = "warn"
    elif total_quoted_nodes <= 0:
        status = "subscribed_but_no_quotes"
        health = "fail"
    elif total_quote_gaps > 0:
        status = "partial_subscription_quote_gap"
        health = "warn"
    else:
        status = "quotes_observed"
        health = "pass"

    return {
        "status": status,
        "reason": status,
        "health": health,
        "totalQuoteSubscriptions": total_subscriptions,
        "totalQuotedNodes": total_quoted_nodes,
        "totalQuotedSemanticNodes": total_quoted_semantic_nodes,
        "totalQuoteSubscriptionGaps": total_quote_gaps,
        "quoteSubscriptionCounts": quote_subscription_counts,
        "quotedNodeCounts": quoted_node_counts,
        "quoteSubscriptionGapCounts": quote_gap_counts,
        "quotedSemanticMatchedNodeCounts": quoted_semantic_counts,
        "unquotedSemanticMatchedNodeCounts": unquoted_semantic_counts,
        "venuesWithSubscriptionQuoteGap": list(
            venue_coverage.get("venuesWithSubscriptionQuoteGap") or [],
        ),
        "quoteSubscriptionLimitExceededCounts": _int_mapping(
            venue_coverage.get("quoteSubscriptionLimitExceededCounts"),
        ),
        "unquotedSemanticMatchedNodeSamples": venue_coverage.get(
            "unquotedSemanticMatchedNodeSamples",
            {},
        ),
        "providerQuotePollStats": stats.get("provider_quote_poll_stats", {}),
        "graphQuoteStates": stats.get("opportunity_graph_quote_states", {}),
        "subscribedInstruments": int(stats.get("subscribed_instruments") or 0),
        "instrumentCacheMiss": int(stats.get("instrument_cache_miss") or 0),
        "quoteOddsRejected": int(stats.get("quote_odds_rejected") or 0),
        "instrumentCacheMissCounts": _int_mapping(stats.get("instrument_cache_miss_by_venue")),
        "quoteOddsRejectedCounts": _int_mapping(stats.get("quote_odds_rejected_by_venue")),
    }


def _int_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, count in value.items():
        try:
            result[str(key)] = int(count or 0)
        except (TypeError, ValueError):
            result[str(key)] = 0
    return result


def _zero_pair_fixture_proof_blocker_counts(
    zero_pairs: list[dict[str, object]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for report in zero_pairs:
        fixture_proof_counts = report.get("fixtureProofBlockerCounts")
        if not isinstance(fixture_proof_counts, dict):
            continue
        for reason, count in fixture_proof_counts.items():
            counts[str(reason)] += int(count or 0)
    return counts


def _cross_venue_quote_readiness(
    *,
    all_pairs: list[str],
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    node_counts: Counter[str],
    edge_counts: Counter[str],
    quoted_edge_counts: Counter[str],
    candidate_counts: dict[str, int],
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for pair in all_pairs:
        if not _is_cross_venue_pair(pair):
            continue
        source, target = pair.split("->", maxsplit=1)
        source_event_keys = _event_keys_for_venue(nodes, source)
        target_event_keys = _event_keys_for_venue(nodes, target)
        common_event_keys = sorted((source_event_keys & target_event_keys) - {""})
        quote_coverage = _common_event_quote_coverage(
            source=source,
            target=target,
            nodes=nodes,
            quotes=quotes,
            common_event_keys=set(common_event_keys),
        )
        common_count = len(common_event_keys)
        fully_quoted_count = int(quote_coverage.get("fullyQuotedCommonEventKeyCount") or 0)
        edge_count = int(edge_counts.get(pair, 0) or 0)
        quoted_edge_count = int(quoted_edge_counts.get(pair, 0) or 0)
        candidate_count = int(candidate_counts.get(pair, 0) or 0)
        if node_counts.get(source, 0) == 0 or node_counts.get(target, 0) == 0:
            status = "missing_instruments"
            health = "warn"
        elif common_count == 0:
            status = "no_common_fixture"
            health = "warn"
        elif fully_quoted_count == 0:
            status = "common_fixture_unquoted"
            health = "warn"
        elif quoted_edge_count == 0:
            status = "quoted_common_fixture_no_quoted_semantic_edge"
            health = "warn"
        elif candidate_count == 0:
            status = "quoted_cross_venue_no_candidate"
            health = "pass"
        else:
            status = "cross_venue_candidates_observed"
            health = "pass"
        reports.append(
            {
                "venuePair": pair,
                "status": status,
                "health": health,
                "sourceNodeCount": int(node_counts.get(source, 0) or 0),
                "targetNodeCount": int(node_counts.get(target, 0) or 0),
                "commonEventKeyCount": common_count,
                "commonEventKeySamples": common_event_keys[:5],
                "edgeCount": edge_count,
                "quotedEdgeCount": quoted_edge_count,
                "candidateCount": candidate_count,
                **quote_coverage,
            },
        )
    return reports


def _quote_subscription_counts_by_venue(strategy) -> Counter[str]:
    counts: Counter[str] = Counter()
    subscribed_ids = getattr(strategy, "_quote_subscribed_instrument_ids", ()) or ()
    for instrument_id in subscribed_ids:
        venue = getattr(instrument_id, "venue", None)
        if venue is None:
            text = str(instrument_id)
            venue = text.rsplit(".", maxsplit=1)[-1] if "." in text else ""
        if venue:
            counts[str(venue).upper()] += 1
    return counts


def _venue_pair_candidate_counts(candidate_venue_pairs: Any) -> dict[str, int]:
    if not isinstance(candidate_venue_pairs, dict):
        return {}
    return {
        str(pair): sum(int(value) for value in buckets.values())
        for pair, buckets in candidate_venue_pairs.items()
        if isinstance(buckets, dict)
    }


def _venue_pair_matches_execution_venue_mode(strategy, pair: str) -> bool:
    source, target = pair.split("->", maxsplit=1)
    config = getattr(strategy, "_config", None)
    mode = str(getattr(config, "execution_venue_mode", "all") or "all").strip().lower()
    if mode == "cross_venue":
        return source != target
    if mode == "same_venue":
        return source == target
    return True


def _enabled_probe_venues(strategy, nodes: dict[Any, object]) -> list[str]:
    config = getattr(strategy, "_config", None)
    enabled_venues = {
        str(venue).upper() for venue in getattr(config, "enabled_venues", ()) or () if str(venue)
    }
    for node in nodes.values():
        venue = _probe_node_venue(node)
        if venue:
            enabled_venues.add(venue)
    return sorted(enabled_venues)


def _probe_node_venue(node) -> str | None:
    instrument = getattr(node, "instrument", None)
    instrument_id = getattr(instrument, "id", None)
    venue = getattr(instrument_id, "venue", None)
    if venue is None:
        venue = getattr(instrument, "venue_name", None)
    return str(venue).upper() if venue else None


def _zero_venue_pair_reason(
    pair: str,
    *,
    node_counts: Counter[str],
    edge_counts: Counter[str],
    quoted_edge_counts: Counter[str],
) -> str:
    source, target = pair.split("->", maxsplit=1)
    if node_counts.get(source, 0) == 0 or node_counts.get(target, 0) == 0:
        return "missing_instruments"
    if edge_counts.get(pair, 0) == 0:
        return "no_semantic_edge"
    if quoted_edge_counts.get(pair, 0) == 0:
        return "no_quoted_semantic_edge"
    return "no_positive_or_threshold_candidate"


def _zero_venue_pair_report(
    pair: str,
    *,
    strategy,
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    node_counts: Counter[str],
    edge_counts: Counter[str],
    quoted_edge_counts: Counter[str],
    candidate_count: int,
) -> dict[str, object]:
    reason = _zero_venue_pair_reason(
        pair,
        node_counts=node_counts,
        edge_counts=edge_counts,
        quoted_edge_counts=quoted_edge_counts,
    )
    source, target = pair.split("->", maxsplit=1)
    report: dict[str, object] = {
        "venuePair": pair,
        "reason": reason,
        "sourceNodeCount": node_counts.get(source, 0),
        "targetNodeCount": node_counts.get(target, 0),
        "edgeCount": edge_counts.get(pair, 0),
        "quotedEdgeCount": quoted_edge_counts.get(pair, 0),
        "candidateCount": int(candidate_count),
    }
    if reason == "missing_instruments":
        return report

    source_event_keys = _event_keys_for_venue(nodes, source)
    target_event_keys = _event_keys_for_venue(nodes, target)
    common_event_keys = sorted((source_event_keys & target_event_keys) - {""})
    common_quote_coverage = _common_event_quote_coverage(
        source=source,
        target=target,
        nodes=nodes,
        quotes=quotes,
        common_event_keys=set(common_event_keys),
    )
    if reason == "no_semantic_edge":
        report["blockerReason"] = (
            CoverageBlockerReason.NO_COMMON_FIXTURE.value
            if not common_event_keys
            else CoverageBlockerReason.NO_SEMANTIC_EDGE.value
        )
    elif reason == "no_quoted_semantic_edge":
        report["blockerReason"] = "quotes_missing_for_semantic_edges"
    else:
        report["blockerReason"] = "pricing_or_threshold"
    report["commonEventKeyCount"] = len(common_event_keys)
    report["commonEventKeySamples"] = common_event_keys[:5]
    report.update(common_quote_coverage)
    report["sourceEventKeySamples"] = sorted(source_event_keys - {""})[:5]
    report["targetEventKeySamples"] = sorted(target_event_keys - {""})[:5]
    report["sourceEventSportCounts"] = dict(sorted(_event_sport_counts(source_event_keys).items()))
    report["targetEventSportCounts"] = dict(sorted(_event_sport_counts(target_event_keys).items()))
    if reason == "no_semantic_edge" and not common_event_keys:
        report["discoveryGapReason"] = "no_common_fixture_loaded"
        report["marketFamilyPairs"] = {}
        report["sampleBlockerCounts"] = {}
        report["samples"] = []
        report.update(
            _fixture_discovery_probe_report(
                strategy=strategy,
                nodes=nodes,
                source=source,
                target=target,
            ),
        )
        return report

    source_nodes = _sample_probe_nodes_for_venue(
        nodes,
        source,
        limit=ZERO_PAIR_SAMPLE_NODE_LIMIT,
        preferred_event_keys=set(common_event_keys),
    )
    target_nodes = _sample_probe_nodes_for_venue(
        nodes,
        target,
        limit=ZERO_PAIR_SAMPLE_NODE_LIMIT,
        preferred_event_keys=set(common_event_keys),
    )
    sample_pairs = _sample_zero_pair_nodes(
        source_nodes=source_nodes,
        target_nodes=target_nodes,
        common_event_keys=set(common_event_keys),
    )
    verified_pairs, fixture_blocker_counts = _verified_fixture_sample_pairs(sample_pairs)
    diagnostic_pairs = verified_pairs or sample_pairs
    report["verifiedCommonFixtureSampleCount"] = len(verified_pairs)
    report["fixtureProofBlockerCounts"] = dict(sorted(fixture_blocker_counts.items()))
    if reason == "no_semantic_edge" and common_event_keys and not verified_pairs:
        report["blockerReason"] = CoverageBlockerReason.FIXTURE_IDENTITY_MISMATCH.value
        report["discoveryGapReason"] = "common_event_aliases_failed_fixture_proof"
    market_family_pairs: Counter[str] = Counter()
    sample_blocker_counts: Counter[str] = Counter()
    samples: list[dict[str, object]] = []
    for source_node, target_node in diagnostic_pairs:
        family = _probe_market_family(source_node, target_node)
        market_family_pairs[family] += 1
        sample = _zero_pair_sample_payload(strategy, source_node, target_node)
        blocker_hint = str(sample.get("blockerHint") or "")
        if blocker_hint:
            sample_blocker_counts[blocker_hint] += 1
        if len(samples) < 5:
            sample["marketFamily"] = family
            samples.append(sample)
    report["marketFamilyPairs"] = dict(market_family_pairs)
    report["sampleBlockerCounts"] = dict(sorted(sample_blocker_counts.items()))
    report["samples"] = samples
    if reason == "no_semantic_edge" and samples:
        report["blockerReason"] = _zero_pair_sample_blocker(samples, report["blockerReason"])
    return report


def _fixture_discovery_probe_report(
    *,
    strategy,
    nodes: dict[Any, object],
    source: str,
    target: str,
) -> dict[str, object]:
    source_nodes = _sample_probe_nodes_for_venue(nodes, source)
    target_nodes = _sample_probe_nodes_for_venue(nodes, target)
    fixture_probe_pairs = _sample_zero_pair_nodes_fallback(
        source_nodes,
        target_nodes,
        limit=8,
    )
    samples: list[dict[str, object]] = []
    blockers: Counter[str] = Counter()
    for source_node, target_node in fixture_probe_pairs:
        payload = _zero_pair_sample_payload(strategy, source_node, target_node)
        proof = payload.get("fixtureIdentityProof")
        if isinstance(proof, dict):
            blocker = str(
                proof.get("blockerReason")
                or proof.get("reason")
                or CoverageBlockerReason.FIXTURE_IDENTITY_MISMATCH.value,
            )
            blockers[blocker] += 1
        if len(samples) < 5:
            samples.append(payload)
    return {
        "fixtureDiscoveryBlockerCounts": dict(sorted(blockers.items())),
        "fixtureDiscoverySamples": samples,
    }


def _verified_fixture_sample_pairs(
    pairs: list[tuple[object, object]],
) -> tuple[list[tuple[object, object]], Counter[str]]:
    verified: list[tuple[object, object]] = []
    blocker_counts: Counter[str] = Counter()
    for source_node, target_node in pairs:
        proof = DEFAULT_FIXTURE_IDENTITY_RESOLVER.resolve(
            source_node.instrument,
            target_node.instrument,
        )
        if proof.same_fixture:
            verified.append((source_node, target_node))
        else:
            blocker_counts[proof.blocker_reason or proof.reason or "fixture_identity_mismatch"] += 1
    return verified, blocker_counts


def _common_event_quote_coverage(
    *,
    source: str,
    target: str,
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    common_event_keys: set[str],
) -> dict[str, object]:
    source_quoted_keys = _quoted_event_keys_for_venue(nodes, quotes, source)
    target_quoted_keys = _quoted_event_keys_for_venue(nodes, quotes, target)
    fully_quoted_keys = common_event_keys & source_quoted_keys & target_quoted_keys
    source_missing_keys = common_event_keys - source_quoted_keys
    target_missing_keys = common_event_keys - target_quoted_keys
    samples: list[dict[str, object]] = []
    for event_key in sorted((source_missing_keys | target_missing_keys) - {""})[:5]:
        samples.append(
            {
                "eventKey": event_key,
                "sourceQuoted": event_key in source_quoted_keys,
                "targetQuoted": event_key in target_quoted_keys,
            },
        )
    return {
        "fullyQuotedCommonEventKeyCount": len(fully_quoted_keys),
        "sourceQuotedCommonEventKeyCount": len(common_event_keys & source_quoted_keys),
        "targetQuotedCommonEventKeyCount": len(common_event_keys & target_quoted_keys),
        "unquotedCommonEventKeySamples": samples,
    }


def _quoted_event_keys_for_venue(
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    venue: str,
) -> set[str]:
    keys: set[str] = set()
    for node_id, node in nodes.items():
        if node_id not in quotes or _probe_node_venue(node) != venue:
            continue
        keys.update(_probe_event_keys_no_time(node))
    return keys


def _zero_pair_sample_blocker(samples: list[dict[str, object]], fallback: object) -> str:
    fallback_reason = str(fallback or CoverageBlockerReason.NO_SEMANTIC_EDGE.value)
    fixture_identity_reason = _fixture_identity_fallback_reason(samples, fallback_reason)
    if fixture_identity_reason is not None:
        return fixture_identity_reason
    blocker_hints: Counter[str] = Counter()
    derived_blockers: Counter[str] = Counter()
    for sample in samples:
        blocker_hint = str(sample.get("blockerHint") or "")
        if blocker_hint:
            blocker_hints[blocker_hint] += 1
            continue
        derived = _zero_pair_sample_derived_blocker(sample)
        if derived:
            derived_blockers[derived] += 1
    if blocker_hints:
        return _prioritized_zero_pair_blocker(blocker_hints)
    if derived_blockers:
        return _prioritized_zero_pair_blocker(derived_blockers)
    return fallback_reason


def _prioritized_zero_pair_blocker(blockers: Counter[str]) -> str:
    for blocker in (
        CoverageBlockerReason.SAME_MARKET_PARAMS_MISMATCH.value,
        CoverageBlockerReason.PROVIDER_SCOPE_MISMATCH.value,
        CoverageBlockerReason.SCOPE_MISMATCH.value,
        CoverageBlockerReason.FIXTURE_IDENTITY_MISMATCH.value,
        CoverageBlockerReason.NO_SEMANTIC_EDGE.value,
        CoverageBlockerReason.UNSUPPORTED_MARKET_FAMILY.value,
    ):
        if blockers.get(blocker, 0) > 0:
            return blocker
    return blockers.most_common(1)[0][0]


def _zero_pair_sample_derived_blocker(sample: dict[str, object]) -> str:
    pattern_a = sample.get("patternA")
    pattern_b = sample.get("patternB")
    if not isinstance(pattern_a, dict) or not isinstance(pattern_b, dict):
        return ""
    family_relation = _market_family_relation(pattern_a, pattern_b)
    if family_relation == "unsupported":
        return CoverageBlockerReason.UNSUPPORTED_MARKET_FAMILY.value
    if pattern_a.get("scope") != pattern_b.get("scope"):
        return CoverageBlockerReason.SCOPE_MISMATCH.value
    if _semantic_pattern_subject(pattern_a) != _semantic_pattern_subject(pattern_b):
        return CoverageBlockerReason.PROVIDER_SCOPE_MISMATCH.value
    if pattern_a.get("marketFamily") == pattern_b.get("marketFamily") and pattern_a.get(
        "paramsKey",
    ) != pattern_b.get("paramsKey"):
        return CoverageBlockerReason.SAME_MARKET_PARAMS_MISMATCH.value
    return ""


def _fixture_identity_fallback_reason(
    samples: list[dict[str, object]],
    fallback_reason: str,
) -> str | None:
    if fallback_reason == CoverageBlockerReason.NO_COMMON_FIXTURE.value:
        return fallback_reason
    if fallback_reason != CoverageBlockerReason.FIXTURE_IDENTITY_MISMATCH.value:
        return None
    if _only_start_time_fixture_mismatches(samples):
        return CoverageBlockerReason.NO_COMMON_FIXTURE.value
    return fallback_reason


def _only_start_time_fixture_mismatches(samples: list[dict[str, object]]) -> bool:
    fixture_blockers: Counter[str] = Counter()
    for sample in samples:
        fixture_proof = sample.get("fixtureIdentityProof")
        if isinstance(fixture_proof, dict):
            blocker = str(fixture_proof.get("blockerReason") or "")
            if blocker:
                fixture_blockers[blocker] += 1
    return bool(fixture_blockers) and set(fixture_blockers) == {"start_time_mismatch"}


def _zero_pair_sample_payload(strategy, source_node, target_node) -> dict[str, object]:
    instrument_a = source_node.instrument
    instrument_b = target_node.instrument
    fixture_proof = DEFAULT_FIXTURE_IDENTITY_RESOLVER.resolve(instrument_a, instrument_b)
    fixture_start_time_a = DEFAULT_FIXTURE_IDENTITY_RESOLVER.parsed_start_time(instrument_a)
    fixture_start_time_b = DEFAULT_FIXTURE_IDENTITY_RESOLVER.parsed_start_time(instrument_b)
    pattern_a = _probe_pattern_payload(source_node)
    pattern_b = _probe_pattern_payload(target_node)
    matcher_suspect, suspect_reason = _strategy_matcher_suspect_reason(
        strategy,
        instrument_a,
        instrument_b,
    )
    fixture_suspect, fixture_suspect_reason = _semantic_fixture_suspect_reason(
        strategy,
        instrument_a,
        instrument_b,
    )
    blocker_hint = _zero_pair_blocker_hint(
        pattern_a,
        pattern_b,
        suspect_reason=suspect_reason,
        fixture_suspect=fixture_suspect,
        fixture_suspect_reason=fixture_suspect_reason,
    )
    return {
        "instrumentIdA": str(getattr(instrument_a, "id", "")),
        "instrumentIdB": str(getattr(instrument_b, "id", "")),
        "eventKeyA": _probe_event_key_no_time(source_node),
        "eventKeyB": _probe_event_key_no_time(target_node),
        "eventAliasKeysA": sorted(_probe_event_keys_no_time(source_node))[:5],
        "eventAliasKeysB": sorted(_probe_event_keys_no_time(target_node))[:5],
        "canonicalEventKeyA": _canonical_probe_event_key_no_time(source_node),
        "canonicalEventKeyB": _canonical_probe_event_key_no_time(target_node),
        "fixtureStartTimeA": _isoformat_utc(fixture_start_time_a),
        "fixtureStartTimeB": _isoformat_utc(fixture_start_time_b),
        "patternA": pattern_a,
        "patternB": pattern_b,
        "matcherSuspect": matcher_suspect,
        "matcherSuspectReason": suspect_reason,
        "fixtureSuspect": fixture_suspect,
        "fixtureSuspectReason": fixture_suspect_reason,
        "fixtureIdentityProof": {
            "sameFixture": fixture_proof.same_fixture,
            "reason": fixture_proof.reason,
            "confidence": fixture_proof.confidence,
            "aliasHits": list(fixture_proof.alias_hits),
            "matchedFields": list(fixture_proof.matched_fields),
            "startTimeDeltaSeconds": fixture_proof.start_time_delta_secs,
            "ambiguous": fixture_proof.ambiguous,
            "blockerReason": fixture_proof.blocker_reason,
        },
        "blockerHint": blocker_hint,
    }


def _isoformat_utc(timestamp) -> str | None:
    if timestamp is None:
        return None
    return timestamp.isoformat().replace("+00:00", "Z")


def _strategy_matcher_suspect_reason(strategy, instrument_a, instrument_b) -> tuple[bool, str]:
    checker = getattr(strategy, "matcher_suspect_reason", None)
    if checker is None:
        checker = getattr(strategy, "_matcher_suspect_reason", None)
    if checker is None:
        from nautilus_trader.examples.strategies.betting_arbitrage import (
            BettingArbitrageStrategy,
        )

        checker = BettingArbitrageStrategy.matcher_suspect_reason
    return checker(instrument_a, instrument_b)


def _zero_pair_blocker_hint(
    pattern_a: dict[str, object],
    pattern_b: dict[str, object],
    *,
    suspect_reason: str,
    fixture_suspect: bool,
    fixture_suspect_reason: str,
) -> str:
    if fixture_suspect and fixture_suspect_reason in {
        "same_venue_event_id_mismatch",
        "event_mismatch",
    }:
        return CoverageBlockerReason.FIXTURE_IDENTITY_MISMATCH.value
    if suspect_reason == "same_market_params_mismatch":
        if _semantic_pattern_subject(pattern_a) != _semantic_pattern_subject(pattern_b):
            return CoverageBlockerReason.PROVIDER_SCOPE_MISMATCH.value
        return CoverageBlockerReason.SAME_MARKET_PARAMS_MISMATCH.value
    family_relation = _market_family_relation(pattern_a, pattern_b)
    if family_relation == "unsupported":
        return CoverageBlockerReason.UNSUPPORTED_MARKET_FAMILY.value
    if pattern_a.get("scope") != pattern_b.get("scope"):
        return CoverageBlockerReason.SCOPE_MISMATCH.value
    if _semantic_pattern_subject(pattern_a) != _semantic_pattern_subject(pattern_b):
        return CoverageBlockerReason.PROVIDER_SCOPE_MISMATCH.value
    if family_relation in {"same_family", "directional_family"} and pattern_a.get(
        "paramsKey",
    ) != pattern_b.get("paramsKey"):
        return CoverageBlockerReason.SAME_MARKET_PARAMS_MISMATCH.value
    return ""


# Cross-family pairs the diagnostic treats as matchable. MATCH_ODDS/DRAW_NO_BET share
# THREE_WAY_STATES and ASIAN_HANDICAP/POINT_SPREAD share handicap margin states
# (classifier.py:136). WINNER belongs with the moneyline group: the normalizer emits
# WINNER vs MATCH_ODDS for the same raw market based solely on the is_two_way_market
# flag (normalization.py), so a WINNER cross-pair there is a semantic-edge gap, not an
# unsupported family. Moneyline<->spread pairs stay unmatchable so the blocker hint
# surfaces the real limitation (#235).
_DIRECTIONAL_MARKET_FAMILY_GROUPS = (
    frozenset({"WINNER", "MATCH_ODDS", "DRAW_NO_BET"}),
    frozenset({"ASIAN_HANDICAP", "POINT_SPREAD"}),
)
_TOTAL_MARKET_FAMILIES = frozenset({"TOTALS", "TEAM_TOTALS"})


def _market_family_relation(
    pattern_a: dict[str, object],
    pattern_b: dict[str, object],
) -> str:
    family_a = str(pattern_a.get("marketFamily") or pattern_a.get("marketType") or "").upper()
    family_b = str(pattern_b.get("marketFamily") or pattern_b.get("marketType") or "").upper()
    if not family_a or not family_b:
        return "unknown"
    if family_a == family_b:
        return "same_family"
    if any(family_a in group and family_b in group for group in _DIRECTIONAL_MARKET_FAMILY_GROUPS):
        return "directional_family"
    if family_a in _TOTAL_MARKET_FAMILIES and family_b in _TOTAL_MARKET_FAMILIES:
        return "same_family"
    return "unsupported"


def _semantic_pattern_subject(pattern: dict[str, object]) -> str:
    params = _semantic_params_from_key(str(pattern.get("paramsKey") or ""))
    explicit = str(params.get("subject") or params.get("market_subject") or "").strip().lower()
    if explicit:
        return explicit
    raw_text = " ".join(
        str(pattern.get(key) or "")
        for key in ("rawMarketName", "rawMarketType", "marketType", "marketFamily")
    ).lower()
    if "corner" in raw_text:
        return "corners"
    if "card" in raw_text:
        return "cards"
    return ""


def _semantic_params_from_key(params_key: str) -> dict[str, str]:
    if not params_key or params_key == "[]":
        return {}
    try:
        raw = json.loads(params_key)
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, list):
        return {}
    params: dict[str, str] = {}
    for item in raw:
        if type(item) in {str, bytes} or not isinstance(item, Sequence) or len(item) != 2:
            continue
        key, value = item
        params[str(key)] = str(value)
    return params


def _event_keys_for_venue(nodes: dict[Any, object], venue: str) -> set[str]:
    keys: set[str] = set()
    for node in nodes.values():
        if _probe_node_venue(node) != venue:
            continue
        keys.update(_probe_event_keys_no_time(node))
    return keys


def _event_sport_counts(event_keys: set[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for event_key in event_keys:
        if not event_key:
            continue
        sport = event_key.split(":", maxsplit=1)[0].strip()
        if sport:
            counts[sport] += 1
    return counts


def _sample_probe_nodes_for_venue(
    nodes: dict[Any, object],
    venue: str,
    limit: int = 40,
    preferred_event_keys: set[str] | None = None,
) -> list:
    sampled: list[object] = []
    preferred = preferred_event_keys or set()
    if preferred:
        for node in nodes.values():
            if _probe_node_venue(node) != venue:
                continue
            if not (_probe_event_keys_no_time(node) & preferred):
                continue
            sampled.append(node)
            if len(sampled) >= limit:
                return sampled
    for node in nodes.values():
        if _probe_node_venue(node) != venue:
            continue
        if node in sampled:
            continue
        sampled.append(node)
        if len(sampled) >= limit:
            break
    return sampled


def _sample_zero_pair_nodes(
    *,
    source_nodes: list,
    target_nodes: list,
    common_event_keys: set[str],
    limit: int = 12,
) -> list[tuple[object, object]]:
    if common_event_keys:
        pairs = _sample_zero_pair_nodes_for_common_events(
            source_nodes,
            target_nodes,
            common_event_keys=common_event_keys,
            limit=limit,
        )
        if pairs:
            return pairs
    return _sample_zero_pair_nodes_fallback(
        source_nodes,
        target_nodes,
        limit=limit,
    )


def _sample_zero_pair_nodes_for_common_events(
    source_nodes: list,
    target_nodes: list,
    *,
    common_event_keys: set[str],
    limit: int,
) -> list[tuple[object, object]]:
    scored_pairs: list[tuple[tuple[int, int, int, str, str], object, object]] = []
    for source_node in source_nodes:
        source_keys = _probe_event_keys_no_time(source_node) & common_event_keys
        if not source_keys:
            continue
        for target_node in target_nodes:
            if not (_probe_event_keys_no_time(target_node) & source_keys):
                continue
            scored_pairs.append(
                (
                    _zero_pair_sample_priority(source_node, target_node),
                    source_node,
                    target_node,
                ),
            )
    scored_pairs.sort(key=lambda item: item[0])
    return [(source_node, target_node) for _, source_node, target_node in scored_pairs[:limit]]


def _zero_pair_sample_priority(source_node, target_node) -> tuple[int, int, int, str, str]:
    pattern_a = _probe_pattern_payload(source_node)
    pattern_b = _probe_pattern_payload(target_node)
    relation = _market_family_relation(pattern_a, pattern_b)
    relation_rank = {
        "same_family": 0,
        "directional_family": 1,
        "unknown": 2,
        "unsupported": 3,
    }.get(relation, 3)
    scope_rank = 0 if pattern_a.get("scope") == pattern_b.get("scope") else 1
    params_rank = 0 if pattern_a.get("paramsKey") == pattern_b.get("paramsKey") else 1
    return (
        relation_rank,
        scope_rank,
        params_rank,
        str(getattr(source_node, "instrument_id", "") or getattr(source_node, "id", "")),
        str(getattr(target_node, "instrument_id", "") or getattr(target_node, "id", "")),
    )


def _sample_zero_pair_nodes_fallback(
    source_nodes: list,
    target_nodes: list,
    *,
    limit: int,
) -> list[tuple[object, object]]:
    pairs: list[tuple[object, object]] = []
    for source_node in source_nodes[:4]:
        for target_node in target_nodes[:4]:
            pairs.append((source_node, target_node))
            if len(pairs) >= limit:
                return pairs
    return pairs


def _probe_event_key_no_time(node) -> str:
    instrument = getattr(node, "instrument", None)
    event_key = getattr(instrument, "event_key", None)
    if callable(event_key):
        try:
            return str(event_key(include_start_time=False))
        except (AttributeError, TypeError, ValueError):
            pass
    return str(getattr(node, "canonical_event_key", ""))


def _canonical_probe_event_key_no_time(node) -> str:
    return _canonical_event_key_text(_probe_event_key_no_time(node))


def _probe_event_keys_no_time(node) -> set[str]:
    keys = {_canonical_probe_event_key_no_time(node)}
    instrument = getattr(node, "instrument", None)
    event_alias_keys = getattr(instrument, "event_alias_keys", None)
    if callable(event_alias_keys):
        try:
            raw_aliases = event_alias_keys(include_start_time=False)
        except (AttributeError, TypeError, ValueError):
            raw_aliases = ()
        if isinstance(raw_aliases, str | bytes):
            raw_aliases = (raw_aliases,)
        for alias in raw_aliases or ():
            keys.add(_canonical_event_key_text(str(alias)))
    return {key for key in keys if key}


def _canonical_event_key_text(value: str) -> str:
    return DEFAULT_FIXTURE_IDENTITY_RESOLVER.canonical_event_key_text(value)


def _probe_pattern_payload(node) -> dict[str, str]:
    instrument = getattr(node, "instrument", None)
    try:
        normalized = MarketNormalizer.normalize(instrument)
    except (AttributeError, TypeError, ValueError):
        return {
            "sport": "",
            "scope": "",
            "marketType": str(getattr(node, "market_type", "") or ""),
            "marketFamily": str(getattr(node, "market_type", "") or ""),
            "selection": str(getattr(node, "outcome", "") or ""),
            "paramsKey": str(getattr(node, "params", "") or ""),
        }
    return {
        "sport": normalized.sport,
        "scope": normalized.scope,
        "marketType": normalized.market_type,
        "marketFamily": normalized.market_family,
        "selection": normalized.selection,
        "paramsKey": _semantic_params_key(normalized.params),
    }


def _is_cross_venue_pair(pair: str) -> bool:
    source, target = pair.split("->", maxsplit=1)
    return source != target


def _semantic_probe_diagnostics(
    graph,
    *,
    nodes: dict[str, object] | None = None,
) -> dict[str, object]:
    # Reuse the caller's snapshot when it has one instead of copying the whole
    # node dict a second time.
    if nodes is None:
        try:
            nodes = dict(graph.nodes_by_id)
        except RuntimeError:
            return {"available": False, "reason": "graph_mutated_during_snapshot"}

    node_diagnostics = _semantic_node_diagnostics(nodes)
    template_diagnostics = _semantic_template_diagnostics(graph)
    supported_provider_node_count = _supported_provider_node_count(
        node_diagnostics["provider_pattern_counts"],
        template_diagnostics["provider_pattern_counts"],
    )
    unsupported_provider_patterns = _unsupported_provider_patterns(
        node_diagnostics["provider_pattern_counts"],
        template_diagnostics["provider_pattern_counts"],
        node_diagnostics["provider_pattern_samples"],
    )
    normalized_node_count = sum(node_diagnostics["pattern_counts"].values())
    common_pattern_key_count = len(
        set(node_diagnostics["pattern_counts"]) & set(template_diagnostics["pattern_counts"]),
    )
    return {
        "available": True,
        "nodeCount": len(nodes),
        "normalizedNodeCount": normalized_node_count,
        "normalizationErrorCount": sum(node_diagnostics["normalization_errors"].values()),
        "supportedProviderNodeCount": supported_provider_node_count,
        "unsupportedProviderNodeCount": unsupported_provider_patterns["node_count"],
        "supportedProviderCoverageRatio": round(
            (
                supported_provider_node_count / normalized_node_count
                if normalized_node_count > 0
                else 0.0
            ),
            6,
        ),
        "commonPatternKeyCount": common_pattern_key_count,
        "unsupportedProviderPatternCount": unsupported_provider_patterns["pattern_count"],
        "unsupportedProviderPatterns": unsupported_provider_patterns["top_patterns"],
        "unsupportedProviderPatternSamples": unsupported_provider_patterns["samples"],
        "templateCount": template_diagnostics["template_count"],
        "nodeSports": _top_counter(node_diagnostics["sport_counts"]),
        "nodeMarkets": _top_counter(node_diagnostics["market_counts"]),
        "templateSports": _top_counter(template_diagnostics["sport_counts"]),
        "templateSafetyTiers": _top_counter(template_diagnostics["tier_counts"]),
        "templateProviderScopes": _top_counter(template_diagnostics["provider_scope_counts"]),
        "templateTierRelationships": _top_counter(
            template_diagnostics["tier_relationship_counts"],
            limit=50,
        ),
        "templateTierCaveats": _top_counter(
            template_diagnostics["tier_caveat_counts"],
            limit=50,
        ),
        "executionSafeTemplates": template_diagnostics["execution_safe_templates"],
        "sameVenueEligibleTemplates": template_diagnostics["same_venue_eligible_templates"],
        "nodePatternKeys": _top_counter(node_diagnostics["pattern_counts"]),
        "templatePatternKeys": _top_counter(template_diagnostics["pattern_counts"]),
        "normalizationErrors": _top_counter(node_diagnostics["normalization_errors"]),
        "normalizationErrorSamples": node_diagnostics["normalization_error_samples"],
        "normalizedNodeSamples": node_diagnostics["normalized_node_samples"],
    }


def _semantic_node_diagnostics(nodes: dict[str, object]) -> dict[str, object]:
    pattern_counts: Counter[tuple[str, ...]] = Counter()
    provider_pattern_counts: Counter[tuple[str, ...]] = Counter()
    sport_counts: Counter[str] = Counter()
    market_counts: Counter[str] = Counter()
    normalization_errors: Counter[str] = Counter()
    normalization_error_samples: list[dict[str, object]] = []
    normalized_node_samples: list[dict[str, object]] = []
    provider_pattern_samples: dict[tuple[str, ...], list[dict[str, object]]] = {}
    for node_id, node in nodes.items():
        instrument = getattr(node, "instrument", None)
        try:
            normalized = MarketNormalizer.normalize(instrument)
        except (AttributeError, TypeError, ValueError) as exc:
            error_type = type(exc).__name__
            normalization_errors[error_type] += 1
            if len(normalization_error_samples) < 5:
                normalization_error_samples.append(
                    {
                        "nodeId": str(node_id),
                        "instrumentId": str(getattr(instrument, "id", node_id)),
                        "marketType": str(getattr(instrument, "market_type", "")),
                        "outcome": str(getattr(instrument, "outcome", "")),
                        "errorType": error_type,
                        "error": str(exc)[:240],
                    },
                )
            continue

        pattern_key = (
            normalized.sport,
            normalized.scope,
            normalized.market_type,
            normalized.market_family,
            normalized.selection,
            _semantic_params_key(normalized.params),
        )
        provider_pattern_key = (normalized.venue, *pattern_key)
        pattern_counts[pattern_key] += 1
        provider_pattern_counts[provider_pattern_key] += 1
        sport_counts[normalized.sport] += 1
        market_counts[normalized.market_type] += 1
        provider_samples = provider_pattern_samples.setdefault(provider_pattern_key, [])
        if len(provider_samples) < 3:
            provider_samples.append(
                {
                    "nodeId": str(node_id),
                    "instrumentId": str(getattr(instrument, "id", node_id)),
                    "venue": normalized.venue,
                    "sport": normalized.sport,
                    "scope": normalized.scope,
                    "marketType": normalized.market_type,
                    "marketFamily": normalized.market_family,
                    "selection": normalized.selection,
                    "paramsKey": pattern_key[-1],
                    "eventKey": normalized.event_key,
                },
            )
        if len(normalized_node_samples) < 5:
            normalized_node_samples.append(
                {
                    "nodeId": str(node_id),
                    "venue": normalized.venue,
                    "sport": normalized.sport,
                    "scope": normalized.scope,
                    "marketType": normalized.market_type,
                    "selection": normalized.selection,
                    "paramsKey": pattern_key[-1],
                },
            )

    return {
        "pattern_counts": pattern_counts,
        "provider_pattern_counts": provider_pattern_counts,
        "sport_counts": sport_counts,
        "market_counts": market_counts,
        "normalization_errors": normalization_errors,
        "normalization_error_samples": normalization_error_samples,
        "normalized_node_samples": normalized_node_samples,
        "provider_pattern_samples": provider_pattern_samples,
    }


# Same sample-capping convention as the sibling diagnostics fields in this
# module (`_coverage_sample_hyperedges`, `proof_payloads[:10]`,
# `_unsupported_provider_patterns(..., limit=10)`): these two template lists
# are diagnostic samples, not data required by any consumer, and were the
# single largest contributor to status.json size when left uncapped.
_TEMPLATE_SAMPLE_LIMIT = 10


def _semantic_template_diagnostics(graph) -> dict[str, object]:
    pattern_counts: Counter[tuple[str, ...]] = Counter()
    provider_pattern_counts: Counter[tuple[str, ...]] = Counter()
    sport_counts: Counter[str] = Counter()
    tier_counts: Counter[str] = Counter()
    provider_scope_counts: Counter[str] = Counter()
    tier_relationship_counts: Counter[tuple[str, str]] = Counter()
    tier_caveat_counts: Counter[tuple[str, str]] = Counter()
    execution_safe_templates: list[dict[str, object]] = []
    same_venue_eligible_templates: list[dict[str, object]] = []
    templates = _semantic_template_payloads_for_diagnostics(graph)
    for template in templates:
        provider_scope = tuple(str(item) for item in template.get("provider_scope", ()))
        provider_scope_key = ",".join(provider_scope) or "venue_agnostic"
        safety_tier = str(template.get("safety_tier") or "unknown")
        provider_scope_counts[provider_scope_key] += 1
        tier_counts[safety_tier] += 1
        tier_relationship_counts[
            (safety_tier, str(template.get("relationship_type") or "unknown"))
        ] += 1
        caveats = template.get("caveats")
        if isinstance(caveats, list):
            for caveat in caveats:
                tier_caveat_counts[(safety_tier, str(caveat))] += 1
        detail = _semantic_template_detail(template)
        if bool(template.get("execution_safe")):
            execution_safe_templates.append(detail)
        if bool(template.get("same_venue_execution_eligible")):
            same_venue_eligible_templates.append(detail)
        for side in ("pattern_a", "pattern_b"):
            pattern = template.get(side)
            if not isinstance(pattern, dict):
                continue
            pattern_key = _template_pattern_key(pattern)
            pattern_counts[pattern_key] += 1
            sport_counts[pattern_key[0]] += 1
            for provider in provider_scope or ("venue_agnostic",):
                provider_pattern_counts[(provider, *pattern_key)] += 1

    return {
        "pattern_counts": pattern_counts,
        "provider_pattern_counts": provider_pattern_counts,
        "sport_counts": sport_counts,
        "tier_counts": tier_counts,
        "provider_scope_counts": provider_scope_counts,
        "tier_relationship_counts": tier_relationship_counts,
        "tier_caveat_counts": tier_caveat_counts,
        "template_count": len(templates),
        "execution_safe_templates": sorted(
            execution_safe_templates,
            key=lambda item: str(item["templateId"]),
        )[:_TEMPLATE_SAMPLE_LIMIT],
        "same_venue_eligible_templates": sorted(
            same_venue_eligible_templates,
            key=lambda item: str(item["templateId"]),
        )[:_TEMPLATE_SAMPLE_LIMIT],
    }


def _semantic_template_detail(template: dict[str, object]) -> dict[str, object]:
    pattern_a = template.get("pattern_a") if isinstance(template.get("pattern_a"), dict) else {}
    pattern_b = template.get("pattern_b") if isinstance(template.get("pattern_b"), dict) else {}
    return {
        "templateId": str(template.get("template_id") or ""),
        "relationshipType": str(template.get("relationship_type") or ""),
        "safetyTier": str(template.get("safety_tier") or ""),
        "providerScope": list(template.get("provider_scope") or []),
        "venueAgnostic": bool(template.get("venue_agnostic")),
        "confidence": template.get("confidence"),
        "caveats": list(template.get("caveats") or []),
        "patternA": pattern_a,
        "patternB": pattern_b,
    }


def _supported_provider_node_count(
    node_provider_pattern_counts: Counter[tuple[str, ...]],
    template_provider_pattern_counts: Counter[tuple[str, ...]],
) -> int:
    supported_provider_node_count = 0
    for provider_pattern_key, count in node_provider_pattern_counts.items():
        venue = provider_pattern_key[0]
        pattern_key = provider_pattern_key[1:]
        if template_provider_pattern_counts.get(
            (venue, *pattern_key),
            0,
        ) or template_provider_pattern_counts.get(("venue_agnostic", *pattern_key), 0):
            supported_provider_node_count += count
    return supported_provider_node_count


def _unsupported_provider_patterns(
    node_provider_pattern_counts: Counter[tuple[str, ...]],
    template_provider_pattern_counts: Counter[tuple[str, ...]],
    provider_pattern_samples: dict[tuple[str, ...], list[dict[str, object]]],
    *,
    limit: int = 10,
) -> dict[str, object]:
    unsupported_counts: Counter[tuple[str, ...]] = Counter()
    for provider_pattern_key, count in node_provider_pattern_counts.items():
        venue = provider_pattern_key[0]
        pattern_key = provider_pattern_key[1:]
        if template_provider_pattern_counts.get((venue, *pattern_key), 0):
            continue
        if template_provider_pattern_counts.get(("venue_agnostic", *pattern_key), 0):
            continue
        unsupported_counts[provider_pattern_key] += count

    return {
        "node_count": sum(unsupported_counts.values()),
        "pattern_count": len(unsupported_counts),
        "top_patterns": _top_counter(unsupported_counts, limit=limit),
        "samples": [
            {
                "provider": key[0],
                "sport": key[1],
                "scope": key[2],
                "marketType": key[3],
                "marketFamily": key[4],
                "selection": key[5],
                "paramsKey": key[6],
                "count": count,
                "samples": provider_pattern_samples.get(key, [])[:3],
            }
            for key, count in unsupported_counts.most_common(limit)
        ],
    }


def _semantic_template_payloads_for_diagnostics(graph) -> list[dict[str, object]]:
    payloads = getattr(graph, "_semantic_template_payloads", None)
    if not callable(payloads):
        return []
    try:
        result = payloads()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return []
    return [item for item in result if isinstance(item, dict)]


def _template_pattern_key(pattern: dict[str, object]) -> tuple[str, ...]:
    return (
        str(pattern.get("sport") or ""),
        str(pattern.get("scope") or ""),
        str(pattern.get("market_type") or ""),
        str(pattern.get("market_family") or ""),
        str(pattern.get("selection") or ""),
        str(pattern.get("params_key") or ""),
    )


def _semantic_params_key(params: tuple[tuple[str, str], ...] | object) -> str:
    if isinstance(params, tuple):
        return json.dumps(list(params), sort_keys=True, separators=(",", ":"))
    return str(params or "")


def _top_counter(counter: Counter, limit: int = 10) -> list[dict[str, object]]:
    return [
        {"key": list(key) if isinstance(key, tuple) else str(key), "count": count}
        for key, count in counter.most_common(limit)
    ]


def _empty_coverage_book_devig_payload() -> dict[str, object]:
    return {
        "sampledHyperedges": 0,
        "quotedHyperedges": 0,
        "incompleteHyperedges": 0,
        "methodCounts": {},
        "valueBuckets": {},
        "overround": _percentile_payload([]),
        "vig": _percentile_payload([]),
        "rawProfitMargin": _percentile_payload([]),
        "feeAdjustedProfitMargin": _percentile_payload([]),
        "samples": [],
    }


def _probe_coverage_book_devig_diagnostics(  # noqa: C901
    strategy,
    *,
    coverage_diagnostics: object,
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    min_profit_margin: Decimal,
) -> dict[str, object]:
    if not isinstance(coverage_diagnostics, dict):
        return _empty_coverage_book_devig_payload()
    sample_hyperedges = coverage_diagnostics.get("sampleHyperedges")
    if not isinstance(sample_hyperedges, list) or not sample_hyperedges:
        return _empty_coverage_book_devig_payload()

    method_counts: Counter[str] = Counter()
    value_buckets: Counter[str] = Counter()
    overround_samples: list[float] = []
    vig_samples: list[float] = []
    raw_margin_samples: list[float] = []
    adjusted_margin_samples: list[float] = []
    samples: list[dict[str, object]] = []
    quoted_hyperedges = 0
    incomplete_hyperedges = 0
    adjuster = getattr(strategy, "fee_adjusted_coverage_basket", None)
    quoted_ids = {str(node_id) for node_id in quotes}
    node_index = _coverage_runtime_node_index(nodes, quoted_ids)

    for hyperedge in sample_hyperedges:
        if not isinstance(hyperedge, dict):
            continue
        instrument_ids = tuple(str(item) for item in hyperedge.get("instrument_ids", ()) or ())
        resolved_ids, missing_ids = _resolve_coverage_hyperedge_node_ids(
            hyperedge,
            nodes=nodes,
            quotes=quotes,
            node_index=node_index,
        )
        if len(instrument_ids) < 2 or not callable(adjuster):
            incomplete_hyperedges += 1
            value_buckets["coverage_reference_book_incomplete"] += 1
            continue
        if missing_ids:
            incomplete_hyperedges += 1
            value_buckets["coverage_reference_book_incomplete"] += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "hyperedgeId": str(hyperedge.get("hyperedge_id") or ""),
                        "coverageProofId": str(hyperedge.get("coverage_proof_id") or ""),
                        "classification": "coverage_reference_book_incomplete",
                        "missingInstrumentIds": missing_ids[:10],
                        "resolvedInstrumentIds": list(resolved_ids),
                    },
                )
            continue

        try:
            instruments = tuple(nodes[instrument_id].instrument for instrument_id in resolved_ids)
            odds = tuple(Decimal(str(quotes[instrument_id].odds)) for instrument_id in resolved_ids)
            adjusted = adjuster(instruments, odds)
        except (AttributeError, ArithmeticError, TypeError, ValueError) as exc:
            incomplete_hyperedges += 1
            value_buckets["coverage_devig_method_failed"] += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "hyperedgeId": str(hyperedge.get("hyperedge_id") or ""),
                        "coverageProofId": str(hyperedge.get("coverage_proof_id") or ""),
                        "classification": "coverage_devig_method_failed",
                        "error": str(exc)[:240],
                    },
                )
            continue

        quoted_hyperedges += 1
        method_counts[str(adjusted.devig_method or "none")] += 1
        overround_samples.append(float(adjusted.overround))
        vig_samples.append(float(adjusted.vig))
        raw_margin_samples.append(float(adjusted.basket.raw_profit_margin))
        adjusted_margin_samples.append(float(adjusted.basket.effective_profit_margin))
        execution_safe = bool(hyperedge.get("execution_safe"))
        hyperedge_venues = {str(instrument.id.venue).upper() for instrument in instruments}
        if (
            len(hyperedge_venues) == 1
            and adjusted.overround < Decimal(1) - SAME_VENUE_UNDERROUND_TOLERANCE
        ):
            # Same guard as _probe_value_classification: one book cannot underround its
            # own coverage basket, so this hyperedge is a data error, never a locked arb.
            classification = "coverage_suspect_same_venue_underround"
        elif adjusted.basket.effective_profit_margin >= min_profit_margin:
            classification = (
                "coverage_locked_execution_safe_arbitrage"
                if execution_safe
                else "coverage_positive_non_executable_hyperedge"
            )
        elif adjusted.basket.effective_profit_margin > 0:
            classification = "coverage_below_threshold"
        else:
            classification = "coverage_negative_margin"
        value_buckets[classification] += 1
        if len(samples) < 5:
            samples.append(
                {
                    "hyperedgeId": str(hyperedge.get("hyperedge_id") or ""),
                    "coverageProofId": str(hyperedge.get("coverage_proof_id") or ""),
                    "instrumentIds": list(resolved_ids),
                    "semanticInstrumentIds": list(instrument_ids),
                    "providerScope": list(hyperedge.get("provider_scope") or []),
                    "safetyTier": str(hyperedge.get("safety_tier") or ""),
                    "executionSafe": execution_safe,
                    "classification": classification,
                    "devigMethod": adjusted.devig_method,
                    "bookOverround": str(adjusted.overround),
                    "bookVig": str(adjusted.vig),
                    "rawProfitMargin": str(adjusted.basket.raw_profit_margin),
                    "feeAdjustedProfitMargin": str(adjusted.basket.effective_profit_margin),
                    "basketRebateRate": str(adjusted.basket.basket_rebate_rate),
                    "basketBoostRate": str(adjusted.basket.basket_boost_rate),
                },
            )

    return {
        "sampledHyperedges": len(sample_hyperedges),
        "quotedHyperedges": quoted_hyperedges,
        "incompleteHyperedges": incomplete_hyperedges,
        "methodCounts": dict(method_counts),
        "valueBuckets": dict(value_buckets),
        "overround": _percentile_payload(overround_samples),
        "vig": _percentile_payload(vig_samples),
        "rawProfitMargin": _percentile_payload(raw_margin_samples),
        "feeAdjustedProfitMargin": _percentile_payload(adjusted_margin_samples),
        "samples": samples,
    }


def _resolve_coverage_hyperedge_node_ids(
    hyperedge: dict[str, object],
    *,
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    node_index: dict[tuple[str, str, str, str, str, str, str], list[str]] | None = None,
) -> tuple[tuple[str, ...], list[str]]:
    node_by_id = {str(node_id): node for node_id, node in nodes.items()}
    quoted_ids = {str(node_id) for node_id in quotes}
    instrument_ids = tuple(str(item) for item in hyperedge.get("instrument_ids", ()) or ())
    direct_ids = tuple(
        instrument_id for instrument_id in instrument_ids if instrument_id in node_by_id
    )
    if len(direct_ids) == len(instrument_ids) and all(
        node_id in quoted_ids for node_id in direct_ids
    ):
        return direct_ids, []

    index = (
        node_index if node_index is not None else _coverage_runtime_node_index(nodes, quoted_ids)
    )
    resolved: list[str] = []
    missing: list[str] = []
    predicates = hyperedge.get("predicates")
    predicate_payloads = predicates if isinstance(predicates, list) else []
    if predicate_payloads:
        for index_hint, predicate in enumerate(predicate_payloads):
            if not isinstance(predicate, dict):
                continue
            node_id = _resolve_coverage_predicate_node_id(
                predicate,
                index=index,
                used=set(resolved),
            )
            if node_id and node_id in quoted_ids:
                resolved.append(node_id)
            else:
                missing.append(
                    str(
                        predicate.get("instrument_id")
                        or predicate.get("predicate_id")
                        or f"predicate-{index_hint}",
                    ),
                )
        return tuple(resolved), missing

    for instrument_id in instrument_ids:
        if instrument_id in node_by_id and instrument_id in quoted_ids:
            resolved.append(instrument_id)
        else:
            missing.append(instrument_id)
    return tuple(resolved), missing


def _coverage_runtime_node_index(
    nodes: dict[Any, object],
    quoted_ids: set[str],
) -> dict[tuple[str, str, str, str, str, str, str], list[str]]:
    index: dict[tuple[str, str, str, str, str, str, str], list[str]] = {}
    for node_id, node in nodes.items():
        provider = _probe_node_venue(node) or ""
        pattern = _probe_pattern_payload(node)
        key = (
            provider,
            _canonical_probe_event_key_no_time(node),
            str(pattern.get("sport") or ""),
            str(pattern.get("scope") or ""),
            str(pattern.get("marketFamily") or ""),
            str(pattern.get("selection") or ""),
            str(pattern.get("paramsKey") or ""),
        )
        bucket = index.setdefault(key, [])
        string_id = str(node_id)
        if string_id in quoted_ids:
            bucket.insert(0, string_id)
        else:
            bucket.append(string_id)
    return index


def _resolve_coverage_predicate_node_id(
    predicate: dict[str, object],
    *,
    index: dict[tuple[str, str, str, str, str, str, str], list[str]],
    used: set[str],
) -> str | None:
    params_key = str(predicate.get("params_key") or "")
    if not params_key:
        params = predicate.get("params")
        params_key = (
            _semantic_params_key(tuple(tuple(item) for item in params))
            if isinstance(params, list)
            else ""
        )
    key = (
        str(predicate.get("provider") or "").upper(),
        _canonical_event_key_text(str(predicate.get("event_key") or "")),
        str(predicate.get("sport") or ""),
        str(predicate.get("scope") or ""),
        str(predicate.get("market_family") or predicate.get("marketFamily") or ""),
        str(predicate.get("selection") or ""),
        params_key,
    )
    for node_id in index.get(key, []):
        if node_id not in used:
            return node_id
    return None


def _probe_edge_profitability(
    strategy,
    *,
    edges,
    nodes,
    quotes,
    min_profit_margin: Decimal,
    provider_quote_poll_stats: object = None,
) -> dict[str, object]:
    matcher = strategy.market_matcher
    counters = ProbeProfitabilityCounters()
    counters.live_quote_age_slo_secs = float(getattr(strategy, "live_quote_age_slo_secs", 5.0))
    counters.stream_healthy_venues = _stream_healthy_venues(provider_quote_poll_stats)

    for edge in edges:
        quoted_edge = _quoted_probe_edge(edge, nodes, quotes)
        if quoted_edge is None:
            continue
        source_node, target_node, quote_a, quote_b = quoted_edge
        if not _probe_edge_matches_execution_venue_mode(strategy, source_node, target_node):
            continue

        counters.quoted_edges += 1
        decision_started_ns = time.perf_counter_ns()
        try:
            allow_same_venue = edge.same_venue_execution_eligible and not edge.execution_safe
            quality = _probe_candidate_quality(
                strategy,
                edge=edge,
                source_node=source_node,
                target_node=target_node,
                quote_a=quote_a,
                quote_b=quote_b,
                min_profit_margin=min_profit_margin,
                allow_same_venue=allow_same_venue,
            )
            _record_probe_quality(counters, quality)
            if not edge.execution_safe and not allow_same_venue:
                continue

            opportunity = matcher.check_arbitrage(
                source_node.instrument,
                target_node.instrument,
                odds_a=quote_a.odds,
                odds_b=quote_b.odds,
                allow_same_venue_execution_eligible=allow_same_venue,
            )
            if opportunity is None:
                continue
            opportunity, fee_adjustment_error = _try_strategy_fee_adjusted_opportunity(
                strategy,
                opportunity,
            )
            if fee_adjustment_error is not None:
                continue

            _record_probe_opportunity(
                counters,
                opportunity=opportunity,
                edge=edge,
                source_node=source_node,
                target_node=target_node,
                allow_same_venue=allow_same_venue,
                min_profit_margin=min_profit_margin,
                timing_clean=_probe_timing_clean(quality.get("timingFlags")),
            )
        finally:
            counters.candidate_decision_latency_ns.append(
                time.perf_counter_ns() - decision_started_ns,
            )

    return counters.to_payload()


def _probe_edge_matches_execution_venue_mode(strategy, source_node, target_node) -> bool:
    config = getattr(strategy, "_config", None)
    mode = str(getattr(config, "execution_venue_mode", "all") or "all").strip().lower()
    if mode == "all":
        return True
    source_venue = _probe_node_venue(source_node)
    target_venue = _probe_node_venue(target_node)
    if not source_venue or not target_venue:
        return True
    same_venue = source_venue == target_venue
    if mode == "cross_venue":
        return not same_venue
    if mode == "same_venue":
        return same_venue
    return True


def _probe_candidate_quality(
    strategy,
    *,
    edge,
    source_node,
    target_node,
    quote_a,
    quote_b,
    min_profit_margin: Decimal,
    allow_same_venue: bool,
) -> dict[str, object]:
    odds_a = Decimal(str(quote_a.odds))
    odds_b = Decimal(str(quote_b.odds))
    raw_probability_a = Decimal(1) / odds_a
    raw_probability_b = Decimal(1) / odds_b
    raw_total_probability = raw_probability_a + raw_probability_b
    raw_profit_margin = (Decimal(1) / raw_total_probability) - Decimal(1)
    match_type = _probe_match_type(source_node.instrument, target_node.instrument)
    raw_opportunity = ArbitrageOpportunity(
        instrument_a=source_node.instrument,
        instrument_b=target_node.instrument,
        probability_a=raw_probability_a,
        probability_b=raw_probability_b,
        total_probability=raw_total_probability,
        profit_margin=raw_profit_margin,
        odds_a=odds_a,
        odds_b=odds_b,
        is_same_venue=source_node.instrument.venue_name == target_node.instrument.venue_name,
        match_type=match_type,
        raw_probability_a=raw_probability_a,
        raw_probability_b=raw_probability_b,
        raw_total_probability=raw_total_probability,
        raw_profit_margin=raw_profit_margin,
    )
    opportunity, fee_adjustment_error = _try_strategy_fee_adjusted_opportunity(
        strategy,
        raw_opportunity,
    )
    # The complementary-partition formula above only yields a real arbitrage margin
    # when the two legs form a complementary partition of the outcome space. For a
    # non-complementary relationship (e.g. EQUIVALENT_SELECTION, the same outcome on
    # two books) the number is meaningless, so we suppress the arb margin while
    # leaving the implied probabilities intact for the independent devig value-edge
    # stream.
    is_arbitrage_relationship = str(edge.relationship_type) in ARB_MARGIN_RELATIONSHIP_TYPES
    raw_profit_margin = raw_profit_margin if is_arbitrage_relationship else Decimal(0)
    fee_adjusted_profit_margin = (
        opportunity.profit_margin if is_arbitrage_relationship else Decimal(0)
    )
    devig_diagnostics = _probe_devig_diagnostics(
        strategy,
        edge=edge,
        source_node=source_node,
        target_node=target_node,
        odds_a=odds_a,
        odds_b=odds_b,
        raw_probability_a=raw_probability_a,
        raw_probability_b=raw_probability_b,
        fee_adjusted_probability_a=opportunity.probability_a,
        fee_adjusted_probability_b=opportunity.probability_b,
        raw_profit_margin=raw_profit_margin,
        fee_adjusted_profit_margin=fee_adjusted_profit_margin,
    )
    total_probability = opportunity.total_probability
    profit_margin = fee_adjusted_profit_margin
    observed_ns = max(int(quote_a.received_ns), int(quote_b.received_ns))
    quote_age_a_secs = strategy.quote_age_secs(observed_ns, quote_a.quote)
    quote_age_b_secs = strategy.quote_age_secs(observed_ns, quote_b.quote)
    quote_delta_secs = strategy._quote_pair_skew_secs(quote_a.quote, quote_b.quote)
    fetch_latency_a_secs = strategy.quote_fetch_latency_secs(quote_a.quote)
    fetch_latency_b_secs = strategy.quote_fetch_latency_secs(quote_b.quote)
    available_size_a = strategy.quote_available_size(quote_a.quote)
    available_size_b = strategy.quote_available_size(quote_b.quote)
    freshness = strategy.quote_freshness_thresholds(source_node.instrument, target_node.instrument)
    rejection_bucket = _probe_rejection_bucket(
        edge=edge,
        allow_same_venue=allow_same_venue,
        profit_margin=profit_margin,
        min_profit_margin=min_profit_margin,
        quote_age_a_secs=quote_age_a_secs,
        quote_age_b_secs=quote_age_b_secs,
        quote_delta_secs=quote_delta_secs,
        fetch_latency_a_secs=fetch_latency_a_secs,
        fetch_latency_b_secs=fetch_latency_b_secs,
        available_size_a=available_size_a,
        available_size_b=available_size_b,
        max_quote_age_secs=freshness.max_quote_age_secs,
        max_pair_skew_secs=freshness.max_pair_skew_secs,
        max_fetch_latency_secs=freshness.max_fetch_latency_secs,
    )
    if fee_adjustment_error is not None:
        rejection_bucket = "invalid_odds"
    same_venue = source_node.instrument.venue_name == target_node.instrument.venue_name
    matcher_suspect, suspect_reason = strategy.matcher_suspect_reason(
        source_node.instrument,
        target_node.instrument,
    )
    fixture_suspect, fixture_suspect_reason = _semantic_fixture_suspect_reason(
        strategy,
        source_node.instrument,
        target_node.instrument,
    )
    fresh_quotes = (
        quote_age_a_secs <= freshness.max_quote_age_secs
        and quote_age_b_secs <= freshness.max_quote_age_secs
        and fetch_latency_a_secs <= freshness.max_fetch_latency_secs
        and fetch_latency_b_secs <= freshness.max_fetch_latency_secs
        and quote_delta_secs <= freshness.max_pair_skew_secs
    )
    liquidity_ok = available_size_a > 0 and available_size_b > 0
    threshold_ok = profit_margin >= min_profit_margin
    same_venue_policy = {
        "sameVenue": same_venue,
        "sameFixture": not fixture_suspect,
        "compatibleMarketFamily": bool(edge.same_venue_execution_eligible),
        "freshQuotes": fresh_quotes,
        "sufficientLiquidity": liquidity_ok,
        "thresholdProfit": threshold_ok,
        "executionDisabledUntilRiskEngineApproval": True,
        "suspectReason": suspect_reason,
        "diagnosticSuspect": matcher_suspect,
        "fixtureSuspectReason": fixture_suspect_reason,
    }
    would_execute_same_venue = (
        edge.same_venue_execution_eligible
        and not edge.execution_safe
        and same_venue
        and not fixture_suspect
        and fresh_quotes
        and liquidity_ok
        and threshold_ok
    )
    normalized_a = _normalized_probe_payload(source_node)
    normalized_b = _normalized_probe_payload(target_node)
    return {
        "instrumentIdA": str(source_node.instrument.id),
        "instrumentIdB": str(target_node.instrument.id),
        "venueA": str(source_node.instrument.id.venue),
        "venueB": str(target_node.instrument.id.venue),
        "venuePair": f"{source_node.instrument.id.venue}->{target_node.instrument.id.venue}",
        "marketA": source_node.market_name,
        "marketB": target_node.market_name,
        "marketFamily": _probe_market_family(source_node, target_node),
        "normalizedA": normalized_a,
        "normalizedB": normalized_b,
        "normalizedSport": normalized_a.get("sport") or normalized_b.get("sport"),
        "normalizedScope": normalized_a.get("scope") or normalized_b.get("scope"),
        "outcomeA": source_node.outcome,
        "outcomeB": target_node.outcome,
        "eventNameA": _instrument_display_text(source_node.instrument, "event_name"),
        "eventNameB": _instrument_display_text(target_node.instrument, "event_name"),
        "homeNameA": _instrument_display_text(source_node.instrument, "home_name"),
        "homeNameB": _instrument_display_text(target_node.instrument, "home_name"),
        "awayNameA": _instrument_display_text(source_node.instrument, "away_name"),
        "awayNameB": _instrument_display_text(target_node.instrument, "away_name"),
        "eventKeyA": _probe_event_key_no_time(source_node) or None,
        "eventKeyB": _probe_event_key_no_time(target_node) or None,
        "marketLabelA": _probe_market_label(normalized_a),
        "marketLabelB": _probe_market_label(normalized_b),
        "profitMargin": str(profit_margin),
        "totalProbability": str(total_probability),
        "rawProfitMargin": str(raw_profit_margin),
        "rawTotalProbability": str(raw_total_probability),
        "feeAdjusted": opportunity.fee_adjusted,
        "feeAdjustedProfitMargin": str(fee_adjusted_profit_margin),
        "feeAdjustedTotalProbability": str(opportunity.total_probability),
        "feeDrag": str(opportunity.fee_drag),
        "feeAdjustedOddsA": str(opportunity.fee_adjusted_odds_a or opportunity.odds_a),
        "feeAdjustedOddsB": str(opportunity.fee_adjusted_odds_b or opportunity.odds_b),
        "feeAdjustmentError": fee_adjustment_error,
        "devig": devig_diagnostics,
        "candidateValueClassification": devig_diagnostics.get("valueClassification"),
        "takerFeeRateA": str(opportunity.taker_fee_rate_a),
        "takerFeeRateB": str(opportunity.taker_fee_rate_b),
        "makerRebateRateA": str(opportunity.maker_rebate_rate_a),
        "makerRebateRateB": str(opportunity.maker_rebate_rate_b),
        "winningProfitFeeRateA": str(opportunity.winning_profit_fee_rate_a),
        "winningProfitFeeRateB": str(opportunity.winning_profit_fee_rate_b),
        "basketRebateRate": str(opportunity.basket_rebate_rate),
        "basketBoostRate": str(opportunity.basket_boost_rate),
        "gapToZero": str(max(Decimal(0), -profit_margin)),
        "gapToMinProfitThreshold": str(max(Decimal(0), min_profit_margin - profit_margin)),
        "marginBand": _probe_margin_band(profit_margin),
        "quoteAgeASeconds": round(quote_age_a_secs, 6),
        "quoteAgeBSeconds": round(quote_age_b_secs, 6),
        "quoteDeltaSeconds": round(quote_delta_secs, 6),
        "fetchLatencyASeconds": round(fetch_latency_a_secs, 6),
        "fetchLatencyBSeconds": round(fetch_latency_b_secs, 6),
        "availableSizeA": str(available_size_a),
        "availableSizeB": str(available_size_b),
        "maxQuoteAgeSeconds": freshness.max_quote_age_secs,
        "maxPairSkewSeconds": freshness.max_pair_skew_secs,
        "maxFetchLatencySeconds": freshness.max_fetch_latency_secs,
        "freshnessProfile": freshness.profile,
        "timingFlags": _probe_timing_flags(
            quote_age_a_secs=quote_age_a_secs,
            quote_age_b_secs=quote_age_b_secs,
            quote_delta_secs=quote_delta_secs,
            fetch_latency_a_secs=fetch_latency_a_secs,
            fetch_latency_b_secs=fetch_latency_b_secs,
            max_quote_age_secs=freshness.max_quote_age_secs,
            max_pair_skew_secs=freshness.max_pair_skew_secs,
            max_fetch_latency_secs=freshness.max_fetch_latency_secs,
        ),
        "rejectionBucket": rejection_bucket,
        "blockerReason": rejection_bucket,
        "ruleId": edge.rule_id,
        "templateId": edge.template_id,
        "relationshipType": edge.relationship_type,
        "safetyTier": edge.safety_tier,
        "executionSafe": edge.execution_safe,
        "sameVenueExecutionEligible": edge.same_venue_execution_eligible,
        "dryRunEligible": edge.execution_safe or edge.same_venue_execution_eligible,
        "dryRunEligibilityReason": (
            "strict_execution_safe"
            if edge.execution_safe
            else "same_venue_risk_engine_required"
            if edge.same_venue_execution_eligible
            else "semantic_topology_only"
        ),
        "sameVenueRiskPolicy": same_venue_policy,
        "wouldExecuteSameVenueDryRun": would_execute_same_venue,
    }


def _strategy_fee_adjusted_opportunity(strategy, opportunity: ArbitrageOpportunity):
    adjuster = getattr(strategy, "fee_adjusted_opportunity", None)
    if callable(adjuster):
        return adjuster(opportunity)
    return opportunity


def _try_strategy_fee_adjusted_opportunity(
    strategy,
    opportunity: ArbitrageOpportunity,
) -> tuple[ArbitrageOpportunity, str | None]:
    try:
        return _strategy_fee_adjusted_opportunity(strategy, opportunity), None
    except (ArithmeticError, ValueError) as exc:
        return opportunity, str(exc)


def _probe_devig_diagnostics(
    strategy,
    *,
    edge,
    source_node,
    target_node,
    odds_a: Decimal,
    odds_b: Decimal,
    raw_probability_a: Decimal,
    raw_probability_b: Decimal,
    fee_adjusted_probability_a: Decimal,
    fee_adjusted_probability_b: Decimal,
    raw_profit_margin: Decimal,
    fee_adjusted_profit_margin: Decimal,
) -> dict[str, object]:
    config = getattr(strategy, "_config", None)
    if not bool(getattr(config, "devig_enabled", False)):
        return {
            "enabled": False,
            "bookStatus": "disabled",
            "valueClassification": "devig_disabled",
        }
    if not bool(getattr(config, "value_diagnostics_enabled", True)):
        return {
            "enabled": True,
            "bookStatus": "disabled",
            "valueClassification": "value_diagnostics_disabled",
        }

    venue_a = str(source_node.instrument.id.venue).upper()
    venue_b = str(target_node.instrument.id.venue).upper()
    reference_venues = getattr(config, "devig_reference_venues", None)
    reference_allowed = (
        not reference_venues or venue_a in reference_venues or venue_b in reference_venues
    )
    book_status = _probe_devig_book_status(
        edge=edge,
        venue_a=venue_a,
        venue_b=venue_b,
        reference_allowed=reference_allowed,
    )
    if book_status == "incomplete_book_no_devig":
        return {
            "enabled": True,
            "bookStatus": book_status,
            "valueClassification": "reference_book_incomplete",
            "referenceVenue": _probe_devig_reference_venue(venue_a, venue_b),
            "referenceQuality": "incomplete",
        }

    devigged_book = None
    try:
        devigged = getattr(strategy, "devigged_book", None)
        if callable(devigged):
            devigged_book = devigged((odds_a, odds_b))
    except (ArithmeticError, ValueError) as exc:
        return {
            "enabled": True,
            "bookStatus": "devig_method_failed",
            "valueClassification": "devig_method_failed",
            "error": str(exc)[:240],
            "referenceVenue": _probe_devig_reference_venue(venue_a, venue_b),
            "referenceQuality": book_status,
        }
    if devigged_book is None:
        return {
            "enabled": False,
            "bookStatus": "disabled",
            "valueClassification": "devig_disabled",
        }

    fair_a, fair_b = devigged_book.no_vig_probabilities
    gross_value_edge_a = fair_a - raw_probability_a
    gross_value_edge_b = fair_b - raw_probability_b
    fee_adjusted_value_edge_a = fair_a - fee_adjusted_probability_a
    fee_adjusted_value_edge_b = fair_b - fee_adjusted_probability_b
    max_gross_value_edge = max(gross_value_edge_a, gross_value_edge_b)
    max_fee_adjusted_value_edge = max(fee_adjusted_value_edge_a, fee_adjusted_value_edge_b)
    best_side = "A" if fee_adjusted_value_edge_a >= fee_adjusted_value_edge_b else "B"
    best_probability = (
        fee_adjusted_probability_a if best_side == "A" else fee_adjusted_probability_b
    )
    relative_value_edge = (
        max_fee_adjusted_value_edge / best_probability if best_probability > 0 else Decimal(0)
    )
    classification = _probe_value_classification(
        config=config,
        venue_a=venue_a,
        venue_b=venue_b,
        book_overround=devigged_book.overround,
        execution_safe=bool(getattr(edge, "execution_safe", False)),
        same_venue_execution_eligible=bool(
            getattr(edge, "same_venue_execution_eligible", False),
        ),
        semantic_blocker_reason=(
            ""
            if bool(getattr(edge, "execution_safe", False))
            else _semantic_non_execution_bucket(edge)
        ),
        is_arbitrage_relationship=(
            str(getattr(edge, "relationship_type", "")) in ARB_MARGIN_RELATIONSHIP_TYPES
        ),
        raw_profit_margin=raw_profit_margin,
        fee_adjusted_profit_margin=fee_adjusted_profit_margin,
        max_gross_value_edge=max_gross_value_edge,
        max_fee_adjusted_value_edge=max_fee_adjusted_value_edge,
    )
    return {
        "enabled": True,
        "bookStatus": book_status,
        "referenceVenue": _probe_devig_reference_venue(venue_a, venue_b),
        "referenceQuality": book_status,
        "rawImpliedProbabilityA": str(raw_probability_a),
        "rawImpliedProbabilityB": str(raw_probability_b),
        "noVigProbabilityA": str(fair_a),
        "noVigProbabilityB": str(fair_b),
        "bookOverround": str(devigged_book.overround),
        "bookVig": str(devigged_book.vig),
        "devigMethod": devigged_book.method,
        "devigMethodReason": devigged_book.method_reason,
        "devigConvergenceStatus": devigged_book.convergence_status,
        "devigIterations": devigged_book.iterations,
        "devigDelta": str(devigged_book.delta),
        "devigZ": str(devigged_book.z) if devigged_book.z is not None else None,
        "grossValueEdgeA": str(gross_value_edge_a),
        "grossValueEdgeB": str(gross_value_edge_b),
        "feeAdjustedValueEdgeA": str(fee_adjusted_value_edge_a),
        "feeAdjustedValueEdgeB": str(fee_adjusted_value_edge_b),
        "grossValueEdge": str(max_gross_value_edge),
        "feeAdjustedValueEdge": str(max_fee_adjusted_value_edge),
        "relativeValueEdge": str(relative_value_edge),
        "bestValueSide": best_side,
        "valueClassification": classification,
        "valueExecutionEnabled": bool(getattr(config, "value_execution_enabled", False)),
        "valueExecutionBlockedReason": (
            ""
            if bool(getattr(config, "value_execution_enabled", False))
            else "value_execution_disabled"
        ),
    }


def _probe_devig_book_status(
    *,
    edge,
    venue_a: str,
    venue_b: str,
    reference_allowed: bool,
) -> str:
    if not reference_allowed:
        return "incomplete_book_no_devig"
    if venue_a == venue_b:
        return "same_venue_complete_pair"
    if bool(getattr(edge, "execution_safe", False)) or bool(
        getattr(edge, "same_venue_execution_eligible", False),
    ):
        return "synthetic_cross_venue_pair"
    return "incomplete_book_no_devig"


def _probe_devig_reference_venue(venue_a: str, venue_b: str) -> str:
    if venue_a == venue_b:
        return venue_a
    return f"mixed:{venue_a}+{venue_b}"


def _probe_value_classification(
    *,
    config,
    venue_a: str,
    venue_b: str,
    book_overround: Decimal | None,
    execution_safe: bool,
    same_venue_execution_eligible: bool,
    semantic_blocker_reason: str,
    is_arbitrage_relationship: bool,
    raw_profit_margin: Decimal,
    fee_adjusted_profit_margin: Decimal,
    max_gross_value_edge: Decimal,
    max_fee_adjusted_value_edge: Decimal,
) -> str:
    min_value_edge = Decimal(str(getattr(config, "min_value_edge", Decimal("0.015"))))
    min_profit_margin = Decimal(str(getattr(config, "min_profit_margin", 0)))
    if (
        is_arbitrage_relationship
        and venue_a == venue_b
        and book_overround is not None
        and book_overround < Decimal(1) - SAME_VENUE_UNDERROUND_TOLERANCE
    ):
        # A single book cannot underround its own complementary market; this pair is a
        # data error (e.g. a wrongly signed handicap leg), never a locked arbitrage. Only
        # arb relationships form a complete book -- an EQUIVALENT_SELECTION pair quotes
        # ONE outcome twice, so its implied sum is legitimately below 1.
        return "suspect_same_venue_underround"
    if is_arbitrage_relationship and fee_adjusted_profit_margin >= min_profit_margin:
        if execution_safe:
            return "locked_execution_safe_arbitrage"
        if same_venue_execution_eligible:
            return "same_venue_positive_dry_run_edge"
        return f"positive_non_executable_semantic_edge:{semantic_blocker_reason or 'unknown'}"
    if raw_profit_margin > 0 and fee_adjusted_profit_margin <= 0:
        return "fee_or_vig_erased_edge"
    if max_gross_value_edge > 0 and max_fee_adjusted_value_edge <= 0:
        return "fee_or_vig_erased_edge"
    if max_fee_adjusted_value_edge >= min_value_edge:
        if "POLYMARKET" in {venue_a, venue_b}:
            return "prediction_market_value_edge"
        return "sportsbook_value_edge"
    return "vig_only_edge"


def _probe_match_type(instrument_a, instrument_b) -> str:
    if getattr(instrument_a, "market_name", None) == getattr(instrument_b, "market_name", None):
        return "same_market"
    if getattr(instrument_a, "venue_name", None) == getattr(instrument_b, "venue_name", None):
        return "cross_market"
    return "cross_venue"


def _semantic_fixture_suspect_reason(
    strategy,
    instrument_a,
    instrument_b,
) -> tuple[bool, str]:
    checker = getattr(strategy, "semantic_fixture_suspect_reason", None)
    if checker is None:
        checker = getattr(strategy, "_semantic_fixture_suspect_reason", None)
    if checker is None:
        checker = getattr(strategy, "matcher_suspect_reason", None)
    if checker is None:
        from nautilus_trader.examples.strategies.betting_arbitrage import (
            BettingArbitrageStrategy,
        )

        checker = BettingArbitrageStrategy.semantic_fixture_suspect_reason
    return checker(instrument_a, instrument_b)


def _probe_rejection_bucket(
    *,
    edge,
    allow_same_venue: bool,
    profit_margin: Decimal,
    min_profit_margin: Decimal,
    quote_age_a_secs: float,
    quote_age_b_secs: float,
    quote_delta_secs: float,
    fetch_latency_a_secs: float,
    fetch_latency_b_secs: float,
    available_size_a: Decimal,
    available_size_b: Decimal,
    max_quote_age_secs: float,
    max_pair_skew_secs: float,
    max_fetch_latency_secs: float,
) -> str:
    if not edge.execution_safe and not allow_same_venue:
        return _semantic_non_execution_bucket(edge)
    if (
        fetch_latency_a_secs > max_fetch_latency_secs
        or fetch_latency_b_secs > max_fetch_latency_secs
    ):
        return "fetch_latency"
    if quote_age_a_secs > max_quote_age_secs or quote_age_b_secs > max_quote_age_secs:
        return "stale"
    if quote_delta_secs > max_pair_skew_secs:
        return "cross_cycle"
    if available_size_a <= 0 or available_size_b <= 0:
        return "liquidity"
    if profit_margin <= 0:
        return "negative_margin"
    if profit_margin < min_profit_margin:
        return "below_threshold"
    return "positive"


def _probe_timing_flags(
    *,
    quote_age_a_secs: float,
    quote_age_b_secs: float,
    quote_delta_secs: float,
    fetch_latency_a_secs: float,
    fetch_latency_b_secs: float,
    max_quote_age_secs: float,
    max_pair_skew_secs: float,
    max_fetch_latency_secs: float,
) -> list[str]:
    flags: list[str] = []
    if (
        fetch_latency_a_secs > max_fetch_latency_secs
        or fetch_latency_b_secs > max_fetch_latency_secs
    ):
        flags.append("fetch_latency")
    if quote_age_a_secs > max_quote_age_secs or quote_age_b_secs > max_quote_age_secs:
        flags.append("quote_age")
    if quote_delta_secs > max_pair_skew_secs:
        flags.append("pair_skew")
    return flags or ["fresh"]


def _probe_timing_clean(timing_flags: object) -> bool:
    # A pair is timing-clean only when it carries no staleness flag (pair_skew /
    # quote_age / fetch_latency / stale), i.e. its flags are a subset of {"fresh"}.
    # ``_probe_timing_flags`` always yields at least ["fresh"], so a missing signal is
    # treated as clean rather than penalised.
    if not isinstance(timing_flags, (list, tuple)):
        return True
    return {str(flag) for flag in timing_flags} <= {"fresh"}


def _record_timing_samples(
    counters: ProbeProfitabilityCounters,
    quality: dict[str, object],
) -> None:
    quote_age_a_secs = float(quality.get("quoteAgeASeconds") or 0.0)
    quote_age_b_secs = float(quality.get("quoteAgeBSeconds") or 0.0)
    fetch_latency_a_secs = float(quality.get("fetchLatencyASeconds") or 0.0)
    fetch_latency_b_secs = float(quality.get("fetchLatencyBSeconds") or 0.0)
    quote_delta_secs = float(quality.get("quoteDeltaSeconds") or 0.0)
    venue_a = str(quality.get("venueA") or "")
    venue_b = str(quality.get("venueB") or "")
    # A dormant stream leg carries a quote older than the SLO threshold on a venue
    # whose realtime stream is healthy: the stream pushes only on book change, so the
    # quote is current and its wall-clock age is not a staleness violation. The
    # adapter exposes stream health (connected flag) but no per-leg last-message
    # timestamp, so there is no substitute staleness measure — these legs are
    # excluded from the quote-age/pair-skew SLO surfaces and counted separately.
    # Per-candidate freshness gates (maxQuoteAgeSeconds etc.) are unaffected. An
    # unhealthy stream disables the exclusion: its quote ages are genuinely stale.
    stream_dormant_leg_a = (
        counters.stream_healthy_venues.get(venue_a.upper(), False)
        and quote_age_a_secs > counters.live_quote_age_slo_secs
    )
    stream_dormant_leg_b = (
        counters.stream_healthy_venues.get(venue_b.upper(), False)
        and quote_age_b_secs > counters.live_quote_age_slo_secs
    )
    counters.stream_dormant_leg_count += int(stream_dormant_leg_a) + int(stream_dormant_leg_b)
    if not stream_dormant_leg_a:
        counters.quote_age_samples_secs.append(quote_age_a_secs)
    if not stream_dormant_leg_b:
        counters.quote_age_samples_secs.append(quote_age_b_secs)
    if venue_a:
        counters.quote_age_samples_by_venue_secs.setdefault(venue_a, []).append(quote_age_a_secs)
    if venue_b:
        counters.quote_age_samples_by_venue_secs.setdefault(venue_b, []).append(quote_age_b_secs)
    counters.fetch_latency_samples_secs.extend([fetch_latency_a_secs, fetch_latency_b_secs])
    if not (stream_dormant_leg_a or stream_dormant_leg_b):
        counters.pair_skew_samples_secs.append(quote_delta_secs)
    venue_pair = str(quality["venuePair"])
    counters.pair_skew_samples_by_venue_pair.setdefault(venue_pair, []).append(quote_delta_secs)
    if str(quality.get("freshnessProfile") or "") == "live":
        _record_live_timing_slo(
            counters,
            quote_age_a_secs=quote_age_a_secs,
            quote_age_b_secs=quote_age_b_secs,
            fetch_latency_a_secs=fetch_latency_a_secs,
            fetch_latency_b_secs=fetch_latency_b_secs,
            quote_delta_secs=quote_delta_secs,
            max_fetch_latency_secs=float(quality.get("maxFetchLatencySeconds") or 0.0),
            max_pair_skew_secs=float(quality.get("maxPairSkewSeconds") or 0.0),
            stream_dormant_leg_a=stream_dormant_leg_a,
            stream_dormant_leg_b=stream_dormant_leg_b,
        )


def _record_probe_quality(
    counters: ProbeProfitabilityCounters,
    quality: dict[str, object],
) -> None:
    margin = Decimal(str(quality["profitMargin"]))
    margin_band = str(quality["marginBand"])
    rejection_bucket = str(quality["rejectionBucket"])
    venue_pair = str(quality["venuePair"])
    market_family = str(quality["marketFamily"])
    counters.margin_bands[margin_band] += 1
    counters.rag_bands[_probe_rag_band(margin)] += 1
    counters.rejection_buckets[rejection_bucket] += 1
    counters.freshness_profiles[str(quality.get("freshnessProfile") or "unknown")] += 1
    if quality.get("feeAdjusted"):
        counters.fee_adjusted_edges += 1
        counters.fee_drag_samples.append(float(quality.get("feeDrag") or 0.0))
        _record_fee_impact_bucket(counters, quality)
    _record_devig_quality(counters, quality)
    quote_age_a_secs = float(quality.get("quoteAgeASeconds") or 0.0)
    quote_age_b_secs = float(quality.get("quoteAgeBSeconds") or 0.0)
    fetch_latency_a_secs = float(quality.get("fetchLatencyASeconds") or 0.0)
    fetch_latency_b_secs = float(quality.get("fetchLatencyBSeconds") or 0.0)
    venue_a = str(quality.get("venueA") or "")
    venue_b = str(quality.get("venueB") or "")
    _record_timing_samples(counters, quality)
    _record_same_venue_dry_run_quality(counters, quality)
    if rejection_bucket in _SEMANTIC_NON_EXECUTION_BUCKETS:
        counters.semantic_blocked_reasons[_semantic_blocked_reason(quality)] += 1
        counters.semantic_blocked_relationships[_semantic_blocked_relationship(quality)] += 1
        samples = counters.blocker_samples.setdefault(rejection_bucket, [])
        if len(samples) < 5:
            samples.append(
                {
                    "blockerReason": rejection_bucket,
                    "instrumentIdA": quality.get("instrumentIdA"),
                    "instrumentIdB": quality.get("instrumentIdB"),
                    "venuePair": quality.get("venuePair"),
                    "marketFamily": quality.get("marketFamily"),
                    "relationshipType": quality.get("relationshipType"),
                    "safetyTier": quality.get("safetyTier"),
                    "normalizedA": quality.get("normalizedA"),
                    "normalizedB": quality.get("normalizedB"),
                },
            )
    timing_flags = quality.get("timingFlags")
    if isinstance(timing_flags, list):
        for flag in timing_flags:
            counters.timing_flags[str(flag)] += 1
    counters.venue_pairs.setdefault(venue_pair, Counter())[rejection_bucket] += 1
    counters.market_families.setdefault(market_family, Counter())[rejection_bucket] += 1
    _record_venue_quote_health(
        counters,
        venue=venue_a,
        quote_age_secs=quote_age_a_secs,
        fetch_latency_secs=fetch_latency_a_secs,
    )
    _record_venue_quote_health(
        counters,
        venue=venue_b,
        quote_age_secs=quote_age_b_secs,
        fetch_latency_secs=fetch_latency_b_secs,
    )
    is_arbitrage_relationship = (
        str(quality.get("relationshipType") or "") in ARB_MARGIN_RELATIONSHIP_TYPES
    )
    if is_arbitrage_relationship and margin > 0:
        if _probe_timing_clean(quality.get("timingFlags")):
            counters.samples.append((margin, quality))
        else:
            counters.skewed_samples.append((margin, quality))
    elif margin > Decimal("-0.05"):
        counters.negative_samples.append((margin, quality))


def _record_devig_quality(
    counters: ProbeProfitabilityCounters,
    quality: dict[str, object],
) -> None:
    devig = quality.get("devig")
    if not isinstance(devig, dict) or not bool(devig.get("enabled")):
        return
    counters.devig_evaluated_edges += 1
    book_status = str(devig.get("bookStatus") or "unknown")
    if book_status in {"same_venue_complete_pair", "synthetic_cross_venue_pair"}:
        counters.devig_complete_books += 1
    else:
        counters.devig_incomplete_books += 1
    method = str(devig.get("devigMethod") or "none")
    method_reason = str(devig.get("devigMethodReason") or "none")
    convergence = str(devig.get("devigConvergenceStatus") or "none")
    value_classification = str(devig.get("valueClassification") or "unknown")
    counters.devig_method_counts[method] += 1
    counters.devig_method_reason_counts[method_reason] += 1
    counters.devig_convergence_counts[convergence] += 1
    counters.devig_value_buckets[value_classification] += 1
    if "bookOverround" in devig:
        counters.overround_samples.append(float(devig.get("bookOverround") or 0.0))
    if "bookVig" in devig:
        counters.vig_samples.append(float(devig.get("bookVig") or 0.0))
    gross_edge = Decimal(str(devig.get("grossValueEdge") or 0))
    net_edge = Decimal(str(devig.get("feeAdjustedValueEdge") or 0))
    counters.gross_value_edge_samples.append(float(gross_edge))
    counters.fee_adjusted_value_edge_samples.append(float(net_edge))
    if value_classification in {
        "sportsbook_value_edge",
        "prediction_market_value_edge",
        "locked_execution_safe_arbitrage",
        "same_venue_positive_dry_run_edge",
    } or value_classification.startswith("positive_non_executable_semantic_edge:"):
        counters.value_samples.append((net_edge, quality))
    if value_classification == "fee_or_vig_erased_edge":
        counters.vig_erased_samples.append((gross_edge, quality))


def _record_fee_impact_bucket(
    counters: ProbeProfitabilityCounters,
    quality: dict[str, object],
) -> None:
    raw_margin = Decimal(str(quality.get("rawProfitMargin") or 0))
    adjusted_margin = Decimal(str(quality.get("feeAdjustedProfitMargin") or raw_margin))
    fee_drag = Decimal(str(quality.get("feeDrag") or 0))
    if adjusted_margin > raw_margin:
        counters.fee_impact_buckets["fee_or_incentive_helped"] += 1
    elif adjusted_margin < raw_margin:
        counters.fee_impact_buckets["fee_hurt"] += 1
    else:
        counters.fee_impact_buckets["fee_neutral"] += 1
    if raw_margin > 0 and adjusted_margin <= 0:
        counters.fee_impact_buckets["raw_positive_fee_adjusted_negative"] += 1
    elif raw_margin <= 0 and adjusted_margin > 0:
        counters.fee_impact_buckets["raw_negative_fee_adjusted_positive"] += 1
    if fee_drag < 0:
        counters.fee_impact_buckets["net_rebate_or_boost"] += 1
    elif fee_drag > 0:
        counters.fee_impact_buckets["net_fee_drag"] += 1


def _record_same_venue_dry_run_quality(
    counters: ProbeProfitabilityCounters,
    quality: dict[str, object],
) -> None:
    if not bool(quality.get("sameVenueExecutionEligible")) or bool(quality.get("executionSafe")):
        return
    if bool(quality.get("wouldExecuteSameVenueDryRun")):
        counters.same_venue_dry_run_passes += 1
        return

    counters.same_venue_dry_run_failures += 1
    policy = quality.get("sameVenueRiskPolicy")
    if not isinstance(policy, dict):
        return
    for reason in (
        "sameVenue",
        "sameFixture",
        "compatibleMarketFamily",
        "freshQuotes",
        "sufficientLiquidity",
        "thresholdProfit",
    ):
        if policy.get(reason) is False:
            counters.same_venue_dry_run_failure_reasons[reason] += 1


def _semantic_blocked_reason(quality: dict[str, object]) -> str:
    return str(
        quality.get("blockerReason")
        or quality.get("rejectionBucket")
        or CoverageBlockerReason.UNSUPPORTED_MARKET_FAMILY.value,
    )


def _semantic_blocked_relationship(quality: dict[str, object]) -> str:
    safety_tier = str(quality.get("safetyTier") or "unknown")
    relationship_type = str(quality.get("relationshipType") or "unknown")
    return f"{safety_tier}:{relationship_type}"


_SEMANTIC_NON_EXECUTION_BUCKETS = frozenset(
    {
        CoverageBlockerReason.AMBIGUOUS_RESOLUTION.value,
        CoverageBlockerReason.EQUIVALENT_SELECTION.value,
        CoverageBlockerReason.FIXTURE_IDENTITY_MISMATCH.value,
        CoverageBlockerReason.NO_SEMANTIC_EDGE.value,
        CoverageBlockerReason.PARTIAL_SETTLEMENT.value,
        CoverageBlockerReason.PROVIDER_SCOPE_MISMATCH.value,
        CoverageBlockerReason.SAME_MARKET_PARAMS_MISMATCH.value,
        CoverageBlockerReason.SAME_VENUE_POLICY.value,
        CoverageBlockerReason.SCOPE_MISMATCH.value,
        CoverageBlockerReason.TOPOLOGY_ONLY.value,
        CoverageBlockerReason.UNKNOWN_SETTLEMENT.value,
        CoverageBlockerReason.UNSUPPORTED_MARKET_FAMILY.value,
        CoverageBlockerReason.VOID_SETTLEMENT.value,
    },
)


def _semantic_non_execution_bucket(edge: object) -> str:
    relationship_type = str(getattr(edge, "relationship_type", "") or "")
    safety_tier = str(getattr(edge, "safety_tier", "") or "")
    caveats = {str(caveat) for caveat in tuple(getattr(edge, "caveats", ()) or ()) if str(caveat)}
    if bool(getattr(edge, "same_venue_execution_eligible", False)):
        return CoverageBlockerReason.SAME_VENUE_POLICY.value
    if bool(getattr(edge, "execution_safe", False)):
        # An execution-safe edge (a promoted void middle or partial-compatible lock) is no
        # longer a settlement-risk reject: do not force it into the void/partial bucket by
        # relationship type. It belongs in the margin buckets, so surface it as positive
        # rather than mislabeling the settlement shape it was proven safe against.
        return CoverageBlockerReason.POSITIVE.value
    relationship_bucket = {
        "EQUIVALENT_SELECTION": CoverageBlockerReason.EQUIVALENT_SELECTION.value,
        "VOID_COMPATIBLE_HEDGE": CoverageBlockerReason.VOID_SETTLEMENT.value,
        "PARTIAL_SETTLEMENT_HEDGE": CoverageBlockerReason.PARTIAL_SETTLEMENT.value,
    }.get(relationship_type)
    if relationship_bucket:
        return relationship_bucket
    normalized_caveats = " ".join(caveat.lower() for caveat in caveats)
    for needle, bucket in (
        ("unknown", CoverageBlockerReason.UNKNOWN_SETTLEMENT.value),
        ("ambiguous", CoverageBlockerReason.AMBIGUOUS_RESOLUTION.value),
        ("50_50", CoverageBlockerReason.AMBIGUOUS_RESOLUTION.value),
        ("provider_scope", CoverageBlockerReason.PROVIDER_SCOPE_MISMATCH.value),
        ("params_mismatch", CoverageBlockerReason.SAME_MARKET_PARAMS_MISMATCH.value),
        ("scope", CoverageBlockerReason.SCOPE_MISMATCH.value),
    ):
        if needle in normalized_caveats:
            return bucket
    if safety_tier == "TOPOLOGY_SAFE":
        return CoverageBlockerReason.TOPOLOGY_ONLY.value
    return CoverageBlockerReason.UNSUPPORTED_MARKET_FAMILY.value


def _record_venue_quote_health(
    counters: ProbeProfitabilityCounters,
    *,
    venue: str,
    quote_age_secs: float,
    fetch_latency_secs: float,
) -> None:
    if not venue:
        return
    counters.venue_quote_counts[venue] += 1
    counters.venue_max_quote_age_secs[venue] = max(
        counters.venue_max_quote_age_secs.get(venue, 0.0),
        quote_age_secs,
    )
    counters.venue_max_fetch_latency_secs[venue] = max(
        counters.venue_max_fetch_latency_secs.get(venue, 0.0),
        fetch_latency_secs,
    )


def _probe_margin_band(profit_margin: Decimal) -> str:
    if profit_margin > 0:
        return "positive"
    if profit_margin >= Decimal("-0.01"):
        return "0% to -1%"
    if profit_margin >= Decimal("-0.02"):
        return "-1% to -2%"
    if profit_margin >= Decimal("-0.05"):
        return "-2% to -5%"
    return "< -5%"


def _probe_rag_band(profit_margin: Decimal) -> str:
    """
    Coarse RAG rollup of a candidate's profit margin, for at-a-glance triage.

    green = profitable (> 0); amber = slightly unprofitable (0% to -5%); red =
    unprofitable (< -5%). Applies to same-venue and cross-venue candidates alike, so
    unprofitable cross-venue candidates are surfaced (not just executable ones).

    """
    if profit_margin > 0:
        return "green"
    if profit_margin >= Decimal("-0.05"):
        return "amber"
    return "red"


def _probe_market_family(source_node, target_node) -> str:
    families: list[str] = []
    for node in (source_node, target_node):
        try:
            normalized = MarketNormalizer.normalize(node.instrument)
            families.append(normalized.market_family or normalized.market_type)
        except (AttributeError, TypeError, ValueError):
            families.append(str(getattr(node, "market_type", "") or node.market_name))
    return " + ".join(families)


def _normalized_probe_payload(node) -> dict[str, object]:
    try:
        normalized = MarketNormalizer.normalize(node.instrument)
    except (AttributeError, TypeError, ValueError):
        return {
            "sport": "",
            "scope": "",
            "marketFamily": str(getattr(node, "market_type", "") or node.market_name),
            "marketType": str(getattr(node, "market_type", "") or node.market_name),
            "selectionRole": str(getattr(node, "outcome", "") or ""),
            "params": {},
            "line": None,
            "providerRuleFlags": [],
        }
    params = dict(normalized.params)
    return {
        "sport": normalized.sport,
        "scope": normalized.scope,
        "marketFamily": normalized.market_family,
        "marketType": normalized.market_type,
        "selectionRole": normalized.selection,
        "params": params,
        "line": params.get("line") or params.get("handicap") or params.get("total"),
        "providerRuleFlags": list(normalized.rules_flags),
        "resolutionPolicy": dict(normalized.resolution_policy),
    }


def _instrument_display_text(instrument, attribute: str) -> str | None:
    value = getattr(instrument, attribute, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _probe_market_label(normalized: dict[str, object]) -> str | None:
    market_type = str(normalized.get("marketType") or "").strip()
    line = normalized.get("line")
    selection = str(normalized.get("selectionRole") or "").strip()
    parts = []
    if market_type:
        parts.append(market_type.upper())
    if line not in (None, ""):
        parts.append(str(line))
    if selection:
        parts.append(f"({selection.upper()})")
    return " ".join(parts) or None


def _quoted_probe_edge(edge, nodes, quotes) -> tuple[object, object, object, object] | None:
    quote_a = quotes.get(edge.source_node_id)
    quote_b = quotes.get(edge.target_node_id)
    if quote_a is None or quote_b is None:
        return None

    source_node = nodes.get(edge.source_node_id)
    target_node = nodes.get(edge.target_node_id)
    if source_node is None or target_node is None:
        return None

    return source_node, target_node, quote_a, quote_b


def _record_probe_opportunity(
    counters: ProbeProfitabilityCounters,
    *,
    opportunity,
    edge,
    source_node,
    target_node,
    allow_same_venue: bool,
    min_profit_margin: Decimal,
    timing_clean: bool = True,
) -> None:
    is_arbitrage_relationship = (
        str(getattr(edge, "relationship_type", "")) in ARB_MARGIN_RELATIONSHIP_TYPES
    )
    is_positive = is_arbitrage_relationship and opportunity.profit_margin > 0
    meets_threshold = is_arbitrage_relationship and opportunity.profit_margin >= min_profit_margin
    # A stale-sibling pair skew (SXBET streams per-market) can flash a transient
    # underround that looks positive; keep those out of the headline positive/threshold
    # counters so a genuine fresh candidate crossing the threshold is not masked.
    if allow_same_venue:
        if timing_clean:
            counters.positive_same_venue += int(is_positive)
            counters.threshold_same_venue += int(meets_threshold)
        else:
            counters.positive_same_venue_skewed += int(is_positive)
            counters.threshold_same_venue_skewed += int(meets_threshold)
    elif timing_clean:
        counters.positive_execution += int(is_positive)
        counters.threshold_execution += int(meets_threshold)
    else:
        counters.positive_execution_skewed += int(is_positive)
        counters.threshold_execution_skewed += int(meets_threshold)

    _ = opportunity, edge, source_node, target_node


if __name__ == "__main__":
    raise SystemExit(main())
