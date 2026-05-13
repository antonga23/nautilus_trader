#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Audit live-execution readiness from strategy-node runtime artifacts.

The runtime probe report is intentionally broad. This helper focuses on the
execution-control plane: arming state, kill switch, risk caps, FX freshness,
latency status, and order lifecycle health. It is designed for unarmed pilot
soaks where execution must be ready enough to explain why it would submit, but
still blocked by an explicit gate until the pilot is intentionally armed.

"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _bool(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else False


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _runtime_probe(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(payload.get("runtimeProbe")) or payload


def _node_id(payload: dict[str, Any], path: Path) -> str:
    return str(payload.get("nodeId") or _runtime_probe(payload).get("nodeId") or path.stem)


def _latency_overall(runtime: dict[str, Any]) -> str:
    latency = _as_dict(runtime.get("latencyDiagnostics"))
    slo = _as_dict(latency.get("sloStatus"))
    return str(slo.get("overall") or "unknown")


def _fx_reasons(payload: dict[str, Any], runtime: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    fx_policy = _as_dict(runtime.get("fxPolicy") or payload.get("fxPolicy"))
    fx_state = _as_dict(runtime.get("fxState") or payload.get("fxState"))
    if not fx_policy and not fx_state:
        return ["fx_state_missing"]
    source = fx_state.get("fxSource") or fx_policy.get("fxSource")
    if not source:
        reasons.append("fx_source_missing")
    if _bool(fx_state.get("stale")) or _bool(fx_policy.get("stale")):
        reasons.append("fx_stale")
    if fx_state.get("blockerReason"):
        reasons.append(f"fx:{fx_state['blockerReason']}")
    if fx_policy.get("blockerReason"):
        reasons.append(f"fx:{fx_policy['blockerReason']}")
    return reasons


def _venue_reasons(readiness: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for venue in _as_list(readiness.get("venues")):
        if not isinstance(venue, dict):
            continue
        name = str(venue.get("venue") or "unknown")
        if _bool(venue.get("executionEnabled")) and not _bool(venue.get("executionDryRun")):
            reasons.append(f"{name}:live_execution_client_enabled")
        if not venue.get("environment"):
            reasons.append(f"{name}:environment_missing")
        if not venue.get("baseCurrency"):
            reasons.append(f"{name}:base_currency_missing")
    return reasons


def _lifecycle_total(live_execution: dict[str, Any]) -> int:
    total = 0
    for counts in _as_dict(live_execution.get("order_lifecycle_counts_by_venue")).values():
        total += sum(_int(value) for value in _as_dict(counts).values())
    return total


def _execution_reasons(
    *,
    validation_mode: bool,
    auto_execute: bool,
    manifest_armed: bool,
    env_armed: bool,
    kill_switch_active: bool,
    halt_reason: str,
    unhedged_exposures: int,
    latency_overall: str,
    readiness: dict[str, Any],
    live_execution: dict[str, Any],
    payload: dict[str, Any],
    runtime: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if validation_mode and auto_execute:
        reasons.append("validation_mode_auto_execute")
    if auto_execute and manifest_armed and not env_armed:
        reasons.append("env_gate_unarmed")
    if auto_execute and manifest_armed and env_armed:
        reasons.append("live_execution_armed")
    if kill_switch_active:
        reasons.append("kill_switch_active")
    if halt_reason:
        reasons.append(f"halt:{halt_reason}")
    if unhedged_exposures > 0:
        reasons.append("unhedged_exposure")
    if latency_overall in {"fail", "warn", "unknown"}:
        reasons.append(f"latency:{latency_overall}")
    if not _bool(readiness.get("semanticCacheConfigured")):
        reasons.append("semantic_cache_not_configured")
    reasons.extend(_venue_reasons(readiness))
    reasons.extend(_fx_reasons(payload, runtime))
    reasons.extend(
        f"block:{reason}"
        for reason, count in sorted(_as_dict(live_execution.get("block_reasons")).items())
        if _int(count) > 0
    )
    return reasons


def _overall_status(
    *,
    auto_execute: bool,
    manifest_armed: bool,
    env_armed: bool,
    kill_switch_active: bool,
    halt_reason: str,
    unhedged_exposures: int,
) -> str:
    if kill_switch_active or halt_reason or unhedged_exposures > 0:
        return "fail"
    if auto_execute and manifest_armed and env_armed:
        return "armed"
    if auto_execute and manifest_armed and not env_armed:
        return "ready_unarmed"
    if auto_execute:
        return "warn"
    return "dry_run"


def summarize_payload(
    payload: dict[str, Any],
    *,
    path: Path = Path("<memory>"),
) -> dict[str, Any]:
    runtime = _runtime_probe(payload)
    readiness = _as_dict(payload.get("executionReadiness") or runtime.get("executionReadiness"))
    live_execution = _as_dict(runtime.get("liveExecution"))
    reasons: list[str] = []

    validation_mode = _bool(readiness.get("validationMode"))
    auto_execute = _bool(readiness.get("autoExecute") or live_execution.get("auto_execute"))
    manifest_armed = _bool(
        readiness.get("liveExecutionArmed") or live_execution.get("manifest_armed"),
    )
    env_armed = _bool(readiness.get("liveExecutionEnvArmed") or live_execution.get("env_armed"))
    kill_switch_active = _bool(live_execution.get("kill_switch_active"))
    halt_reason = str(live_execution.get("halt_reason") or "")
    unhedged_exposures = _int(live_execution.get("unhedged_exposures"))
    submissions = _int(live_execution.get("submissions"))
    lifecycle_events = _lifecycle_total(live_execution)
    latency_overall = _latency_overall(runtime)
    reasons = _execution_reasons(
        validation_mode=validation_mode,
        auto_execute=auto_execute,
        manifest_armed=manifest_armed,
        env_armed=env_armed,
        kill_switch_active=kill_switch_active,
        halt_reason=halt_reason,
        unhedged_exposures=unhedged_exposures,
        latency_overall=latency_overall,
        readiness=readiness,
        live_execution=live_execution,
        payload=payload,
        runtime=runtime,
    )
    overall = _overall_status(
        auto_execute=auto_execute,
        manifest_armed=manifest_armed,
        env_armed=env_armed,
        kill_switch_active=kill_switch_active,
        halt_reason=halt_reason,
        unhedged_exposures=unhedged_exposures,
    )

    return {
        "nodeId": _node_id(payload, path),
        "artifact": str(path),
        "overall": overall,
        "validationMode": validation_mode,
        "autoExecute": auto_execute,
        "manifestArmed": manifest_armed,
        "envArmed": env_armed,
        "killSwitchActive": kill_switch_active,
        "haltReason": halt_reason or None,
        "unhedgedExposures": unhedged_exposures,
        "submissions": submissions,
        "lifecycleEvents": lifecycle_events,
        "notionalUsed": str(live_execution.get("notional_used") or "0"),
        "realizedLoss": str(live_execution.get("realized_loss") or "0"),
        "latencyOverall": latency_overall,
        "riskCaps": readiness.get("riskCaps")
        or {
            "maxLegStake": live_execution.get("max_leg_stake"),
            "maxDailyNotional": live_execution.get("max_daily_notional"),
            "maxDailyLoss": live_execution.get("max_daily_loss"),
        },
        "executionVenueMode": readiness.get("executionVenueMode")
        or live_execution.get("execution_venue_mode"),
        "venues": readiness.get("venues") or [],
        "reasons": reasons,
    }


def summarize_files(paths: list[Path]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        summarize_payload(json.loads(path.read_text(encoding="utf-8")), path=path) for path in paths
    ]
    overall_counts: Counter[str] = Counter(str(node["overall"]) for node in nodes)
    reason_counts: Counter[str] = Counter(
        reason for node in nodes for reason in _as_list(node.get("reasons"))
    )
    return {
        "artifactCount": len(nodes),
        "overallCounts": dict(sorted(overall_counts.items())),
        "reasonCounts": dict(sorted(reason_counts.items())),
        "nodes": nodes,
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "runtime execution readiness audit",
        f"  artifacts={summary['artifactCount']}",
        f"  overall={summary['overallCounts']}",
        f"  reasons={summary['reasonCounts']}",
    ]
    for node in summary["nodes"]:
        lines.append(
            f"  node={node['nodeId']} overall={node['overall']} "
            f"auto_execute={node['autoExecute']} manifest_armed={node['manifestArmed']} "
            f"env_armed={node['envArmed']} kill_switch={node['killSwitchActive']} "
            f"unhedged={node['unhedgedExposures']} submissions={node['submissions']} "
            f"latency={node['latencyOverall']} reasons={node['reasons']}",
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status_json", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--require-unarmed",
        action="store_true",
        help="Return non-zero when any artifact has the live execution env gate armed",
    )
    parser.add_argument(
        "--require-no-halt",
        action="store_true",
        help="Return non-zero when any artifact has a halt reason or kill switch active",
    )
    parser.add_argument(
        "--require-no-unhedged",
        action="store_true",
        help="Return non-zero when any artifact reports unhedged exposure",
    )
    args = parser.parse_args(argv)

    summary = summarize_files(args.status_json)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_summary(summary))

    nodes = summary["nodes"]
    if args.require_unarmed and any(_bool(node.get("envArmed")) for node in nodes):
        return 2
    if args.require_no_halt and any(
        _bool(node.get("killSwitchActive")) or bool(node.get("haltReason")) for node in nodes
    ):
        return 3
    if args.require_no_unhedged and any(_int(node.get("unhedgedExposures")) > 0 for node in nodes):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
