"""
Experiment: sxbet-cycle-watchdog

GOAL
----
Bound SXBET poll cycles. Live evidence: ONE poll cycle ran 35.5h with a
max_fetch of 68,475s (~19h) inside it -- a single hung await wedged the
poller for the node's entire lifetime.

GROUNDING (read nautilus_trader/adapters/sxbet/data.py and http_client.py)
---------------------------------------------------------------------------
* http_client.py DOES set a 30s aiohttp.ClientTimeout(total=...) per request
  (see SXBetHttpClient.__init__ / connect / _request). So there is a nominal
  per-request timeout -- but it lives ENTIRELY inside aiohttp's own
  bookkeeping, with no independent backstop above it. The live incident
  (68,475s stall against a supposed 30s ClientTimeout) is direct proof that
  relying on that single mechanism is not sufficient in practice (aiohttp
  total timeouts are known to not reliably fire across connector-pool
  starvation / stuck-socket edge cases). There is no defense-in-depth.
* data.py's poll-cycle orchestration has ZERO timeout of its own:
    - `_poll_order_books_once` awaits `_fetch_order_book_results` /
      `_fetch_best_odds_batch_results` with no `asyncio.wait_for`.
    - Both of those call `asyncio.gather(*[_fetch(m) for m in markets])`
      with no timeout -- `gather` does not return until EVERY child
      coroutine finishes, so one hung market wedges the whole batch.
    - `_poll_order_books` (the outer `while self._running` loop) awaits
      `await self._poll_order_books_once()` with no timeout either, so a
      wedged cycle also blocks every subsequent cycle forever (matches the
      "one cycle ran 35.5h" symptom: the node never got to a second cycle).

This experiment injects a hang directly at the `self._http_client` call
boundary in data.py (a mocked async client whose `get_order_book` blocks on
an `asyncio.Event` that is never set for one market, and resolves in ~50ms
for all others). This is the fair simulation of "the sole existing defense
(aiohttp's internal timeout) did not fire", which is exactly what production
observed. We then measure whether the REAL, unmodified `_poll_order_books_once`
/ `_poll_order_books` methods on `SXBetDataClient` ever complete, and compare
against a variant subclass that adds an independent per-request
`asyncio.wait_for` plus a hard cycle-level deadline around the SAME
`_fetch_and_publish_quote_stats` logic (reused unmodified).

HARD RULES
----------
* Self-contained: baseline = real unmodified SXBetDataClient methods.
  variant = a subclass that only overrides the batch-dispatch methods to add
  wait_for/deadline wrapping around the same unmodified per-market fetch
  logic. No installed source is edited.
* No live network calls -- http client is an in-process AsyncMock.
* Deterministic failure-injection: correctness is binary (wedged vs bounded),
  so we still repeat n=5 for baseline-wedge confirmation and n=5 for
  variant-bound confirmation, and report variance on the variant's cycle
  elapsed times.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock


# Ensure the repo root (which owns the real `tests` package providing
# TESTS_PACKAGE_ROOT, imported transitively by nautilus_trader.test_kit) wins
# over any same-named `tests` package installed in site-packages.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
sys.path = [p for p in sys.path if p != _REPO_ROOT]
sys.path.insert(0, _REPO_ROOT)
sys.modules.pop("tests", None)  # drop any wrong same-named package already cached

from nautilus_trader.adapters.betting.common.enums import SelectionSide  # noqa: E402
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument  # noqa: E402
from nautilus_trader.adapters.betting.runtime_cache import decode_venue_quote_poll_stats  # noqa: E402
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key  # noqa: E402
from nautilus_trader.adapters.sxbet.config import SXBetDataClientConfig  # noqa: E402
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig  # noqa: E402
from nautilus_trader.adapters.sxbet.data import SXBetDataClient  # noqa: E402
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider  # noqa: E402
from nautilus_trader.adapters.sxbet.signing import decimal_odds_to_percentage  # noqa: E402
from nautilus_trader.common.component import Logger  # noqa: E402
from nautilus_trader.common.functions import get_event_loop  # noqa: E402
from nautilus_trader.model.identifiers import Venue  # noqa: E402
from nautilus_trader.model.objects import Currency  # noqa: E402
from nautilus_trader.test_kit.stubs.component import TestComponentStubs  # noqa: E402


ARTIFACT_PATH = Path(
    os.environ.get("EXP_ARTIFACT_PATH", "exp4_sxbet-cycle-watchdog.json"),
)

# Test-speed constants (scaled down from production seconds -> fractions of a
# second so the suite runs in well under a minute; ratios to `target` are what
# the success threshold cares about, not absolute magnitude).
TARGET_CYCLE_SECS = 1.0  # stand-in for order_book_poll_interval_secs
HEALTHY_LATENCY_SECS = 0.05  # every non-hung market responds this fast
NUM_MARKETS = 20
HUNG_MARKET_INDEX = 7  # arbitrary market in the middle of the batch
BASELINE_OBSERVATION_WINDOW_SECS = 5.0  # generous external test-harness bound
# (production has NO such bound)
FETCH_TIMEOUT_SECS = 0.3  # variant: independent per-request wait_for
CYCLE_DEADLINE_SECS = 2 * TARGET_CYCLE_SECS  # variant: hard cycle backstop
REPEATS = 5


def _make_instrument(*, market_hash: str) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue("SXBET"),
        event_id=f"fixture-{market_hash}",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name="Match Odds",
        market_type="match_odds",
        outcome="home",
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDT"),
        market_id=market_hash,
        info={"outcome_one": True},
    )


def _make_provider(instruments: list[CryptoBettingInstrument]) -> SXBetInstrumentProvider:
    provider = SXBetInstrumentProvider(
        http_client=Mock(),
        config=SXBetInstrumentProviderConfig(),
        logger=Mock(),
    )
    by_id = {instrument.id: instrument for instrument in instruments}
    by_hash: dict[str, list[CryptoBettingInstrument]] = {}
    for instrument in instruments:
        by_hash.setdefault(instrument.market_id, []).append(instrument)  # type: ignore[arg-type]
    provider.find = Mock(side_effect=by_id.get)  # type: ignore[method-assign]
    provider.find_by_market_hash = Mock(side_effect=lambda h: by_hash.get(h, []))  # type: ignore[method-assign]
    return provider


def _order_book_payload(market_hash: str) -> dict:
    return {
        "data": {
            "orders": [
                {
                    "isMakerBettingOutcomeOne": True,
                    "percentageOdds": decimal_odds_to_percentage(2.0),
                },
            ],
        },
    }


def _build_client(client_cls, *, market_hashes: list[str], hung_hash: str, calls: list[str]):
    instruments = [_make_instrument(market_hash=h) for h in market_hashes]
    provider = _make_provider(instruments)

    hang_event = asyncio.Event()  # never set -> awaiting it hangs forever

    async def fake_get_order_book(market_hash: str):
        calls.append(market_hash)
        if market_hash == hung_hash:
            await hang_event.wait()
            raise AssertionError("unreachable: hang_event is never set")
        await asyncio.sleep(HEALTHY_LATENCY_SECS)
        return _order_book_payload(market_hash)

    http_client = Mock()
    http_client.get_order_book = AsyncMock(side_effect=fake_get_order_book)

    client = client_cls(
        loop=get_event_loop(),
        http_client=http_client,
        instrument_provider=provider,
        msgbus=TestComponentStubs.msgbus(),
        cache=TestComponentStubs.cache(),
        clock=TestComponentStubs.clock(),
        logger=Logger(name="test-sxbet-watchdog"),
        config=SXBetDataClientConfig(
            order_book_concurrency=4,
            order_book_poll_interval_secs=TARGET_CYCLE_SECS,
        ),
    )
    client._subscribed_instruments = {instrument.id for instrument in instruments}
    client._handle_data = Mock()
    return client


class PatchedSXBetDataClient(SXBetDataClient):
    """
    Variant: adds an independent per-request timeout + a hard cycle-level
    deadline around the SAME unmodified `_fetch_and_publish_quote_stats`
    logic. This is the in-benchmark wrapper described in the experiment
    plan -- it does not reimplement the fetch/publish logic, only bounds it.
    """

    _fetch_timeout_secs = FETCH_TIMEOUT_SECS
    _cycle_deadline_secs = CYCLE_DEADLINE_SECS

    async def _fetch_order_book_results(self, market_hashes, *, cycle_started_ns=None):
        semaphore = asyncio.Semaphore(max(1, self._order_book_concurrency))

        async def _bounded_fetch(market_hash: str):
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self._fetch_and_publish_quote_stats(
                            market_hash,
                            quote_event_ns=cycle_started_ns,
                        ),
                        timeout=self._fetch_timeout_secs,
                    )
                except TimeoutError:
                    return (
                        0,  # published
                        0,  # orders
                        False,  # has_outcome_one
                        False,  # has_outcome_two
                        self._fetch_timeout_secs,  # elapsed
                        True,  # failed
                        False,  # rate_limited
                        f"request_timeout>{self._fetch_timeout_secs}s",  # error
                    )

        tasks = [asyncio.ensure_future(_bounded_fetch(h)) for h in sorted(market_hashes)]
        done, pending = await asyncio.wait(tasks, timeout=self._cycle_deadline_secs)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        results = []
        for task in tasks:
            if task in done:
                results.append(task.result())
            else:
                results.append(
                    (
                        0,
                        0,
                        False,
                        False,
                        self._cycle_deadline_secs,
                        True,
                        False,
                        "cycle_deadline_exceeded",
                    ),
                )
        return results


async def run_baseline_single_cycle() -> dict:
    calls: list[str] = []
    market_hashes = [f"market-{i}" for i in range(NUM_MARKETS)]
    hung_hash = market_hashes[HUNG_MARKET_INDEX]
    client = _build_client(
        SXBetDataClient,
        market_hashes=market_hashes,
        hung_hash=hung_hash,
        calls=calls,
    )

    start = time.perf_counter()
    completed = True
    try:
        await asyncio.wait_for(
            client._poll_order_books_once(),
            timeout=BASELINE_OBSERVATION_WINDOW_SECS,
        )
    except TimeoutError:
        completed = False
    elapsed = time.perf_counter() - start

    stats_encoded = client._cache.get(venue_quote_poll_stats_key("SXBET"))
    stats_recorded = stats_encoded is not None

    return {
        "completed_within_window": completed,
        "observation_window_secs": BASELINE_OBSERVATION_WINDOW_SECS,
        "elapsed_secs": elapsed,
        "markets_contacted": len(set(calls)),
        "num_markets": NUM_MARKETS,
        "stats_recorded": stats_recorded,
    }


async def run_baseline_outer_loop() -> dict:
    """
    Confirm the outer polling loop never reaches a second cycle either.

    Note: `_poll_order_books` internally does
    `except asyncio.CancelledError: break`, so if we drove this via
    `asyncio.wait_for(...)` the timeout's own cancellation would be
    swallowed by that handler and the coroutine would return *normally*,
    making the loop look "completed" -- an artifact of the test harness
    cancelling it, not of the loop finishing a cycle on its own. Production
    has no such external canceller for a wedged task, so the honest
    measurement is: spawn the loop as a background task, let wall-clock time
    pass with NOTHING cancelling it, and check it is still pending (never
    voluntarily returned) and never incremented past cycle 0.

    """
    calls: list[str] = []
    market_hashes = [f"market-{i}" for i in range(NUM_MARKETS)]
    hung_hash = market_hashes[HUNG_MARKET_INDEX]
    client = _build_client(
        SXBetDataClient,
        market_hashes=market_hashes,
        hung_hash=hung_hash,
        calls=calls,
    )
    client._running = True

    start = time.perf_counter()
    task = asyncio.ensure_future(client._poll_order_books())
    await asyncio.sleep(BASELINE_OBSERVATION_WINDOW_SECS)
    elapsed = time.perf_counter() - start
    still_wedged = not task.done()
    cycle_id_reached = client._quote_poll_cycle_id

    # Clean up only now, after observing the wedge -- production never does this.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    return {
        "loop_ever_returned": not still_wedged,
        "observation_window_secs": BASELINE_OBSERVATION_WINDOW_SECS,
        "elapsed_secs": elapsed,
        "cycle_id_reached": cycle_id_reached,  # 0 => never finished cycle 1
    }


async def run_variant_single_cycle() -> dict:
    calls: list[str] = []
    market_hashes = [f"market-{i}" for i in range(NUM_MARKETS)]
    hung_hash = market_hashes[HUNG_MARKET_INDEX]
    client = _build_client(
        PatchedSXBetDataClient,
        market_hashes=market_hashes,
        hung_hash=hung_hash,
        calls=calls,
    )

    start = time.perf_counter()
    # Outer safety bound only to protect the *test harness* against a script
    # bug; it is far above CYCLE_DEADLINE_SECS and should never itself fire.
    await asyncio.wait_for(client._poll_order_books_once(), timeout=CYCLE_DEADLINE_SECS + 5.0)
    elapsed = time.perf_counter() - start

    stats_encoded = client._cache.get(venue_quote_poll_stats_key("SXBET"))
    stats = decode_venue_quote_poll_stats(stats_encoded) if stats_encoded else None

    published_calls = client._handle_data.call_count
    healthy_markets_contacted = len(set(calls) - {hung_hash})

    return {
        "elapsed_secs": elapsed,
        "cycle_deadline_secs": CYCLE_DEADLINE_SECS,
        "target_cycle_secs": TARGET_CYCLE_SECS,
        "bounded_within_2x_target": elapsed <= 2 * TARGET_CYCLE_SECS + 1e-6,
        "hung_market_contacted": hung_hash in calls,
        "healthy_markets_contacted": healthy_markets_contacted,
        "num_healthy_markets": NUM_MARKETS - 1,
        "quotes_published": published_calls,
        "stats_failure_count": stats.failure_count if stats else None,
        "stats_last_error": stats.last_error if stats else None,
        "stats_quote_count": stats.quote_count if stats else None,
    }


async def run_variant_multi_cycle() -> dict:
    """
    Confirm the NEXT cycle starts -- run the real outer loop for a bounded wall-clock
    window and check more than one cycle completed.
    """
    calls: list[str] = []
    market_hashes = [f"market-{i}" for i in range(NUM_MARKETS)]
    hung_hash = market_hashes[HUNG_MARKET_INDEX]
    client = _build_client(
        PatchedSXBetDataClient,
        market_hashes=market_hashes,
        hung_hash=hung_hash,
        calls=calls,
    )
    client._running = True

    window_secs = 4 * TARGET_CYCLE_SECS
    task = asyncio.ensure_future(client._poll_order_books())
    await asyncio.sleep(window_secs)
    client._running = False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    return {
        "window_secs": window_secs,
        "cycles_completed": client._quote_poll_cycle_id,
        "multiple_cycles_ran": client._quote_poll_cycle_id >= 2,
    }


async def main() -> None:
    print("=== BASELINE: single cycle, real unmodified SXBetDataClient ===")
    baseline_single_runs = [await run_baseline_single_cycle() for _ in range(REPEATS)]
    for i, r in enumerate(baseline_single_runs):
        print(f"  run {i}: {r}")
    baseline_all_wedged = all(not r["completed_within_window"] for r in baseline_single_runs)
    baseline_no_stats = all(not r["stats_recorded"] for r in baseline_single_runs)

    print("\n=== BASELINE: outer polling loop, real unmodified SXBetDataClient ===")
    baseline_outer_runs = [await run_baseline_outer_loop() for _ in range(REPEATS)]
    for i, r in enumerate(baseline_outer_runs):
        print(f"  run {i}: {r}")
    baseline_loop_never_returns = all(not r["loop_ever_returned"] for r in baseline_outer_runs)
    baseline_never_reaches_cycle = all(r["cycle_id_reached"] == 0 for r in baseline_outer_runs)

    print("\n=== VARIANT: single cycle, PatchedSXBetDataClient (wait_for + deadline) ===")
    variant_single_runs = [await run_variant_single_cycle() for _ in range(REPEATS)]
    for i, r in enumerate(variant_single_runs):
        print(f"  run {i}: {r}")
    variant_elapsed = [r["elapsed_secs"] for r in variant_single_runs]
    variant_bounded = all(r["bounded_within_2x_target"] for r in variant_single_runs)
    variant_no_loss = all(
        r["healthy_markets_contacted"] == r["num_healthy_markets"] for r in variant_single_runs
    )
    variant_hung_counted = all(
        r["hung_market_contacted"] and (r["stats_failure_count"] or 0) >= 1
        for r in variant_single_runs
    )
    variant_quotes_match = all(
        r["quotes_published"] == r["num_healthy_markets"] for r in variant_single_runs
    )

    print("\n=== VARIANT: multi-cycle (does the NEXT cycle start?) ===")
    variant_multi_runs = [await run_variant_multi_cycle() for _ in range(3)]
    for i, r in enumerate(variant_multi_runs):
        print(f"  run {i}: {r}")
    variant_next_cycle_starts = all(r["multiple_cycles_ran"] for r in variant_multi_runs)

    verdict_pass = (
        baseline_all_wedged
        and baseline_no_stats
        and baseline_loop_never_returns
        and baseline_never_reaches_cycle
        and variant_bounded
        and variant_no_loss
        and variant_hung_counted
        and variant_quotes_match
        and variant_next_cycle_starts
    )

    result = {
        "slug": "sxbet-cycle-watchdog",
        "grounding": {
            "http_client_has_30s_client_timeout": True,
            "data_py_has_independent_timeout_before_fix": False,
            "poll_cycle_uses_bare_asyncio_gather": True,
            "outer_loop_has_no_timeout": True,
        },
        "config": {
            "num_markets": NUM_MARKETS,
            "hung_market_index": HUNG_MARKET_INDEX,
            "target_cycle_secs": TARGET_CYCLE_SECS,
            "healthy_latency_secs": HEALTHY_LATENCY_SECS,
            "baseline_observation_window_secs": BASELINE_OBSERVATION_WINDOW_SECS,
            "variant_fetch_timeout_secs": FETCH_TIMEOUT_SECS,
            "variant_cycle_deadline_secs": CYCLE_DEADLINE_SECS,
            "repeats": REPEATS,
        },
        "baseline": {
            "single_cycle_runs": baseline_single_runs,
            "all_wedged_past_observation_window": baseline_all_wedged,
            "no_stats_ever_recorded": baseline_no_stats,
            "outer_loop_runs": baseline_outer_runs,
            "outer_loop_never_returns": baseline_loop_never_returns,
            "outer_loop_never_reaches_cycle_1_completion": baseline_never_reaches_cycle,
        },
        "variant": {
            "single_cycle_runs": variant_single_runs,
            "elapsed_secs_median": statistics.median(variant_elapsed),
            "elapsed_secs_stdev": statistics.pstdev(variant_elapsed)
            if len(variant_elapsed) > 1
            else 0.0,
            "elapsed_secs_min": min(variant_elapsed),
            "elapsed_secs_max": max(variant_elapsed),
            "bounded_within_2x_target_every_run": variant_bounded,
            "zero_healthy_market_loss": variant_no_loss,
            "hung_market_surfaced_in_failure_counters": variant_hung_counted,
            "quote_counts_match_healthy_markets": variant_quotes_match,
            "multi_cycle_runs": variant_multi_runs,
            "next_cycle_starts": variant_next_cycle_starts,
        },
        "verdict": "PASS" if verdict_pass else "FAIL",
    }

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(result, indent=2, default=str))

    print("\n=== VERDICT ===")
    print(json.dumps({"verdict": result["verdict"]}, indent=2))
    print(f"Artifact written to {ARTIFACT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
