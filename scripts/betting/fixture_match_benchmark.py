#!/usr/bin/env python3
"""
Labeled cross-venue fixture-match recall/precision benchmark.

Continuous-experimentation harness for the cross-venue fixture-identity path. Scores
MarketMatcher.explain_hedge_event_match against a hand-labeled corpus of
CLOUDBET/POLYMARKET/SXBET scenarios and emits structured JSON metrics. The primary
signal is precision on doubleheader-ambiguity scenarios (same teams, two fixtures the
same day, both within the cross-venue soft start-time tolerance) alongside recall on
genuine cross-venue matches.

"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency


def _inst(
    *,
    venue: str,
    event_name: str,
    home_name: str,
    away_name: str,
    sport_name: str = "basketball",
    start_time: str = "2026-05-10T18:00:00Z",
    event_id: str | None = None,
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id=event_id or f"{venue.lower()}-{event_name}-{start_time}".replace(" ", "-"),
        event_name=event_name,
        home_name=home_name,
        away_name=away_name,
        sport_name=sport_name,
        competition_name="Test League",
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        start_time=start_time,
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    instrument: CryptoBettingInstrument
    candidate: CryptoBettingInstrument
    pool: list[CryptoBettingInstrument]
    expected_match: bool


def _scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []

    # --- clear cross-venue matches (recall) ---
    cb = _inst(
        venue="CLOUDBET",
        event_name="MIN Timberwolves v SA Spurs",
        home_name="MIN Timberwolves",
        away_name="SA Spurs",
        start_time="2026-05-10T18:00:00Z",
    )
    sx = _inst(
        venue="SXBET",
        event_name="Minnesota Timberwolves vs San Antonio Spurs",
        home_name="Minnesota Timberwolves",
        away_name="San Antonio Spurs",
        start_time="2026-05-10T18:00:00Z",
    )
    scenarios.append(Scenario("alias_exact_time", "clear_match", cb, sx, [cb, sx], True))

    cb2 = _inst(
        venue="CLOUDBET",
        event_name="HOU Astros v TB Rays",
        home_name="HOU Astros",
        away_name="TB Rays",
        sport_name="baseball",
        start_time="2026-05-10T18:00:00Z",
    )
    sx2 = _inst(
        venue="SXBET",
        event_name="Houston Astros vs Tampa Bay Rays",
        home_name="Houston Astros",
        away_name="Tampa Bay Rays",
        sport_name="baseball",
        start_time="2026-05-10T18:20:00Z",
    )
    scenarios.append(Scenario("alias_skew_20min", "skew_match", cb2, sx2, [cb2, sx2], True))

    # --- clear non-matches (precision floor) ---
    diff_a = _inst(
        venue="CLOUDBET",
        event_name="Boston Celtics v Miami Heat",
        home_name="Boston Celtics",
        away_name="Miami Heat",
    )
    diff_b = _inst(
        venue="SXBET",
        event_name="Los Angeles Lakers vs Denver Nuggets",
        home_name="Los Angeles Lakers",
        away_name="Denver Nuggets",
    )
    scenarios.append(
        Scenario("different_teams", "non_match", diff_a, diff_b, [diff_a, diff_b], False),
    )

    # city-prefix false-positive trap (#222): shared "new york" prefix, different teams
    ny_a = _inst(
        venue="CLOUDBET",
        event_name="New York Knicks v Chicago Bulls",
        home_name="New York Knicks",
        away_name="Chicago Bulls",
    )
    ny_b = _inst(
        venue="SXBET",
        event_name="New York Nets vs Chicago Bulls",
        home_name="New York Nets",
        away_name="Chicago Bulls",
    )
    scenarios.append(Scenario("city_prefix_trap", "non_match", ny_a, ny_b, [ny_a, ny_b], False))

    # --- doubleheader ambiguity (#231/#237): same teams, two same-day fixtures on the
    # source venue, both within the cross-venue soft tolerance of the target. The target
    # cannot be uniquely attributed, so a hedge match must NOT be asserted. ---
    dh_early = _inst(
        venue="CLOUDBET",
        event_name="COL Rockies v SF Giants",
        home_name="COL Rockies",
        away_name="SF Giants",
        sport_name="baseball",
        start_time="2026-05-10T18:00:00Z",
        event_id="cb-rockies-giants-g1",
    )
    dh_late = _inst(
        venue="CLOUDBET",
        event_name="COL Rockies v SF Giants",
        home_name="COL Rockies",
        away_name="SF Giants",
        sport_name="baseball",
        start_time="2026-05-10T21:00:00Z",
        event_id="cb-rockies-giants-g2",
    )
    dh_target = _inst(
        venue="SXBET",
        event_name="Colorado Rockies vs San Francisco Giants",
        home_name="Colorado Rockies",
        away_name="San Francisco Giants",
        sport_name="baseball",
        start_time="2026-05-10T19:30:00Z",
    )
    dh_pool = [dh_early, dh_late, dh_target]
    scenarios.append(
        Scenario(
            "doubleheader_both_times",
            "doubleheader_ambiguous",
            dh_target,
            dh_early,
            dh_pool,
            False,
        ),
    )

    # single (non-doubleheader) same fixture with start times => must still match (recall guard)
    single_cb = _inst(
        venue="CLOUDBET",
        event_name="ATL Braves v NY Mets",
        home_name="ATL Braves",
        away_name="NY Mets",
        sport_name="baseball",
        start_time="2026-05-10T18:00:00Z",
    )
    single_sx = _inst(
        venue="SXBET",
        event_name="Atlanta Braves vs New York Mets",
        home_name="Atlanta Braves",
        away_name="New York Mets",
        sport_name="baseball",
        start_time="2026-05-10T18:40:00Z",
    )
    scenarios.append(
        Scenario(
            "single_fixture_both_times",
            "clear_match",
            single_sx,
            single_cb,
            [single_cb, single_sx],
            True,
        ),
    )

    return scenarios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args(argv)

    matcher = MarketMatcher()
    rows = []
    for scenario in _scenarios():
        result = matcher.explain_hedge_event_match(
            scenario.instrument,
            scenario.candidate,
            scenario.pool,
        )
        matched = bool(result["matched"])
        correct = matched == scenario.expected_match
        rows.append(
            {
                "name": scenario.name,
                "category": scenario.category,
                "expected": scenario.expected_match,
                "matched": matched,
                "correct": correct,
                "reason": result["reason"],
                "ambiguous": result["ambiguous"],
                "confidence": result["confidence"],
            },
        )

    should_match = [r for r in rows if r["expected"]]
    should_not = [r for r in rows if not r["expected"]]
    tp = sum(1 for r in should_match if r["matched"])
    fn = sum(1 for r in should_match if not r["matched"])
    fp = sum(1 for r in should_not if r["matched"])
    tn = sum(1 for r in should_not if not r["matched"])
    doubleheader = [r for r in rows if r["category"] == "doubleheader_ambiguous"]
    dh_false_matches = sum(1 for r in doubleheader if r["matched"])

    metrics = {
        "totalScenarios": len(rows),
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
        "truePositives": tp,
        "falseNegatives": fn,
        "falsePositives": fp,
        "trueNegatives": tn,
        "doubleheaderFalseMatches": dh_false_matches,
        "correctScenarios": sum(1 for r in rows if r["correct"]),
        "rows": rows,
    }

    print(json.dumps(metrics, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(metrics, indent=2, default=str))
    # non-zero exit if any scenario is mislabeled by the matcher (for CI-style gating)
    return 0 if metrics["correctScenarios"] == metrics["totalScenarios"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
