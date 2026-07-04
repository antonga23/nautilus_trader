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
    # Both venues' records share one fixture-bucket identity (family + cluster anchor).
    assert complementary[0].evidence_event_key.startswith("soccer|team a|team b|")
    assert "@" in complementary[0].evidence_event_key


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
    # ONE physical fixture across two venues must count as ONE event, not inflate the
    # event_count diversity gate that guards EXECUTION_SAFE promotion.
    assert templates[0].support.event_count == 1

    store = RuleStore(FileRuleCache(tmp_path))
    promoted = RulePromotionPolicy().promote_template(store, templates[0])
    assert promoted is not None
    assert promoted.venue_agnostic is True


def test_event_count_not_inflated_by_timestamp_precision(tmp_path) -> None:
    # The same fixture listed with exact + date-only + no-time keys across venues is
    # still one fixture: event_count must be 1 (regression for evidence-key inflation).
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
            event_key="soccer|team a|team b|2026-07-04T18:20:00Z",
            selection="UNDER",
        ),
        _record(
            record_id="pm-under",
            venue="POLYMARKET",
            event_key="soccer|team a|team b|2026-07-04",
            selection="UNDER",
        ),
    ]
    templates = _miner(tmp_path).mine_templates(
        records,
        persist=False,
        persist_event_candidates=False,
    )
    # OVER+UNDER (complementary) and UNDER+UNDER (equivalent) are two template shapes;
    # what matters is that each counts the fixture ONCE, not thrice.
    complementary = [
        t for t in templates if t.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    ]
    assert len(complementary) == 1
    assert complementary[0].support.event_count == 1


def test_chained_clusters_do_not_transitively_merge(tmp_path) -> None:
    # Anchor is fixed at each cluster's first member (matches the Rust runtime graph):
    # 12:00 and 13:30 are within 2h so they merge; 15:00 is >2h from the 12:00 anchor
    # (though <2h from 13:30) so it must open a SEPARATE fixture, not chain in.
    records = [
        _record(
            record_id="a-1200",
            venue="CLOUDBET",
            event_key="baseball|team a|team b|2026-07-04T12:00:00Z",
            selection="OVER",
        ),
        _record(
            record_id="b-1330",
            venue="SXBET",
            event_key="baseball|team a|team b|2026-07-04T13:30:00Z",
            selection="UNDER",
        ),
        _record(
            record_id="c-1500",
            venue="POLYMARKET",
            event_key="baseball|team a|team b|2026-07-04T15:00:00Z",
            selection="UNDER",
        ),
    ]
    rules = _miner(tmp_path).mine_event_candidates(records, persist=False)
    # 12:00 OVER pairs with 13:30 UNDER (one bucket); 15:00 UNDER is its own bucket with
    # no OVER counterpart -> exactly one complementary rule, keyed to the first cluster.
    complementary = [
        r for r in rules if r.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    ]
    assert len(complementary) == 1
    keys = {r.evidence_event_key for r in complementary}
    assert len(keys) == 1


def test_two_hour_boundary_is_inclusive(tmp_path) -> None:
    # Exactly 2h apart is within tolerance (<=) -> same fixture, one complementary rule.
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
            event_key="soccer|team a|team b|2026-07-04T20:00:00Z",
            selection="UNDER",
        ),
    ]
    rules = _miner(tmp_path).mine_event_candidates(records, persist=False)
    complementary = [
        r for r in rules if r.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    ]
    assert len(complementary) == 1
