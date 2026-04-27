#!/usr/bin/env python3
"""
Analyze persisted betting arbitrage node logs.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys


def _load_analysis_module():
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module("scripts.strategy_nodes.betting_arbitrage_log_analysis")


_analysis_module = _load_analysis_module()
analyze_betting_arbitrage_log_text = _analysis_module.analyze_betting_arbitrage_log_text
render_betting_arbitrage_analysis = _analysis_module.render_betting_arbitrage_analysis


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path", type=Path, help="Path to a persisted strategy-node log")
    parser.add_argument("--json", action="store_true", help="Print the full analysis as JSON")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of accepted opportunities to render in text mode",
    )
    args = parser.parse_args()

    text = args.log_path.read_text(encoding="utf-8")
    analysis = analyze_betting_arbitrage_log_text(text)

    if args.json:
        json.dump(analysis.to_dict(), sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
        return 0

    print(render_betting_arbitrage_analysis(analysis, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
