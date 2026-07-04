# skipcq
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for generalized semantic coverage proofs.
# -------------------------------------------------------------------------------------------------

from nautilus_trader.adapters.betting.semantics import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics import CoverageBlockerReason
from nautilus_trader.adapters.betting.semantics import CoverageEngine
from nautilus_trader.adapters.betting.semantics import CoverageGap
from nautilus_trader.adapters.betting.semantics import CoverageRisk
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import NormalizedSelection
from nautilus_trader.adapters.betting.semantics import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics import OutcomeUniverse
from nautilus_trader.adapters.betting.semantics import RuleMiner
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SelectionPredicate
from nautilus_trader.adapters.betting.semantics import SelectionPredicateBuilder


def _selection(
    *,
    instrument_id: str,
    market_type: str,
    selection: str,
    params: tuple[tuple[str, str], ...] = (),
    venue: str = "CLOUDBET",
) -> NormalizedSelection:
    return NormalizedSelection(
        venue=venue,
        instrument_id=instrument_id,
        sport="soccer",
        event_key="team-a-team-b-2026-05-04",
        period="full_time",
        scope="full_time",
        market_type=market_type,
        market_family=market_type,
        selection=selection,
        params=params,
        raw_market_name=market_type,
        raw_market_type=market_type,
        raw_outcome=selection,
        outcome_key=selection,
    )


def _predicate(
    *,
    predicate_id: str,
    instrument_id: str,
    win_states: tuple[str, ...],
    result_states: tuple[str, ...],
    params: tuple[tuple[str, str], ...] = (),
) -> SelectionPredicate:
    return SelectionPredicate(
        predicate_id=predicate_id,
        instrument_id=instrument_id,
        sport="basketball",
        scope="full_time",
        market_type="WINNING_MARGIN",
        market_family="WINNING_MARGIN",
        selection=instrument_id,
        params=params,
        result_states=result_states,
        win_states=win_states,
        lose_states=tuple(state for state in result_states if state not in win_states),
        provider="CLOUDBET",
        event_key="basketball-event-1",
    )


def test_binary_complements_produce_complete_coverage_same_venue_dry_run_only():
    over = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="over-25",
            market_type=CanonicalMarketType.TOTALS.value,
            selection="OVER",
            params=(("line", "2.5"),),
        ),
    )
    under = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="under-25",
            market_type=CanonicalMarketType.TOTALS.value,
            selection="UNDER",
            params=(("line", "2.5"),),
        ),
    )

    proof = CoverageEngine().evaluate((over, under))

    assert proof.complete is True
    assert proof.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value
    assert proof.same_venue_execution_eligible is True
    assert proof.execution_safe is False
    assert not proof.gaps


def test_whole_total_complement_is_coverage_safe_because_push_state_exists():
    over = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="over-2",
            market_type=CanonicalMarketType.TOTALS.value,
            selection="OVER",
            params=(("line", "2"),),
        ),
    )
    under = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="under-2",
            market_type=CanonicalMarketType.TOTALS.value,
            selection="UNDER",
            params=(("line", "2"),),
        ),
    )

    proof = CoverageEngine().evaluate((over, under))

    assert proof.complete is False
    assert CoverageBlockerReason.INCOMPLETE_COVERAGE.value in proof.blocker_reasons
    assert CoverageBlockerReason.VOID_SETTLEMENT.value in proof.blocker_reasons
    assert proof.execution_safe is False


def test_three_way_full_book_is_detected_as_hyperedge(tmp_path):
    records = [
        NormalizedSelectionRecord(
            record_id=f"record-{selection.lower()}",
            provider="CLOUDBET",
            selection=_selection(
                instrument_id=f"match-{selection.lower()}",
                market_type=CanonicalMarketType.MATCH_ODDS.value,
                selection=selection,
            ),
        )
        for selection in ("HOME", "DRAW", "AWAY")
    ]

    proofs, hyperedges = RuleMiner(RuleStore(FileRuleCache(tmp_path))).mine_coverage(
        records,
        persist=False,
    )

    complete_three_way = [
        proof for proof in proofs if proof.complete and len(proof.predicates) == 3
    ]
    assert len(complete_three_way) == 1
    assert len(hyperedges) == 1
    assert not complete_three_way[0].gaps


