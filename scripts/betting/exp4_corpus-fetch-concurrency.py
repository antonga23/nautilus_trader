#!/usr/bin/env python3
"""
Experiment: corpus-fetch-concurrency.

Grounds the "fresh-mine corpus fetch is fetch-bound" hypothesis against the real
orchestration in nautilus_trader/adapters/betting/semantics/corpus.py
(`SnapshotIngestor.refresh_cloudbet`).

Grounding read (verified against this checkout, not assumed):
  - The from/to window logic lives at lines ~114-296 of corpus.py: a
    `for sport_key in selected_sports:` loop, and inside it a `while True:`
    adaptive-window block (lines ~130-175) that does `await
    client.get_events_for_sport(...)` and only doubles the window and retries
    *when adaptive_window=True*. With adaptive_window=False (this experiment's
    setting) that inner while-loop always executes exactly once and `break`s
    immediately (line 169-174), so the only sequential-await structure left is
    the **outer per-sport loop**, which is a plain `await`-in-a-`for` with no
    data dependency between sports: `get_sport`, `get_events_for_sport`,
    `get_competition`, `get_event` (lines 116-397) all run one sport fully
    before starting the next.
  - `client.event_to_selection` (nautilus_trader/adapters/cloudbet/client/core.py
    ~723) builds `Selection` msgspec structs from `event.competitions`; the
    real `Selection` struct (schema.py ~249) has **no** `market_url` field, so
    `_cloudbet_selection_field(selection, "market_url")` (corpus.py line 381)
    always resolves via `getattr(..., None)` to `None` for the CLOUDBET
    primary path -- meaning `client.get_line(...)` (corpus.py line 392) is
    dead in practice for this path. Confirmed by reading both files; the
    mock therefore reproduces 4 awaited calls/sport (get_sport,
    get_events_for_sport, get_competition, get_event), not 5.
  - `include_bets` (corpus.py 408-434) is a separate, inherently sequential
    paginated block unrelated to the per-sport loop; disabled here
    (`include_bets=False`) to isolate the fetch-loop concurrency signal named
    in the task brief.

PLAN vs HARD RULES:
  - Baseline calls the REAL, unmodified `SnapshotIngestor.refresh_cloudbet`
    (imported from the installed source, not edited) against a fake
    `CloudbetClient` whose network methods are `await asyncio.sleep(0.1)` +
    deterministic payloads (real msgspec response Structs where the real
    client returns real Structs: `GetSportsResponse`, `GetSportsResponseSport`,
    `Selection`; small structurally-equivalent local Structs standing in for
    the real, deeply nested `GetEventsForSportResponse`/competition tree,
    since corpus.py + our own fake `event_to_selection` only ever touch
    `.competitions` / `.key` on that object).
  - Variant is a mirrored per-sport worker (`_process_sport`) that is a
    line-for-line port of the real per-sport loop body (same branches, same
    order of `_save_snapshot` calls, same coverage_report/normalized_records
    construction) run under `asyncio.gather` with a `Semaphore(N)`, then
    merged back into the shared accumulators **in original sport-list
    order** (`asyncio.gather` preserves input order regardless of completion
    order) -- this is what gives byte-identical `RuleCorpusManifest` /
    snapshot-store contents vs. the sequential baseline, not any assumption
    about scheduling order.
  - `_utc_now` is monkeypatched to a fixed timestamp for both baseline and
    variant runs so `fetched_at` (which feeds every snapshot_id/manifest_id
    hash) is identical across separately-timed runs -- otherwise two
    sequential runs of the *same* unmodified code would never hash-match
    either, which would be a meaningless "identical output" bar.
  - No live network/venue calls; the only I/O is `asyncio.sleep`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from contextlib import suppress
from dataclasses import asdict as dc_asdict
from pathlib import Path
from typing import Any

import msgspec

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT))

from nautilus_trader.adapters.betting.semantics import corpus as corpus_mod  # noqa: E402
from nautilus_trader.adapters.betting.semantics.corpus import SnapshotIngestor  # noqa: E402
from nautilus_trader.adapters.betting.semantics.normalization import MarketNormalizer  # noqa: E402
from nautilus_trader.adapters.betting.semantics.store import RuleStore  # noqa: E402
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelectionRecord  # noqa: E402
from nautilus_trader.adapters.betting.semantics.types import RuleCorpusManifest  # noqa: E402
from nautilus_trader.adapters.cloudbet.client.schema import (  # noqa: E402
    GetSportsResponse,
    GetSportsResponseSport,
    Selection,
    SelectionStatus,
)

REQUEST_LATENCY_SECONDS = 0.1
SPORT_COUNT = 25  # P providers*windows-equivalent: 25 sports * 4 requests/sport + 1 = 101 requests
REPEATS = 7
CONCURRENCY_LEVELS = (8, 16)
FIXED_FETCHED_AT = "2026-07-10T00:00:00Z"
FROM_TS = 1_800_000_000
TO_TS = 1_800_086_400


# ----------------------------------------------------------------------------
# Minimal structurally-equivalent stand-ins for the real (deeply nested)
# Cloudbet events-for-sport response tree. corpus.py and our fake client's own
# event_to_selection only ever touch `.competitions` / `.key` on this object
# (verified above), so this is not inventing new adapter API surface, only
# satisfying the exact attribute contract corpus.py exercises.
# ----------------------------------------------------------------------------
class _FakeCompetition(msgspec.Struct):
    key: str


class _FakeEventsResponse(msgspec.Struct):
    competitions: list[_FakeCompetition]


class InMemoryCache:
    """
    Dict-backed stand-in for RuleStore's cache dependency (FileRuleCache duck-type).
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def add(self, key: str, value: bytes) -> None:
        self._data[key] = value

    def get(self, key: str) -> bytes | None:
        return self._data.get(key)


