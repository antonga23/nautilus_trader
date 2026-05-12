#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Scan betting runtime code for brittle legacy matching seams.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


DEFAULT_SCAN_PATHS = (
    Path("nautilus_trader/adapters/betting"),
    Path("nautilus_trader/examples/strategies/betting_arbitrage.py"),
    Path("nautilus_trader/examples/strategies/betting_market_maker.py"),
    Path("nautilus_trader/examples/strategies/opportunity_graph.py"),
    Path("crates/model/src/python/opportunity_graph.rs"),
)

BANNED_SYMBOLS = (
    "MARKET_HEDGE_MAP",
    "MARKET_MAPPER",
    "_legacy_cross_market_candidate",
    "legacy_cross_market_candidate",
    "cross_market_confidence",
    "can_hedge_market",
    "MATCH_ODDS_DOUBLE_CHANCE",
    "BaseRiskEngine",
)

DIRECT_EVENT_MATCH_RE = re.compile(r"\.matches_event\(")
DIRECT_EVENT_MATCH_BLOCKLIST = (
    Path("nautilus_trader/adapters/betting/market_matcher.py"),
    Path("nautilus_trader/examples/strategies/betting_arbitrage.py"),
    Path("nautilus_trader/examples/strategies/opportunity_graph.py"),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    rule: str
    line: str

    def format(self) -> str:
        return f"{self.path}:{self.line_number}: {self.rule}: {self.line.strip()}"

    def to_dict(self) -> dict[str, str | int]:
        return {
            "path": str(self.path),
            "lineNumber": self.line_number,
            "rule": self.rule,
            "line": self.line.strip(),
        }


def iter_python_and_rust_files(paths: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        files.extend(
            child
            for child in sorted(path.rglob("*"))
            if child.suffix in {".py", ".rs"} and ".git" not in child.parts
        )
    return files


def scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return findings

    for line_number, line in enumerate(lines, start=1):
        for symbol in BANNED_SYMBOLS:
            if symbol in line:
                findings.append(Finding(path, line_number, f"banned-symbol:{symbol}", line))
        if is_direct_event_match_blocklisted(path) and DIRECT_EVENT_MATCH_RE.search(line):
            findings.append(Finding(path, line_number, "direct-matches-event-gate", line))
    return findings


def is_direct_event_match_blocklisted(path: Path) -> bool:
    path_parts = path.parts
    for blocklisted in DIRECT_EVENT_MATCH_BLOCKLIST:
        blocklisted_parts = blocklisted.parts
        if len(path_parts) >= len(blocklisted_parts) and (
            path_parts[-len(blocklisted_parts) :] == blocklisted_parts
        ):
            return True
    return False


def scan(paths: tuple[Path, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_python_and_rust_files(paths):
        findings.extend(scan_file(path))
    return findings


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=list(DEFAULT_SCAN_PATHS),
        help="Files or directories to scan.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    findings = scan(tuple(args.paths))
    if args.json:
        print(json.dumps({"findings": [finding.to_dict() for finding in findings]}, indent=2))
        return 1 if findings else 0
    if not findings:
        print("No betting brittleness patterns found.")
        return 0
    for finding in findings:
        print(finding.format())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
