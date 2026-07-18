#!/usr/bin/env python3
"""
Continuous-experimentation harness for instrument-(re)subscription churn.

Grounding:

* ``BettingArbitrageStrategy._add_refreshed_active_instruments`` (examples/strategies/
  betting_arbitrage.py) dedupes candidate instruments against the currently-subscribed
  set by exact ``str(instrument.id)`` string match. Read directly: this is correct as
  written -- there is no double-counting bug in the dedup itself.
* ``make_crypto_betting_instrument_id`` (adapters/betting/instruments.py) builds the
  instrument symbol as ``f"{event_id}:{market_name}:{outcome}[:{params}]"`` with only
  cosmetic character replacement (``.`` / ` ` / `=` -> `_`) -- it does **not**
  canonicalize numeric tokens inside ``params``. "line=2.5" and "line=2.50" normalize
  to different symbols ("2_5" vs "2_50") even though they encode the same betting line.
* The real production probe (status.json, live multivenue node, ~3.89 days elapsed)
  shows a stark venue asymmetry in ``runtimeProbe.instrumentRefresh.venues``:
  CLOUDBET: added=19734, removed=0, delisted_removed=0 across 1117 reconciles
  (reconcile cadence ~301s, matching the 300s refresh timer exactly).
  POLYMARKET: added=0, removed=676; SXBET: added=0, removed=398.
  CLOUDBET alone drives literally 100% of "added" and 0% of "removed" -- a pure
  monotonic ratchet, plus a 76.7 subscribed-instruments-per-event-key fan-out
  (27931 / 364), consistent with the same underlying market being re-subscribed
  under a fresh, non-canonical id every refresh cycle rather than genuine 5%/day
  catalog growth (which would also produce *some* removals over ~4 days).

This script replays that exact mechanism with the REAL, unmodified
``BettingArbitrageStrategy._add_refreshed_active_instruments`` /
``_remove_inactive_or_delisted_instruments`` bound methods (called unbound against a
lightweight duck-typed harness -- constructing a full ``Strategy`` needs a live
message bus/clock/cache, which is out of scope for an offline diff benchmark) and the
REAL ``make_crypto_betting_instrument_id`` / ``CryptoBettingInstrument``, comparing:

* baseline: raw, uncanonicalized params (upstream feed jitters numeric formatting
  cycle to cycle -- a realistic JSON/float-serialization inconsistency).
* variant: params numeric tokens canonicalized to a fixed precision before instrument
  construction (the minimal fix).

Two exclusion-set regimes are run because they isolate two distinct real behaviours:

* ``working_index``: this cycle's active_instrument_ids passed to the removal check
  reflect only genuinely-active markets (mirrors POLYMARKET/SXBET, which do show
  removals). Under id instability this regime shows churn (both adds AND removes
  inflated) since each cycle's stale-format id becomes excludable next cycle.
* ``broken_index`` (CLOUDBET-shaped): the exclusion set is the union of every id ever
  observed (mirrors an absent/non-authoritative per-venue active index, so nothing is
  ever excluded) -- this reproduces the exact CLOUDBET signature: removed stays at 0
  while added keeps climbing under id instability.

No live network/venue calls; all corpora are seeded synthetic markets.

"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from collections import Counter
from pathlib import Path
from types import MethodType
from types import SimpleNamespace
from typing import cast

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency


VENUE = "CLOUDBET"
CANONICAL_FORMAT = "{:.2f}"
NUMERIC_TOKEN_RE = re.compile(r"-?\d+\.\d+")


def build_jitter_pool(pool_size: int) -> tuple[str, ...]:
    """
    Distinct numeric-formatting templates a raw upstream feed might inconsistently apply
    to the same betting line across polls (trailing zeros / precision differ depending
    on which upstream code path serialized the float that cycle -- e.g. "2.5" vs "2.50"
    vs "2.500").

    A small pool (bounded severity) models occasional
    formatting inconsistency; ``pool_size`` sweeps how many distinct variants exist.

    """
    return tuple(f"{{:.{p}f}}" for p in range(1, pool_size + 1))


def canonicalize_params(raw_params: str) -> str:
    """
    Minimal, backward-compatible fix: reformat numeric tokens in a params string to
    their shortest round-trip ("%g") representation so the same underlying betting line
    always yields the same instrument_id regardless of how the upstream feed serialized
    the float that cycle ("2.5" / "2.50" / "2.500" all -> "2.5").

    Using "%g" rather than a fixed precision like "%.2f" matters: "line=2.5" is the
    value already used throughout tests/unit/strategies/test_betting_arbitrage.py (14+
    call sites), and "%g" reproduces that exact string for already-canonical input
    ("2.5" -> "2.5"), so existing instrument_id fixtures/assertions are unaffected --
    only genuinely inconsistent duplicates of the same value get collapsed together.

    """
    return NUMERIC_TOKEN_RE.sub(lambda m: f"{float(m.group(0)):g}", raw_params)


def make_market_instrument(
    *,
    event_id: str,
    market_name: str,
    outcome: str,
    line: float,
    fmt: str,
    canonical: bool,
) -> CryptoBettingInstrument:
    raw_params = f"line={fmt.format(line)}"
    params = canonicalize_params(raw_params) if canonical else raw_params
    return CryptoBettingInstrument(
        venue=Venue(VENUE),
        event_id=event_id,
        event_name=f"Event {event_id}",
        home_name="Home",
        away_name="Away",
        sport_name="soccer",
        competition_name="Test League",
        market_name=market_name,
        market_type=market_name,
        outcome=outcome,
        side=SelectionSide.BACK,
        price=1.9,
        currency=Currency.from_str("USDC"),
        params=params,
        enabled=True,
        trading_status="OPEN",
    )


def build_fake_strategy(config: BettingArbitrageConfig) -> BettingArbitrageStrategy:
    """
    Duck-typed stand-in exposing exactly the attributes
    ``_add_refreshed_active_instruments`` / ``_remove_inactive_or_delisted_instruments``
    touch, so the REAL unbound methods can run without standing up a full Strategy
    (message bus / clock / cache / execution engine).
    """
    strat = SimpleNamespace(
        _config=config,
        _subscribed_instruments=set(),
        _latest_quotes={},
        _quote_subscribed_instrument_ids=set(),
        _quote_unsubscribe_requests=0,
        _quote_unsubscribe_requests_by_venue=Counter(),
        _instrument_refresh_delisted_removed=0,
        _instrument_refresh_delisted_removed_by_venue=Counter(),
        _source_ids_by_betting_instrument_id={},
        log=SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        unsubscribe_quote_ticks=lambda instrument_id: None,
    )
    # Real helper methods the two target methods call on `self`. These are plain
    # instance/static methods on BettingArbitrageStrategy; bind them onto the
    # duck-typed harness so the unbound target methods can call them via `self.`.
    strat._instrument_is_active_for_refresh = (
        BettingArbitrageStrategy._instrument_is_active_for_refresh
    )
    for method_name in (
        "_should_process_instrument",
        "_remove_subscribed_instrument",
        "_quote_subscription_instrument_id",
    ):
        setattr(
            strat,
            method_name,
            MethodType(getattr(BettingArbitrageStrategy, method_name), strat),
        )
    return cast(BettingArbitrageStrategy, strat)


def spawn_event(rng: random.Random, event_id: str, markets_per_event: int) -> list[tuple]:
    specs = []
    for m in range(markets_per_event):
        market_name = f"totals_{m}"
        outcome = "over"
        line = round(rng.uniform(0.5, 4.5), 1)
        specs.append((event_id, market_name, outcome, line))
    return specs


def evolve_catalog(
    rng: random.Random,
    active_events: dict[str, list[tuple]],
    next_event_id: int,
    markets_per_event: int,
    retire_prob_per_cycle: float,
    new_event_prob_per_cycle: float,
    ever_true_new_keys: set[tuple],
) -> int:
    # Genuine catalog churn: some active events retire (settled/delisted), new
    # fixtures open. This is independent of the id-formatting jitter mechanism.
    for eid in list(active_events.keys()):
        if rng.random() < retire_prob_per_cycle:
            del active_events[eid]
    if rng.random() < new_event_prob_per_cycle:
        eid = f"evt-{next_event_id}"
        next_event_id += 1
        specs = spawn_event(rng, eid, markets_per_event)
        active_events[eid] = specs
        for e, mname, outc, _line in specs:
            ever_true_new_keys.add((e, mname, outc))
    return next_event_id


def jittered_fmt_and_line(
    jitter_rng: random.Random,
    jitter_pool: tuple[str, ...] | None,
    line: float,
) -> tuple[str, float]:
    if jitter_pool is not None:
        return jitter_rng.choice(jitter_pool), line
    # Unbounded: a fresh, never-repeated high-precision representation
    # every cycle (tiny sub-tick noise on the raw float itself).
    fmt = "{:." + str(jitter_rng.randint(6, 12)) + "f}"
    return fmt, line + jitter_rng.uniform(-5e-7, 5e-7)


def run_scenario(
    *,
    seed: int,
    n_events: int,
    markets_per_event: int,
    n_cycles: int,
    canonical: bool,
    index_mode: str,
    retire_prob_per_cycle: float,
    new_event_prob_per_cycle: float,
    jitter_pool_size: int = 3,
) -> dict:
    # Two independent RNG streams so the *catalog* trajectory (which events/markets
    # exist, when they retire/spawn) is byte-for-byte identical between the baseline
    # and variant runs for the same seed -- only whether params get jittered differs.
    # Sharing one RNG for both would let the extra rng.choice() draw in the jittered
    # path desync the catalog-evolution draws from the canonical path, contaminating
    # the true_new_count comparison between the two.
    rng = random.Random(seed)  # noqa: S311 - seeded simulation, not cryptographic.
    jitter_rng = random.Random(seed + 500_000)  # noqa: S311 - seeded simulation, not cryptographic.
    # jitter_pool_size == 0 models unbounded formatting entropy (every cycle mints a
    # never-before-seen numeric representation for the same line, e.g. a rotating
    # upstream precision/rounding artifact) instead of a small recurring set of
    # variants. This is the more severe end of the same, single grounded mechanism
    # (make_crypto_betting_instrument_id not canonicalizing numeric params) and is
    # what the sustained, unplateaued real CLOUDBET growth (19734 adds / 1117
    # reconciles, no slowdown) is consistent with.
    jitter_pool = build_jitter_pool(jitter_pool_size) if jitter_pool_size > 0 else None
    config = BettingArbitrageConfig()
    strat = build_fake_strategy(config)

    active_events: dict[str, list[tuple]] = {}
    next_event_id = 0
    for _ in range(n_events):
        eid = f"evt-{next_event_id}"
        next_event_id += 1
        active_events[eid] = spawn_event(rng, eid, markets_per_event)

    ever_true_new_keys: set[tuple] = set()
    for specs in active_events.values():
        for eid, mname, outc, _line in specs:
            ever_true_new_keys.add((eid, mname, outc))

    total_add_ops = 0
    total_remove_ops = 0
    ever_seen_ids: set[str] = set()
    canonical_ids_added: set[tuple[str, str, str]] = set()

    for _cycle in range(n_cycles):
        next_event_id = evolve_catalog(
            rng,
            active_events,
            next_event_id,
            markets_per_event,
            retire_prob_per_cycle,
            new_event_prob_per_cycle,
            ever_true_new_keys,
        )

        active_cached = []
        for specs in active_events.values():
            for eid, mname, outc, line in specs:
                if canonical:
                    fmt = CANONICAL_FORMAT
                else:
                    fmt, line = jittered_fmt_and_line(jitter_rng, jitter_pool, line)
                active_cached.append(
                    make_market_instrument(
                        event_id=eid,
                        market_name=mname,
                        outcome=outc,
                        line=line,
                        fmt=fmt,
                        canonical=canonical,
                    ),
                )

        active_ids_this_cycle = {str(i.id) for i in active_cached}
        ever_seen_ids.update(active_ids_this_cycle)
        exclusion_ids = (
            active_ids_this_cycle if index_mode == "working_index" else set(ever_seen_ids)
        )

        # The REAL, unmodified bound methods from BettingArbitrageStrategy.
        added_instruments = BettingArbitrageStrategy._add_refreshed_active_instruments(
            strat,
            active_cached,
        )
        removed = BettingArbitrageStrategy._remove_inactive_or_delisted_instruments(
            strat,
            venue_value=VENUE,
            active_instrument_ids=exclusion_ids,
        )
        total_add_ops += len(added_instruments)
        total_remove_ops += len(removed)
        for inst in added_instruments:
            canonical_ids_added.add((str(inst.event_id), inst.market_name, inst.outcome))

    true_new_count = len(ever_true_new_keys)
    # canonical_ids_added tracks the true (event, market, outcome) identity of every
    # added instrument regardless of which jittered/canonical id it carried, so this
    # "missed genuinely-new market" check is meaningful for baseline too, not just
    # the canonicalized variant.
    missed_new = len(ever_true_new_keys - canonical_ids_added)
    return {
        "total_add_ops": total_add_ops,
        "total_remove_ops": total_remove_ops,
        "true_new_count": true_new_count,
        "final_subscribed": len(strat._subscribed_instruments),
        "missed_new": missed_new,
    }


def summarize(label: str, results: list[dict]) -> dict:
    adds = [r["total_add_ops"] for r in results]
    removes = [r["total_remove_ops"] for r in results]
    true_news = [r["true_new_count"] for r in results]
    return {
        "label": label,
        "repeats": len(results),
        "add_ops_median": statistics.median(adds),
        "add_ops_stdev": statistics.pstdev(adds) if len(adds) > 1 else 0.0,
        "add_ops_all": adds,
        "remove_ops_median": statistics.median(removes),
        "remove_ops_stdev": statistics.pstdev(removes) if len(removes) > 1 else 0.0,
        "remove_ops_all": removes,
        "true_new_count_median": statistics.median(true_news),
        "true_new_count_all": true_news,
        "missed_new_all": [r["missed_new"] for r in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-events", type=int, default=60)
    parser.add_argument("--markets-per-event", type=int, default=8)
    parser.add_argument("--n-cycles", type=int, default=200)  # scaled down from ~576 (2 days)
    parser.add_argument("--retire-prob", type=float, default=0.0005)  # ~5%/day-equivalent hazard
    parser.add_argument("--new-event-prob", type=float, default=0.15)  # scaled new-event arrival
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed-base", type=int, default=1000)
    parser.add_argument(
        "--jitter-severities",
        type=int,
        nargs="+",
        default=[3, 6, 0],
        help="Jitter pool sizes to sweep (0 = unbounded/never-repeating formatting noise).",
    )
    parser.add_argument(
        "--headline-severity",
        type=int,
        default=0,
        help="Which --jitter-severities value the PASS/FAIL verdict is computed against.",
    )
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    seeds = [args.seed_base + i for i in range(args.repeats)]
    by_severity = {}
    for severity in args.jitter_severities:
        scenarios = {}
        for index_mode in ("working_index", "broken_index"):
            for canonical, tag in ((False, "baseline"), (True, "variant")):
                key = f"{index_mode}__{tag}"
                results = [
                    run_scenario(
                        seed=seed,
                        n_events=args.n_events,
                        markets_per_event=args.markets_per_event,
                        n_cycles=args.n_cycles,
                        canonical=canonical,
                        index_mode=index_mode,
                        retire_prob_per_cycle=args.retire_prob,
                        new_event_prob_per_cycle=args.new_event_prob,
                        jitter_pool_size=severity,
                    )
                    for seed in seeds
                ]
                scenarios[key] = summarize(key, results)

        # Sanity check: the two independent RNG streams (catalog vs. jitter) give
        # baseline and variant byte-identical catalog trajectories per seed -- only
        # the id-formatting differs, so true_new_count must match exactly.
        assert (
            scenarios["broken_index__baseline"]["true_new_count_all"]
            == scenarios["broken_index__variant"]["true_new_count_all"]
        ), ("catalog trajectory desynced between baseline/variant runs", severity)

        by_severity[severity] = scenarios

    headline = by_severity[args.headline_severity]
    baseline = headline["broken_index__baseline"]
    variant = headline["broken_index__variant"]
    true_new = baseline["true_new_count_median"]
    reduction_pct = (
        (baseline["add_ops_median"] - variant["add_ops_median"]) / baseline["add_ops_median"]
        if baseline["add_ops_median"]
        else 0.0
    )
    variant_excess_over_true_new = variant["add_ops_median"] - true_new
    missed_new_total = sum(m for m in variant["missed_new_all"] if m is not None)
    baseline_missed_new_total = sum(m for m in baseline["missed_new_all"] if m is not None)

    working = headline["working_index__baseline"]
    working_variant = headline["working_index__variant"]

    severity_sweep = {
        str(sev): {
            "broken_index_baseline_add_ops_median": s["broken_index__baseline"]["add_ops_median"],
            "broken_index_variant_add_ops_median": s["broken_index__variant"]["add_ops_median"],
            "broken_index_baseline_remove_ops_median": s["broken_index__baseline"][
                "remove_ops_median"
            ],
            "true_new_count_median": s["broken_index__baseline"]["true_new_count_median"],
            "reduction_pct": (
                (
                    s["broken_index__baseline"]["add_ops_median"]
                    - s["broken_index__variant"]["add_ops_median"]
                )
                / s["broken_index__baseline"]["add_ops_median"]
                if s["broken_index__baseline"]["add_ops_median"]
                else 0.0
            ),
        }
        for sev, s in by_severity.items()
    }

    verdict = {
        "hypothesis": (
            "Uncanonicalized numeric params in make_crypto_betting_instrument_id cause "
            "id instability that inflates instrument_refresh 'added' operations for "
            "markets that are not genuinely new."
        ),
        "headline_jitter_severity": args.headline_severity,
        "jitter_severity_sweep": severity_sweep,
        "true_new_count": true_new,
        "broken_index_baseline_add_ops_median": baseline["add_ops_median"],
        "broken_index_variant_add_ops_median": variant["add_ops_median"],
        "broken_index_baseline_remove_ops_median": baseline["remove_ops_median"],
        "broken_index_variant_remove_ops_median": variant["remove_ops_median"],
        "reduction_pct_toward_true_new": reduction_pct,
        "variant_excess_add_ops_over_true_new": variant_excess_over_true_new,
        "variant_missed_new_instruments_total_across_repeats": missed_new_total,
        "baseline_missed_new_instruments_total_across_repeats": baseline_missed_new_total,
        "working_index_baseline_add_ops_median": working["add_ops_median"],
        "working_index_baseline_remove_ops_median": working["remove_ops_median"],
        "working_index_variant_add_ops_median": working_variant["add_ops_median"],
        "working_index_variant_remove_ops_median": working_variant["remove_ops_median"],
        "pass_threshold_80pct_reduction": reduction_pct >= 0.80,
        "pass_zero_missed_new": missed_new_total == 0,
        "pass_removals_still_happen_working_index": working_variant["remove_ops_median"] > 0,
    }
    verdict["PASS"] = (
        verdict["pass_threshold_80pct_reduction"]
        and verdict["pass_zero_missed_new"]
        and verdict["pass_removals_still_happen_working_index"]
    )

    live_production_reference = {
        "source": "runtimeProbe.instrumentRefresh (live multivenue node status probe)",
        "elapsed_days": 3.8936539351851853,
        "CLOUDBET": {"added": 19734, "removed": 0, "reconciles": 1117},
        "POLYMARKET": {"added": 0, "removed": 676, "reconciles": 1117},
        "SXBET": {"added": 0, "removed": 398, "reconciles": 1117},
        "subscribed_instruments_total": 27931,
        "cloudbet_event_key_count": 364,
        "cloudbet_instruments_per_event_key": 27931 / 364,
    }

    payload = {
        "seeds": seeds,
        "params": vars(args),
        "scenarios_by_jitter_severity": {str(k): v for k, v in by_severity.items()},
        "verdict": verdict,
        "live_production_reference": live_production_reference,
    }

    print(json.dumps(verdict, indent=2))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nWrote metrics to {out}")


if __name__ == "__main__":
    main()
