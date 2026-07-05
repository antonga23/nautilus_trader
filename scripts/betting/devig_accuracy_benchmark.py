#!/usr/bin/env python3
"""
Devig (margin-removal) method accuracy + robustness benchmark.

Continuous-experimentation harness. Generates quoted odds from known fair
probabilities using a favorite-longshot-biased margin model (a documented
real-world vig structure that matches no single devig method's assumption, so
the comparison is not circular), then scores each method on fair-probability
recovery error (MAE / max abs error) and robustness (sum-to-1 error,
convergence failures) across a grid of 2-way and 3-way markets.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from nautilus_trader.adapters.betting.common.odds import devig_probabilities


METHODS = ("auto", "proportional", "shin", "logarithmic")


def _favorite_longshot_odds(
    fair: list[float],
    base_margin: float,
    skew: float,
) -> tuple[list[float], float]:
    # Load overround disproportionately on longshots: the implied probability is
    # inflated more for low-probability outcomes (they are over-bet in real books),
    # so quoted decimal odds are shorter than fair for longshots. margin_i grows as
    # fair prob p_i shrinks. This matches none of the pure devig models exactly.
    # Cap each inflated implied probability just below 1.0: a single outcome never
    # reaches implied 1.0 in a real book (decimal odds stay > 1), and without the cap an
    # extreme margin+skew on a strong favorite would synthesise an invalid (odds <= 1) book.
    inflated = [
        min(p * (1.0 + base_margin * (1.0 + skew * (1.0 - p))), 0.98) for p in fair
    ]
    overround = sum(inflated)
    quoted_odds = [1.0 / implied for implied in inflated]
    return quoted_odds, overround


def _fair_grids() -> list[list[float]]:
    grids: list[list[float]] = []
    # 2-way: balanced -> skewed
    for p in (0.50, 0.55, 0.62, 0.70, 0.80, 0.90):
        grids.append([p, 1.0 - p])
    # 3-way (e.g. 1X2): balanced -> skewed
    grids.append([0.34, 0.33, 0.33])
    grids.append([0.45, 0.30, 0.25])
    grids.append([0.60, 0.25, 0.15])
    grids.append([0.75, 0.15, 0.10])
    return grids


def _mae(recovered: list[float], fair: list[float]) -> float:
    return sum(abs(r - f) for r, f in zip(recovered, fair)) / len(fair)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--base-margin", type=float, default=0.06)
    parser.add_argument("--skew", type=float, default=0.8)
    parser.add_argument("--only-method", type=str, default="")
    args = parser.parse_args(argv)

    methods = (args.only_method,) if args.only_method else METHODS
    grids = _fair_grids()
    per_method: dict[str, dict] = {}
    for method in methods:
        maes: list[float] = []
        max_errs: list[float] = []
        sum_errs: list[float] = []
        failures = 0
        for fair in grids:
            odds, _overround = _favorite_longshot_odds(fair, args.base_margin, args.skew)
            book = devig_probabilities([str(o) for o in odds], method=method)
            recovered = [float(p) for p in book.no_vig_probabilities]
            maes.append(_mae(recovered, fair))
            max_errs.append(max(abs(r - f) for r, f in zip(recovered, fair)))
            sum_errs.append(abs(sum(recovered) - 1.0))
            if str(book.convergence_status) == "failed":
                failures += 1
        per_method[method] = {
            "meanMAE": round(sum(maes) / len(maes), 6),
            "worstMAE": round(max(maes), 6),
            "meanMaxAbsError": round(sum(max_errs) / len(max_errs), 6),
            "maxSumToOneError": round(max(sum_errs), 8),
            "convergenceFailures": failures,
            "grids": len(grids),
        }

    ranked = sorted(per_method.items(), key=lambda kv: kv[1]["meanMAE"])
    metrics = {
        "marginModel": f"favorite_longshot(base={args.base_margin},skew={args.skew})",
        "perMethod": per_method,
        "bestByMeanMAE": ranked[0][0],
        "bestMeanMAE": ranked[0][1]["meanMAE"],
        "defaultMethod": "auto",
        "defaultMeanMAE": per_method.get("auto", {}).get("meanMAE"),
    }
    print(json.dumps(metrics, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(metrics, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
