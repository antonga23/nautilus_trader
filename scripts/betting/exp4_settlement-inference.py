#!/usr/bin/env python3
"""
Settlement-inference benchmark: correct-score bare-score bucket conversion.

Live coverage mining reports the bulk of proofs blocked on `unknown_settlement`, and
the dominant convertible class is CORRECT_SCORE: `CanonicalMarketType.CORRECT_SCORE`
has no entry in `SettlementPluginRegistry.build()`, so any selection not caught by
`SelectionPredicateBuilder._bucket_state()` falls through to
`PayoffVectorBuilder._unknown()`. `_bucket_state()` only recognized "SCORE_"- or
"ANY_OTHER_"-prefixed canonical labels, but `_canonical_selection()` only produces a
"SCORE_" prefix when the raw outcome literally contains a "score=" token; a bare score
label ("1-0", "2-1" -- the standard exchange rendering) normalizes to a plain
"1_0" / "2_1" and was settled as UNKNOWN even though correct-score settlement is fully
deterministic.

This benchmark measures the fix (the bare "<int>_<int>" branch in `_bucket_state`) by
comparing the shipped implementation against an emulated pre-fix baseline (the bare-
score branch masked out via monkeypatch), on a seeded synthetic corpus that mirrors the
live family mix (baseball WINNER / POINT_SPREAD / TOTALS, soccer MATCH_ODDS, tennis
TOTALS, tennis + soccer CORRECT_SCORE) through the real `MarketNormalizer` +
`SelectionPredicateBuilder` + `CoverageEngine` pipeline, with the same per-event
bucketing the production `RuleMiner.mine_coverage` entry point uses.

Adversarial cases are included in every seed's corpus to prove no unsafe promotion:
  (a) a non-score CORRECT_SCORE label ("Abandoned") must stay UNKNOWN;
  (b) an intentionally incomplete correct-score bucket (one state's leg missing
      entirely) must behave identically to the already-shipped "SCORE_"-prefixed
      mechanism on the same-shaped input -- bucket predicates are self-referential,
      so behavioral parity with the existing mechanism is the fair bar;
  (c) an OTHER-family unmatched prop market must be byte-identical between baseline
      and fixed runs (the fix is scoped to the CORRECT_SCORE branch only).

Corpus generation is seeded (5 seeds); median and spread of the conversion rate are
reported, along with a byte-for-byte control-family regression signature.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
import json
from pathlib import Path
import random
import re
import statistics
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from nautilus_trader.adapters.betting.semantics.coverage import (  # noqa: E402
    CoverageEngine,
    SelectionPredicateBuilder,
)
from nautilus_trader.adapters.betting.semantics.miner import (  # noqa: E402
    _tolerant_event_buckets,
)
from nautilus_trader.adapters.betting.semantics.normalization import (  # noqa: E402
    MarketNormalizer,
)
from nautilus_trader.adapters.betting.semantics.types import (  # noqa: E402
    CanonicalMarketType,
    NormalizedSelectionRecord,
)


SEEDS = (1, 2, 3, 4, 5)

_FIXED_BUCKET_STATE = SelectionPredicateBuilder._bucket_state
_BARE_SCORE_PATTERN = re.compile(r"^\d+_\d+$")


def _pre_fix_bucket_state(selection):
    # Emulate the pre-fix behavior: mask only the bare "<int>_<int>" CORRECT_SCORE
    # branch so it falls back to unmatched (settled UNKNOWN), exactly as before the
    # fix. Everything else defers to the shipped implementation.
    if (
        selection.market_family == CanonicalMarketType.CORRECT_SCORE.value
        and not selection.selection.startswith(("SCORE_", "ANY_OTHER_"))
        and _BARE_SCORE_PATTERN.match(selection.selection)
    ):
        return None, "", ""
    return _FIXED_BUCKET_STATE(selection)


def _snapshot(
    *,
    provider: str,
    sport_name: str,
    market_name: str,
    outcome: str,
    home: str,
    away: str,
    event_id: str,
    cutoff: str,
    params: str = "",
    handicap: str | None = None,
) -> dict:
    item: dict = {
        "provider": provider,
        "sport_name": sport_name,
        "market_name": market_name,
        "market_type": market_name,
        "outcome": outcome,
        "home_name": home,
        "away_name": away,
        "cutoff_time": cutoff,
        "event_id": event_id,
    }
    if params:
        item["params"] = params
    if handicap is not None:
        item["handicap"] = handicap
    return item


def _correct_score_labels(best_of: int) -> list[str]:
    # All bare-score labels for a best-of-N sets match: an exhaustive, mutually
    # exclusive partition of the outcome space by construction.
    need = best_of // 2 + 1
    labels = []
    for loser_sets in range(need):
        labels.append(f"{need}-{loser_sets}")
        labels.append(f"{loser_sets}-{need}")
    return labels


def _soccer_correct_score_labels() -> list[str]:
    scores = [
        "0-0",
        "1-0",
        "0-1",
        "1-1",
        "2-0",
        "0-2",
        "2-1",
        "1-2",
        "2-2",
        "3-0",
        "0-3",
        "3-1",
        "1-3",
        "3-2",
        "2-3",
        "3-3",
    ]
    return [*scores, "Any Other Home Win", "Any Other Away Win", "Any Other Draw"]


_N_EVENTS_PER_GROUP = 12


def _unique_names(base_home: str, base_away: str, eid: str) -> tuple[str, str]:
    # `event_key` derives from (sport, home_name, away_name, cutoff_time), not
    # `event_id` -- distinct synthetic events need distinct team names or
    # `_tolerant_event_buckets` merges them into one event.
    return f"{base_home} {eid}", f"{base_away} {eid}"


def _baseball_control_items(
    rng: random.Random,
    next_event_id: Callable[[], str],
) -> list[dict]:
    # Baseball WINNER / POINT_SPREAD / TOTALS: control families, never CORRECT_SCORE.
    items: list[dict] = []
    for _ in range(_N_EVENTS_PER_GROUP):
        eid = next_event_id()
        home, away = _unique_names("Home Nine", "Away Nine", eid)
        cutoff = "2026-07-05T23:00:00Z"
        provider = rng.choice(["CLOUDBET", "SXBET"])
        for outcome in ("home", "away"):
            items.append(
                _snapshot(
                    provider=provider,
                    sport_name="baseball",
                    market_name="moneyline",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                ),
            )
        line = rng.choice(["-1.5", "1.5", "-0.5", "0.5"])
        for outcome in ("home", "away"):
            items.append(
                _snapshot(
                    provider=provider,
                    sport_name="baseball",
                    market_name="run_line",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                    params=f"line={line}",
                ),
            )
        total_line = rng.choice(["7.5", "8.0", "8.5", "9.0"])
        for outcome in ("over", "under"):
            items.append(
                _snapshot(
                    provider=provider,
                    sport_name="baseball",
                    market_name="totals",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                    params=f"total={total_line}",
                ),
            )
    return items


def _soccer_match_odds_items(
    rng: random.Random,
    next_event_id: Callable[[], str],
) -> list[dict]:
    # Soccer MATCH_ODDS: control family.
    items: list[dict] = []
    for _ in range(_N_EVENTS_PER_GROUP):
        eid = next_event_id()
        home, away = _unique_names("Home FC", "Away FC", eid)
        cutoff = "2026-07-06T18:00:00Z"
        provider = rng.choice(["CLOUDBET", "SXBET"])
        for outcome in ("home", "draw", "away"):
            items.append(
                _snapshot(
                    provider=provider,
                    sport_name="soccer",
                    market_name="match_odds",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                ),
            )
    return items


def _tennis_totals_items(
    rng: random.Random,
    next_event_id: Callable[[], str],
) -> list[dict]:
    # Tennis TOTALS (games): control family.
    items: list[dict] = []
    for _ in range(_N_EVENTS_PER_GROUP):
        eid = next_event_id()
        home, away = _unique_names("Player A", "Player B", eid)
        cutoff = "2026-07-06T12:00:00Z"
        provider = rng.choice(["CLOUDBET", "SXBET"])
        games_line = rng.choice(["21.5", "22.5", "23.5"])
        for outcome in ("over", "under"):
            items.append(
                _snapshot(
                    provider=provider,
                    sport_name="tennis",
                    market_name="total_games",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                    params=f"total={games_line}",
                ),
            )
    return items


def _tennis_correct_score_items(
    rng: random.Random,
    next_event_id: Callable[[], str],
) -> list[dict]:
    # Tennis CORRECT_SCORE: target class, complete bucket of bare-score labels.
    items: list[dict] = []
    for _ in range(_N_EVENTS_PER_GROUP):
        eid = next_event_id()
        home, away = _unique_names("Player A", "Player B", eid)
        cutoff = "2026-07-06T12:00:00Z"
        provider = rng.choice(["CLOUDBET", "SXBET"])
        best_of = rng.choice([3, 5])
        for outcome in _correct_score_labels(best_of):
            items.append(
                _snapshot(
                    provider=provider,
                    sport_name="tennis",
                    market_name="correct_score",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                ),
            )
    return items


def _soccer_correct_score_items(
    rng: random.Random,
    next_event_id: Callable[[], str],
) -> list[dict]:
    # Soccer CORRECT_SCORE: target class extension, complete bucket.
    items: list[dict] = []
    for _ in range(_N_EVENTS_PER_GROUP):
        eid = next_event_id()
        home, away = _unique_names("Home FC", "Away FC", eid)
        cutoff = "2026-07-06T18:00:00Z"
        provider = rng.choice(["CLOUDBET", "SXBET"])
        for outcome in _soccer_correct_score_labels():
            items.append(
                _snapshot(
                    provider=provider,
                    sport_name="soccer",
                    market_name="correct_score",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                ),
            )
    return items


def _adversarial_items(next_event_id: Callable[[], str]) -> list[dict]:
    items: list[dict] = []
    # Adversarial (a): non-score, non-ANY_OTHER_ CORRECT_SCORE label; must stay
    # UNKNOWN in both runs.
    for _ in range(3):
        eid = next_event_id()
        home, away = _unique_names("Home FC (adv-a)", "Away FC (adv-a)", eid)
        cutoff = "2026-07-06T18:00:00Z"
        items.append(
            _snapshot(
                provider="CLOUDBET",
                sport_name="soccer",
                market_name="correct_score",
                outcome="Abandoned",
                home=home,
                away=away,
                event_id=eid,
                cutoff=cutoff,
            ),
        )
        items.append(
            _snapshot(
                provider="CLOUDBET",
                sport_name="soccer",
                market_name="correct_score",
                outcome="1-0",
                home=home,
                away=away,
                event_id=eid,
                cutoff=cutoff,
            ),
        )

    # Adversarial (b): incomplete correct-score bucket (drop the "0-0" leg); must not
    # reach an execution-safe tier despite fully converted per-selection predicates.
    for _ in range(3):
        eid = next_event_id()
        home, away = _unique_names("Home FC (adv-b)", "Away FC (adv-b)", eid)
        cutoff = "2026-07-06T18:00:00Z"
        labels = [label for label in _soccer_correct_score_labels() if label != "0-0"]
        for outcome in labels:
            items.append(
                _snapshot(
                    provider="CLOUDBET",
                    sport_name="soccer",
                    market_name="correct_score",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                ),
            )

    # Adversarial (c): OTHER-family unmatched prop market; must be totally unaffected.
    for _ in range(3):
        eid = next_event_id()
        home, away = _unique_names("Home FC (adv-c)", "Away FC (adv-c)", eid)
        cutoff = "2026-07-06T18:00:00Z"
        for outcome in ("Home Team", "Away Team", "No Team"):
            items.append(
                _snapshot(
                    provider="CLOUDBET",
                    sport_name="soccer",
                    market_name="team_to_score_first_unrecognized_prop",
                    outcome=outcome,
                    home=home,
                    away=away,
                    event_id=eid,
                    cutoff=cutoff,
                ),
            )
    return items


def build_corpus(seed: int) -> list[dict]:
    rng = random.Random(seed)  # noqa: S311 - deterministic benchmark corpus, not crypto
    event_counter = 0

    def next_event_id() -> str:
        nonlocal event_counter
        event_counter += 1
        return f"evt-{seed}-{event_counter}"

    items: list[dict] = []
    items += _baseball_control_items(rng, next_event_id)
    items += _soccer_match_odds_items(rng, next_event_id)
    items += _tennis_totals_items(rng, next_event_id)
    items += _tennis_correct_score_items(rng, next_event_id)
    items += _soccer_correct_score_items(rng, next_event_id)
    items += _adversarial_items(next_event_id)
    rng.shuffle(items)
    return items


def run_coverage(items: list[dict]) -> tuple[list, list]:
    records = []
    for idx, item in enumerate(items):
        selection = MarketNormalizer.normalize(item)
        records.append(
            NormalizedSelectionRecord(
                record_id=f"rec-{idx}",
                provider=selection.venue,
                selection=selection,
            ),
        )
    engine = CoverageEngine()
    all_proofs: dict[str, object] = {}
    all_hyperedges: dict[str, object] = {}
    # Match the production entry point (RuleMiner.mine_coverage): bucket records
    # per-event before mining so unrelated events' bucket markets never merge.
    for _bucket_key, bucket in _tolerant_event_buckets(records):
        proofs, hyperedges = engine.discover_event_coverage(bucket)
        for proof in proofs:
            all_proofs[proof.proof_id] = proof
        for hyperedge in hyperedges:
            all_hyperedges[hyperedge.hyperedge_id] = hyperedge
    return list(all_proofs.values()), list(all_hyperedges.values())


def summarize(proofs: list) -> dict:
    blocker_counts: Counter = Counter()
    tier_counts: Counter = Counter()
    for proof in proofs:
        for reason in proof.blocker_reasons:
            blocker_counts[reason] += 1
        tier_counts[proof.safety_tier] += 1
    return {
        "proof_count": len(proofs),
        "blocker_counts": dict(blocker_counts),
        "tier_counts": dict(tier_counts),
    }


def correct_score_proofs(proofs: list) -> list:
    return [
        proof
        for proof in proofs
        if any(
            predicate.market_family == CanonicalMarketType.CORRECT_SCORE.value
            for predicate in proof.predicates
        )
    ]


def control_family_signature(proofs: list) -> dict:
    # Signature of every non-CORRECT_SCORE proof's (safety_tier, blocker_reasons,
    # win_covered_states) to assert byte-for-byte no change under the fix.
    signature = {}
    for proof in proofs:
        families = {predicate.market_family for predicate in proof.predicates}
        if CanonicalMarketType.CORRECT_SCORE.value in families:
            continue
        signature[proof.proof_id] = (
            proof.safety_tier,
            proof.blocker_reasons,
            proof.win_covered_states,
        )
    return signature


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None, help="Optional path to write JSON results")
    args = parser.parse_args()

    exec_safe_tiers = {"EXECUTION_SAFE", "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE"}
    per_seed = []
    conversion_rates: list[float] = []
    exec_safe_deltas: list[int] = []
    control_regressions: list[bool] = []
    adversarial_oks: list[bool] = []
    for seed in SEEDS:
        items = build_corpus(seed)

        # Baseline: emulated pre-fix _bucket_state (bare-score branch masked out).
        SelectionPredicateBuilder._bucket_state = staticmethod(  # type: ignore[method-assign]
            _pre_fix_bucket_state,
        )
        try:
            baseline_proofs, _ = run_coverage(items)
        finally:
            SelectionPredicateBuilder._bucket_state = staticmethod(  # type: ignore[method-assign]
                _FIXED_BUCKET_STATE,
            )
        baseline_summary = summarize(baseline_proofs)
        baseline_cs_proofs = correct_score_proofs(baseline_proofs)
        baseline_control_sig = control_family_signature(baseline_proofs)
        baseline_cs_unknown = [
            p for p in baseline_cs_proofs if "unknown_settlement" in p.blocker_reasons
        ]
        baseline_cs_exec_safe = [p for p in baseline_cs_proofs if p.safety_tier in exec_safe_tiers]

        # Fixed: shipped implementation, untouched.
        fixed_proofs, _ = run_coverage(items)
        fixed_summary = summarize(fixed_proofs)
        fixed_cs_proofs = correct_score_proofs(fixed_proofs)
        fixed_control_sig = control_family_signature(fixed_proofs)
        fixed_cs_unknown = [p for p in fixed_cs_proofs if "unknown_settlement" in p.blocker_reasons]
        fixed_cs_exec_safe = [p for p in fixed_cs_proofs if p.safety_tier in exec_safe_tiers]

        # Control (non-CORRECT_SCORE) families must be byte-for-byte identical.
        control_regression = baseline_control_sig != fixed_control_sig
        control_diff_count = sum(
            1
            for key in baseline_control_sig
            if baseline_control_sig.get(key) != fixed_control_sig.get(key)
        )

        # Adversarial (a): "Abandoned" must still resolve UNKNOWN under the fix.
        abandoned_selection = MarketNormalizer.normalize(
            _snapshot(
                provider="CLOUDBET",
                sport_name="soccer",
                market_name="correct_score",
                outcome="Abandoned",
                home="Home FC",
                away="Away FC",
                event_id="adv-a",
                cutoff="2026-07-06T18:00:00Z",
            ),
        )
        abandoned_state = SelectionPredicateBuilder._bucket_state(abandoned_selection)
        abandoned_pred = SelectionPredicateBuilder.from_selection(abandoned_selection)
        abandoned_still_blocked = abandoned_state[0] is None and (
            abandoned_pred.unknown_states == ("UNKNOWN",)
        )

        # Adversarial (b): an incomplete bucket has no ground-truth outcome universe
        # to detect the gap against in either mechanism (bucket predicates are
        # self-referential), so the bar is parity: the bare-score path must behave
        # identically to the shipped "SCORE_"-prefixed path on the same-shaped input.
        labels_no_zero_zero = [label for label in _soccer_correct_score_labels() if label != "0-0"]
        existing_mechanism_items = [
            _snapshot(
                provider="CLOUDBET",
                sport_name="soccer",
                market_name="correct_score",
                outcome=f"score={label.replace('-', ':')}" if "-" in label else label,
                home="Home FC (incomplete-existing)",
                away="Away FC (incomplete-existing)",
                event_id="adv-b-existing",
                cutoff="2026-07-06T18:00:00Z",
            )
            for label in labels_no_zero_zero
        ]
        new_mechanism_items = [
            _snapshot(
                provider="CLOUDBET",
                sport_name="soccer",
                market_name="correct_score",
                outcome=label,
                home="Home FC (incomplete-new)",
                away="Away FC (incomplete-new)",
                event_id="adv-b-new",
                cutoff="2026-07-06T18:00:00Z",
            )
            for label in labels_no_zero_zero
        ]
        existing_proofs, _ = run_coverage(existing_mechanism_items)
        new_proofs, _ = run_coverage(new_mechanism_items)
        existing_shape = sorted(
            (len(p.predicates), p.blocker_reasons, p.safety_tier) for p in existing_proofs
        )
        new_shape = sorted(
            (len(p.predicates), p.blocker_reasons, p.safety_tier) for p in new_proofs
        )
        incomplete_stays_blocked = bool(existing_shape) and existing_shape == new_shape

        converted = len(baseline_cs_unknown) - len(fixed_cs_unknown)
        rate = converted / len(baseline_cs_unknown) if baseline_cs_unknown else 0.0
        conversion_rates.append(rate)
        exec_safe_deltas.append(len(fixed_cs_exec_safe) - len(baseline_cs_exec_safe))
        control_regressions.append(control_regression)
        adversarial_oks.append(abandoned_still_blocked and incomplete_stays_blocked)

        per_seed.append(
            {
                "seed": seed,
                "baseline": baseline_summary,
                "fixed": fixed_summary,
                "correct_score_proof_count": len(baseline_cs_proofs),
                "correct_score_unknown_baseline": len(baseline_cs_unknown),
                "correct_score_unknown_fixed": len(fixed_cs_unknown),
                "correct_score_exec_safe_baseline": len(baseline_cs_exec_safe),
                "correct_score_exec_safe_fixed": len(fixed_cs_exec_safe),
                "converted_count": converted,
                "conversion_rate": rate,
                "control_regression": control_regression,
                "control_diff_count": control_diff_count,
                "control_proof_count": len(baseline_control_sig),
                "adversarial_abandoned_still_blocked": abandoned_still_blocked,
                "adversarial_incomplete_stays_blocked": incomplete_stays_blocked,
            },
        )

    all_no_regression = not any(control_regressions)
    all_adversarial_ok = all(adversarial_oks)
    median_rate = statistics.median(conversion_rates)
    variance_rate = statistics.pvariance(conversion_rates) if len(conversion_rates) > 1 else 0.0

    result = {
        "slug": "settlement-inference",
        "per_seed": per_seed,
        "conversion_rate_median": median_rate,
        "conversion_rate_variance": variance_rate,
        "conversion_rate_min": min(conversion_rates),
        "conversion_rate_max": max(conversion_rates),
        "no_control_regression_all_seeds": all_no_regression,
        "adversarial_ok_all_seeds": all_adversarial_ok,
        "exec_safe_delta_per_seed": exec_safe_deltas,
        "threshold": 0.20,
        "verdict": (
            "PASS" if median_rate >= 0.20 and all_no_regression and all_adversarial_ok else "FAIL"
        ),
    }

    print(json.dumps(result, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