def test_incomplete_full_book_reports_uncovered_draw_state():
    home = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="match-home",
            market_type=CanonicalMarketType.MATCH_ODDS.value,
            selection="HOME",
        ),
    )
    away = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="match-away",
            market_type=CanonicalMarketType.MATCH_ODDS.value,
            selection="AWAY",
        ),
    )
    universe = OutcomeUniverse.from_state_ids(
        sport="soccer",
        scope="full_time",
        state_ids=("HOME_WIN", "DRAW", "AWAY_WIN"),
    )

    proof = CoverageEngine().evaluate((home, away), universe=universe)

    assert proof.complete is False
    assert (
        CoverageGap(
            state_id="DRAW",
            reason=CoverageBlockerReason.INCOMPLETE_COVERAGE.value,
            detail="No selection wins on this outcome state.",
        )
        in proof.gaps
    )
    assert proof.safety_tier == SafetyTier.AUDIT_ONLY.value


def test_range_bucket_overlap_is_reported_as_coverage_risk():
    result_states = ("MARGIN_0_9", "MARGIN_10_19", "MARGIN_20_PLUS")
    low = _predicate(
        predicate_id="low",
        instrument_id="margin-0-19",
        win_states=("MARGIN_0_9", "MARGIN_10_19"),
        result_states=result_states,
        params=(("range", "0-19"),),
    )
    mid = _predicate(
        predicate_id="mid",
        instrument_id="margin-10-19",
        win_states=("MARGIN_10_19",),
        result_states=result_states,
        params=(("range", "10-19"),),
    )
    high = _predicate(
        predicate_id="high",
        instrument_id="margin-20-plus",
        win_states=("MARGIN_20_PLUS",),
        result_states=result_states,
        params=(("range", "20+"),),
    )

    proof = CoverageEngine().evaluate((low, mid, high))

    assert proof.complete is True
    assert "MARGIN_10_19" in proof.overlapping_win_states
    assert (
        CoverageRisk(
            reason=CoverageBlockerReason.OVERLAPPING_COVERAGE.value,
            state_id="MARGIN_10_19",
            detail="More than one selection wins on this outcome state.",
            severity="risk",
        )
        in proof.risks
    )
    assert proof.safety_tier == SafetyTier.COVERAGE_SAFE.value


def test_correct_score_basket_reports_missing_catch_all_state():
    universe = OutcomeUniverse.from_state_ids(
        sport="soccer",
        scope="full_time",
        state_ids=("SCORE_1_0", "SCORE_1_1", "ANY_OTHER_HOME_WIN"),
    )
    score_1_0 = _predicate(
        predicate_id="score-1-0",
        instrument_id="score-1-0",
        win_states=("SCORE_1_0",),
        result_states=universe.state_ids,
        params=(("score", "1-0"),),
    )
    score_1_1 = _predicate(
        predicate_id="score-1-1",
        instrument_id="score-1-1",
        win_states=("SCORE_1_1",),
        result_states=universe.state_ids,
        params=(("score", "1-1"),),
    )

    proof = CoverageEngine().evaluate((score_1_0, score_1_1), universe=universe)

    assert proof.complete is False
    assert (
        CoverageGap(
            state_id="ANY_OTHER_HOME_WIN",
            reason=CoverageBlockerReason.INCOMPLETE_COVERAGE.value,
            detail="No selection wins on this outcome state.",
        )
        in proof.gaps
    )


def test_set_score_full_book_uses_same_coverage_model():
    universe = OutcomeUniverse.from_state_ids(
        sport="tennis",
        scope="match_best_of_3",
        state_ids=("HOME_2_0", "HOME_2_1", "AWAY_2_0", "AWAY_2_1"),
    )
    predicates = tuple(
        _predicate(
            predicate_id=f"set-{state.lower()}",
            instrument_id=f"set-{state.lower()}",
            win_states=(state,),
            result_states=universe.state_ids,
            params=(("set_score", state),),
        )
        for state in universe.state_ids
    )

    proof = CoverageEngine().evaluate(predicates, universe=universe)

    assert proof.complete is True
    assert not proof.gaps
    assert proof.same_venue_execution_eligible is True


