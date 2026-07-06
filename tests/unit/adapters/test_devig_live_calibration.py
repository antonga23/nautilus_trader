# skipcq
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the live-data devig calibration join (stored odds books vs settled bets).
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys

from nautilus_trader.adapters.betting.common.odds import devig_probabilities
from nautilus_trader.adapters.betting.semantics import CorpusSnapshot
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleStore


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "betting" / "devig_live_calibration.py"
)
METHODS = ("auto", "proportional", "shin", "logarithmic")
CUTOFF = "2026-07-04T12:00:00Z"


def _load_module():
    spec = importlib.util.spec_from_file_location("devig_live_calibration", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _selection(outcome: str, params: str, price: float) -> dict:
    return {
        "outcome": outcome,
        "params": params,
        "price": price,
        "status": "SELECTION_ENABLED",
        "side": "BACK",
    }


def _market(selections: list[dict], submarket: str = "period=ft") -> dict:
    return {"submarkets": {submarket: {"sequence": "1", "selections": selections}}}


def _events_snapshot(snapshot_id: str, fetched_at: str, events: list[dict]) -> CorpusSnapshot:
    payload = {
        "competitions": [
            {
                "name": "League",
                "key": "league",
                "sport": {"name": "Soccer", "key": "soccer"},
                "events": events,
            },
        ],
    }
    return CorpusSnapshot(
        snapshot_id=snapshot_id,
        provider="CLOUDBET",
        endpoint="/pub/v2/odds/events?sport=soccer&from=0&to=1",
        fetched_at=fetched_at,
        payload=json.dumps(payload).encode("utf-8"),
    )


def _bets_snapshot(snapshot_id: str, items: list[dict]) -> CorpusSnapshot:
    return CorpusSnapshot(
        snapshot_id=snapshot_id,
        provider="CLOUDBET",
        endpoint="/pub/v4/bets?offset=0&limit=50&isSettled=true",
        fetched_at="2026-07-05T00:00:00Z",
        payload=json.dumps({"items": items, "hasNext": False}).encode("utf-8"),
    )


def _post_cutoff_snapshot(snapshot_id: str = "snap-post") -> CorpusSnapshot:
    return _events_snapshot(
        snapshot_id,
        "2026-07-04T13:00:00Z",
        [
            {
                "id": 101,
                "status": "RESULTED",
                "cutoffTime": CUTOFF,
                "markets": {
                    "soccer.match_odds": _market(
                        [
                            _selection("home", "", 1.01),
                            _selection("draw", "", 30.0),
                            _selection("away", "", 40.0),
                        ],
                    ),
                },
            },
        ],
    )


def _seed_store(cache_root: Path) -> None:
    store = RuleStore(FileRuleCache(cache_root))
    store.save_snapshot(
        _events_snapshot(
            "snap-early",
            "2026-07-04T10:00:00Z",
            [
                {
                    "id": 101,
                    "status": "TRADING",
                    "cutoffTime": CUTOFF,
                    "markets": {
                        "soccer.match_odds": _market(
                            [
                                _selection("home", "", 2.5),
                                _selection("draw", "", 3.2),
                                _selection("away", "", 3.3),
                            ],
                        ),
                    },
                },
            ],
        ),
    )
    store.save_snapshot(
        _events_snapshot(
            "snap-closing",
            "2026-07-04T11:30:00Z",
            [
                {
                    "id": 101,
                    "status": "TRADING",
                    "cutoffTime": CUTOFF,
                    "markets": {
                        "soccer.match_odds": _market(
                            [
                                _selection("home", "", 1.30),
                                _selection("draw", "", 5.0),
                                _selection("away", "", 9.0),
                            ],
                        ),
                    },
                },
                {
                    "id": 102,
                    "status": "TRADING",
                    "cutoffTime": CUTOFF,
                    "markets": {
                        "soccer.totals": _market(
                            [
                                _selection("over", "total=2.5", 1.40),
                                _selection("under", "total=2.5", 2.90),
                            ],
                        ),
                    },
                },
                # Python-attribute key style, as written by the corpus ingestor's
                # msgspec round-trip (cutoff_time instead of cutoffTime).
                {
                    "id": 103,
                    "status": "TRADING",
                    "cutoff_time": CUTOFF,
                    "markets": {
                        "basketball.handicap": _market(
                            [
                                _selection("home", "handicap=-5.5", 1.35),
                                _selection("away", "handicap=5.5", 3.10),
                            ],
                        ),
                    },
                },
                {
                    "id": 104,
                    "status": "TRADING",
                    "cutoffTime": CUTOFF,
                    "markets": {
                        "soccer.totals": _market(
                            [
                                _selection("over", "total=2.0", 1.90),
                                _selection("under", "total=2.0", 2.00),
                            ],
                        ),
                    },
                },
            ],
        ),
    )
    store.save_snapshot(_post_cutoff_snapshot())
    store.save_snapshot(
        _bets_snapshot(
            "snap-bets",
            [
                {
                    "betType": "STRAIGHT",
                    "result": "WIN",
                    "selection": {
                        "eventId": "101",
                        "marketUrl": "soccer.match_odds/home",
                        "result": "WIN",
                    },
                },
                {
                    "betType": "STRAIGHT",
                    "result": "LOSS",
                    "selection": {
                        "eventId": "102",
                        "marketUrl": "soccer.totals/under?total=2.5",
                        "result": "LOSS",
                    },
                },
                # Python-attribute key style leg, as written by the ingestor round-trip.
                {
                    "result": "WIN",
                    "selections": [
                        {
                            "event_id": "103",
                            "market_url": "basketball.handicap/home?handicap=-5.5",
                            "result": "WIN",
                        },
                    ],
                },
                {
                    "betType": "STRAIGHT",
                    "result": "PUSH",
                    "selection": {
                        "eventId": "104",
                        "marketUrl": "soccer.totals/over?total=2.0",
                        "result": "PUSH",
                    },
                },
            ],
        ),
    )


def _run(module, cache_dir: Path, out: Path, min_books: int) -> dict:
    rc = module.main(
        [
            "--cache-dir",
            str(cache_dir),
            "--min-books",
            str(min_books),
            "--out",
            str(out),
        ],
    )
    assert rc == 0
    return json.loads(out.read_text())


def _expected_log_loss(method: str) -> float:
    # (closing odds, winner index) for the three settled books; e101 must score on the
    # latest pre-cutoff snapshot, e102's winner comes from LOSS-elimination, and e103
    # groups the mirrored handicap line into one book.
    settled = (
        ([1.30, 5.0, 9.0], 0),
        ([1.40, 2.90], 0),
        ([1.35, 3.10], 0),
    )
    losses = []
    for odds, winner_index in settled:
        book = devig_probabilities([str(o) for o in odds], method=method)
        losses.append(-math.log(float(book.no_vig_probabilities[winner_index])))
    return sum(losses) / len(losses)


def test_join_settles_books_and_scores_all_methods(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    _seed_store(cache_dir)

    report = _run(_load_module(), cache_dir, tmp_path / "report.json", min_books=1)

    assert report["insufficientData"] is False
    assert report["counts"]["candidateBooks"] == 4
    assert report["counts"]["settledBooks"] == 3
    assert report["counts"]["nonBinarySettlementBooks"] == 1
    assert report["counts"]["unresolvedBooks"] == 0
    assert set(report["perMethod"]) == set(METHODS)
    for method in METHODS:
        scores = report["perMethod"][method]
        assert scores["books"] == 3
        assert scores["selections"] == 7
        assert math.isfinite(scores["brier"])
        assert math.isfinite(scores["logLoss"])
        # Sharp books where the favorite won: every method must beat a coin flip.
        assert 0.0 < scores["brier"] < 0.5
        assert 0.0 < scores["logLoss"] < math.log(2)
        assert sum(row["count"] for row in scores["calibrationTable"]) == 7
        assert all(0.0 <= row["empiricalWinRate"] <= 1.0 for row in scores["calibrationTable"])
        assert math.isclose(scores["logLoss"], _expected_log_loss(method), abs_tol=1e-5)
    ranked = report["rankedByLogLoss"]
    assert sorted(ranked, key=lambda m: report["perMethod"][m]["logLoss"]) == ranked
    assert report["bestByLogLoss"] == ranked[0]
    assert report["defaultMethod"] == "auto"
    assert report["defaultLogLoss"] == report["perMethod"]["auto"]["logLoss"]


def test_insufficient_data_flag_degrades_gracefully(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    _seed_store(cache_dir)

    report = _run(_load_module(), cache_dir, tmp_path / "report.json", min_books=50)

    assert report["insufficientData"] is True
    assert report["minBooks"] == 50
    assert report["counts"]["settledBooks"] == 3
    assert set(report["perMethod"]) == set(METHODS)


def test_post_cutoff_snapshots_never_form_books(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    store = RuleStore(FileRuleCache(cache_dir))
    store.save_snapshot(_post_cutoff_snapshot())
    store.save_snapshot(
        _bets_snapshot(
            "snap-bets",
            [
                {
                    "result": "WIN",
                    "selection": {
                        "eventId": "101",
                        "marketUrl": "soccer.match_odds/home",
                        "result": "WIN",
                    },
                },
            ],
        ),
    )

    report = _run(_load_module(), cache_dir, tmp_path / "report.json", min_books=1)

    assert report["counts"]["candidateBooks"] == 0
    assert report["counts"]["settledBooks"] == 0
    assert report["insufficientData"] is True
    assert report["perMethod"] == {}
    assert report["bestByLogLoss"] is None
    assert report["defaultLogLoss"] is None
