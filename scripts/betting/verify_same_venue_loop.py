#!/usr/bin/env python3
"""
Assert the SX.bet same-venue live-execution observability loop is populated.

Reads a betting strategy-node ``status.json`` (written by ``validate-manifest``,
``probe-runtime``, or a running ``run`` node) and checks the plumbing that the
M1.8 acceptance runbook exercises in Phase B: same-venue execution readiness,
SX.bet real quote depth, semantic-cache readiness, and an idle approval queue.

Idle is correct. With correctly-priced same-venue SX.bet books (a normal ~-2%
overround) the node stages ZERO positive-margin candidates and holds an empty
approval queue; this script treats that state as a pass and only fails when the
plumbing itself is broken (wrong venue mode, missing risk caps, no SX.bet quote
subscriptions, or SX.bet books that never quote). A genuine positive-margin
candidate or a non-empty approval queue is surfaced as an operator-attention
note, not a failure.

Reuses the normalization in ``runtime_probe_report`` so the same coverage and
candidate fields feed both the release report and this loop check.

"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_probe_report import _as_dict
from runtime_probe_report import _int_value
from runtime_probe_report import _load_json
from runtime_probe_report import summarize_payload

OK = "pass"
WARN = "warn"
FAIL = "fail"

_STATUS_RANK = {OK: 0, WARN: 1, FAIL: 2}


def execution_approvals(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract the execution-approval summary from a status payload.

    Mirrors ``tools.nodeops.server.execution_approvals_from_probe`` without
    importing the HTTP server: prefer ``runtimeProbe.executionApprovals`` and
    fall back to the legacy ``strategyStats.execution_approvals`` location.

    """
    for container_key, approvals_key in (
        ("runtimeProbe", "executionApprovals"),
        ("strategyStats", "execution_approvals"),
    ):
        container = _as_dict(payload.get(container_key))
        approvals = container.get(approvals_key)
        if isinstance(approvals, dict):
            return approvals
    return None


def _venue_count(mapping: dict[str, Any], venue: str) -> int:
    return _int_value(_as_dict(mapping).get(venue))


