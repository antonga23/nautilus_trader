#!/usr/bin/env python3
# ruff: noqa: E402
"""
Miner CPU-stage benchmark over a deterministic synthetic selection corpus.

Continuous-experimentation harness (mine-performance iteration 2). Iteration 1
removed the storage fsync bottleneck on the write path (~84x); this measures the
CPU side that remains. It synthesizes a structurally diverse corpus of
NormalizedSelectionRecord (multiple sports, ~records/8 fixtures with per-venue
cutoff skew, TOTALS/MATCH_ODDS/WINNER/POINT_SPREAD families with complementary
selection pairs), then times each RuleMiner bootstrap stage separately: in-memory
(pure mining CPU) and via the store-backed entrypoints that
scripts/betting/semantic_rule_mining.py uses for the real bootstrap.
RuleStore.bulk_writes() does not exist on this branch, so corpus population pays
per-record fsync (reported separately as populateSecs) and the *_from_store
stages include store reads. With --profile the largest stage is re-run under
cProfile and the top functions by cumulative time are embedded in the JSON
output. Exit code is always 0: this is a measurement tool.

"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from collections.abc import Sequence
import cProfile
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import json
from pathlib import Path
import pstats
import statistics
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nautilus_trader.adapters.betting.semantics import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import NormalizedSelection
from nautilus_trader.adapters.betting.semantics import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics import RuleMiner
from nautilus_trader.adapters.betting.semantics import RuleStore


DEFAULT_VENUES = ("CLOUDBET", "SXBET", "POLYMARKET")
# soccer can draw -> MATCH_ODDS three-way; basketball/baseball are no-draw sports ->
# WINNER two-way, which also exercises the WINNER x (+/-0.5 spread) cross-family
# partition projection path in the classifier.
SPORTS = ("soccer", "basketball", "baseball")
TOTALS_LINES = ("1.5", "2.5", "3.5")
VENUE_SKEW_MINUTES = 20
CORPUS_EPOCH = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
STAGE_NAMES = (
    "mine_event_candidates",
    "mine_templates",
    "store_load_records",
    "mine_store",
    "mine_templates_from_store",
    "mine_coverage_from_store",
)
STORAGE_NOTE = (
    "RuleStore.bulk_writes() is absent on this branch: corpus population pays "
    "per-record fsync (populateSecs, excluded from stages) and *_from_store stages "
    "include store reads; persist=False keeps mining writes out of all stage timings."
)

_HEAD_MARKET_BY_SPORT = {
    "soccer": CanonicalMarketType.MATCH_ODDS.value,
    "basketball": CanonicalMarketType.WINNER.value,
    "baseball": CanonicalMarketType.WINNER.value,
}
_RAW_MARKET = {
    CanonicalMarketType.TOTALS.value: ("Total Points", "totals"),
    CanonicalMarketType.MATCH_ODDS.value: ("Match Odds", "match_odds"),
    CanonicalMarketType.WINNER.value: ("Moneyline", "winner"),
    CanonicalMarketType.POINT_SPREAD.value: ("Point Spread", "point_spread"),
}


def _fixture_menu(
    sport: str,
    roles: tuple[str, str, str],
    line: str,
    alt_line: str,
) -> tuple[tuple[str, str, str, str], ...]:
    # Slot order keeps complementary counterparts inside any prefix truncation: the
    # default budget (records/fixtures = 8) emits slots 0-7, which still yields
    # OVER/UNDER pairs + a cross-venue equivalent (TOTALS), MATCH_ODDS/WINNER vs
    # +/-0.5 spread pairs, and one deliberately unpaired +1.5 spread leg as noise.
    totals = CanonicalMarketType.TOTALS.value
    spread = CanonicalMarketType.POINT_SPREAD.value
    head = _HEAD_MARKET_BY_SPORT[sport]
    venue_a, venue_b, venue_c = roles
    return (
        (venue_a, totals, "OVER", line),
        (venue_b, totals, "UNDER", line),
        (venue_b, totals, "OVER", line),
        (venue_a, head, "HOME", ""),
        (venue_b, head, "AWAY", ""),
        (venue_c, spread, "HOME", "-0.5"),
        (venue_a, spread, "AWAY", "0.5"),
        (venue_c, spread, "AWAY", "1.5"),
        (venue_a, spread, "HOME", "-1.5"),
        (venue_c, totals, "UNDER", alt_line),
        (venue_a, totals, "OVER", alt_line),
        (venue_c, head, "HOME", ""),
    )


def _record(
    *,
    record_id: str,
    venue: str,
    sport: str,
    event_key: str,
    market_type: str,
    selection: str,
    line: str,
) -> NormalizedSelectionRecord:
    raw_market_name, raw_market_type = _RAW_MARKET[market_type]
    return NormalizedSelectionRecord(
        record_id=record_id,
        provider=venue,
        selection=NormalizedSelection(
            venue=venue,
            instrument_id=record_id,
            sport=sport,
            event_key=event_key,
            period="full_time",
            scope="full_time",
            market_type=market_type,
            market_family=market_type,
            selection=selection,
            params=(("line", line),) if line else (),
            raw_market_name=raw_market_name,
            raw_market_type=raw_market_type,
            raw_outcome=selection,
            outcome_key=selection,
        ),
    )


def build_corpus(
    *,
    records: int,
    venues: Sequence[str] = DEFAULT_VENUES,
    fixtures: int | None = None,
    seed: int = 7,
) -> list[NormalizedSelectionRecord]:
    venue_list = tuple(venues)
    fixture_count = fixtures or max(1, records // 8)
    per_fixture, remainder = divmod(records, fixture_count)
    # 0/20/40min per-venue cutoff skew stays inside the miner's 2h cross-venue
    # cluster tolerance, so every venue's listing of a fixture lands in one bucket.
    skew = {venue: index * VENUE_SKEW_MINUTES for index, venue in enumerate(venue_list)}
    corpus: list[NormalizedSelectionRecord] = []
    for fixture in range(fixture_count):
        sport = SPORTS[fixture % len(SPORTS)]
        roles = tuple(venue_list[(fixture + i) % len(venue_list)] for i in range(3))
        line = TOTALS_LINES[fixture % len(TOTALS_LINES)]
        alt_line = TOTALS_LINES[(fixture + 1) % len(TOTALS_LINES)]
        menu = _fixture_menu(sport, roles, line, alt_line)
        start = CORPUS_EPOCH + timedelta(hours=(fixture * 7 + seed * 13) % (24 * 14))
        slots = per_fixture + (1 if fixture < remainder else 0)
        for slot in range(slots):
            venue, market_type, selection, slot_line = menu[slot % len(menu)]
            cutoff = start + timedelta(minutes=skew[venue])
            event_key = (
                f"{sport}|team {fixture} home|team {fixture} away|{cutoff:%Y-%m-%dT%H:%M:%S}Z"
            )
            corpus.append(
                _record(
                    record_id=f"{venue.lower()}-f{fixture}-s{slot}-"
                    f"{market_type.lower()}-{selection.lower()}",
                    venue=venue,
                    sport=sport,
                    event_key=event_key,
                    market_type=market_type,
                    selection=selection,
                    line=slot_line,
                ),
            )
    return corpus


def populate_store(records: list[NormalizedSelectionRecord], store_dir: Path) -> float:
    store = RuleStore(FileRuleCache(store_dir))
    started = time.perf_counter()
    with store.defer_index_writes():
        for record in records:
            store.save_normalized_selection(record)
    return time.perf_counter() - started


def _stage_runner(
    name: str,
    records: list[NormalizedSelectionRecord],
    store_dir: Path,
) -> Callable[[], int]:
    miner = RuleMiner(RuleStore(FileRuleCache(store_dir)))
    if name == "mine_event_candidates":
        return lambda: len(miner.mine_event_candidates(records, persist=False))
    if name == "mine_templates":
        return lambda: len(
            miner.mine_templates(records, persist=False, persist_event_candidates=False),
        )
    if name == "store_load_records":
        return lambda: len(miner.load_records())
    if name == "mine_store":
        return lambda: len(miner.mine_store(persist=False))
    if name == "mine_templates_from_store":
        return lambda: len(
            miner.mine_templates_from_store(persist=False, persist_event_candidates=False),
        )
    if name == "mine_coverage_from_store":
        return lambda: len(miner.mine_coverage_from_store(persist=False)[0])
    raise ValueError(f"Unknown stage {name}")


def _profile_stage(
    name: str,
    records: list[NormalizedSelectionRecord],
    store_dir: Path,
    top: int = 20,
) -> list[dict[str, object]]:
    runner = _stage_runner(name, records, store_dir)
    profiler = cProfile.Profile()
    profiler.runcall(runner)
    stats = pstats.Stats(profiler)
    entries = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:top]
    rows: list[dict[str, object]] = []
    for (filename, lineno, funcname), (_primitive, ncalls, tottime, cumtime, _callers) in entries:
        module = Path(filename).stem if filename != "~" else "builtin"
        rows.append(
            {
                "function": f"{module}:{funcname}",
                "cumtimeSecs": round(cumtime, 6),
                "tottimeSecs": round(tottime, 6),
                "ncalls": ncalls,
                "location": f"{filename}:{lineno}",
            },
        )
    return rows


def _sample_stage(
    name: str,
    records: list[NormalizedSelectionRecord],
    store_dir: Path,
) -> tuple[float, int] | str:
    runner = _stage_runner(name, records, store_dir)
    try:
        started = time.perf_counter()
        count = runner()
        return time.perf_counter() - started, count
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _stage_stats(
    timings: list[float],
    record_count: int,
    result_count: int,
) -> dict[str, float | int]:
    median = statistics.median(timings)
    spread_pct = ((max(timings) - min(timings)) / median * 100.0) if median else 0.0
    return {
        "medianSecs": round(median, 6),
        "recordsPerSec": round(record_count / median, 1) if median else 0.0,
        "variancePct": round(spread_pct, 2),
        "repeats": len(timings),
        "resultCount": result_count,
    }


def run_benchmark(
    records: list[NormalizedSelectionRecord],
    *,
    repeats: int = 3,
    store_dir: Path,
    profile: bool = False,
) -> dict[str, object]:
    store_path = Path(store_dir)
    populate_secs = populate_store(records, store_path)
    samples: dict[str, list[float]] = {name: [] for name in STAGE_NAMES}
    result_counts: dict[str, int] = {}
    stage_errors: dict[str, str] = {}
    for _ in range(max(1, repeats)):
        for name in STAGE_NAMES:
            if name in stage_errors:
                continue
            sample = _sample_stage(name, records, store_path)
            if isinstance(sample, str):
                stage_errors[name] = sample
                continue
            elapsed, result_counts[name] = sample
            samples[name].append(elapsed)

    per_stage = {
        name: _stage_stats(timings, len(records), result_counts[name])
        for name, timings in samples.items()
        if timings
    }
    payload: dict[str, object] = {
        "perStage": per_stage,
        "stageErrors": stage_errors,
        "populateSecs": round(populate_secs, 6),
        "storageNote": STORAGE_NOTE,
    }
    if profile and per_stage:
        largest = max(per_stage, key=lambda stage: per_stage[stage]["medianSecs"])
        payload["profiledStage"] = largest
        payload["profileTop"] = _profile_stage(largest, records, store_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=20000)
    parser.add_argument("--fixtures", type=int, default=0)
    parser.add_argument("--venues", type=str, default=",".join(DEFAULT_VENUES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args(argv)

    venues = tuple(item.strip().upper() for item in args.venues.split(",") if item.strip())
    fixtures = args.fixtures or max(1, args.records // 8)
    try:
        corpus = build_corpus(
            records=args.records,
            venues=venues or DEFAULT_VENUES,
            fixtures=fixtures,
            seed=args.seed,
        )
        with tempfile.TemporaryDirectory(prefix="miner-cpu-bench-") as tmp_dir:
            results = run_benchmark(
                corpus,
                repeats=args.repeats,
                store_dir=Path(tmp_dir) / "store",
                profile=args.profile,
            )
        metrics: dict[str, object] = {
            "records": args.records,
            "fixtures": fixtures,
            "venues": list(venues),
            "seed": args.seed,
            "repeats": args.repeats,
            **results,
        }
    except Exception as exc:
        metrics = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(metrics, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(metrics, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