def test_correct_score_records_mine_bucket_coverage_from_realized_selection_labels(tmp_path):
    records = [
        NormalizedSelectionRecord(
            record_id=f"record-{selection.lower()}",
            provider="CLOUDBET",
            selection=_selection(
                instrument_id=f"score-{selection.lower()}",
                market_type=CanonicalMarketType.CORRECT_SCORE.value,
                selection=selection,
            ),
        )
        for selection in ("SCORE_1_0", "SCORE_2_0", "ANY_OTHER_HOME_WIN")
    ]

    proofs, hyperedges = RuleMiner(RuleStore(FileRuleCache(tmp_path))).mine_coverage(
        records,
        persist=False,
    )

    assert any(proof.complete for proof in proofs)
    assert len(hyperedges) == 1


def test_other_bucket_market_records_mine_full_book_coverage(tmp_path):
    records = [
        NormalizedSelectionRecord(
            record_id=f"record-{selection.lower()}",
            provider="CLOUDBET",
            selection=NormalizedSelection(
                venue="CLOUDBET",
                instrument_id=f"inning-{selection.lower()}",
                sport="baseball",
                event_key="baseball-event-1",
                period="overtime",
                scope="overtime",
                market_type=CanonicalMarketType.OTHER.value,
                market_family=CanonicalMarketType.OTHER.value,
                selection=selection,
                params=(("period", "ft|ot|innings1|inning2|inning3"),),
                raw_market_name="baseball.highest_scoring_inning",
                raw_market_type="baseball.highest_scoring_inning",
                raw_outcome=selection.lower(),
                outcome_key=selection.lower(),
            ),
        )
        for selection in ("1ST_INNING", "2ND_INNING", "3RD_INNING")
    ]

    proofs, hyperedges = RuleMiner(RuleStore(FileRuleCache(tmp_path))).mine_coverage(
        records,
        persist=False,
    )

    assert any(proof.complete for proof in proofs)
    assert len(hyperedges) == 1


def test_other_bucket_markets_do_not_merge_across_distinct_raw_market_keys(tmp_path):
    records: list[NormalizedSelectionRecord] = []
    for market_name in (
        "baseball.highest_scoring_inning",
        "baseball.exact_goals",
    ):
        for selection in ("1ST_INNING", "2ND_INNING"):
            records.append(
                NormalizedSelectionRecord(
                    record_id=f"{market_name}-{selection.lower()}",
                    provider="CLOUDBET",
                    selection=NormalizedSelection(
                        venue="CLOUDBET",
                        instrument_id=f"{market_name}-{selection.lower()}",
                        sport="baseball",
                        event_key="baseball-event-2",
                        period="full_time",
                        scope="full_time",
                        market_type=CanonicalMarketType.OTHER.value,
                        market_family=CanonicalMarketType.OTHER.value,
                        selection=selection,
                        params=(),
                        raw_market_name=market_name,
                        raw_market_type=market_name,
                        raw_outcome=selection.lower(),
                        outcome_key=selection.lower(),
                    ),
                ),
            )

    proofs, _ = RuleMiner(RuleStore(FileRuleCache(tmp_path))).mine_coverage(
        records,
        persist=False,
    )

    complete_two_leg = [proof for proof in proofs if proof.complete and len(proof.predicates) == 2]

    assert len(complete_two_leg) == 2
    assert {
        tuple(sorted(predicate.instrument_id for predicate in proof.predicates))
        for proof in complete_two_leg
    } == {
        (
            "baseball.exact_goals-1st_inning",
            "baseball.exact_goals-2nd_inning",
        ),
        (
            "baseball.highest_scoring_inning-1st_inning",
            "baseball.highest_scoring_inning-2nd_inning",
        ),
    }