def verify(
    payload: dict[str, Any],
    *,
    venue: str = "SXBET",
    require_quoted_depth: bool = False,
) -> dict[str, Any]:
    summary = summarize_payload(payload)
    semantic_cache = _as_dict(summary.get("semanticCache"))
    coverage = _as_dict(summary.get("venueCoverage"))
    candidates = _as_dict(summary.get("candidates"))
    # The status.json executionReadiness block carries the full same-venue wiring
    # (executionVenueMode / allowSameVenueLiveExecution / riskCaps); the
    # summarize_payload projection intentionally drops these, so read them raw.
    raw_readiness = _as_dict(payload.get("executionReadiness"))
    has_runtime_probe = isinstance(payload.get("runtimeProbe"), dict)

    checks: list[dict[str, Any]] = []

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    # --- Execution readiness (same-venue wiring) --------------------------------
    venue_mode = raw_readiness.get("executionVenueMode")
    record(
        "execution_venue_mode_same_venue",
        OK if venue_mode == "same_venue" else FAIL,
        f"executionVenueMode={venue_mode!r} (expected 'same_venue')",
    )

    allow_same = raw_readiness.get("allowSameVenueLiveExecution")
    record(
        "allow_same_venue_live_execution",
        OK if allow_same is True else FAIL,
        f"allowSameVenueLiveExecution={allow_same!r} (expected True)",
    )

    risk_caps = _as_dict(raw_readiness.get("riskCaps"))
    cap_ok = risk_caps and all(
        _positive_decimal(risk_caps.get(key))
        for key in ("maxLegStake", "maxDailyNotional", "maxDailyLoss")
    )
    record(
        "risk_caps_present",
        OK if cap_ok else FAIL,
        f"riskCaps={risk_caps or None}",
    )

    venues = [str(item.get("venue")) for item in _as_list(raw_readiness.get("venues"))]
    sxbet_venue = next(
        (item for item in _as_list(raw_readiness.get("venues")) if item.get("venue") == venue),
        None,
    )
    record(
        "sxbet_venue_data_enabled",
        OK if sxbet_venue and sxbet_venue.get("dataEnabled") is True else FAIL,
        f"venues={venues}; {venue} dataEnabled={_as_dict(sxbet_venue).get('dataEnabled')}",
    )
    other_venues = [name for name in venues if name and name != venue]
    if other_venues:
        record(
            "single_venue_only",
            WARN,
            f"non-{venue} venues present for a same-venue node: {other_venues}",
        )

    # --- Safety posture (informational; Phase B expects unarmed dry-run) --------
    record(
        "safety_posture",
        OK,
        "validationMode={} liveExecutionArmed={} liveExecutionEnvArmed={}".format(
            raw_readiness.get("validationMode"),
            raw_readiness.get("liveExecutionArmed"),
            raw_readiness.get("liveExecutionEnvArmed"),
        ),
    )

    # --- Semantic cache ---------------------------------------------------------
    if raw_readiness.get("semanticCacheConfigured"):
        ready = semantic_cache.get("ready")
        record(
            "semantic_cache_ready",
            OK if ready else WARN,
            "semanticCache.ready={} source={} promotedTemplateCount={}".format(
                ready,
                semantic_cache.get("source"),
                semantic_cache.get("promotedTemplateCount"),
            ),
        )

    # --- Live observability depth (only meaningful once a node has run) ---------
    if has_runtime_probe:
        subs = _venue_count(coverage.get("quoteSubscriptionCounts"), venue)
        record(
            "sxbet_quote_subscriptions",
            OK if subs > 0 else FAIL,
            f"{venue} quoteSubscriptionCounts={subs}",
        )
        quoted = _venue_count(coverage.get("quotedNodeCounts"), venue)
        depth_status = OK if quoted > 0 else (FAIL if require_quoted_depth else WARN)
        record(
            "sxbet_real_depth_quoting",
            depth_status,
            f"{venue} quotedNodeCounts={quoted} (real bid/ask depth; 0 => books not quoting yet)",
        )
    else:
        record(
            "runtime_probe_present",
            WARN,
            "no runtimeProbe in status.json; run probe-runtime or a live node for Phase B depth",
        )

    # --- Candidate/approval idle state (idle is CORRECT) ------------------------
    positive = _int_value(candidates.get("positiveTotal"))
    record(
        "positive_margin_candidates_idle",
        OK if positive == 0 else WARN,
        (
            "0 positive-margin candidates (expected: same-venue SX.bet books carry a "
            "normal overround)"
            if positive == 0
            else f"{positive} positive-margin candidate(s) staged - genuine arb, operator review"
        ),
    )

    approvals = execution_approvals(payload)
    pending = len(_as_list(_as_dict(approvals).get("pending"))) if approvals else 0
    record(
        "approval_queue_idle",
        OK if pending == 0 else WARN,
        (
            "approval queue empty"
            if pending == 0
            else f"{pending} pending approval(s) - operator has staged candidates to review"
        ),
    )

    # --- Heartbeat --------------------------------------------------------------
    heartbeat_path = payload.get("heartbeatPath")
    record(
        "heartbeat_path_present",
        OK if heartbeat_path else WARN,
        f"heartbeatPath={heartbeat_path}",
    )

    overall = OK
    for check in checks:
        if _STATUS_RANK[check["status"]] > _STATUS_RANK[overall]:
            overall = check["status"]

    return {
        "nodeId": summary.get("nodeId"),
        "status": summary.get("status"),
        "overall": overall,
        "idleIsCorrect": positive == 0 and pending == 0,
        "checks": checks,
        "observed": {
            "executionVenueMode": venue_mode,
            "validationMode": raw_readiness.get("validationMode"),
            "liveExecutionArmed": raw_readiness.get("liveExecutionArmed"),
            "liveExecutionEnvArmed": raw_readiness.get("liveExecutionEnvArmed"),
            "riskCaps": risk_caps or None,
            f"{venue.lower()}QuoteSubscriptions": _venue_count(
                coverage.get("quoteSubscriptionCounts"),
                venue,
            ),
            f"{venue.lower()}QuotedNodes": _venue_count(coverage.get("quotedNodeCounts"), venue),
            "positiveMarginCandidates": positive,
            "pendingApprovals": pending,
        },
    }


def _positive_decimal(value: Any) -> bool:
    try:
        return float(str(value)) > 0
    except (TypeError, ValueError):
        return False


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _format_text(path: Path, result: dict[str, Any]) -> str:
    lines = [
        f"same-venue loop check: {path}",
        f"  node: {result['nodeId']}  status: {result['status']}  overall: {result['overall'].upper()}",
        f"  idle-is-correct: {result['idleIsCorrect']}",
    ]
    for check in result["checks"]:
        lines.append(f"  [{check['status'].upper():4}] {check['name']}: {check['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status_json", type=Path, help="Path to a node status.json")
    parser.add_argument("--venue", default="SXBET", help="Single venue expected (default SXBET)")
    parser.add_argument(
        "--require-quoted-depth",
        action="store_true",
        help="Fail (not warn) when the venue reports zero quoted real-depth nodes",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures (default: only 'fail' checks set a non-zero exit)",
    )
    parser.add_argument("--json", action="store_true", help="Emit the JSON verdict")
    args = parser.parse_args(argv)

    payload = _load_json(args.status_json)
    result = verify(
        payload,
        venue=str(args.venue).upper(),
        require_quoted_depth=args.require_quoted_depth,
    )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_format_text(args.status_json, result))

    if result["overall"] == FAIL:
        return 1
    if args.strict and result["overall"] == WARN:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
