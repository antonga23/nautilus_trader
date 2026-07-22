"""
Experiment: cloudbet-line-fallback-fanout

GOAL
----
Cut CLOUDBET poll requests/cycle. Live evidence (providerQuotePollStats.CLOUDBET
in a real multivenue node probe): subscribed_instrument_count=120,
event_request_count=8, line_request_count=56, request_count=64,
cycle_elapsed_secs=6.385 vs poll_target_cycle_secs=4.0, quote_count=64.

GROUNDING (nautilus_trader/adapters/cloudbet/data_client.py, read in full before
writing this script)
--------------------------------------------------------------------------------
* `_fetch_quote_ticks_event_batched` groups subscribed instruments by
  `instrument.event_id` (`_group_quote_instruments_by_event`) and issues ONE
  `self._client.get_event(event_id)` request per distinct event
  (`_fetch_quote_ticks_for_event_group`) -- this is the `event_request_count`.
* For each instrument in a group, `_quote_tick_from_event` looks up
  `event.markets.get(str(instrument.market_name))`. If that key is entirely
  absent from the event payload, OR present but no submarket selection matches
  `(outcome, params)`, OR the matched selection's price is <=0, the method
  returns None. Every instrument whose quote is None this way lands in
  `unresolved_ids` and is re-fetched ONE-BY-ONE via
  `_fetch_quote_ticks_individual` -> `_fetch_quote_tick` ->
  `self._client.get_latest_odds(...)`. This per-instrument re-fetch is the
  `line_request_count` / "line fallback fanout".
* Nothing in this path ever records WHY a market failed to resolve, and
  nothing carries that information forward to the next cycle -- an instrument
  whose market is structurally absent from the event schema (not merely
  suspended) will fail the SAME way, forever, on every single poll cycle,
  paying a full individual-request round trip for zero information gain
  every time.
* The live numbers are consistent with exactly this: quote_count (64) equals
  event_request_count + line_request_count minus... more precisely,
  quote_count == request_count == 64 while line_request_count == 56. Read
  literally: 64 instruments resolved directly off the event batch (no
  fallback, all published), and the 56 that fell back to individual fetches
  published NOTHING extra (quote_count did not exceed the batch-resolved
  count). I.e. the live node is paying 56 full extra HTTP round trips per
  cycle that recover zero additional quotes -- structurally-missing markets,
  not transiently-suspended ones.

HYPOTHESIS / FIX MODELLED HERE
-------------------------------
Cache, per (event_id, instrument_id), whether the market KEY was found absent
from `event.markets` (a structural / schema-level fact -- if the market isn't
offered for this fixture at all, per-line fallback for the exact same
market/outcome/params cannot find it either, since it queries the identical
market path). On the NEXT cycle, skip the network round trip for
already-confirmed-absent instruments and simply record quote=None, while
still doing the real event fetch (needed for the OTHER, resolvable
instruments in the same event group) and still doing REAL per-instrument
fallback for anything not yet confirmed absent (first sighting, or found
present-but-selection-missing / suspended, which IS worth re-checking every
cycle since it can flip back to trading). This is deliberately conservative:
we do not skip anything for merely-suspended markets (only for markets whose
key is not present in `event.markets` at all), and we revalidate a
"confirmed absent" market on the pollability registry's time-based schedule
(quote_poll_unpollable_revalidate_secs) in case the market later gets added
to the event schema (e.g. a prop market opening closer to kickoff).

HARD RULES
----------
* Self-contained: variant = the real `CloudbetDataClient`, which now tracks
  known-absent markets in its `MarketPollabilityRegistry`. Baseline = a
  subclass that clears `_market_pollability` before every event-group fetch,
  reproducing the pre-cache behaviour (every unresolved instrument pays a
  real per-line fallback request, every cycle). No installed source is
  edited.
* No live network calls -- `CloudbetClient.get_event` / `get_latest_odds` are
  in-process AsyncMocks with an injected 100ms latency per request.
* >=5 repeats, median + variance reported for wall-clock; request/quote
  counts are deterministic given the fixed mock corpus (repeats confirm this
  determinism holds, in addition to timing variance).
* Verdict requires identical published QuoteTick sets between baseline and
  variant across all cycles (never a materially different set of ticks),
  plus meeting the request-count / wall-time thresholds.
"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

# Ensure the repo root (which owns the real `tests` package providing
# TESTS_PACKAGE_ROOT, imported transitively by nautilus_trader.test_kit) wins
# over any same-named `tests` package installed in site-packages.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
sys.path = [p for p in sys.path if p != _REPO_ROOT]
sys.path.insert(0, _REPO_ROOT)
sys.modules.pop("tests", None)  # drop any wrong same-named package already cached

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.adapters.cloudbet.client.schema import (
    CompetitionWithCategory,
    EventStatus,
    GetEventResponse,
    Identifier,
    MarketModel,
    SelectionModel,
    SelectionSide,
    SelectionStatus,
    SubmarketModel,
    TeamIdentifier,
)
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.config import CloudbetDataClientConfig
from nautilus_trader.adapters.cloudbet.data_client import CloudbetDataClient
from nautilus_trader.adapters.betting.runtime_cache import decode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key
from nautilus_trader.common.component import Logger
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


ARTIFACT_PATH = Path("exp4_cloudbet-line-fallback-fanout.json")

# --- Corpus sized to mirror the live probe exactly ---------------------------
NUM_EVENTS = 8  # matches live event_request_count == 8
INSTRUMENTS_PER_EVENT = 15  # 8 events * 15 == 120 == live subscribed_instrument_count
PRESENT_PER_EVENT = 8  # resolves off the event batch -> published
ABSENT_PER_EVENT = INSTRUMENTS_PER_EVENT - PRESENT_PER_EVENT  # 7
# 8 events * 7 absent == 56 == live line_request_count
# 8 events * 8 present == 64 == live quote_count == live request_count

REQUEST_LATENCY_SECS = 0.100  # injected per-request latency (event fetch AND line fallback)
NUM_CYCLES = 6  # 1 cold cycle (must match baseline exactly) + 5 steady-state
REPEATS = 5


def _make_instrument(*, event_id: int, market_name: str, present: bool) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=CLOUDBET_VENUE,
        home_name="Home",
        away_name="Away",
        sport_name="soccer",
        competition_name="Test League",
        price=2.0,
        currency=USD,
        event_name=f"Home vs Away {event_id}",
        market_name=market_name,
        live=False,
        enabled=True,
        outcome="home" if present else "special_outcome",
        side=SelectionSide.BACK,
        params="",
        market_type=f"{market_name}_period=ft",
        event_id=event_id,
    )


def _build_corpus() -> tuple[list[CryptoBettingInstrument], dict[int, GetEventResponse]]:
    """
    Build NUM_EVENTS events.

    Each event has PRESENT_PER_EVENT instruments whose market_name IS a key in
    event.markets (with a matching selection -> quote resolves straight off the event
    batch), and ABSENT_PER_EVENT instruments whose market_name is a key NEVER present in
    that event's markets dict at all (structurally missing market for this fixture --
    e.g. a prop market not offered for this particular game).

    """
    instruments: list[CryptoBettingInstrument] = []
    events: dict[int, GetEventResponse] = {}

    for event_idx in range(NUM_EVENTS):
        event_id = 1000 + event_idx
        markets: dict[str, MarketModel] = {}

        for p in range(PRESENT_PER_EVENT):
            market_name = f"present_market_{event_idx}_{p}"
            instrument = _make_instrument(event_id=event_id, market_name=market_name, present=True)
            instruments.append(instrument)
            markets[market_name] = MarketModel(
                submarkets={
                    "period=ft": SubmarketModel(
                        sequence="1",
                        selections=[
                            SelectionModel(
                                outcome=instrument.outcome,
                                params=instrument.params,
                                price=1.85,
                                minStake=1,
                                maxStake=250,
                                probability=0.54,
                                status=SelectionStatus.ENABLED.value,
                                side=SelectionSide.BACK.value,
                            ),
                        ],
                    ),
                },
            )

        for a in range(ABSENT_PER_EVENT):
            market_name = f"absent_market_{event_idx}_{a}"
            instrument = _make_instrument(event_id=event_id, market_name=market_name, present=False)
            instruments.append(instrument)
            # Deliberately NOT added to `markets` -- structurally absent from
            # this event's schema.

        events[event_id] = GetEventResponse(
            sequence="1",
            id=event_id,
            sport=Identifier(name="soccer", key="soccer"),
            competition=CompetitionWithCategory(
                category=Identifier(name="category", key="category"),
                key="competition",
                name="Test League",
            ),
            home=TeamIdentifier(abbreviation="H", key="home", name="Home", nationality=""),
            away=TeamIdentifier(abbreviation="A", key="away", name="Away", nationality=""),
            status=EventStatus.TRADING,
            markets=markets,
            name=f"Home vs Away {event_idx}",
            key="event",
            cutoff_time="2026-05-07T12:00:00Z",
            type="EVENT",
            end_time="2026-05-07T14:00:00Z",
            grading_duration=None,
        )

    assert len(instruments) == NUM_EVENTS * INSTRUMENTS_PER_EVENT == 120
    return instruments, events


class _FakeInstrumentProvider(InstrumentProvider):
    def __init__(self, instruments: list[CryptoBettingInstrument]):
        super().__init__()
        self._by_id = {i.id: i for i in instruments}

    def find(self, instrument_id: InstrumentId):
        return self._by_id.get(instrument_id)

    def list_all(self):
        return list(self._by_id.values())


def _build_mock_client(events: dict[int, GetEventResponse], call_log: list[tuple[str, str]]):
    client = AsyncMock(spec=CloudbetClient)

    async def fake_get_event(event_id: int):
        call_log.append(("event", str(event_id)))
        await asyncio.sleep(REQUEST_LATENCY_SECS)
        return events[event_id]

    async def fake_get_latest_odds(*, event_id, market_url: str):
        call_log.append(("line", f"{event_id}:{market_url}"))
        await asyncio.sleep(REQUEST_LATENCY_SECS)
        if "absent_market" in market_url:
            # Genuinely 404s -- the market does not exist for this event, so
            # the per-line endpoint (same market/outcome/params specificity
            # as the event-batch lookup) finds nothing either.
            raise CloudbetAPIError("market not found", code="404")
        raise AssertionError(
            f"unexpected individual fallback for {market_url}: should have resolved via event batch",
        )

    client.get_event = AsyncMock(side_effect=fake_get_event)
    client.get_latest_odds = AsyncMock(side_effect=fake_get_latest_odds)
    return client


def _build_data_client(client_cls, *, instruments, events, call_log):
    client = _build_mock_client(events, call_log)
    provider = _FakeInstrumentProvider(instruments)
    data_client = client_cls(
        loop=asyncio.get_event_loop(),
        client=client,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-cloudbet-fallback-fanout"),
        market_filter={},
        instrument_provider=provider,
        config=CloudbetDataClientConfig(
            quote_poll_concurrency=16,
            quote_poll_min_concurrency=16,
            quote_poll_max_concurrency=16,
            quote_poll_adaptive_concurrency=False,  # keep concurrency fixed/deterministic
            quote_poll_event_batching=True,
            quote_poll_missing_prune_threshold=10_000,  # never prune mid-experiment
        ),
    )
    data_client._subscribed_quote_instruments = {i.id for i in instruments}
    data_client._requested_quote_instruments = {
        i.id for i in instruments
    }  # never pruned, like live
    data_client._test_published = []
    data_client._handle_data = lambda quote: data_client._test_published.append(quote.instrument_id)
    return data_client


class NoCacheCloudbetDataClient(CloudbetDataClient):
    """
    Baseline: reproduces the pre-cache behaviour by clearing the known-absent
    cache before every event-group fetch, so every unresolved instrument pays
    a real per-line fallback request on every cycle.
    """

    async def _fetch_quote_ticks_for_event_group(self, event_id, group_instrument_ids, semaphore):
        self._market_pollability.clear()
        return await super()._fetch_quote_ticks_for_event_group(
            event_id,
            group_instrument_ids,
            semaphore,
        )


async def _run_cycles(client_cls, *, instruments, events) -> dict:
    call_log: list[tuple[str, str]] = []
    data_client = _build_data_client(
        client_cls,
        instruments=instruments,
        events=events,
        call_log=call_log,
    )

    cycle_stats = []
    published_per_cycle = []
    for _ in range(NUM_CYCLES):
        data_client._test_published = []
        start = time.perf_counter()
        publish_count, requested = await data_client._poll_quote_ticks_once()
        elapsed = time.perf_counter() - start

        stats_encoded = data_client._cache.get(venue_quote_poll_stats_key("CLOUDBET"))
        stats = decode_venue_quote_poll_stats(stats_encoded)
        assert stats is not None
        cycle_stats.append(
            {
                "requested": requested,
                "published": publish_count,
                "request_count": stats.request_count,
                "event_request_count": stats.event_request_count,
                "line_request_count": stats.line_request_count,
                "cycle_elapsed_secs": elapsed,
            },
        )
        published_per_cycle.append(sorted(str(x) for x in data_client._test_published))

    return {"cycle_stats": cycle_stats, "published_per_cycle": published_per_cycle}


async def run_repeat(client_cls) -> dict:
    instruments, events = _build_corpus()
    return await _run_cycles(client_cls, instruments=instruments, events=events)


async def main() -> None:
    print(
        f"Corpus: {NUM_EVENTS} events x {INSTRUMENTS_PER_EVENT} instruments "
        f"({PRESENT_PER_EVENT} present / {ABSENT_PER_EVENT} absent per event) "
        f"= {NUM_EVENTS * INSTRUMENTS_PER_EVENT} total, matching live "
        f"subscribed_instrument_count=120, event_request_count=8, line_request_count=56.",
    )

    print("\n=== BASELINE: NoCacheCloudbetDataClient (pre-cache behaviour) ===")
    baseline_runs = [await run_repeat(NoCacheCloudbetDataClient) for _ in range(REPEATS)]
    for i, r in enumerate(baseline_runs):
        c1 = r["cycle_stats"][0]
        c_last = r["cycle_stats"][-1]
        print(f"  run {i}: cycle1={c1}  cycleN={c_last}")

    print("\n=== VARIANT: real CloudbetDataClient (skips confirmed-absent fallback) ===")
    variant_runs = [await run_repeat(CloudbetDataClient) for _ in range(REPEATS)]
    for i, r in enumerate(variant_runs):
        c1 = r["cycle_stats"][0]
        c_last = r["cycle_stats"][-1]
        print(f"  run {i}: cycle1={c1}  cycleN={c_last}")

    # --- Correctness: identical published tick sets, cycle-for-cycle -------
    correctness_ok = True
    correctness_detail = []
    for run_idx in range(REPEATS):
        b_pub = baseline_runs[run_idx]["published_per_cycle"]
        v_pub = variant_runs[run_idx]["published_per_cycle"]
        for cycle_idx in range(NUM_CYCLES):
            same = b_pub[cycle_idx] == v_pub[cycle_idx]
            correctness_detail.append(
                {
                    "run": run_idx,
                    "cycle": cycle_idx,
                    "identical_published_set": same,
                    "baseline_published_count": len(b_pub[cycle_idx]),
                    "variant_published_count": len(v_pub[cycle_idx]),
                },
            )
            if not same:
                correctness_ok = False

    # --- Requests/cycle + wall-time: cold cycle (index 0) vs steady state --
    def _agg(runs, key, cycle_idx):
        return [r["cycle_stats"][cycle_idx][key] for r in runs]

    baseline_cold_requests = _agg(baseline_runs, "request_count", 0)
    variant_cold_requests = _agg(variant_runs, "request_count", 0)

    baseline_steady_requests = [
        statistics.median([r["cycle_stats"][c]["request_count"] for c in range(1, NUM_CYCLES)])
        for r in baseline_runs
    ]
    variant_steady_requests = [
        statistics.median([r["cycle_stats"][c]["request_count"] for c in range(1, NUM_CYCLES)])
        for r in variant_runs
    ]

    baseline_steady_elapsed = [
        statistics.median([r["cycle_stats"][c]["cycle_elapsed_secs"] for c in range(1, NUM_CYCLES)])
        for r in baseline_runs
    ]
    variant_steady_elapsed = [
        statistics.median([r["cycle_stats"][c]["cycle_elapsed_secs"] for c in range(1, NUM_CYCLES)])
        for r in variant_runs
    ]

    baseline_steady_requests_median = statistics.median(baseline_steady_requests)
    variant_steady_requests_median = statistics.median(variant_steady_requests)
    request_reduction_pct = (
        100.0
        * (baseline_steady_requests_median - variant_steady_requests_median)
        / baseline_steady_requests_median
    )

    baseline_steady_elapsed_median = statistics.median(baseline_steady_elapsed)
    variant_steady_elapsed_median = statistics.median(variant_steady_elapsed)

    print("\n=== CORRECTNESS ===")
    all_ok = correctness_ok
    print(
        f"  identical published QuoteTick sets across all {REPEATS} runs x {NUM_CYCLES} cycles: {all_ok}",
    )
    for d in correctness_detail:
        if not d["identical_published_set"]:
            print(f"    MISMATCH: {d}")

    print(
        "\n=== COLD CYCLE (cycle 0) -- must match baseline exactly (first sighting, no cache yet) ===",
    )
    print(f"  baseline request_count: {baseline_cold_requests}")
    print(f"  variant  request_count: {variant_cold_requests}")
    cold_cycle_matches = baseline_cold_requests == variant_cold_requests

    print("\n=== STEADY STATE (cycles 1..N-1, median per run) ===")
    print(
        f"  baseline request_count per run: {baseline_steady_requests} -> median {baseline_steady_requests_median}",
    )
    print(
        f"  variant  request_count per run: {variant_steady_requests} -> median {variant_steady_requests_median}",
    )
    print(f"  request reduction: {request_reduction_pct:.1f}%")
    print(
        f"  baseline cycle_elapsed_secs per run (median over cycles): {baseline_steady_elapsed} -> median {baseline_steady_elapsed_median:.4f}s, stdev {statistics.stdev(baseline_steady_elapsed):.4f}s",
    )
    print(
        f"  variant  cycle_elapsed_secs per run (median over cycles): {variant_steady_elapsed} -> median {variant_steady_elapsed_median:.4f}s, stdev {statistics.stdev(variant_steady_elapsed):.4f}s",
    )

    threshold_requests_met = request_reduction_pct >= 40.0
    threshold_latency_met = variant_steady_elapsed_median <= baseline_steady_elapsed_median
    verdict = (
        "PASS"
        if (
            correctness_ok
            and cold_cycle_matches
            and threshold_requests_met
            and threshold_latency_met
        )
        else "FAIL"
    )

    print(f"\n=== VERDICT: {verdict} ===")
    print(
        f"  correctness_ok={correctness_ok} cold_cycle_matches={cold_cycle_matches} "
        f"threshold_requests_met={threshold_requests_met} (>=40%) "
        f"threshold_latency_met={threshold_latency_met}",
    )

    artifact = {
        "slug": "cloudbet-line-fallback-fanout",
        "verdict": verdict,
        "live_evidence": {
            "subscribed_instrument_count": 120,
            "event_request_count": 8,
            "line_request_count": 56,
            "request_count": 64,
            "quote_count": 64,
            "cycle_elapsed_secs": 6.385405,
            "poll_target_cycle_secs": 4.0,
        },
        "corpus": {
            "num_events": NUM_EVENTS,
            "instruments_per_event": INSTRUMENTS_PER_EVENT,
            "present_per_event": PRESENT_PER_EVENT,
            "absent_per_event": ABSENT_PER_EVENT,
            "request_latency_secs": REQUEST_LATENCY_SECS,
            "num_cycles": NUM_CYCLES,
            "unpollable_revalidate_secs": 600.0,
            "repeats": REPEATS,
        },
        "correctness": {
            "identical_published_sets_all_runs": correctness_ok,
            "detail": correctness_detail,
        },
        "cold_cycle": {
            "baseline_request_count": baseline_cold_requests,
            "variant_request_count": variant_cold_requests,
            "matches": cold_cycle_matches,
        },
        "steady_state": {
            "baseline_request_count_per_run": baseline_steady_requests,
            "variant_request_count_per_run": variant_steady_requests,
            "baseline_request_count_median": baseline_steady_requests_median,
            "variant_request_count_median": variant_steady_requests_median,
            "request_reduction_pct": request_reduction_pct,
            "baseline_cycle_elapsed_secs_per_run": baseline_steady_elapsed,
            "variant_cycle_elapsed_secs_per_run": variant_steady_elapsed,
            "baseline_cycle_elapsed_secs_median": baseline_steady_elapsed_median,
            "variant_cycle_elapsed_secs_median": variant_steady_elapsed_median,
            "baseline_cycle_elapsed_secs_stdev": statistics.stdev(baseline_steady_elapsed),
            "variant_cycle_elapsed_secs_stdev": statistics.stdev(variant_steady_elapsed),
        },
        "full_baseline_runs": baseline_runs,
        "full_variant_runs": variant_runs,
    }
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"\nWrote artifact to {ARTIFACT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
