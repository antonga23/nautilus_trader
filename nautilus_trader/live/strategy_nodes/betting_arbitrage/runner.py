from __future__ import annotations

import argparse
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


def main(argv: list[str] | None = None) -> int:
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
            _write_json(
                status_path,
                {
                    "nodeId": manifest.node_id,
                    "status": "validated",
                    "validatedAt": _utc_now(),
                    "manifestPath": str(manifest_snapshot),
                    "renderedConfigPath": str(rendered_config_path),
                    "semanticCache": semantic_cache,
                },
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
            _write_json(
                status_path,
                {
                    "nodeId": manifest.node_id,
                    "status": "failed",
                    "failedAt": _utc_now(),
                    "error": repr(exc),
                    "manifestPath": str(manifest_snapshot),
                    "renderedConfigPath": str(rendered_config_path),
                    "semanticCache": semantic_cache,
                },
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

    _write_json(
        status_path,
        {
            "nodeId": manifest.node_id,
            "status": "building",
            "at": _utc_now(),
            "manifestPath": str(manifest_snapshot),
            "renderedConfigPath": str(rendered_config_path),
            "semanticCache": semantic_cache,
        },
    )
    node.build()
    _write_json(
        status_path,
        {
            "nodeId": manifest.node_id,
            "status": "built",
            "at": _utc_now(),
            "manifestPath": str(manifest_snapshot),
            "renderedConfigPath": str(rendered_config_path),
            "semanticCache": semantic_cache,
        },
    )

    if args.no_start:
        node.dispose()
        return 0

    heartbeat_writer.start()
    _write_json(
        status_path,
        {
            "nodeId": manifest.node_id,
            "status": "running",
            "startedAt": _utc_now(),
            "heartbeatPath": str(heartbeat_path),
            "manifestPath": str(manifest_snapshot),
            "renderedConfigPath": str(rendered_config_path),
            "semanticCache": semantic_cache,
        },
    )

    try:
        node.run()
        _write_json(
            status_path,
            {
                "nodeId": manifest.node_id,
                "status": "completed",
                "completedAt": _utc_now(),
                "heartbeatPath": str(heartbeat_path),
                "semanticCache": semantic_cache,
            },
        )
        return 0
    except Exception as e:
        _write_json(
            status_path,
            {
                "nodeId": manifest.node_id,
                "status": "failed",
                "failedAt": _utc_now(),
                "error": repr(e),
                "heartbeatPath": str(heartbeat_path),
                "semanticCache": semantic_cache,
            },
        )
        raise
    finally:
        heartbeat_stop.set()
        node.dispose()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")


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


if __name__ == "__main__":
    raise SystemExit(main())
