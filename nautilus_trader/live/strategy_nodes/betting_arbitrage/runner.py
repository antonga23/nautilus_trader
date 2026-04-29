from __future__ import annotations

import argparse
from decimal import Decimal
import json
import threading
import time
from pathlib import Path
from typing import Any

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


def main(argv: list[str] | None = None) -> int:  # noqa: C901
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
    probe_parser.add_argument("--min-positive-margin-candidates", type=int, default=0)
    probe_parser.add_argument("--require-rust-semantic-topology", action="store_true")

    args = parser.parse_args(argv)

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
    semantic_cache: dict[str, object] | None = None

    try:
        semantic_cache = _ensure_semantic_cache(manifest)

        if args.command == "validate-manifest":
            config = build_trading_node_config(manifest)
            write_manifest_snapshot(manifest, manifest_snapshot)
            write_rendered_node_config(config, rendered_config_path)
            _write_status(
                status_path,
                manifest=manifest,
                status="validated",
                semantic_cache=semantic_cache,
                manifest_snapshot=manifest_snapshot,
                rendered_config_path=rendered_config_path,
                validatedAt=_utc_now(),
            )
            return 0

        config = build_trading_node_config(manifest)
        write_manifest_snapshot(manifest, manifest_snapshot)
        write_rendered_node_config(config, rendered_config_path)

        if args.command == "render-node-config":
            if args.output:
                write_rendered_node_config(config, args.output)
            else:
                print(rendered_config_path.read_text())
            return 0
    except Exception as exc:
        if args.command != "render-node-config":
            _write_status(
                status_path,
                manifest=manifest,
                status="failed",
                semantic_cache=semantic_cache,
                manifest_snapshot=manifest_snapshot,
                rendered_config_path=rendered_config_path,
                failedAt=_utc_now(),
                error=exc,
            )
        raise

    from nautilus_trader.live.node import TradingNode

    node = TradingNode(config=config)
    heartbeat_stop = threading.Event()
    heartbeat_writer = HeartbeatWriter(
        heartbeat_path=heartbeat_path,
        node_id=manifest.node_id,
        interval_secs=manifest.heartbeat_interval_secs,
        stop_event=heartbeat_stop,
    )

    _write_status(
        status_path,
        manifest=manifest,
        status="building",
        semantic_cache=semantic_cache,
        manifest_snapshot=manifest_snapshot,
        rendered_config_path=rendered_config_path,
        at=_utc_now(),
    )
    node.build()
    _write_status(
        status_path,
        manifest=manifest,
        status="built",
        semantic_cache=semantic_cache,
        manifest_snapshot=manifest_snapshot,
        rendered_config_path=rendered_config_path,
        at=_utc_now(),
    )

    if args.command == "run" and args.no_start:
        node.dispose()
        return 0

    if args.command == "probe-runtime":
        try:
            payload = _probe_runtime(
                node=node,
                manifest=manifest,
                status_path=status_path,
                heartbeat_path=heartbeat_path,
                manifest_snapshot=manifest_snapshot,
                rendered_config_path=rendered_config_path,
                semantic_cache=semantic_cache,
                timeout_seconds=float(args.timeout_seconds),
                poll_interval_secs=float(args.poll_interval_secs),
                min_connected_nodes=int(args.min_connected_nodes),
                min_match_instruments=int(args.min_match_instruments),
                min_positive_margin_candidates=int(args.min_positive_margin_candidates),
                require_rust_semantic_topology=bool(args.require_rust_semantic_topology),
            )
            _write_status(
                status_path,
                manifest=manifest,
                status="probed",
                semantic_cache=semantic_cache,
                manifest_snapshot=manifest_snapshot,
                rendered_config_path=rendered_config_path,
                heartbeat_path=heartbeat_path,
                completedAt=_utc_now(),
                runtime_probe=payload,
            )
            return 0
        except Exception as exc:
            _write_status(
                status_path,
                manifest=manifest,
                status="failed",
                semantic_cache=semantic_cache,
                manifest_snapshot=manifest_snapshot,
                rendered_config_path=rendered_config_path,
                heartbeat_path=heartbeat_path,
                failedAt=_utc_now(),
                error=exc,
            )
            raise
        finally:
            node.dispose()

    runtime_probe_stop: threading.Event | None = None
    runtime_probe_writer: RuntimeProbeStatusWriter | None = None
    if manifest.validation_mode and hasattr(node, "trader"):
        runtime_probe_stop = threading.Event()
        runtime_probe_writer = RuntimeProbeStatusWriter(
            status_path=status_path,
            manifest=manifest,
            strategy=_resolve_betting_strategy(node),
            semantic_cache=semantic_cache,
            manifest_snapshot=manifest_snapshot,
            rendered_config_path=rendered_config_path,
            heartbeat_path=heartbeat_path,
            interval_secs=max(1.0, manifest.heartbeat_interval_secs),
            stop_event=runtime_probe_stop,
        )

    heartbeat_writer.start()
    _write_status(
        status_path,
        manifest=manifest,
        status="running",
        semantic_cache=semantic_cache,
        manifest_snapshot=manifest_snapshot,
        rendered_config_path=rendered_config_path,
        heartbeat_path=heartbeat_path,
        startedAt=_utc_now(),
    )
    if runtime_probe_writer is not None:
        runtime_probe_writer.start()

    try:
        node.run()
        _write_status(
            status_path,
            manifest=manifest,
            status="completed",
            semantic_cache=semantic_cache,
            manifest_snapshot=manifest_snapshot,
            rendered_config_path=rendered_config_path,
            heartbeat_path=heartbeat_path,
            completedAt=_utc_now(),
        )
        return 0
    except Exception as exc:
        _write_status(
            status_path,
            manifest=manifest,
            status="failed",
            semantic_cache=semantic_cache,
            manifest_snapshot=manifest_snapshot,
            rendered_config_path=rendered_config_path,
            heartbeat_path=heartbeat_path,
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
        except BaseException as exc:  # pragma: no cover - surfaced below
            run_error.append(exc)

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

    raise RuntimeError(
        "Runtime probe did not observe the required semantic coverage "
        f"(connected_nodes={latest_payload['connectedNodes']}, "
        f"semantic_match_instruments={latest_payload['semanticMatchInstruments']}, "
        f"positive_margin_candidates={latest_payload['positiveMarginCandidates']['total']}, "
        f"graph_engine={latest_payload.get('graphEngine')}, "
        f"topology_source={latest_payload.get('topologySource')}, "
        f"semantic_template_count={latest_payload.get('semanticTemplateCount')})",
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
    graph = strategy._opportunity_graph
    stats = strategy.get_stats()
    snapshot = _snapshot_probe_graph_state(graph)
    if snapshot is None:
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
            "strategyStats": stats,
            "sampleCandidates": [],
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
        "strategyStats": stats,
        "sampleCandidates": profitability["sample_candidates"],
    }


def _runtime_probe_satisfied(
    payload: dict[str, object],
    *,
    min_connected_nodes: int,
    min_match_instruments: int,
    min_positive_margin_candidates: int,
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
        and positive_candidates >= min_positive_margin_candidates
        and (rust_semantic_topology_ok or not require_rust_semantic_topology)
    )


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


def _probe_edge_profitability(  # noqa: C901
    strategy,
    *,
    edges,
    nodes,
    quotes,
    min_profit_margin: Decimal,
) -> dict[str, object]:
    matcher = strategy._matcher
    quoted_edges = 0
    positive_execution = 0
    positive_same_venue = 0
    threshold_execution = 0
    threshold_same_venue = 0
    samples: list[tuple[Decimal, dict[str, object]]] = []

    for edge in edges:
        quote_a = quotes.get(edge.source_node_id)
        quote_b = quotes.get(edge.target_node_id)
        if quote_a is None or quote_b is None:
            continue

        source_node = nodes.get(edge.source_node_id)
        target_node = nodes.get(edge.target_node_id)
        if source_node is None or target_node is None:
            continue

        quoted_edges += 1
        allow_same_venue = edge.same_venue_execution_eligible and not edge.execution_safe
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

        is_positive = opportunity.profit_margin > 0
        meets_threshold = opportunity.profit_margin >= min_profit_margin
        if allow_same_venue:
            if is_positive:
                positive_same_venue += 1
            if meets_threshold:
                threshold_same_venue += 1
        else:
            if is_positive:
                positive_execution += 1
            if meets_threshold:
                threshold_execution += 1

        if is_positive:
            samples.append(
                (
                    opportunity.profit_margin,
                    {
                        "instrumentIdA": str(source_node.instrument.id),
                        "instrumentIdB": str(target_node.instrument.id),
                        "marketA": source_node.market_name,
                        "marketB": target_node.market_name,
                        "outcomeA": source_node.outcome,
                        "outcomeB": target_node.outcome,
                        "profitMargin": str(opportunity.profit_margin),
                        "safetyTier": edge.safety_tier,
                        "executionSafe": edge.execution_safe,
                        "sameVenueExecutionEligible": edge.same_venue_execution_eligible,
                    },
                ),
            )

    samples.sort(key=lambda item: item[0], reverse=True)
    return {
        "quoted_edges": quoted_edges,
        "positive_execution": positive_execution,
        "positive_same_venue": positive_same_venue,
        "threshold_execution": threshold_execution,
        "threshold_same_venue": threshold_same_venue,
        "sample_candidates": [payload for _, payload in samples[:10]],
    }


if __name__ == "__main__":
    raise SystemExit(main())