def test_halftime_fulltime_bucket_market_mines_complete_hyperedge(tmp_path):
    records = [
        NormalizedSelectionRecord(
            record_id=f"record-{selection.lower()}",
            provider="CLOUDBET",
            selection=NormalizedSelection(
                venue="CLOUDBET",
                instrument_id=f"htft-{selection.lower()}",
                sport="soccer",
                event_key="soccer-event-htft-1",
                period="full_time",
                scope="full_time",
                market_type=CanonicalMarketType.OTHER.value,
                market_family=CanonicalMarketType.OTHER.value,
                selection=selection,
                params=(("period", "ft|1h"),),
                raw_market_name="soccer.halftime_fulltime_result",
                raw_market_type="soccer.halftime_fulltime_result",
                raw_outcome=selection.lower(),
                outcome_key=selection.lower(),
            ),
        )
        for selection in (
            "HOME_HOME",
            "DRAW_HOME",
            "AWAY_HOME",
            "HOME_DRAW",
            "DRAW_DRAW",
            "AWAY_DRAW",
            "HOME_AWAY",
            "DRAW_AWAY",
            "AWAY_AWAY",
        )
    ]

    proofs, hyperedges = RuleMiner(RuleStore(FileRuleCache(tmp_path))).mine_coverage(
        records,
        persist=False,
    )

    assert any(proof.complete and len(proof.predicates) == 9 for proof in proofs)
    assert len(hyperedges) == 1


def test_winning_margin_bucket_market_mines_complete_hyperedge(tmp_path):
    records = [
        NormalizedSelectionRecord(
            record_id=f"record-{selection.lower()}",
            provider="CLOUDBET",
            selection=NormalizedSelection(
                venue="CLOUDBET",
                instrument_id=f"margin-{selection.lower()}",
                sport="basketball",
                event_key="basketball-event-margin-1",
                period="overtime",
                scope="overtime",
                market_type=CanonicalMarketType.OTHER.value,
                market_family=CanonicalMarketType.OTHER.value,
                selection=selection,
                params=(("period", "ot"),),
                raw_market_name="basketball.winning_margin",
                raw_market_type="basketball.winning_margin",
                raw_outcome=selection.lower(),
                outcome_key=selection.lower(),
            ),
        )
        for selection in (
            "HOME_BY_6_PLUS",
            "OTHER",
            "AWAY_BY_6_PLUS",
        )
    ]

    proofs, hyperedges = RuleMiner(RuleStore(FileRuleCache(tmp_path))).mine_coverage(
        records,
        persist=False,
    )

    assert any(proof.complete and len(proof.predicates) == 3 for proof in proofs)
    assert len(hyperedges) == 1


def test_cross_venue_binary_complement_produces_distinct_cross_venue_basket():
    records = [
        NormalizedSelectionRecord(
            record_id=f"{provider.lower()}-{selection.lower()}",
            provider=provider,
            selection=_selection(
                instrument_id=f"{provider.lower()}-{selection.lower()}-25",
                market_type=CanonicalMarketType.TOTALS.value,
                selection=selection,
                params=(("line", "2.5"),),
                venue=provider,
            ),
        )
        for provider in ("CLOUDBET", "POLYMARKET")
        for selection in ("OVER", "UNDER")
    ]

    proofs, _ = CoverageEngine().discover_event_coverage(records)

    cross_venue = [
        proof
        for proof in proofs
        if proof.complete and len(proof.coverage_set.provider_scope) == 2
    ]
    assert cross_venue, "expected a genuine cross-venue OVER/UNDER coverage basket"
    assert any(proof.execution_safe for proof in cross_venue)
    assert any(
        proof.coverage_set.provider_scope == ("CLOUDBET", "POLYMARKET")
        for proof in cross_venue
    )


def test_coverage_proofs_round_trip_through_rule_store(tmp_path):
    home = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="home",
            market_type=CanonicalMarketType.WINNER.value,
            selection="HOME",
        ),
        provider="CLOUDBET",
    )
    away = SelectionPredicateBuilder.from_selection(
        _selection(
            instrument_id="away",
            market_type=CanonicalMarketType.WINNER.value,
            selection="AWAY",
            venue="SXBET",
        ),
        provider="SXBET",
    )
    proof = CoverageEngine().evaluate((home, away))
    store = RuleStore(FileRuleCache(tmp_path))

    store.save_coverage_proof(proof)
    loaded = store.load_coverage_proof(proof.proof_id)

    assert loaded == proof
    assert store.list_coverage_proof_ids() == [proof.proof_id]
