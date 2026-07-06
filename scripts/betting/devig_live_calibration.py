#!/usr/bin/env python3
# ruff: noqa: E402
"""
Score devig methods against real stored closing odds and settled outcomes.

Reads a semantic rule cache (``--cache-dir``) populated by ``semantic_rule_mining.py
refresh-corpus``. Odds books are rebuilt from Cloudbet ``/pub/v2/odds`` snapshots (all
outcomes of one market/submarket/line, taking the latest pre-cutoff snapshot as the
closing book). Winners come from the account's settled-bet snapshots (``/pub/v4/bets``,
the same evidence the ``validate`` step consumes): a settled WIN identifies the book
winner directly, and LOSS evidence on all but one outcome settles the book by
elimination. Books touched by void/partial/cashed-out settlement are excluded. Every
devig method is scored on the settled books with multiclass Brier score, winner log-
loss, and calibration deciles; the report degrades to ``insufficientData`` while the
corpus is still accumulating.

"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from urllib.parse import parse_qsl
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nautilus_trader.adapters.betting.common.odds import devig_probabilities
from nautilus_trader.adapters.betting.semantics.store import FileRuleCache
from nautilus_trader.adapters.betting.semantics.store import RuleStore


METHODS = ("auto", "proportional", "shin", "logarithmic")
ODDS_ENDPOINT_PREFIX = "/pub/v2/odds/"
BETS_ENDPOINT_PREFIX = "/pub/v4/bets"
MIN_OVERROUND = 1.0
MAX_OVERROUND = 1.25
PROB_CLIP = 1e-6
WIN_RESULTS = frozenset({"WIN"})
LOSE_RESULTS = frozenset({"LOSS", "LOSE"})
NON_BINARY_RESULTS = frozenset(
    {
        "PUSH",
        "VOID",
        "HALF_WIN",
        "HALF_LOSS",
        "HALF_LOSE",
        "PARTIAL",
        "PARTIAL_WIN",
        "PARTIAL_LOSE",
        "CASHED_OUT",
    },
)
PRE_START_STATUSES = frozenset({"", "PRE_TRADING", "TRADING"})
# Handicap params are quoted home-relative (home -1.5 <-> away +1.5); folding to the
# absolute value groups both sides of one line into one book.
MIRRORED_LINE_KEYS = frozenset({"handicap"})


@dataclass(frozen=True)
class BookSelection:
    outcome: str
    match_params: frozenset[tuple[str, str]]
    price: float


@dataclass
class Book:
    event_id: str
    market_name: str
    submarket: str
    line_key: tuple[tuple[str, str], ...]
    fetched_at: str
    sort_ts: str
    selections: tuple[BookSelection, ...]
    winner: str | None = None

    @property
    def key(self) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
        return (self.event_id, self.market_name, self.submarket, self.line_key)


@dataclass(frozen=True)
class SettlementEvidence:
    event_id: str
    market_name: str
    outcome: str
    params: frozenset[tuple[str, str]]
    result: str


def _get(payload: dict, *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _canonical_params(query: str | None) -> tuple[tuple[str, str], ...]:
    if not query:
        return ()
    items: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key in MIRRORED_LINE_KEYS:
            with suppress(ValueError):
                value = str(abs(float(value)))
        items.append((key, value))
    return tuple(sorted(set(items)))


def _iter_events(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    competitions = _get(payload, "competitions", "competition")
    if isinstance(competitions, list):
        events: list[dict] = []
        for competition in competitions:
            if isinstance(competition, dict):
                events.extend(
                    event for event in competition.get("events", ()) if isinstance(event, dict)
                )
        return events
    direct_events = payload.get("events")
    if isinstance(direct_events, list):
        return [event for event in direct_events if isinstance(event, dict)]
    if isinstance(payload.get("markets"), dict) and payload.get("id") is not None:
        return [payload]
    return []


def _event_books(event: dict, fetched_at: str) -> list[Book]:
    event_id = _get(event, "id")
    markets = _get(event, "markets")
    if event_id is None or not isinstance(markets, dict):
        return []
    status = str(_get(event, "status") or "")
    cutoff_ts = _parse_ts(str(_get(event, "cutoff_time", "cutoffTime") or ""))
    fetched_ts = _parse_ts(fetched_at)
    # Closing-book gate: fetched no later than the event cutoff when both timestamps
    # parse, otherwise only clearly pre-start statuses (in-play odds are a different
    # regime and must not enter the calibration).
    if cutoff_ts is not None and fetched_ts is not None:
        if fetched_ts > cutoff_ts:
            return []
    elif status not in PRE_START_STATUSES:
        return []

    sort_ts = fetched_ts.isoformat() if fetched_ts is not None else fetched_at
    books: list[Book] = []
    for market_name, market_value in markets.items():
        books.extend(
            _market_books(
                event_id=str(event_id),
                market_name=str(market_name),
                market_value=market_value,
                fetched_at=fetched_at,
                sort_ts=sort_ts,
            ),
        )
    return books


def _market_books(
    *,
    event_id: str,
    market_name: str,
    market_value: object,
    fetched_at: str,
    sort_ts: str,
) -> list[Book]:
    if not isinstance(market_value, dict):
        return []
    submarkets = _get(market_value, "submarkets")
    if not isinstance(submarkets, dict):
        return []
    books: list[Book] = []
    for submarket_key, submarket_value in submarkets.items():
        if not isinstance(submarket_value, dict):
            continue
        selections = submarket_value.get("selections")
        if not isinstance(selections, list):
            continue
        books.extend(
            _submarket_books(
                event_id=event_id,
                market_name=market_name,
                submarket_key=str(submarket_key),
                selections=selections,
                fetched_at=fetched_at,
                sort_ts=sort_ts,
            ),
        )
    return books


def _submarket_books(
    *,
    event_id: str,
    market_name: str,
    submarket_key: str,
    selections: list,
    fetched_at: str,
    sort_ts: str,
) -> list[Book]:
    submarket_params = _canonical_params(submarket_key)
    grouped: dict[tuple[tuple[str, str], ...], list[BookSelection]] = defaultdict(list)
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        outcome = str(_get(selection, "outcome") or "").strip().lower()
        price = _get(selection, "price")
        status = str(_get(selection, "status", "selection_status") or "")
        if not outcome or not isinstance(price, int | float) or price <= 1.0:
            continue
        if status and status != "SELECTION_ENABLED":
            continue
        line_key = _canonical_params(str(_get(selection, "params") or ""))
        grouped[line_key].append(
            BookSelection(
                outcome=outcome,
                match_params=frozenset(line_key) | frozenset(submarket_params),
                price=float(price),
            ),
        )
    books: list[Book] = []
    for line_key, book_selections in grouped.items():
        outcomes = [selection.outcome for selection in book_selections]
        if len(outcomes) < 2 or len(set(outcomes)) != len(outcomes):
            continue
        overround = sum(1.0 / selection.price for selection in book_selections)
        if not MIN_OVERROUND <= overround <= MAX_OVERROUND:
            continue
        books.append(
            Book(
                event_id=event_id,
                market_name=market_name,
                submarket=submarket_key,
                line_key=line_key,
                fetched_at=fetched_at,
                sort_ts=sort_ts,
                selections=tuple(book_selections),
            ),
        )
    return books


def _collect_books(store: RuleStore) -> tuple[dict, int]:
    latest: dict[tuple, Book] = {}
    odds_snapshots = 0
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if (
            snapshot is None
            or snapshot.provider != "CLOUDBET"
            or not snapshot.endpoint.startswith(ODDS_ENDPOINT_PREFIX)
        ):
            continue
        try:
            payload = json.loads(snapshot.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        events = _iter_events(payload)
        if not events:
            continue
        odds_snapshots += 1
        for event in events:
            for book in _event_books(event, snapshot.fetched_at):
                current = latest.get(book.key)
                if current is None or book.sort_ts > current.sort_ts:
                    latest[book.key] = book
    return latest, odds_snapshots


def _bet_legs(item: dict) -> list[dict]:
    legs = _get(item, "selections")
    if isinstance(legs, list) and legs:
        return [leg for leg in legs if isinstance(leg, dict)]
    leg = _get(item, "selection")
    if isinstance(leg, dict):
        return [leg]
    return [item]


def _collect_settlement(
    store: RuleStore,
) -> tuple[dict[tuple[str, str], list[SettlementEvidence]], int]:
    evidence: dict[tuple[str, str], list[SettlementEvidence]] = defaultdict(list)
    bets_snapshots = 0
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if (
            snapshot is None
            or snapshot.provider != "CLOUDBET"
            or not snapshot.endpoint.startswith(BETS_ENDPOINT_PREFIX)
        ):
            continue
        try:
            payload = json.loads(snapshot.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        bets_snapshots += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            legs = _bet_legs(item)
            # A multi-leg (parlay) result is combo-level and says nothing about a
            # single leg, so only single-leg bets may inherit the item result.
            item_result = str(_get(item, "result") or "") if len(legs) == 1 else ""
            for leg in legs:
                entry = _leg_evidence(leg, fallback_result=item_result)
                if entry is not None:
                    evidence[(entry.event_id, entry.market_name)].append(entry)
    return evidence, bets_snapshots


def _leg_evidence(leg: dict, *, fallback_result: str) -> SettlementEvidence | None:
    result = str(_get(leg, "result") or fallback_result or "").upper()
    event_id = _get(leg, "event_id", "eventId", "legacy_event_id")
    market_url = str(_get(leg, "market_url", "marketUrl", "legacy_market_url") or "")
    if not result or result == "PENDING" or event_id is None or "/" not in market_url:
        return None
    market_name, _, rest = market_url.partition("/")
    outcome_raw, _, query = rest.partition("?")
    outcome = unquote(outcome_raw).strip().lower()
    if not market_name or not outcome:
        return None
    return SettlementEvidence(
        event_id=str(event_id),
        market_name=market_name,
        outcome=outcome,
        params=frozenset(_canonical_params(query)),
        result=result,
    )


def _match_books(
    entry: SettlementEvidence,
    books: list[Book],
) -> list[Book]:
    matched: list[Book] = []
    for book in books:
        for selection in book.selections:
            if selection.outcome != entry.outcome:
                continue
            if entry.params <= selection.match_params or selection.match_params <= entry.params:
                matched.append(book)
                break
    return matched


def _resolve_winners(
    books: dict[tuple, Book],
    evidence: dict[tuple[str, str], list[SettlementEvidence]],
) -> dict[str, int]:
    by_market: dict[tuple[str, str], list[Book]] = defaultdict(list)
    for book in books.values():
        by_market[(book.event_id, book.market_name)].append(book)

    results_by_book: dict[tuple, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    ambiguous = 0
    for market_key, entries in evidence.items():
        candidates = by_market.get(market_key, [])
        for entry in entries:
            matched = _match_books(entry, candidates)
            if len(matched) != 1:
                ambiguous += len(matched) > 1
                continue
            results_by_book[matched[0].key][entry.outcome].add(entry.result)

    counts = {"settled": 0, "nonBinary": 0, "unresolved": 0, "ambiguousEvidence": ambiguous}
    for key, outcome_results in results_by_book.items():
        book = books[key]
        flat_results = {result for results in outcome_results.values() for result in results}
        if flat_results & NON_BINARY_RESULTS:
            counts["nonBinary"] += 1
            continue
        winner = _winner_from_results(book, outcome_results)
        if winner is None:
            counts["unresolved"] += 1
            continue
        book.winner = winner
        counts["settled"] += 1
    return counts


def _winner_from_results(
    book: Book,
    outcome_results: dict[str, set[str]],
) -> str | None:
    if any(
        results & WIN_RESULTS and results & LOSE_RESULTS for results in outcome_results.values()
    ):
        return None
    winners = {outcome for outcome, results in outcome_results.items() if results & WIN_RESULTS}
    if len(winners) == 1:
        return next(iter(winners))
    if winners:
        return None
    losers = {outcome for outcome, results in outcome_results.items() if results & LOSE_RESULTS}
    remaining = [
        selection.outcome for selection in book.selections if selection.outcome not in losers
    ]
    if len(remaining) == 1:
        return remaining[0]
    return None


def _clip(probability: float) -> float:
    return min(max(probability, PROB_CLIP), 1.0 - PROB_CLIP)


def _score_methods(settled_books: list[Book]) -> dict[str, dict]:
    per_method: dict[str, dict] = {}
    for method in METHODS:
        briers: list[float] = []
        log_losses: list[float] = []
        selections_scored = 0
        convergence_failures = 0
        devig_errors = 0
        buckets: list[dict[str, float]] = [
            {"count": 0, "sumPredicted": 0.0, "wins": 0} for _ in range(10)
        ]
        for book in settled_books:
            odds = [str(selection.price) for selection in book.selections]
            try:
                devigged = devig_probabilities(odds, method=method)
            except (ValueError, ArithmeticError):
                devig_errors += 1
                continue
            if str(devigged.convergence_status) == "failed":
                convergence_failures += 1
            probabilities = [float(p) for p in devigged.no_vig_probabilities]
            outcomes = [selection.outcome for selection in book.selections]
            targets = [1.0 if outcome == book.winner else 0.0 for outcome in outcomes]
            briers.append(
                sum((p - y) ** 2 for p, y in zip(probabilities, targets, strict=True)),
            )
            winner_index = targets.index(1.0)
            log_losses.append(-math.log(_clip(probabilities[winner_index])))
            selections_scored += len(outcomes)
            for probability, target in zip(probabilities, targets, strict=True):
                bucket = buckets[min(int(probability * 10), 9)]
                bucket["count"] += 1
                bucket["sumPredicted"] += probability
                bucket["wins"] += target
        if not briers:
            continue
        per_method[method] = {
            "books": len(briers),
            "selections": selections_scored,
            "brier": round(sum(briers) / len(briers), 6),
            "logLoss": round(sum(log_losses) / len(log_losses), 6),
            "convergenceFailures": convergence_failures,
            "devigErrors": devig_errors,
            "calibrationTable": [
                {
                    "bucket": f"{index / 10:.1f}-{(index + 1) / 10:.1f}",
                    "count": int(bucket["count"]),
                    "meanPredicted": round(bucket["sumPredicted"] / bucket["count"], 6),
                    "empiricalWinRate": round(bucket["wins"] / bucket["count"], 6),
                }
                for index, bucket in enumerate(buckets)
                if bucket["count"]
            ],
        }
    return per_method


def run_calibration(cache_dir: str, min_books: int) -> dict:
    store = RuleStore(FileRuleCache(cache_dir))
    books, odds_snapshots = _collect_books(store)
    evidence, bets_snapshots = _collect_settlement(store)
    settle_counts = _resolve_winners(books, evidence)
    settled_books = sorted(
        (book for book in books.values() if book.winner is not None),
        key=lambda book: book.key,
    )
    per_method = _score_methods(settled_books)
    ranked = sorted(per_method.items(), key=lambda kv: kv[1]["logLoss"])
    return {
        "cacheDir": str(cache_dir),
        "counts": {
            "snapshots": len(store.list_snapshot_ids()),
            "oddsSnapshots": odds_snapshots,
            "betsSnapshots": bets_snapshots,
            "candidateBooks": len(books),
            "settledBooks": len(settled_books),
            "nonBinarySettlementBooks": settle_counts["nonBinary"],
            "unresolvedBooks": settle_counts["unresolved"],
            "ambiguousEvidence": settle_counts["ambiguousEvidence"],
        },
        "minBooks": min_books,
        "insufficientData": len(settled_books) < min_books,
        "perMethod": per_method,
        "rankedByLogLoss": [name for name, _ in ranked],
        "bestByLogLoss": ranked[0][0] if ranked else None,
        "defaultMethod": "auto",
        "defaultLogLoss": per_method.get("auto", {}).get("logLoss"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--min-books", type=int, default=50)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args(argv)

    report = run_calibration(args.cache_dir, args.min_books)
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.out:
        Path(args.out).write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
