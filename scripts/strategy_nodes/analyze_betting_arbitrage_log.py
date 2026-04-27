#!/usr/bin/env python3
"""
Analyze persisted betting arbitrage node logs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from betting_arbitrage_log_analysis import analyze_betting_arbitrage_log_text
from betting_arbitrage_log_analysis import render_betting_arbitrage_analysis


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
