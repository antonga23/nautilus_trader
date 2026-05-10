#!/usr/bin/env python3
"""
Inspect GitHub self-hosted deploy runner availability.

This is a local/operator companion to the release workflow guard. It explains whether a
deploy run is blocked because no EC2 runner with the expected labels is online, without
requiring a full strategy-node release attempt.

"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


DEFAULT_REQUIRED_LABELS = ("self-hosted", "Linux", "X64", "ec2", "deploy", "trading")
DEFAULT_REPO = "antonga23/cloudbet-market-maker"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _runner_labels(runner: dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    for label in _as_list(runner.get("labels")):
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            labels.add(str(label["name"]))
        elif isinstance(label, str):
            labels.add(label)
    return labels


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _payload_from_json(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _gh_executable() -> str:
    gh_path = shutil.which("gh")
    if not gh_path:
        raise RuntimeError("gh executable not found in PATH")
    return gh_path


def _fetch_runner_payload_with_gh(repo: str, *, token: str | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    if token:
        env.setdefault("GH_TOKEN", token)
    completed = subprocess.run(  # noqa: S603 - fixed gh API arguments.
        [_gh_executable(), "api", f"repos/{repo}/actions/runners", "--paginate"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"gh runner API fallback failed: {detail}")
    return _payload_from_json(completed.stdout)


def _fetch_runner_payload(repo: str, *, token: str | None) -> dict[str, Any]:
    return _fetch_runner_payload_with_gh(repo, token=token)


def _runner_summary(runner: dict[str, Any], required_labels: set[str]) -> dict[str, Any]:
    labels = _runner_labels(runner)
    missing = sorted(required_labels - labels)
    online = runner.get("status") == "online"
    busy = bool(runner.get("busy"))
    return {
        "id": runner.get("id"),
        "name": runner.get("name"),
        "status": runner.get("status"),
        "busy": busy,
        "labels": sorted(labels),
        "matchesRequiredLabels": not missing,
        "missingLabels": missing,
        "online": online,
        "available": online and not busy,
    }


def evaluate_runner_payload(
    payload: dict[str, Any],
    *,
    required_labels: Iterable[str] = DEFAULT_REQUIRED_LABELS,
) -> dict[str, Any]:
    required = {str(label) for label in required_labels if str(label)}
    runners = [_as_dict(runner) for runner in _as_list(payload.get("runners"))]
    summaries = [_runner_summary(runner, required) for runner in runners]
    matching = [runner for runner in summaries if runner["matchesRequiredLabels"]]
    online = [runner for runner in matching if runner["online"]]
    available = [runner for runner in matching if runner["available"]]
    reasons: list[str] = []
    if not matching:
        reasons.append("no_runner_with_required_labels")
    elif not online:
        reasons.append("matching_runner_offline")
    elif not available:
        reasons.append("matching_runner_busy")
    return {
        "status": "pass" if online else "fail",
        "requiredLabels": sorted(required),
        "runnerCount": len(summaries),
        "matchingRunnerCount": len(matching),
        "matchingOnlineRunnerCount": len(online),
        "matchingAvailableRunnerCount": len(available),
        "reasons": reasons,
        "matchingRunners": matching,
        "runners": summaries,
    }


def _format_text(payload: dict[str, Any]) -> str:
    lines = [
        "deploy-runner "
        f"status={payload.get('status')} "
        f"matching={payload.get('matchingRunnerCount')} "
        f"online={payload.get('matchingOnlineRunnerCount')} "
        f"available={payload.get('matchingAvailableRunnerCount')} "
        f"reasons={','.join(str(reason) for reason in payload.get('reasons') or [])}",
    ]
    for runner in _as_list(payload.get("matchingRunners")):
        summary = _as_dict(runner)
        lines.append(
            "  runner "
            f"name={summary.get('name')} status={summary.get('status')} "
            f"busy={summary.get('busy')} missing={summary.get('missingLabels')}",
        )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo owner/name")
    parser.add_argument(
        "--input-json",
        type=Path,
        help="Read an Actions runners API JSON payload from disk instead of GitHub",
    )
    parser.add_argument(
        "--required-label",
        action="append",
        dest="required_labels",
        help="Required runner label; may be repeated",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format",
    )
    parser.add_argument(
        "--require-online",
        action="store_true",
        help="Return non-zero unless a matching runner is online",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    labels = args.required_labels or list(DEFAULT_REQUIRED_LABELS)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        payload = (
            _load_json(args.input_json)
            if args.input_json
            else _fetch_runner_payload(args.repo, token=token)
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result = evaluate_runner_payload(payload, required_labels=labels)
    if args.format == "text":
        print(_format_text(result))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    if args.require_online and result["status"] != "pass":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
