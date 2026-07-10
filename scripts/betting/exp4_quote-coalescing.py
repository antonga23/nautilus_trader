#!/usr/bin/env python3
"""
Experiment: quote-coalescing.

Cuts delivered-quote staleness under consumer backpressure by replacing FIFO
delivery of QuoteTick messages with latest-wins coalescing keyed by
instrument_id, at the exact real integration point in this codebase.

GROUNDING:

  nautilus_trader/live/data_engine.py -- LiveDataEngine is the real live
  consumer of every published Data message (QuoteTick included):

    - ``LiveDataEngine.process(self, data)`` is the msgbus-registered handler
      (``DataClient._handle_data`` in ``data/client.pyx`` does
      ``self._msgbus.send(endpoint="DataEngine.process", msg=data)``). It just
      calls ``self._data_enqueuer.enqueue(data)``.
    - ``ThrottledEnqueuer.enqueue`` (live/enqueue.py) puts the item onto a
      plain ``asyncio.Queue`` (``self._data_queue``, default
      ``LiveDataEngineConfig.qsize = 100_000`` -- effectively unbounded).
    - ``LiveDataEngine._run_data_queue`` is a single coroutine that does
      ``data = await self._data_queue.get(); ... ; self._handle_data(data)``
      one item at a time, FIFO, with no coalescing. ``_handle_data`` is what
      ultimately reaches ``BettingArbitrageStrategy.on_quote_tick``
      (nautilus_trader/examples/strategies/betting_arbitrage.py:1967), which
      is exactly where ``quote_event_to_strategy`` latency is measured
      (``strategy_received_ns - tick.ts_event``, see
      ``_record_quote_receive_latency`` at line 2000).

  A live pilot runtime probe status snapshot (the
  ``latencyDiagnostics`` block produced by
  scripts/betting/runtime_probe_report.py) confirms the production symptom is
  a QUEUEING problem, not a per-tick compute problem: ``graph_scan`` (the
  actual per-tick strategy compute,
  ``_handle_graph_quote_tick``/``update_quote_and_scan_fast``) sits at
  p50=0.015ms, p95=0.041ms -- negligible -- while ``quote_event_to_strategy``
  (queue-to-delivery latency) is p50=4977.9ms, p95=24418.4ms,
  p99=207386.3ms, max=24853413.1ms (6.9 HOURS). Per-tick strategy cost is not
  the bottleneck; FIFO queue backlog when the consumer can't keep up is. Only
  the newest tick per instrument is ever actionable for arbitrage (older
  odds are simply stale-on-arrival), so a FIFO queue that redelivers every
  superseded tick is pure wasted latency budget.

  Adapter side confirms ticks really do arrive in per-cycle bursts that can
  repeatedly enqueue duplicates for the same instrument before the previous
  cycle's tick is even consumed: adapters/sxbet/data.py
  ``_fetch_and_publish_quote_stats`` / ``_fetch_and_publish_best_odds_batch_stats``
  and adapters/cloudbet/data_client.py's poll loop both call
  ``self._handle_data(quote)`` once per instrument per poll cycle,
  independent of whether the consumer has drained the prior cycle's tick for
  that same instrument_id.

This script does not exercise the live engine directly. Both the FIFO
baseline and the latest-wins variant are implemented here, in-process, as two
queue disciplines applied to identical arrival traces built from a
discrete-event, single-server-queue simulation (see ``simulate_queue``
below) -- not real ``asyncio.sleep`` wall-clock waits, since reproducing
multi-minute backlogs via a real event loop would itself take minutes per
repeat. The simulation is an exact (not approximate) model of a single FIFO
``asyncio.Queue`` consumer loop draining at a fixed rate: each item is served
for a deterministic ``service_time`` seconds, strictly in queue order,
single-threaded, with instantaneous (zero-cost) enqueue -- which is precisely
what ``LiveDataEngine._run_data_queue`` / ``ThrottledEnqueuer.enqueue``
implement. Real ``nautilus_trader.model.data.QuoteTick`` objects (built with
real ``InstrumentId``/``Price``/``Quantity``) are used as the payload proving
the approach works against the real wire type, threaded through the simulated
timeline as the ``.value`` carried by each simulated queue slot.

Consumer service time is fixed at ``5 / R`` seconds/item, i.e. the consumer
literally processes at ``R / 5`` (5x backpressure) by construction -- this
directly encodes "consumer processes at R/5" rather than modelling
on_quote_tick's internal compute (which the real probe numbers above show is
negligible relative to the queueing delay it's meant to bound).
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_PATH = REPO_ROOT / "artifacts" / "exp4_quote-coalescing.json"

sys.path.insert(0, str(REPO_ROOT))
from nautilus_trader.model.data import QuoteTick  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402
from nautilus_trader.model.objects import Price, Quantity  # noqa: E402

N_INSTRUMENTS = 200
AGGREGATE_RATE_HZ = 500.0  # R: total arrival rate across all instruments
BACKPRESSURE_FACTOR = 5.0  # consumer serves at R / BACKPRESSURE_FACTOR
PRODUCER_DURATION_SECS = 60.0  # wall-clock (virtual) span of arrivals
REPEATS = 7
VENUES = ("CLOUDBET", "SXBET")


def _build_instrument_ids(n: int) -> list[InstrumentId]:
    ids = []
    for i in range(n):
        venue = VENUES[i % len(VENUES)]
        ids.append(InstrumentId.from_str(f"MATCH-{i:05d}-BINARY.{venue}"))
    return ids


INSTRUMENT_IDS = _build_instrument_ids(N_INSTRUMENTS)


@dataclass
class Arrival:
    t: float  # arrival (== ts_event, in virtual seconds)
    instrument_idx: int
    tick: QuoteTick


def _make_tick(instrument_idx: int, ts_event_ns: int, rng: random.Random) -> QuoteTick:
    bid = 1.01 + rng.random() * 13.0
    spread = 0.01 + rng.random() * 0.05
    return QuoteTick(
        instrument_id=INSTRUMENT_IDS[instrument_idx],
        bid_price=Price.from_str(f"{bid:.2f}"),
        ask_price=Price.from_str(f"{bid + spread:.2f}"),
        bid_size=Quantity.from_str("100"),
        ask_size=Quantity.from_str("100"),
        ts_event=ts_event_ns,
        ts_init=ts_event_ns,
    )


def _generate_arrivals(seed: int) -> list[Arrival]:
    rng = random.Random(seed)  # noqa: S311
    arrivals: list[Arrival] = []
    t = 0.0
    k = 0
    while t < PRODUCER_DURATION_SECS:
        dt = rng.expovariate(AGGREGATE_RATE_HZ)
        t += dt
        instrument_idx = k % N_INSTRUMENTS
        ts_event_ns = int(t * 1_000_000_000)
        tick = _make_tick(instrument_idx, ts_event_ns, rng)
        arrivals.append(Arrival(t=t, instrument_idx=instrument_idx, tick=tick))
        k += 1
    return arrivals


@dataclass
class Delivery:
    instrument_idx: int
    ts_event_ns: int
    processed_at: float  # virtual seconds: when the consumer started serving this item


def simulate_queue(
    arrivals: list[Arrival],
    service_time: float,
    *,
    coalesce: bool,
) -> list[Delivery]:
    """
    Exact discrete-event simulation of a single-server queue draining a fixed arrival
    trace at a constant per-item service time, either:

      - FIFO (coalesce=False): every arrival is queued and served in order,
        mirroring ``asyncio.Queue`` + ``LiveDataEngine._run_data_queue``
        today.
      - latest-wins (coalesce=True): a per-instrument slot holds only the
        newest not-yet-served tick; a re-arrival for an instrument that
        already has a queued, unserved slot overwrites the slot's value in
        place (same queue position) instead of adding a new slot. This is
        exactly the position-preserving, value-refreshing discipline the
        LiveDataEngine change adds to ``process`` / ``_run_data_queue``.

    Both disciplines share the same server: instantaneous enqueue, serve one
    item at a time for ``service_time`` seconds, in strict queue order.

    """
    deliveries: list[Delivery] = []
    server_free_at = 0.0

    if coalesce:
        order: deque[int] = deque()  # instrument_idx queue positions
        pending: dict[int, Arrival] = {}
    else:
        fifo: deque[Arrival] = deque()

    def drain_until(deadline: float) -> None:
        nonlocal server_free_at
        while True:
            has_backlog = bool(order) if coalesce else bool(fifo)
            if not has_backlog:
                return
            start = server_free_at
            if start >= deadline:
                return
            if coalesce:
                idx = order.popleft()
                arrival = pending.pop(idx)
            else:
                arrival = fifo.popleft()
            deliveries.append(
                Delivery(
                    instrument_idx=arrival.instrument_idx,
                    ts_event_ns=arrival.tick.ts_event,
                    processed_at=start,
                ),
            )
            server_free_at = start + service_time

    for arrival in arrivals:
        # Serve as much backlog as fits strictly before this arrival's time.
        drain_until(arrival.t)
        if coalesce:
            idx = arrival.instrument_idx
            if idx not in pending:
                order.append(idx)
            pending[idx] = arrival
        else:
            fifo.append(arrival)

    # Producer stopped: fully drain whatever remains (no more arrivals to race against).
    drain_until(float("inf"))
    return deliveries


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def _ages_ms(deliveries: list[Delivery]) -> list[float]:
    return [max(0.0, (d.processed_at - d.ts_event_ns / 1_000_000_000)) * 1000.0 for d in deliveries]


def _final_state(deliveries: list[Delivery]) -> dict[int, int]:
    """
    Last delivered ts_event per instrument (i.e. terminal knowledge state).
    """
    state: dict[int, int] = {}
    for d in deliveries:
        state[d.instrument_idx] = d.ts_event_ns  # deliveries are in processing order
    return state


def _assert_monotonic_per_instrument(deliveries: list[Delivery]) -> None:
    last_ts: dict[int, int] = {}
    for d in deliveries:
        prev = last_ts.get(d.instrument_idx, -1)
        assert d.ts_event_ns >= prev, (
            f"newest-tick-loss / reordering: instrument={d.instrument_idx} "
            f"delivered ts_event {d.ts_event_ns} after {prev}"
        )
        last_ts[d.instrument_idx] = d.ts_event_ns


def run_one(seed: int) -> dict:
    arrivals = _generate_arrivals(seed)
    service_time = BACKPRESSURE_FACTOR / AGGREGATE_RATE_HZ  # consumer serves at R / 5

    fifo_deliveries = simulate_queue(arrivals, service_time, coalesce=False)
    coalesced_deliveries = simulate_queue(arrivals, service_time, coalesce=True)

    # Ground truth: last arrival generated per instrument.
    ground_truth: dict[int, int] = {}
    for a in arrivals:
        ground_truth[a.instrument_idx] = a.tick.ts_event

    _assert_monotonic_per_instrument(fifo_deliveries)
    _assert_monotonic_per_instrument(coalesced_deliveries)

    fifo_final = _final_state(fifo_deliveries)
    coalesced_final = _final_state(coalesced_deliveries)
    assert fifo_final == ground_truth, "FIFO baseline final state diverged from ground truth"
    assert coalesced_final == ground_truth, (
        "coalescing variant final state diverged from ground truth"
    )
    assert fifo_final == coalesced_final, (
        "coalescing variant final state != FIFO baseline final state"
    )

    fifo_ages = _ages_ms(fifo_deliveries)
    coalesced_ages = _ages_ms(coalesced_deliveries)

    return {
        "seed": seed,
        "n_arrivals": len(arrivals),
        "fifo": {
            "delivered_count": len(fifo_deliveries),
            "age_p50_ms": _percentile(fifo_ages, 0.50),
            "age_p95_ms": _percentile(fifo_ages, 0.95),
            "age_max_ms": max(fifo_ages) if fifo_ages else 0.0,
        },
        "coalesced": {
            "delivered_count": len(coalesced_deliveries),
            "age_p50_ms": _percentile(coalesced_ages, 0.50),
            "age_p95_ms": _percentile(coalesced_ages, 0.95),
            "age_max_ms": max(coalesced_ages) if coalesced_ages else 0.0,
        },
        "newest_tick_loss": False,
        "final_state_identical_to_fifo": True,
        "final_state_matches_ground_truth": True,
    }


def main() -> None:
    started = time.perf_counter()
    per_repeat = [run_one(seed=1000 + i) for i in range(REPEATS)]
    elapsed = time.perf_counter() - started

    fifo_p50s = [r["fifo"]["age_p50_ms"] for r in per_repeat]
    fifo_p95s = [r["fifo"]["age_p95_ms"] for r in per_repeat]
    coalesced_p50s = [r["coalesced"]["age_p50_ms"] for r in per_repeat]
    coalesced_p95s = [r["coalesced"]["age_p95_ms"] for r in per_repeat]
    reductions_p50 = [
        (f - c) / f if f > 0 else 0.0 for f, c in zip(fifo_p50s, coalesced_p50s, strict=True)
    ]
    reductions_p95 = [
        (f - c) / f if f > 0 else 0.0 for f, c in zip(fifo_p95s, coalesced_p95s, strict=True)
    ]

    all_correct = all(
        r["newest_tick_loss"] is False
        and r["final_state_identical_to_fifo"]
        and r["final_state_matches_ground_truth"]
        for r in per_repeat
    )

    median_p50_reduction = statistics.median(reductions_p50)
    median_p95_reduction = statistics.median(reductions_p95)
    threshold_met = median_p50_reduction >= 0.90

    verdict = "PASS" if (threshold_met and all_correct) else "FAIL"

    summary = {
        "slug": "quote-coalescing",
        "config": {
            "n_instruments": N_INSTRUMENTS,
            "aggregate_rate_hz": AGGREGATE_RATE_HZ,
            "backpressure_factor": BACKPRESSURE_FACTOR,
            "consumer_rate_hz": AGGREGATE_RATE_HZ / BACKPRESSURE_FACTOR,
            "producer_duration_secs": PRODUCER_DURATION_SECS,
            "repeats": REPEATS,
        },
        "real_probe_grounding": {
            "source": (
                "live pilot runtime probe status snapshot -- latencyDiagnostics block "
                "produced by scripts/betting/runtime_probe_report.py"
            ),
            "quote_event_to_strategy_p50_ms": 4977.852725,
            "quote_event_to_strategy_p95_ms": 24418.422903,
            "quote_event_to_strategy_max_ms": 24853413.066723,
            "graph_scan_p50_ms": 0.015358,
            "graph_scan_p95_ms": 0.04087,
            "note": (
                "graph_scan (real per-tick strategy compute) is ~5 orders of "
                "magnitude cheaper than quote_event_to_strategy (queue-to-delivery "
                "latency) -- confirms the staleness is a queueing problem, not a "
                "compute problem."
            ),
        },
        "per_repeat": per_repeat,
        "aggregate": {
            "fifo_age_p50_ms_median": statistics.median(fifo_p50s),
            "fifo_age_p50_ms_stdev": statistics.stdev(fifo_p50s) if len(fifo_p50s) > 1 else 0.0,
            "fifo_age_p95_ms_median": statistics.median(fifo_p95s),
            "coalesced_age_p50_ms_median": statistics.median(coalesced_p50s),
            "coalesced_age_p50_ms_stdev": (
                statistics.stdev(coalesced_p50s) if len(coalesced_p50s) > 1 else 0.0
            ),
            "coalesced_age_p95_ms_median": statistics.median(coalesced_p95s),
            "p50_age_reduction_median": median_p50_reduction,
            "p95_age_reduction_median": median_p95_reduction,
            "delivered_count_fifo_median": statistics.median(
                [r["fifo"]["delivered_count"] for r in per_repeat],
            ),
            "delivered_count_coalesced_median": statistics.median(
                [r["coalesced"]["delivered_count"] for r in per_repeat],
            ),
        },
        "correctness": {
            "all_repeats_zero_newest_tick_loss": all_correct,
            "all_repeats_final_state_identical_to_fifo": all_correct,
            "all_repeats_final_state_matches_ground_truth": all_correct,
        },
        "success_threshold": ">=90% p50 delivered-tick-age reduction, identical final state, zero newest-tick loss",
        "threshold_met": threshold_met,
        "verdict": verdict,
        "elapsed_secs": elapsed,
    }

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(summary, indent=2))

    print(json.dumps(summary["aggregate"], indent=2))
    print(f"verdict={verdict} threshold_met={threshold_met} all_correct={all_correct}")
    print(f"artifact written to {ARTIFACT_PATH}")


if __name__ == "__main__":
    main()
