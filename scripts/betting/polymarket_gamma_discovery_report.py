#!/usr/bin/env python3
"""
Summarize Polymarket Gamma sport metadata for runtime discovery audits.

When runtime coverage is thin for one sport, the first question is whether the Gamma
`/sports` taxonomy has useful tag IDs for that sport. This helper turns a saved
`/sports` payload into canonical sport groups so an operator can see which tags the
provider should query.

"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.request import Request
from urllib.request import urlopen

from nautilus_trader.adapters.polymarket.providers import _canonical_polymarket_sport
from nautilus_trader.adapters.polymarket.providers import _selected_sports_tag_ids


DEFAULT_GAMMA_SPORTS_URL = "https://gamma-api.polymarket.com/sports"


def _load_sports_payload(path: Path | None, *, fetch: bool) -> list[Any]:
    if path is not None:
        data = json.loads(path.read_text())
    elif fetch:
        request = Request(  # noqa: S310 - fixed public HTTPS URL.
            DEFAULT_GAMMA_SPORTS_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "cloudbet-market-maker/polymarket-gamma-report",
            },
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed public HTTPS URL.
            data = json.loads(response.read().decode("utf-8"))
    else:
        raise ValueError("Provide --sports-json or --fetch")
    if not isinstance(data, list):
        raise ValueError("Gamma sports payload must be a list")
    return data


def summarize_sports_metadata(
    sports_payload: list[Any],
    *,
    requested_sports: set[str] | None = None,
) -> dict[str, Any]:
    requested = {_canonical_polymarket_sport(sport) for sport in requested_sports or set() if sport}
    sport_codes: dict[str, set[str]] = defaultdict(set)
    tag_ids: dict[str, set[str]] = defaultdict(set)
    skipped = 0
    for item in sports_payload:
        if not isinstance(item, dict):
            skipped += 1
            continue
        sport_code = str(item.get("sport") or "")
        canonical_sport = _canonical_polymarket_sport(sport_code)
        if requested and canonical_sport not in requested:
            continue
        if sport_code:
            sport_codes[canonical_sport].add(sport_code)
        for tag_id in _selected_sports_tag_ids(item):
            tag_ids[canonical_sport].add(str(tag_id))

    sports = {
        sport: {
            "sportCodes": sorted(sport_codes[sport]),
            "tagIds": sorted(tag_ids[sport], key=lambda value: (len(value), value)),
            "tagCount": len(tag_ids[sport]),
        }
        for sport in sorted(sport_codes)
    }
    return {
        "sportCount": len(sports),
        "skippedRows": skipped,
        "requestedSports": sorted(requested),
        "unresolvedRequestedSports": sorted(requested - set(sports)),
        "sports": sports,
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "polymarket gamma discovery",
        f"  sport_count={summary['sportCount']} skipped_rows={summary['skippedRows']}",
    ]
    unresolved = summary.get("unresolvedRequestedSports") or []
    if unresolved:
        lines.append(f"  unresolved_requested_sports={unresolved}")
    for sport, payload in summary["sports"].items():
        lines.append(
            f"  {sport}: codes={payload['sportCodes']} tags={payload['tagIds']}",
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sports-json", type=Path, help="Saved Gamma /sports JSON payload")
    parser.add_argument("--fetch", action="store_true", help="Fetch Gamma /sports directly")
    parser.add_argument(
        "--sport",
        action="append",
        default=[],
        help="Canonical or provider sport key to include. May be repeated.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    payload = _load_sports_payload(args.sports_json, fetch=args.fetch)
    summary = summarize_sports_metadata(payload, requested_sports=set(args.sport))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
