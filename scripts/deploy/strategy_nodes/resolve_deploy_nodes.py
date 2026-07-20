#!/usr/bin/env python3
"""
Resolve the strategy-node deploy target list for strategy-node-release.yml.

Precedence (all inputs optional, read from the environment):
  1. NODES   - explicit JSON array of {"manifest","container"} objects (ad-hoc set)
  2. FLEET   - a named key in deploy/strategy_nodes/fleets.json (fans out to its nodes)
  3. MANIFEST + CONTAINER - a single node (backward-compatible one-element list)

Emits GitHub Actions step outputs on stdout (append to $GITHUB_OUTPUT):
  nodes=<compact JSON array>        # consumed by the deploy matrix
  manifests=<space-separated paths> # consumed by the validate loop

"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

FLEETS_PATH = Path("deploy/strategy_nodes/fleets.json")
DEFAULT_MANIFEST = "deploy/strategy_nodes/betting_arbitrage/sxbet-single-venue.json"
DEFAULT_CONTAINER = "betting-arbitrage-node-sxbet"
CONTAINER_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _fail(msg: str) -> None:
    print(f"resolve_deploy_nodes: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _validate(nodes: list[dict]) -> list[dict]:
    if not nodes:
        _fail("resolved an empty node list")
    seen_containers: set[str] = set()
    for node in nodes:
        manifest = str(node.get("manifest", "")).strip()
        container = str(node.get("container", "")).strip()
        if not manifest.startswith("deploy/strategy_nodes/") or not manifest.endswith(".json"):
            _fail(f"manifest must be a deploy/strategy_nodes/*.json path: {manifest!r}")
        if not Path(manifest).is_file():
            _fail(f"manifest file not found: {manifest}")
        if not CONTAINER_RE.match(container):
            _fail(f"invalid container name: {container!r}")
        if container in seen_containers:
            _fail(f"duplicate container in node list: {container}")
        seen_containers.add(container)
    return [{"manifest": n["manifest"].strip(), "container": n["container"].strip()} for n in nodes]


def resolve() -> list[dict]:
    nodes_input = os.environ.get("NODES", "").strip()
    fleet_input = os.environ.get("FLEET", "").strip()
    manifest_input = os.environ.get("MANIFEST", "").strip()
    container_input = os.environ.get("CONTAINER", "").strip()

    if nodes_input:
        try:
            parsed = json.loads(nodes_input)
        except json.JSONDecodeError as exc:
            _fail(f"`nodes` is not valid JSON: {exc}")
        if not isinstance(parsed, list):
            _fail("`nodes` must be a JSON array of {manifest, container} objects")
        return _validate(parsed)

    if fleet_input:
        if not FLEETS_PATH.is_file():
            _fail(f"{FLEETS_PATH} not found")
        fleets = json.loads(FLEETS_PATH.read_text())
        if fleet_input not in fleets:
            available = ", ".join(k for k in fleets if not k.startswith("_"))
            _fail(f"unknown fleet {fleet_input!r}; available: {available}")
        return _validate(fleets[fleet_input])

    return _validate(
        [
            {
                "manifest": manifest_input or DEFAULT_MANIFEST,
                "container": container_input or DEFAULT_CONTAINER,
            },
        ],
    )


def main() -> int:
    nodes = resolve()
    print(f"nodes={json.dumps(nodes, separators=(',', ':'))}")
    print("manifests=" + " ".join(n["manifest"] for n in nodes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