class FakeCloudbetClient:
    """
    Duck-typed CloudbetClient double: same call signatures corpus.py invokes,
    `REQUEST_LATENCY_SECONDS` injected latency per call, deterministic
    payloads seeded only by sport_key/event_id (no randomness, no clock reads
    besides the frozen `fetched_at` corpus.py itself threads through).
    """

    def __init__(self, sport_keys: list[str], *, latency: float) -> None:
        self.sport_keys = sport_keys
        self.latency = latency
        self.in_flight = 0
        self.peak_in_flight = 0
        self.call_count = 0

    async def _sleep(self) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.call_count += 1
        try:
            await asyncio.sleep(self.latency)
        finally:
            self.in_flight -= 1

    async def get_sports(self) -> GetSportsResponse:
        await self._sleep()
        return GetSportsResponse(
            sports=[
                GetSportsResponseSport(
                    name=key.replace("_", " ").title(),
                    key=key,
                    competition_count=1,
                    event_count=2,
                )
                for key in self.sport_keys
            ],
        )

    async def get_sport(self, sport_key: str) -> dict[str, Any]:
        await self._sleep()
        return {"key": sport_key, "name": sport_key.title(), "competitionCount": 1, "eventCount": 2}

    async def get_events_for_sport(
        self,
        *,
        sport_key: str,
        from_timestamp: int,
        to_timestamp: int,
        limit: int,
    ) -> _FakeEventsResponse:
        await self._sleep()
        return _FakeEventsResponse(competitions=[_FakeCompetition(key=f"{sport_key}-comp-1")])

    def event_to_selection(self, events_response: _FakeEventsResponse) -> list[Selection]:
        # Pure/no I/O, matches the real client's event_to_selection (no await).
        selections: list[Selection] = []
        for competition in events_response.competitions:
            event_id = abs(hash(competition.key)) % 1_000_000
            for outcome, side in (("home", "BACK"), ("away", "LAY")):
                selections.append(
                    Selection(
                        competition_name=competition.key,
                        competition_key=competition.key,
                        sport_name=competition.key,
                        sport_key=competition.key,
                        event_id=event_id,
                        status="TRADING",
                        market_name="moneyline",
                        submarket_name="moneyline_full_time",
                        submarket_period="full_time",
                        sequence="1",
                        outcome=outcome,
                        price=1.9,
                        min_stake=1.0,
                        max_stake=100.0,
                        probability=0.5,
                        selection_status=SelectionStatus.ENABLED,
                        side=side,
                        cutoff_time="2026-07-10T00:00:00Z",
                        event_name=f"{competition.key} event",
                        params="",
                    ),
                )
        return selections

    async def get_competition(self, competition_key: str) -> dict[str, Any]:
        await self._sleep()
        return {
            "name": competition_key,
            "key": competition_key,
            "sport": {"name": competition_key, "key": competition_key},
            "events": [],
        }

    async def get_event(self, event_id: int) -> dict[str, Any]:
        await self._sleep()
        return {"id": event_id, "name": f"event-{event_id}"}

    async def get_line(self, event_id: int, market_url: str) -> dict[str, Any]:
        await self._sleep()
        return {"eventId": event_id, "marketUrl": market_url}


