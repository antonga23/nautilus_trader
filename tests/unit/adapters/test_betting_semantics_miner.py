# skipcq
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for tolerant cross-venue event bucketing in the rule miner.
# -------------------------------------------------------------------------------------------------

from nautilus_trader.adapters.betting.semantics import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import NormalizedSelection
from nautilus_trader.adapters.betting.semantics import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics import RelationshipType
from nautilus_trader.adapters.betting.semantics import RuleMiner
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore


def _record(
    *,
    record_id: str,
    venue: str,
    event_key: str,
    selection: str,
    line: str = "2.5",
) -> NormalizedSelectionRecord:
    return NormalizedSelectionRecord(
        record_id=record_id,
        provider=venue,
        selection=NormalizedSelection(
            venue=venue,
            instrument_id=record_id,
            sport="soccer",
            event_key=event_key,
            period="full_time",
            scope="full_time",
            market_type=CanonicalMarketType.TOTALS.value,
            market_family=CanonicalMarketType.TOTALS.value,
            selection=selection,
            params=(("line", line),),
            raw_market_name="Total Goals",
            raw_market_type="totals",
            raw_outcome=selection,
            outcome_key=selection,
        ),
    )


def _miner(tmp_path) -> RuleMiner:
    return RuleMiner(RuleStore(FileRuleCache(tmp_path)))


def test_cross_venue_timestamp_skew_pairs_same_fixture(tmp_path) -> None:
    # Same fixture, different venue timestamp precision (30 min skew) — the exact
    # event keys differ, but the tolerant buckets must pair them.
    records = [
        _record(
            record_id="cb-over",
            venue="CLOUDBET",
            event_key="soccer|team a|team b|2026-07-04T18:00:00Z",
            selection="OVER",
        ),
        _record(
            record_id="sx-under",
            venue="SXBET",
            event_key="soccer|team a|team b|2026-07-04T18:30:00Z",
            selection="UNDER",
        ),
    ]

    rules = _miner(tmp_path).mine_event_candidates(records, persist=False)

    complementary = [
        rule
        for rule in rules
        if rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    ]
    assert len(complementary) == 1
    assert complementary[0].venue_scope == ("CLOUDBET", "SXBET")
    # The shared no-time family key is retained as fixture evidence.
    assert complementary[0].evidence_event_key == "soccer|team a|team b"


def test_doubleheader_fixtures_stay_separate(tmp_path) -> None:
    # Same teams, same day, 7 hours apart -> two fixtures; OVER from one leg must not
    # pair with UNDER from the other.
    records = [
        _record(
            record_id="cb-over-early",
            venue="CLOUDBET",
            event_key="baseball|team a|team b|2026-07-04T12:00:00Z",
            selection="OVER",
        ),
        _record(
            record_id="sx-under-late",
            venue="SXBET",
            event_key="baseball|team a|team b|2026-07-04T19:00:00Z",
            selection="UNDER",
        ),
    ]

    rules = _miner(tmp_path).mine_event_candidates(records, persist=False)

    assert rules == []


def test_date_only_cutoff_joins_same_date_cluster(tmp_path) -> None:
    # Some venues publish only the fixture date; a date-only cutoff must join the
    # (single) exact-time cluster on that UTC date even though midnight is far
    # outside the start-time tolerance.
    records = [
        _record(
            record_id="cb-over",
            venue="CLOUDBET",
            event_key="soccer|team a|team b|2026-07-04T18:00:00Z",
            selection="OVER",
        ),
        _record(
            record_id="pm-under",
            venue="POLYMARKET",
            event_key="soccer|team a|team b|2026-07-04",
            selection="UNDER",
        ),
    ]
    rules = _miner(tmp_path).mine_event_candidates(records, persist=False)
    assert len(rules) == 1

    # With a doubleheader (two clusters on the same date) the date-only record is
    # ambiguous and must not pair with either leg.
    ambiguous = [
        *records,
        _record(
            record_id="cb-over-late",
            venue="CLOUDBET",
            event_key="soccer|team a|team b|2026-07-04T23:30:00Z",
            selection="OVER",
        ),
    ]
    rules = _miner(tmp_path).mine_event_candidates(ambiguous, persist=False)
    assert rules == []


def test_timeless_record_joins_only_unambiguous_family(tmp_path) -> None:
    # A record whose key carries no time segment at all joins a single-cluster
    # family...
    unambiguous = [
        _record(
            record_id="cb-over",
            venue="CLOUDBET",
            event_key="soccer|team a|team b|2026-07-04T18:00:00Z",
            selection="OVER",
        ),
        _record(
            record_id="pm-under",
            venue="POLYMARKET",
            event_key="soccer|team a|team b",
            selection="UNDER",
        ),
    ]
    rules = _miner(tmp_path).mine_event_candidates(unambiguous, persist=False)
    assert len(rules) == 1

    # ...but stays alone when the family has two time clusters (ambiguous).
    ambiguous = [
        *unambiguous,
        _record(
            record_id="cb-over-late",
            venue="CLOUDBET",
            event_key="soccer|team a|team b|2026-07-05T02:00:00Z",
            selection="OVER",
        ),
    ]
    rules = _miner(tmp_path).mine_event_candidates(ambiguous, persist=False)
    assert rules == []


def test_cross_venue_evidence_promotes_venue_agnostic_template(tmp_path) -> None:
    # Cross-venue pairing yields provider_count=2 support, which promotion turns into a
    # venue-agnostic template (the systemic unlock for cross-venue topology edges).
    records = [
        _record(
            record_id="cb-over",
            venue="CLOUDBET",
            event_key="soccer|team a|team b|2026-07-04T18:00:00Z",
            selection="OVER",
        ),
        _record(
            record_id="sx-under",
            venue="SXBET",
            event_key="soccer|team a|team b|2026-07-04T18:30:00Z",
            selection="UNDER",
        ),
    ]
    miner = _miner(tmp_path)
    templates = miner.mine_templates(records, persist=False, persist_event_candidates=False)
    assert len(templates) == 1
    assert templates[0].support.provider_count == 2

    store = RuleStore(FileRuleCache(tmp_path))
    promoted = RulePromotionPolicy().promote_template(store, templates[0])
    assert promoted is not None
    assert promoted.venue_agnostic is True
