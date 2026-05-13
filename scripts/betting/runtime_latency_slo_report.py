#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Aggregate runtime latency SLO status from strategy-node artifacts.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _runtime_probe(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(payload.get("runtimeProbe")) or payload


def _node_id(payload: dict[str, Any], path: Path) -> str:
    return str(payload.get("nodeId") or _runtime_probe(payload).get("nodeId") or path.stem)


def _latency(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime_probe(payload)
    latency = _as_dict(runtime.get("latencyDiagnostics"))
    quality = _as_dict(runtime.get("candidateQuality"))
    if latency:
        return latency
    return {"sloStatus": quality.get("latencySloStatus")}


def _stage_status(slo: dict[str, Any], stage: str) -> str:
    value = _as_dict(slo.get(stage)).get("status")
    return str(value or "unknown")


def _stage_observations(slo: dict[str, Any], stage: str) -> int:
    value = _as_dict(slo.get(stage)).get("observations")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def summarize_payload(payload: dict[str, Any], *, path: Path = Path("<memory>")) -> dict[str, Any]:
    latency = _latency(payload)
    slo = _as_dict(latency.get("sloStatus"))
    strategy = _as_dict(slo.get("strategyLatency"))
    stages = {
        "quoteAge": _stage_status(slo, "quoteAge"),
        "fetchLatency": _stage_status(slo, "fetchLatency"),
        "pairSkew": _stage_status(slo, "pairSkew"),
    }
    missing_stages = [
        key
        for key, observed in (
            ("graph_scan", strategy.get("graphScanObserved")),
            ("candidate_decision", strategy.get("candidateDecisionObserved")),
            ("quote_receive", strategy.get("quoteReceiveObserved")),
            ("provider_latency", strategy.get("providerLatencyObserved")),
        )
        if observed is False
    ]
    return {
        "nodeId": _node_id(payload, path),
        "artifact": str(path),
        "overall": str(slo.get("overall") or "unknown"),
        "stages": stages,
        "observations": {
            "quoteAge": _stage_observations(slo, "quoteAge"),
            "fetchLatency": _stage_observations(slo, "fetchLatency"),
            "pairSkew": _stage_observations(slo, "pairSkew"),
        },
        "missingStages": missing_stages,
        "warnings": list(latency.get("diagnosticWarnings") or []),
    }


def summarize_files(paths: list[Path]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        summarize_payload(json.loads(path.read_text(encoding="utf-8")), path=path) for path in paths
    ]
    overall_counts: Counter[str] = Counter(str(node["overall"]) for node in nodes)
    stage_counts: dict[str, Counter[str]] = {
        stage: Counter(node["stages"][stage] for node in nodes)
        for stage in ("quoteAge", "fetchLatency", "pairSkew")
    }
    failing_nodes = [
        node
        for node in nodes
        if node["overall"] in {"fail", "warn", "unknown"} or node["missingStages"]
    ]
    return {
        "artifactCount": len(paths),
        "overallCounts": dict(sorted(overall_counts.items())),
        "stageCounts": {
            stage: dict(sorted(counts.items())) for stage, counts in stage_counts.items()
        },
        "failingNodes": failing_nodes,
        "nodes": nodes,
    }


def _format_text(summary: dict[str, Any]) -> str:
    lines = ["runtime latency slo audit", "========================="]
    lines.append(f"artifacts={summary['artifactCount']}")
    lines.append(f"overall={summary['overallCounts']}")
    lines.append(f"stages={summary['stageCounts']}")
    lines.append("failing_or_unknown_nodes:")
    for node in summary["failingNodes"]:
        lines.append(
            f"- {node['nodeId']}: overall={node['overall']} "
            f"stages={node['stages']} missing={node['missingStages']} warnings={node['warnings']}",
        )
    if not summary["failingNodes"]:
        lines.append("- none")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = summarize_files(args.artifacts)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(_format_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