def make_ingestor() -> tuple[SnapshotIngestor, InMemoryCache]:
    cache = InMemoryCache()
    store = RuleStore(cache)
    return SnapshotIngestor(store, MarketNormalizer()), cache


SPORT_KEYS = [f"sport_{i:03d}" for i in range(SPORT_COUNT)]


async def run_baseline() -> tuple[RuleCorpusManifest, InMemoryCache, FakeCloudbetClient, float]:
    ingestor, cache = make_ingestor()
    client: Any = FakeCloudbetClient(SPORT_KEYS, latency=REQUEST_LATENCY_SECONDS)
    start = time.perf_counter()
    manifest = await ingestor.refresh_cloudbet(
        client,
        sports=SPORT_KEYS,
        from_timestamp=FROM_TS,
        to_timestamp=TO_TS,
        limit=20,
        adaptive_window=False,
        include_recent_past_on_sparse=False,
        include_bets=False,
        fetch_concurrency=1,
    )
    elapsed = time.perf_counter() - start
    return manifest, cache, client, elapsed


# ----------------------------------------------------------------------------
# Variant: mirrored per-sport worker, run concurrently under a semaphore.
# Line-for-line port of corpus.py SnapshotIngestor.refresh_cloudbet's per-sport
# body for the adaptive_window=False / include_recent_past_on_sparse=False
# path (the path this experiment's baseline call also takes), factored out so
# it can be scheduled independently per sport.
# ----------------------------------------------------------------------------
async def _process_sport(  # noqa: C901
    ingestor: SnapshotIngestor,
    client: FakeCloudbetClient,
    sport_key: str,
    *,
    fetched_at: str,
    from_timestamp: int,
    to_timestamp: int,
    limit: int,
    max_window_seconds: int,
) -> dict[str, Any]:
    source_refs: list[str] = []
    market_names: set[str] = set()
    normalized_records: list[NormalizedSelectionRecord] = []

    with suppress(Exception):
        source_refs.append(
            ingestor._save_snapshot(
                provider="CLOUDBET",
                endpoint=f"/pub/v2/odds/sports/{sport_key}",
                fetched_at=fetched_at,
                payload=await client.get_sport(sport_key),
            ),
        )

    base_window_seconds = max(to_timestamp - from_timestamp, 24 * 60 * 60)
    window_seconds = min(base_window_seconds, max_window_seconds)
    attempt_from = from_timestamp
    attempt_to = from_timestamp + window_seconds
    events_response = None
    selections: list[Any] = []
    attempt_reports: list[dict[str, Any]] = []
    try:
        events_response = await client.get_events_for_sport(
            sport_key=sport_key,
            from_timestamp=attempt_from,
            to_timestamp=attempt_to,
            limit=limit,
        )
    except Exception as exc:
        attempt_reports.append(
            {"from": attempt_from, "to": attempt_to, "error": type(exc).__name__},
        )
        events_response = None
    else:
        snapshot_id = ingestor._save_snapshot(
            provider="CLOUDBET",
            endpoint=f"/pub/v2/odds/events?sport={sport_key}&from={attempt_from}&to={attempt_to}",
            fetched_at=fetched_at,
            payload=events_response,
        )
        source_refs.append(snapshot_id)
        selections = client.event_to_selection(events_response)
        seen_attempt_events = {selection.event_id for selection in selections}
        attempt_reports.append(
            {
                "from": attempt_from,
                "to": attempt_to,
                "event_count": len(seen_attempt_events),
                "selection_count": len(selections),
            },
        )
    # adaptive_window=False => real code's while-loop always breaks here (corpus.py 169-174).

    if events_response is None:
        return {
            "sport_key": sport_key,
            "source_refs": source_refs,
            "market_names": market_names,
            "selection_count": 0,
            "event_count": 0,
            "normalized_records": normalized_records,
            "coverage_entry": (
                sport_key,
                {"event_count": 0, "selection_count": 0, "attempts": attempt_reports},
            ),
        }

    first_competition = next(iter(events_response.competitions), None)
    competition_payload = None
    if first_competition is not None:
        with suppress(Exception):
            competition_payload = await client.get_competition(first_competition.key)
            source_refs.append(
                ingestor._save_snapshot(
                    provider="CLOUDBET",
                    endpoint=f"/pub/v2/odds/competitions/{first_competition.key}",
                    fetched_at=fetched_at,
                    payload=competition_payload,
                ),
            )

    if not selections and competition_payload:
        selections = SnapshotIngestor._cloudbet_competition_to_selections(competition_payload)
        attempt_reports.append(
            {
                "source": "competition",
                "event_count": len(
                    {
                        SnapshotIngestor._cloudbet_selection_field(selection, "event_id")
                        for selection in selections
                    },
                ),
                "selection_count": len(selections),
            },
        )

    selection_count = len(selections)
    seen_event_ids = {
        event_id
        for selection in selections
        if (event_id := SnapshotIngestor._cloudbet_selection_field(selection, "event_id"))
        is not None
    }
    event_count = len(seen_event_ids)
    market_names.update(
        market_name
        for selection in selections
        if (market_name := SnapshotIngestor._cloudbet_selection_field(selection, "market_name"))
    )
    sparse_event_threshold = 4
    coverage_entry = (
        sport_key,
        {
            "event_count": event_count,
            "selection_count": selection_count,
            "attempts": attempt_reports,
            "sparse": event_count < sparse_event_threshold,
            "sparse_event_threshold": sparse_event_threshold,
        },
    )

    for selection in selections:
        normalized = ingestor._normalizer.normalize(selection)
        normalized_records.append(
            NormalizedSelectionRecord(
                record_id=SnapshotIngestor._normalized_record_id("CLOUDBET", normalized),
                provider="CLOUDBET",
                selection=normalized,
                manifest_id=None,
            ),
        )

    first_event_id = next(iter(seen_event_ids), None)
    if first_event_id is not None:
        try:
            event_response = await client.get_event(first_event_id)
        except Exception:
            event_response = None
        if event_response is not None:
            source_refs.append(
                ingestor._save_snapshot(
                    provider="CLOUDBET",
                    endpoint=f"/pub/v2/odds/events/{first_event_id}",
                    fetched_at=fetched_at,
                    payload=event_response,
                ),
            )
        first_line_selection = next(
            (
                selection
                for selection in selections
                if SnapshotIngestor._cloudbet_selection_field(selection, "event_id")
                == first_event_id
            ),
            None,
        )
        market_url = (
            SnapshotIngestor._cloudbet_selection_field(first_line_selection, "market_url")
            if first_line_selection is not None
            else None
        )
        if market_url:
            with suppress(Exception):
                source_refs.append(
                    ingestor._save_snapshot(
                        provider="CLOUDBET",
                        endpoint="/pub/v2/odds/lines",
                        fetched_at=fetched_at,
                        payload=await client.get_line(first_event_id, market_url),
                    ),
                )

    return {
        "sport_key": sport_key,
        "source_refs": source_refs,
        "market_names": market_names,
        "selection_count": selection_count,
        "event_count": event_count,
        "normalized_records": normalized_records,
        "coverage_entry": coverage_entry,
    }


