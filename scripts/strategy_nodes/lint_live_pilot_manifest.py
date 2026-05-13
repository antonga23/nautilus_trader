#!/usr/bin/env python3
"""
Lint live-pilot betting strategy-node manifests before deploy.

The live execution manifests intentionally require two gates: the manifest must
declare live intent, and deployment must still provide the environment arming
gate. This linter checks the durable manifest side so cross-venue and same-venue
pilots do not drift into unsafe mode.

"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_MAX_TOTAL_STAKE = 25.0
DEFAULT_MAX_LEG_STAKE = 15.0
DEFAULT_MAX_DAILY_NOTIONAL = 100.0
DEFAULT_MAX_DAILY_LOSS = 25.0
USD_EQUIVALENTS = {"USD", "USDC", "USDT"}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _float_value(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _venue_names(manifest: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for venue in _as_list(manifest.get("venues")):
        venue_name = _as_dict(venue).get("venue")
        if isinstance(venue_name, str) and venue_name:
            names.append(venue_name)
    return names


def _add_if(condition: bool, issues: list[str], reason: str) -> None:
    if condition:
        issues.append(reason)


def _check_tiny_pilot_caps(strategy: dict[str, Any], issues: list[str]) -> None:
    cap_checks = (
        ("max_total_stake", DEFAULT_MAX_TOTAL_STAKE),
        ("max_leg_stake", DEFAULT_MAX_LEG_STAKE),
        ("max_daily_notional", DEFAULT_MAX_DAILY_NOTIONAL),
        ("max_daily_loss", DEFAULT_MAX_DAILY_LOSS),
    )
    for key, limit in cap_checks:
        value = _float_value(strategy.get(key))
        if value is None:
            issues.append(f"missing_{key}")
        elif value > limit:
            issues.append(f"{key}_above_tiny_pilot_limit")


def _check_currency_policy(manifest: dict[str, Any], issues: list[str]) -> None:
    strategy = _as_dict(manifest.get("strategy"))
    allow_cross_currency = bool(strategy.get("allow_cross_currency_live_execution"))
    stablecoins = {str(item).upper() for item in _as_list(strategy.get("stablecoin_currencies"))}
    _add_if(
        str(strategy.get("portfolio_base_currency", "")).upper() != "USD",
        issues,
        "portfolio_base_currency_not_usd",
    )
    _add_if(not USD_EQUIVALENTS.issubset(stablecoins), issues, "missing_usd_stablecoin_set")
    _add_if(allow_cross_currency, issues, "cross_currency_live_execution_allowed")
    for venue in _as_list(manifest.get("venues")):
        payload = _as_dict(venue)
        venue_name = str(payload.get("venue") or "UNKNOWN")
        base_currency = str(payload.get("base_currency") or "").upper()
        environment = str(payload.get("environment") or "").lower()
        dry_run = bool(payload.get("execution_dry_run"))
        if base_currency == "PLAY_EUR" and environment == "prod" and not dry_run:
            issues.append(f"{venue_name}:play_eur_used_for_prod_live_execution")
        if base_currency not in USD_EQUIVALENTS and not dry_run and not allow_cross_currency:
            issues.append(f"{venue_name}:non_usd_live_currency_without_cross_currency_gate")


def _check_execution_mode(
    manifest: dict[str, Any],
    issues: list[str],
    *,
    expected_mode: str | None,
) -> None:
    strategy = _as_dict(manifest.get("strategy"))
    mode = strategy.get("execution_venue_mode")
    if expected_mode and mode != expected_mode:
        issues.append(f"execution_venue_mode_not_{expected_mode}")
    venues = _venue_names(manifest)
    if mode == "cross_venue":
        _add_if(len(set(venues)) < 2, issues, "cross_venue_mode_requires_two_venues")
        _add_if(
            bool(strategy.get("allow_same_venue_live_execution")),
            issues,
            "cross_venue_mode_allows_same_venue_execution",
        )
    if mode == "same_venue":
        _add_if(len(set(venues)) != 1, issues, "same_venue_mode_requires_one_venue")
        _add_if(
            not bool(strategy.get("allow_same_venue_live_execution")),
            issues,
            "same_venue_mode_does_not_allow_same_venue_execution",
        )


def _check_execution_safety(manifest: dict[str, Any], issues: list[str]) -> None:
    strategy = _as_dict(manifest.get("strategy"))
    _add_if(
        not bool(strategy.get("opportunity_graph_enabled", True)),
        issues,
        "opportunity_graph_disabled",
    )
    _add_if(
        str(strategy.get("opportunity_graph_engine") or "").lower() != "semantic_rust",
        issues,
        "opportunity_graph_engine_not_semantic_rust",
    )
    _add_if(bool(manifest.get("validation_mode")), issues, "live_pilot_manifest_in_validation_mode")
    _add_if(
        not bool(strategy.get("auto_execute")),
        issues,
        "auto_execute_not_enabled_for_live_pilot",
    )
    _add_if(
        not bool(strategy.get("live_execution_armed")),
        issues,
        "manifest_live_execution_gate_not_declared",
    )
    _add_if(bool(strategy.get("value_execution_enabled")), issues, "value_execution_enabled")
    _add_if(
        strategy.get("execution_price_change_policy") != "better",
        issues,
        "unsafe_price_change_policy",
    )
    _add_if(
        _float_value(strategy.get("live_quote_age_slo_secs")) != 5.0,
        issues,
        "quote_age_slo_not_5s",
    )
    _add_if(
        _float_value(strategy.get("quote_max_pair_skew_secs")) != 1.0,
        issues,
        "pair_skew_slo_not_1s",
    )
    _check_tiny_pilot_caps(strategy, issues)
    for venue in _as_list(manifest.get("venues")):
        payload = _as_dict(venue)
        venue_name = str(payload.get("venue") or "UNKNOWN")
        _add_if(
            not bool(payload.get("execution_enabled")),
            issues,
            f"{venue_name}:execution_disabled",
        )
        metadata = _as_dict(payload.get("metadata"))
        if venue_name == "SXBET":
            _add_if(
                metadata.get("execution_mode") != "taker_fill",
                issues,
                "SXBET:taker_fill_not_configured",
            )


def lint_manifest(path: Path, *, expected_mode: str | None = None) -> dict[str, Any]:
    manifest = _load_json(path)
    issues: list[str] = []
    _check_execution_safety(manifest, issues)
    _check_execution_mode(manifest, issues, expected_mode=expected_mode)
    _check_currency_policy(manifest, issues)
    return {
        "path": str(path),
        "nodeId": manifest.get("node_id"),
        "status": "pass" if not issues else "fail",
        "issues": sorted(dict.fromkeys(issues)),
        "venues": _venue_names(manifest),
        "executionVenueMode": _as_dict(manifest.get("strategy")).get("execution_venue_mode"),
    }


def _format_text(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for result in results:
        lines.append(
            f"{result['path']}: status={result['status']} "
            f"node={result.get('nodeId')} mode={result.get('executionVenueMode')} "
            f"issues={','.join(str(issue) for issue in result.get('issues') or [])}",
        )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Live-pilot manifest paths")
    parser.add_argument(
        "--expected-mode",
        choices=("cross_venue", "same_venue"),
        help="Require a specific execution venue mode",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    results = [lint_manifest(path, expected_mode=args.expected_mode) for path in args.paths]
    if args.format == "text":
        print(_format_text(results))
    else:
        print(json.dumps(results, indent=2, sort_keys=True))
    if args.fail_on_issue and any(result["status"] != "pass" for result in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
