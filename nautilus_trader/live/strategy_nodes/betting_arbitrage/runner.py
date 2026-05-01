from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from decimal import Decimal
import json
import threading
import time
from pathlib import Path
from typing import Any

from nautilus_trader.adapters.betting.semantics import MarketNormalizer
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import build_trading_node_config
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import default_render_paths
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import load_manifest
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import write_manifest_snapshot
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import write_rendered_node_config
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    SemanticCacheStatus,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    ensure_semantic_cache_ready,
)


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
    margin_bands: Counter[str] = field(default_factory=Counter)
    rejection_buckets: Counter[str] = field(default_factory=Counter)
    venue_pairs: dict[str, Counter[str]] = field(default_factory=dict)
    market_families: dict[str, Counter[str]] = field(default_factory=dict)
    samples: list[tuple[Decimal, dict[str, object]]] = field(default_factory=list)
    negative_samples: list[tuple[Decimal, dict[str, object]]] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        self.samples.sort(key=lambda item: item[0], reverse=True)
        self.negative_samples.sort(key=lambda item: item[0], reverse=True)
        return {
            "quoted_edges": self.quoted_edges,
            "positive_execution": self.positive_execution,
            "positive_same_venue": self.positive_same_venue,
            "threshold_execution": self.threshold_execution,
            "threshold_same_venue": self.threshold_same_venue,
            "margin_bands": dict(self.margin_bands),
            "rejection_buckets": dict(self.rejection_buckets),
            "venue_pairs": {
                key: dict(counter)
                for key, counter in sorted(self.venue_pairs.items(), key=lambda item: item[0])
            },
            "market_families": {
                key: dict(counter)
                for key, counter in sorted(self.market_families.items(), key=lambda item: item[0])
            },
            "sample_candidates": [payload for _, payload in self.samples[:10]],
            "negative_near_misses": [payload for _, payload in self.negative_samples[:10]],
        }


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

    def run(self) -> None:
        min_profit_margin = Decimal(str(self._manifest.strategy.min_profit_margin))
        while not self._stop_event.wait(self._interval_secs):
            runtime_probe = _collect_runtime_probe_payload(
                self._strategy,
                min_profit_margin=min_profit_margin,
                elapsed_seconds=time.monotonic() - self._started_at,
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
        node.dispose()
        raise
    finally:
        if args.command == "run" and args.no_start:
            node.dispose()


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
            require_rust_semantic_topology=bool(args.require_rust_semantic_topology),
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
        node.dispose()


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
    if context.manifest.validation_mode and hasattr(node, "trader"):
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
        node.dispose()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")


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
        "compatibilityVersion": payload.get("compatibility_version"),
        "compatibilityScope": payload.get("compatibility_scope"),
        "compatible": payload.get("compatible", True),
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
    require_rust_semantic_topology: bool,
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

    try:
        while time.monotonic() - started_at < timeout_seconds:
            latest_payload = _collect_runtime_probe_payload(
                strategy,
                min_profit_margin=min_profit_margin,
                elapsed_seconds=time.monotonic() - started_at,
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
                require_rust_semantic_topology=require_rust_semantic_topology,
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

    semantic_diagnostics = latest_payload.get("semanticDiagnostics", {})
    candidate_quality = latest_payload.get("candidateQuality", {})
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
    raise RuntimeProbeCoverageError(
        "Runtime probe did not observe the required semantic coverage "
        f"(connected_nodes={latest_payload['connectedNodes']}, "
        f"semantic_match_instruments={latest_payload['semanticMatchInstruments']}, "
        f"quoted_semantic_match_instruments={latest_payload['quotedSemanticMatchInstruments']}, "
        f"quoted_edges={latest_payload['quotedEdges']}, "
        f"positive_margin_candidates={latest_payload['positiveMarginCandidates']['total']}, "
        f"graph_engine={latest_payload.get('graphEngine')}, "
        f"topology_source={latest_payload.get('topologySource')}, "
        f"semantic_template_count={latest_payload.get('semanticTemplateCount')}, "
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


def _collect_runtime_probe_payload(
    strategy,
    *,
    min_profit_margin: Decimal,
    elapsed_seconds: float,
) -> dict[str, object]:
    graph = strategy.opportunity_graph
    stats = strategy.get_stats()
    snapshot = _snapshot_probe_graph_state(graph)
    semantic_diagnostics = _semantic_probe_diagnostics(graph)
    if snapshot is None:
        venue_coverage = _venue_pair_coverage(
            strategy,
            edges=[],
            nodes={},
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )
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
            "candidateQuality": _empty_candidate_quality_payload(),
            "strategyStats": stats,
            "semanticDiagnostics": semantic_diagnostics,
            "venueCoverage": venue_coverage,
            "sampleCandidates": [],
            "negativeNearMisses": [],
        }
    execution_safe_edges = sum(1 for edge in snapshot["edges"] if edge.execution_safe)
    same_venue_eligible_edges = sum(
        1 for edge in snapshot["edges"] if edge.same_venue_execution_eligible
    )
    profitability = _probe_edge_profitability(
        strategy,
        edges=snapshot["edges"],
        nodes=snapshot["nodes"],
        quotes=snapshot["quotes"],
        min_profit_margin=min_profit_margin,
    )

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
        "semanticMatchInstruments": len(snapshot["matched_node_ids"]),
        "quotedSemanticMatchInstruments": sum(
            1 for node_id in snapshot["matched_node_ids"] if node_id in snapshot["quotes"]
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
        "candidateQuality": {
            "quotedEdges": profitability["quoted_edges"],
            "marginBands": profitability["margin_bands"],
            "rejectionBuckets": profitability["rejection_buckets"],
            "venuePairs": profitability["venue_pairs"],
            "marketFamilies": profitability["market_families"],
            "topPositiveCandidates": profitability["sample_candidates"],
            "topNegativeNearMisses": profitability["negative_near_misses"],
        },
        "strategyStats": stats,
        "semanticDiagnostics": semantic_diagnostics,
        "venueCoverage": _venue_pair_coverage(
            strategy,
            edges=snapshot["edges"],
            nodes=snapshot["nodes"],
            quotes=snapshot["quotes"],
            matched_node_ids=snapshot["matched_node_ids"],
            candidate_venue_pairs=profitability["venue_pairs"],
        ),
        "sampleCandidates": profitability["sample_candidates"],
        "negativeNearMisses": profitability["negative_near_misses"],
    }


def _runtime_probe_satisfied(
    payload: dict[str, object],
    *,
    min_connected_nodes: int,
    min_match_instruments: int,
    min_quoted_match_instruments: int = 0,
    min_quoted_edges: int = 0,
    min_positive_margin_candidates: int = 0,
    require_rust_semantic_topology: bool = False,
) -> bool:
    positive_candidates = payload["positiveMarginCandidates"]["total"]
    rust_semantic_topology_ok = (
        payload.get("graphEngine") == "rust"
        and payload.get("topologySource") == "rust_semantic"
        and int(payload.get("semanticTemplateCount") or 0) > 0
    )
    return (
        payload["connectedNodes"] >= min_connected_nodes
        and payload["semanticMatchInstruments"] >= min_match_instruments
        and payload["quotedSemanticMatchInstruments"] >= min_quoted_match_instruments
        and payload["quotedEdges"] >= min_quoted_edges
        and positive_candidates >= min_positive_margin_candidates
        and (rust_semantic_topology_ok or not require_rust_semantic_topology)
    )


def _empty_candidate_quality_payload() -> dict[str, object]:
    return {
        "quotedEdges": 0,
        "marginBands": {},
        "rejectionBuckets": {},
        "venuePairs": {},
        "marketFamilies": {},
        "topPositiveCandidates": [],
        "topNegativeNearMisses": [],
    }


def _snapshot_probe_graph_state(graph) -> dict[str, object] | None:
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
        return None


def _venue_pair_coverage(
    strategy,
    *,
    edges: Any,
    nodes: dict[Any, object],
    quotes: dict[Any, object],
    matched_node_ids: set[Any],
    candidate_venue_pairs: Any,
) -> dict[str, object]:
    venues = _enabled_probe_venues(strategy, nodes)
    node_counts: Counter[str] = Counter()
    quoted_node_counts: Counter[str] = Counter()
    matched_node_counts: Counter[str] = Counter()
    quoted_matched_node_counts: Counter[str] = Counter()
    edge_counts: Counter[str] = Counter()
    quoted_edge_counts: Counter[str] = Counter()

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

    all_pairs = [f"{source}->{target}" for source in venues for target in venues]
    candidate_counts = _venue_pair_candidate_counts(candidate_venue_pairs)
    zero_pairs = [
        {
            "venuePair": pair,
            "reason": _zero_venue_pair_reason(
                pair,
                node_counts=node_counts,
                edge_counts=edge_counts,
                quoted_edge_counts=quoted_edge_counts,
            ),
        }
        for pair in all_pairs
        if candidate_counts.get(pair, 0) == 0
    ]
    cross_venue_candidate_count = sum(
        count for pair, count in candidate_counts.items() if _is_cross_venue_pair(pair)
    )

    return {
        "enabledVenues": venues,
        "nodeCounts": {venue: node_counts.get(venue, 0) for venue in venues},
        "quotedNodeCounts": {venue: quoted_node_counts.get(venue, 0) for venue in venues},
        "semanticMatchedNodeCounts": {venue: matched_node_counts.get(venue, 0) for venue in venues},
        "quotedSemanticMatchedNodeCounts": {
            venue: quoted_matched_node_counts.get(venue, 0) for venue in venues
        },
        "edgeCounts": {pair: edge_counts.get(pair, 0) for pair in all_pairs},
        "quotedEdgeCounts": {pair: quoted_edge_counts.get(pair, 0) for pair in all_pairs},
        "candidateCounts": {pair: candidate_counts.get(pair, 0) for pair in all_pairs},
        "crossVenueCandidateCount": cross_venue_candidate_count,
        "crossVenuePairsWithCandidates": [
            pair
            for pair in all_pairs
            if _is_cross_venue_pair(pair) and candidate_counts.get(pair, 0) > 0
        ],
        "zeroCandidateVenuePairs": zero_pairs,
    }


def _venue_pair_candidate_counts(candidate_venue_pairs: Any) -> dict[str, int]:
    if not isinstance(candidate_venue_pairs, dict):
        return {}
    return {
        str(pair): sum(int(value) for value in buckets.values())
        for pair, buckets in candidate_venue_pairs.items()
        if isinstance(buckets, dict)
    }


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


def _is_cross_venue_pair(pair: str) -> bool:
    source, target = pair.split("->", maxsplit=1)
    return source != target


def _semantic_probe_diagnostics(graph) -> dict[str, object]:
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
    common_pattern_key_count = len(
        set(node_diagnostics["pattern_counts"]) & set(template_diagnostics["pattern_counts"]),
    )
    return {
        "available": True,
        "nodeCount": len(nodes),
        "normalizedNodeCount": sum(node_diagnostics["pattern_counts"].values()),
        "normalizationErrorCount": sum(node_diagnostics["normalization_errors"].values()),
        "supportedProviderNodeCount": supported_provider_node_count,
        "commonPatternKeyCount": common_pattern_key_count,
        "templateCount": template_diagnostics["template_count"],
        "nodeSports": _top_counter(node_diagnostics["sport_counts"]),
        "nodeMarkets": _top_counter(node_diagnostics["market_counts"]),
        "templateSports": _top_counter(template_diagnostics["sport_counts"]),
        "templateSafetyTiers": _top_counter(template_diagnostics["tier_counts"]),
        "templateProviderScopes": _top_counter(template_diagnostics["provider_scope_counts"]),
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
    }


def _semantic_template_diagnostics(graph) -> dict[str, object]:
    pattern_counts: Counter[tuple[str, ...]] = Counter()
    provider_pattern_counts: Counter[tuple[str, ...]] = Counter()
    sport_counts: Counter[str] = Counter()
    tier_counts: Counter[str] = Counter()
    provider_scope_counts: Counter[str] = Counter()
    templates = _semantic_template_payloads_for_diagnostics(graph)
    for template in templates:
        provider_scope = tuple(str(item) for item in template.get("provider_scope", ()))
        provider_scope_key = ",".join(provider_scope) or "venue_agnostic"
        provider_scope_counts[provider_scope_key] += 1
        tier_counts[str(template.get("safety_tier") or "unknown")] += 1
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
        "template_count": len(templates),
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


def _probe_edge_profitability(
    strategy,
    *,
    edges,
    nodes,
    quotes,
    min_profit_margin: Decimal,
) -> dict[str, object]:
    matcher = strategy.market_matcher
    counters = ProbeProfitabilityCounters()

    for edge in edges:
        quoted_edge = _quoted_probe_edge(edge, nodes, quotes)
        if quoted_edge is None:
            continue
        source_node, target_node, quote_a, quote_b = quoted_edge

        counters.quoted_edges += 1
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

        _record_probe_opportunity(
            counters,
            opportunity=opportunity,
            edge=edge,
            source_node=source_node,
            target_node=target_node,
            allow_same_venue=allow_same_venue,
            min_profit_margin=min_profit_margin,
        )

    return counters.to_payload()


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
    probability_a = Decimal(1) / odds_a
    probability_b = Decimal(1) / odds_b
    total_probability = probability_a + probability_b
    profit_margin = (Decimal(1) / total_probability) - Decimal(1)
    observed_ns = max(int(quote_a.received_ns), int(quote_b.received_ns))
    quote_age_a_secs = strategy.quote_age_secs(observed_ns, quote_a.quote)
    quote_age_b_secs = strategy.quote_age_secs(observed_ns, quote_b.quote)
    quote_delta_secs = abs(int(quote_a.quote.ts_event) - int(quote_b.quote.ts_event)) / (
        1_000_000_000
    )
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
    same_venue = source_node.instrument.venue_name == target_node.instrument.venue_name
    matcher_suspect, suspect_reason = strategy.matcher_suspect_reason(
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
        "sameFixture": not matcher_suspect,
        "compatibleMarketFamily": bool(edge.same_venue_execution_eligible),
        "freshQuotes": fresh_quotes,
        "sufficientLiquidity": liquidity_ok,
        "thresholdProfit": threshold_ok,
        "executionDisabledUntilRiskEngineApproval": True,
        "suspectReason": suspect_reason,
    }
    would_execute_same_venue = (
        edge.same_venue_execution_eligible
        and not edge.execution_safe
        and same_venue
        and not matcher_suspect
        and fresh_quotes
        and liquidity_ok
        and threshold_ok
    )
    return {
        "instrumentIdA": str(source_node.instrument.id),
        "instrumentIdB": str(target_node.instrument.id),
        "venueA": str(source_node.instrument.id.venue),
        "venueB": str(target_node.instrument.id.venue),
        "venuePair": f"{source_node.instrument.id.venue}->{target_node.instrument.id.venue}",
        "marketA": source_node.market_name,
        "marketB": target_node.market_name,
        "marketFamily": _probe_market_family(source_node, target_node),
        "outcomeA": source_node.outcome,
        "outcomeB": target_node.outcome,
        "profitMargin": str(profit_margin),
        "totalProbability": str(total_probability),
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
        "rejectionBucket": rejection_bucket,
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
        return "semantic_blocked"
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
    counters.rejection_buckets[rejection_bucket] += 1
    counters.venue_pairs.setdefault(venue_pair, Counter())[rejection_bucket] += 1
    counters.market_families.setdefault(market_family, Counter())[rejection_bucket] += 1
    if margin > 0:
        counters.samples.append((margin, quality))
    elif margin > Decimal("-0.05"):
        counters.negative_samples.append((margin, quality))


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


def _probe_market_family(source_node, target_node) -> str:
    families: list[str] = []
    for node in (source_node, target_node):
        try:
            normalized = MarketNormalizer.normalize(node.instrument)
            families.append(normalized.market_family or normalized.market_type)
        except (AttributeError, TypeError, ValueError):
            families.append(str(getattr(node, "market_type", "") or node.market_name))
    return " + ".join(families)


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
) -> None:
    is_positive = opportunity.profit_margin > 0
    meets_threshold = opportunity.profit_margin >= min_profit_margin
    if allow_same_venue:
        counters.positive_same_venue += int(is_positive)
        counters.threshold_same_venue += int(meets_threshold)
    else:
        counters.positive_execution += int(is_positive)
        counters.threshold_execution += int(meets_threshold)

    _ = opportunity, edge, source_node, target_node


if __name__ == "__main__":
    raise SystemExit(main())