async def refresh_cloudbet_concurrent(
    ingestor: SnapshotIngestor,
    client: FakeCloudbetClient,
    *,
    sports: list[str],
    from_timestamp: int,
    to_timestamp: int,
    limit: int,
    max_window_seconds: int,
    concurrency: int,
    fetched_at: str,
) -> RuleCorpusManifest:
    sports_response = await client.get_sports()
    ingestor._save_snapshot(
        provider="CLOUDBET",
        endpoint="/pub/v2/odds/sports",
        fetched_at=fetched_at,
        payload=sports_response,
    )
    selected_sports = SnapshotIngestor._resolve_cloudbet_sports(
        requested_sports=sports,
        available_sports=sports_response.sports,
    )

    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(sport_key: str) -> dict[str, Any]:
        async with semaphore:
            return await _process_sport(
                ingestor,
                client,
                sport_key,
                fetched_at=fetched_at,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                limit=limit,
                max_window_seconds=max_window_seconds,
            )

    # asyncio.gather preserves *input* order in its result list regardless of
    # completion order, so merging below reproduces the exact sequential
    # baseline's append order deterministically.
    results = await asyncio.gather(*(_bounded(sport_key) for sport_key in selected_sports))

    source_refs: list[str] = []
    market_names: set[str] = set()
    selection_count = 0
    event_count = 0
    normalized_records: list[NormalizedSelectionRecord] = []
    coverage_report: dict[str, Any] = {
        "provider": "CLOUDBET",
        "from_timestamp": from_timestamp,
        "to_timestamp": to_timestamp,
        "adaptive_window": False,
        "max_window_seconds": max_window_seconds,
        "min_events_per_sport": 1,
        "past_sparse_event_threshold": 4,
        "sports": {},
    }
    for result in results:
        source_refs.extend(result["source_refs"])
        market_names.update(result["market_names"])
        selection_count += result["selection_count"]
        event_count += result["event_count"]
        normalized_records.extend(result["normalized_records"])
        sport_key, entry = result["coverage_entry"]
        coverage_report["sports"][sport_key] = entry

    source_refs.append(
        ingestor._save_snapshot(
            provider="CLOUDBET",
            endpoint="/semantic/coverage/cloudbet",
            fetched_at=fetched_at,
            payload=coverage_report,
        ),
    )

    manifest = RuleCorpusManifest(
        manifest_id=corpus_mod._hash_payload(
            "manifest",
            {
                "provider": "CLOUDBET",
                "fetched_at": fetched_at,
                "sports": selected_sports,
                "market_names": sorted(market_names),
            },
        ),
        provider="CLOUDBET",
        fetched_at=fetched_at,
        endpoint_version="feed:v2,trading:v4",
        sport_count=len(selected_sports),
        event_count=event_count,
        selection_count=selection_count,
        market_taxonomy_hash=corpus_mod.hashlib.sha256(
            corpus_mod.json.dumps(sorted(market_names), separators=(",", ":")).encode("utf-8"),
        ).hexdigest()[:24],
        source_refs=tuple(source_refs),
    )
    ingestor._persist_normalized_records(normalized_records, manifest.manifest_id)
    ingestor._store.save_manifest(manifest)
    return manifest


async def run_variant(
    concurrency: int,
) -> tuple[RuleCorpusManifest, InMemoryCache, FakeCloudbetClient, float]:
    ingestor, cache = make_ingestor()
    client = FakeCloudbetClient(SPORT_KEYS, latency=REQUEST_LATENCY_SECONDS)
    start = time.perf_counter()
    manifest = await refresh_cloudbet_concurrent(
        ingestor,
        client,
        sports=SPORT_KEYS,
        from_timestamp=FROM_TS,
        to_timestamp=TO_TS,
        limit=20,
        max_window_seconds=7 * 24 * 60 * 60,
        concurrency=concurrency,
        fetched_at=FIXED_FETCHED_AT,
    )
    elapsed = time.perf_counter() - start
    return manifest, cache, client, elapsed


def manifest_fingerprint(manifest: RuleCorpusManifest, cache: InMemoryCache) -> dict[str, Any]:
    return {
        "manifest": dc_asdict(manifest),
        "cache_keys_sorted": sorted(cache._data.keys()),
        "cache_key_count": len(cache._data),
    }


async def main(out: str | None) -> None:
    corpus_mod._utc_now = (
        lambda: FIXED_FETCHED_AT
    )  # freeze time for both sides (see module docstring)

    baseline_times = []
    baseline_manifest = None
    baseline_cache = None
    baseline_calls = None
    for _ in range(REPEATS):
        manifest, cache, client, elapsed = await run_baseline()
        baseline_times.append(elapsed)
        baseline_manifest, baseline_cache, baseline_calls = manifest, cache, client.call_count

    assert baseline_manifest is not None
    assert baseline_cache is not None
    baseline_fp = manifest_fingerprint(baseline_manifest, baseline_cache)

    results: dict[str, Any] = {
        "experiment": "corpus-fetch-concurrency",
        "request_latency_seconds": REQUEST_LATENCY_SECONDS,
        "sport_count": SPORT_COUNT,
        "repeats": REPEATS,
        "baseline": {
            "description": "real SnapshotIngestor.refresh_cloudbet, fetch_concurrency=1 (sequential)",
            "wall_times_seconds": baseline_times,
            "median_seconds": statistics.median(baseline_times),
            "stdev_seconds": statistics.stdev(baseline_times) if len(baseline_times) > 1 else 0.0,
            "requests_per_run": baseline_calls,
        },
        "variants": {},
    }

    correctness_ok = True
    for concurrency in CONCURRENCY_LEVELS:
        variant_times = []
        variant_manifest = None
        variant_cache = None
        variant_client = None
        for _ in range(REPEATS):
            manifest, cache, client, elapsed = await run_variant(concurrency)
            variant_times.append(elapsed)
            variant_manifest, variant_cache, variant_client = manifest, cache, client

        assert variant_manifest is not None
        assert variant_cache is not None
        assert variant_client is not None
        variant_fp = manifest_fingerprint(variant_manifest, variant_cache)
        identical = variant_fp == baseline_fp
        correctness_ok = correctness_ok and identical
        median = statistics.median(variant_times)
        baseline_median = results["baseline"]["median_seconds"]
        reduction_pct = 100.0 * (1.0 - median / baseline_median)

        results["variants"][str(concurrency)] = {
            "description": f"mirrored per-sport worker, asyncio.gather + Semaphore({concurrency})",
            "wall_times_seconds": variant_times,
            "median_seconds": median,
            "stdev_seconds": statistics.stdev(variant_times) if len(variant_times) > 1 else 0.0,
            "requests_per_run": variant_client.call_count,
            "peak_in_flight_last_run": variant_client.peak_in_flight,
            "bounded_by_n": variant_client.peak_in_flight <= concurrency,
            "wall_time_reduction_pct_vs_baseline": reduction_pct,
            "manifest_identical_to_baseline": identical,
            "meets_50pct_threshold": reduction_pct >= 50.0,
        }

    results["correctness_all_variants_identical"] = correctness_ok
    results["verdict"] = (
        "PASS"
        if correctness_ok and any(v["meets_50pct_threshold"] for v in results["variants"].values())
        else "FAIL"
    )

    print(json.dumps(results, indent=2, default=str))
    if out:
        Path(out).write_text(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="Optional path for the JSON results artifact")
    asyncio.run(main(parser.parse_args().out))
