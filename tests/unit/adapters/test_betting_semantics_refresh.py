# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the Cloudbet-backed semantic mining refresh.
# -------------------------------------------------------------------------------------------------
import asyncio
from dataclasses import replace
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import msgspec

import pytest

from nautilus_trader.adapters.betting.semantics import completion as completion_module
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.semantics import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics import CorpusSnapshot
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import HistoricalRuleValidator
from nautilus_trader.adapters.betting.semantics import MarketNormalizer
from nautilus_trader.adapters.betting.semantics import NormalizedSelection
from nautilus_trader.adapters.betting.semantics import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics import PayoffVectorBuilder
from nautilus_trader.adapters.betting.semantics import PolymarketSportsTransformer
from nautilus_trader.adapters.betting.semantics import RelationshipType
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics import RuleMiner
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import RuleValidationStats
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import SettlementState
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.adapters.betting.semantics.corpus import SnapshotIngestor
from nautilus_trader.adapters.betting.semantics import build_completion_report
from nautilus_trader.adapters.betting.semantics import corpus as corpus_module
from nautilus_trader.adapters.cloudbet.client.schema import Selection
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from tests import TESTS_PACKAGE_ROOT


class DictCache:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def add(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


def betting_instrument(
    *,
    sport: str = "soccer",
    market_name: str,
    market_type: str,
    outcome: str,
    event_name: str = "Team A vs Team B",
    home_name: str = "Team A",
    away_name: str = "Team B",
    params: str = "",
    venue: str = "SXBET",
    price: float = 2.1,
    handicap: float | None = None,
    info: dict | None = None,
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id="event-1",
        event_name=event_name,
        home_name=home_name,
        away_name=away_name,
        sport_name=sport,
        competition_name="Test League",
        market_name=market_name,
        market_type=market_type,
        outcome=outcome,
        side=SelectionSide.BACK,
        price=price,
        currency=Currency.from_str("USDC"),
        params=params,
        handicap=handicap,
        start_time="2026-03-13T18:00:00Z",
        info=info or {},
    )


def polymarket_binary_option(
    *,
    symbol: str,
    outcome: str,
    question: str,
    info: dict,
) -> BinaryOption:
    return BinaryOption(
        instrument_id=InstrumentId(Symbol(symbol), Venue("POLYMARKET")),
        raw_symbol=Symbol(symbol),
        outcome=outcome,
        description=question,
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=Price.from_str("0.001"),
        price_precision=3,
        size_increment=Quantity.from_str("0.000001"),
        size_precision=6,
        activation_ns=0,
        expiration_ns=1,
        max_quantity=None,
        min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
        info=info,
    )


def _load_cloudbet_selection(filename: str, index: int) -> Selection:
    path = (
        TESTS_PACKAGE_ROOT / "integration_tests" / "adapters" / "cloudbet" / "resources" / filename
    )
    selections = msgspec.json.decode(path.read_bytes(), type=list[Selection])
    return selections[index]


def test_cloudbet_basketball_spread_normalizes_from_fixture():
    normalizer = MarketNormalizer()
    selection = _load_cloudbet_selection("basketball_selections.json", 3)

    normalized = normalizer.normalize(selection)

    assert normalized.market_type == CanonicalMarketType.POINT_SPREAD.value
    assert normalized.selection == "HOME"
    # Whole-game period=ot&period=ft is the full-time market; the overtime nuance is
    # carried by the includes_overtime rules_flag, not the scope, so the corpus record
    # keeps the same identity as the live instrument (which has no period token).
    assert normalized.scope == "full_time"
    assert normalized.param("period") is None
    assert "includes_overtime" in normalized.rules_flags
    assert normalized.param("line") == "3.5"
    assert normalized.param("handicap") is None


def _semantic_identity(selection: NormalizedSelection) -> tuple[str, ...]:
    # Mirror the runtime node/template match key (runner._template_pattern_key and the
    # semantic node identity dict): rules_flags are metadata, not part of the identity.
    return (
        selection.sport,
        selection.scope,
        selection.market_type,
        selection.market_family,
        selection.selection,
        json.dumps(list(selection.params), sort_keys=True, separators=(",", ":")),
    )


def test_cloudbet_whole_game_corpus_identity_matches_live_and_sxbet():
    normalizer = MarketNormalizer()
    cb_corpus = normalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "baseball",
            "event_id": "evt-1",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "baseball.moneyline",
            "market_type": "baseball.moneyline",
            "submarket_period": "period=ot&period=ft",
            "outcome": "home",
        },
    )
    cb_live = normalizer.normalize(
        betting_instrument(
            sport="baseball",
            market_name="baseball.moneyline",
            market_type="baseball.moneyline",
            outcome="home",
            venue="CLOUDBET",
        ),
    )
    sxbet_match_odds = normalizer.normalize(
        {
            "provider": "SXBET",
            "sport_name": "baseball",
            "event_id": "evt-1",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "match_odds",
            "market_type": "match_odds",
            "outcome": "home",
            "raw_market_type": 52,
            "info": {
                "raw_market_type": 52,
                "is_two_way_market": True,
                "sxbet_market_hash": "h-home",
            },
        },
    )

    assert cb_corpus.scope == "full_time"
    assert cb_corpus.param("period") is None
    # (a) The corpus record's matching identity is byte-identical to the live instrument
    # it must hedge against, and to the equivalent SXBET two-way market.
    assert _semantic_identity(cb_corpus) == _semantic_identity(cb_live)
    assert _semantic_identity(cb_corpus) == _semantic_identity(sxbet_match_odds)
    # (b) The overtime nuance survives as a rules_flag, not in the identity, so it does
    # not re-introduce the divergence it used to encode in the scope.
    assert "includes_overtime" in cb_corpus.rules_flags
    assert "includes_overtime" not in cb_live.rules_flags


def test_cloudbet_multi_inning_submarket_uses_stable_deterministic_scope():
    normalizer = MarketNormalizer()

    def scope_for(order: str) -> str:
        return normalizer.normalize(
            {
                "provider": "CLOUDBET",
                "sport_name": "baseball",
                "event_id": "evt-1",
                "event_name": "Team A vs Team B",
                "home_name": "Team A",
                "away_name": "Team B",
                "market_name": "baseball.moneyline_innings_1_to_5",
                "market_type": "baseball.moneyline_innings_1_to_5",
                "submarket_period": order,
                "outcome": "home",
            },
        ).scope

    orderings = {
        scope_for("period=inning1&period=inning2&period=inning3&period=inning4&period=inning5"),
        scope_for("period=inning5&period=inning3&period=inning1&period=inning4&period=inning2"),
    }
    # (c) A genuine sub-game market keeps a distinct scope, and the multi-inning span
    # resolves to one stable value regardless of set iteration order (was arbitrary).
    assert orderings == {"innings_1_to_5"}

    single_inning = normalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "baseball",
            "event_id": "evt-1",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "baseball.inning_winner",
            "market_type": "baseball.inning_winner",
            "submarket_period": "period=inning3",
            "outcome": "home",
        },
    )
    assert single_inning.scope == "inning_3"


def test_classifier_skips_mixed_scope_pairs_and_counts_them():
    classifier = RuleClassifier()
    normalizer = MarketNormalizer()
    full_time = normalizer.normalize(
        betting_instrument(
            sport="baseball",
            market_name="baseball.moneyline",
            market_type="baseball.moneyline",
            outcome="home",
            venue="CLOUDBET",
        ),
    )
    first_half = replace(full_time, scope="first_half", period="first_half")

    assert classifier.scope_mismatch_skips == 0
    # (d) A scope-mismatched pair is skipped (never becomes a scope="mixed" template)
    # and the skip is counted.
    assert classifier.classify(full_time, first_half) is None
    assert classifier.scope_mismatch_skips == 1
    # Equal-scope pairs still classify, and do not increment the counter.
    assert classifier.classify(full_time, full_time) is not None
    assert classifier.scope_mismatch_skips == 1


def test_mini_mine_yields_cross_venue_template_for_co_quoted_fixture(tmp_path):
    normalizer = MarketNormalizer()
    home = "Arizona Diamondbacks"
    away = "Los Angeles Dodgers"
    start = "2026-07-12T02:40:00Z"

    def cb_record(outcome: str, index: int) -> NormalizedSelectionRecord:
        selection = normalizer.normalize(
            {
                "provider": "CLOUDBET",
                "sport_name": "baseball",
                "event_id": "cb-1",
                "event_name": f"{home} vs {away}",
                "home_name": home,
                "away_name": away,
                "cutoff_time": start,
                "market_name": "baseball.moneyline",
                "market_type": "baseball.moneyline",
                "submarket_period": "period=ot&period=ft",
                "outcome": outcome,
            },
        )
        return NormalizedSelectionRecord(
            record_id=f"cb-{index}",
            provider="CLOUDBET",
            selection=selection,
        )

    def sxbet_record(outcome: str, index: int) -> NormalizedSelectionRecord:
        selection = normalizer.normalize(
            {
                "provider": "SXBET",
                "sport_name": "baseball",
                "event_id": "sx-1",
                "event_name": f"{home} vs {away}",
                "home_name": home,
                "away_name": away,
                "cutoff_time": start,
                "market_name": "match_odds",
                "market_type": "match_odds",
                "outcome": outcome,
                "raw_market_type": 52,
                "info": {
                    "raw_market_type": 52,
                    "is_two_way_market": True,
                    "sxbet_market_hash": f"h-{outcome}",
                },
            },
        )
        return NormalizedSelectionRecord(
            record_id=f"sx-{index}",
            provider="SXBET",
            selection=selection,
        )

    records = [
        cb_record("home", 0),
        cb_record("away", 1),
        sxbet_record("home", 0),
        sxbet_record("away", 1),
    ]
    miner = RuleMiner(RuleStore(FileRuleCache(tmp_path)))
    templates = miner.mine_templates(
        records,
        persist=False,
        persist_event_candidates=False,
    )

    # (e) Before the corpus/live scope reconciliation, CloudBet records bucketed at
    # full_time_including_overtime and never co-bucketed with SXBET's full_time, so no
    # venue-spanning template could exist. Now the same fixture mines one.
    cross_venue = [
        template for template in templates if set(template.provider_scope) == {"CLOUDBET", "SXBET"}
    ]
    assert cross_venue
    assert all(template.scope == "full_time" for template in cross_venue)
    assert any(template.safety_tier == SafetyTier.EXECUTION_SAFE.value for template in cross_venue)


def test_cloudbet_away_spread_normalizes_home_relative_line_to_selection_line():
    normalizer = MarketNormalizer()
    home = normalizer.normalize(_load_cloudbet_selection("basketball_selections.json", 3))
    away = normalizer.normalize(_load_cloudbet_selection("basketball_selections.json", 4))

    assert away.market_type == CanonicalMarketType.POINT_SPREAD.value
    assert away.selection == "AWAY"
    assert away.param("line") == "-3.5"
    assert away.param("handicap") is None
    rule = RuleClassifier().classify(home, away)
    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value


def test_cloudbet_tennis_total_games_normalizes_as_totals_and_full_scope():
    normalizer = MarketNormalizer()

    normalized = normalizer.normalize(
        {
            "provider": "CLOUDBET",
            "event_id": "cb-tennis-1",
            "event_name": "Andre Ilagan vs Dane Sweeny",
            "home_name": "Andre Ilagan",
            "away_name": "Dane Sweeny",
            "sport_key": "tennis",
            "cutoff_time": "2026-05-06T08:00:00Z",
            "market_url": "tennis.total_games/over?line=22&period=default&period=set1&period=set2&period=set3&period=wo",
            "market_name": "tennis.total_games",
            "market_type": "tennis.total_games",
            "outcome": "over",
        },
    )

    assert normalized.market_type == CanonicalMarketType.TOTALS.value
    assert normalized.market_family == CanonicalMarketType.TOTALS.value
    assert normalized.selection == "OVER"
    assert normalized.scope == "full_time"
    assert normalized.param("line") == "22"


def test_cloudbet_tennis_total_games_in_set_prefers_set_scope_over_walkover_flag():
    normalizer = MarketNormalizer()

    normalized = normalizer.normalize(
        {
            "provider": "CLOUDBET",
            "event_id": "cb-tennis-2",
            "event_name": "Andre Ilagan vs Dane Sweeny",
            "home_name": "Andre Ilagan",
            "away_name": "Dane Sweeny",
            "sport_key": "tennis",
            "cutoff_time": "2026-05-06T08:00:00Z",
            "market_url": "tennis.total_games_in_set/under?line=9.5&period=set1&period=wo&set=1",
            "market_name": "tennis.total_games_in_set",
            "market_type": "tennis.total_games_in_set",
            "outcome": "under",
        },
    )

    assert normalized.market_type == CanonicalMarketType.TOTALS.value
    assert normalized.selection == "UNDER"
    assert normalized.scope == "set1"
    assert normalized.param("set") == "1"


def test_cloudbet_second_half_total_prefers_second_half_scope_over_overtime_flag():
    normalizer = MarketNormalizer()

    normalized = normalizer.normalize(
        {
            "provider": "CLOUDBET",
            "event_id": "cb-basketball-1",
            "event_name": "BC Zalgiris Kaunas vs Fenerbahce Istanbul",
            "home_name": "BC Zalgiris Kaunas",
            "away_name": "Fenerbahce Istanbul",
            "sport_key": "basketball",
            "cutoff_time": "2026-05-06T17:00:00Z",
            "market_url": "basketball.total_period_second_half/over?line=80.5&period=ot&period=2h&period=q1&period=q2",
            "market_name": "basketball.total_period_second_half",
            "market_type": "basketball.total_period_second_half",
            "outcome": "over",
        },
    )

    assert normalized.market_type == CanonicalMarketType.TOTALS.value
    assert normalized.selection == "OVER"
    assert normalized.scope == "second_half"
    assert "includes_overtime" in normalized.rules_flags


def test_cloudbet_team_win_to_nil_normalizes_as_winner_binary_market():
    normalized = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Soccer",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "soccer.team_win_to_nil",
            "market_type": "soccer.team_win_to_nil",
            "outcome": "yes",
            "params": "period=ft&team=home",
        },
    )

    assert normalized.market_type == CanonicalMarketType.WINNER.value
    assert normalized.selection == "YES"
    assert normalized.scope == "full_time"
    assert dict(normalized.params)["team"] == "home"


def test_cloudbet_team_to_win_a_set_normalizes_as_winner_binary_market():
    normalized = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Tennis",
            "event_name": "Player A vs Player B",
            "home_name": "Player A",
            "away_name": "Player B",
            "market_name": "tennis.team_to_win_a_set",
            "market_type": "tennis.team_to_win_a_set",
            "outcome": "no",
            "params": "period=default|set1|set2|set3|set4|set5|wo&team=away",
        },
    )

    assert normalized.market_type == CanonicalMarketType.WINNER.value
    assert normalized.selection == "NO"
    assert normalized.scope == "winner_only"
    assert dict(normalized.params)["team"] == "away"


@pytest.mark.parametrize(
    ("sport_name", "market_name", "params"),
    [
        ("Soccer", "soccer.team_clean_sheet", "period=ft&team=home"),
        ("Tennis", "tennis.any_set_to_nil", "period=default|set1|set2|set3|set4|set5|wo"),
        ("Basketball", "basketball.team_to_lead_by_points", "period=ot&team=away&points=12"),
        ("Basketball", "basketball.any_team_to_lead_by_points", "period=ot&points=12"),
    ],
)
def test_additional_deterministic_yes_no_families_normalize_as_winner_markets(
    sport_name,
    market_name,
    params,
):
    normalized = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": sport_name,
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": market_name,
            "market_type": market_name,
            "outcome": "yes",
            "params": params,
        },
    )

    assert normalized.market_type == CanonicalMarketType.WINNER.value
    assert normalized.selection == "YES"


def test_cloudbet_with_extra_inning_prop_normalizes_as_other_not_winner():
    # "Will there be an extra inning" is a fixture proposition, not a winner market:
    # classified as WINNER its YES/NO selections shadowed the true moneyline and could
    # never pair with SXBET match_odds HOME/AWAY, keeping the baseball venue pair dead.
    normalized = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Baseball",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "baseball.with_extra_inning",
            "market_type": "baseball.with_extra_inning",
            "outcome": "yes",
            "params": "period=ft|ot",
        },
    )

    assert normalized.market_type == CanonicalMarketType.OTHER.value
    assert normalized.market_family == CanonicalMarketType.OTHER.value
    assert normalized.selection == "YES"


def test_cloudbet_baseball_moneyline_normalizes_as_winner_home_away():
    normalized = {
        outcome: MarketNormalizer.normalize(
            {
                "provider": "CLOUDBET",
                "sport_name": "Baseball",
                "event_name": "Team A vs Team B",
                "home_name": "Team A",
                "away_name": "Team B",
                "market_name": "baseball.moneyline",
                "market_type": "baseball.moneyline",
                "outcome": outcome,
                "params": "period=ot&period=ft",
            },
        )
        for outcome in ("home", "away")
    }

    assert {item.market_type for item in normalized.values()} == {
        CanonicalMarketType.WINNER.value,
    }
    assert normalized["home"].selection == "HOME"
    assert normalized["away"].selection == "AWAY"


def test_team_scoped_binary_winner_payoffs_preserve_team_axis():
    builder = PayoffVectorBuilder()
    classifier = RuleClassifier()
    home_yes = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Soccer",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "soccer.team_win_to_nil",
            "market_type": "soccer.team_win_to_nil",
            "outcome": "yes",
            "params": "period=ft&team=home",
        },
    )
    home_no = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Soccer",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "soccer.team_win_to_nil",
            "market_type": "soccer.team_win_to_nil",
            "outcome": "no",
            "params": "period=ft&team=home",
        },
    )
    away_yes = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Soccer",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "soccer.team_win_to_nil",
            "market_type": "soccer.team_win_to_nil",
            "outcome": "yes",
            "params": "period=ft&team=away",
        },
    )

    home_vector = builder.build(home_yes)
    away_vector = builder.build(away_yes)

    assert home_vector.result_states == ("HOME_EVENT_TRUE", "HOME_EVENT_FALSE")
    assert away_vector.result_states == ("AWAY_EVENT_TRUE", "AWAY_EVENT_FALSE")
    assert classifier.classify(home_yes, home_no) is not None
    assert classifier.classify(home_yes, away_yes) is None


def test_cloudbet_halftime_fulltime_selection_preserves_bucket_order():
    normalized = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Soccer",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "soccer.halftime_fulltime_result",
            "market_type": "soccer.halftime_fulltime_result",
            "outcome": "draw_away",
            "params": "period=ft|1h",
        },
    )

    assert normalized.market_type == CanonicalMarketType.OTHER.value
    assert normalized.selection == "DRAW_AWAY"


def test_cloudbet_winning_margin_selection_decodes_query_encoded_outcome():
    normalized = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Basketball",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "basketball.winning_margin",
            "market_type": "basketball.winning_margin",
            "outcome": "outcome=%7B%7Bhome%7D%7D%20by%206%2B",
            "params": "period=ot",
        },
    )

    assert normalized.selection == "HOME_BY_6_PLUS"


def test_cloudbet_highest_scoring_quarter_uses_full_game_scope_for_multi_quarter_market():
    normalized = MarketNormalizer.normalize(
        {
            "provider": "CLOUDBET",
            "sport_name": "Basketball",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "market_name": "basketball.highest_scoring_quarter",
            "market_type": "basketball.highest_scoring_quarter",
            "outcome": "1st_quarter",
            "params": "period=ot|q1|q2|q3|q4",
        },
    )

    assert normalized.scope == "full_time_including_overtime"


@pytest.mark.parametrize(
    ("sport", "market_name", "market_type"),
    [
        ("soccer", "soccer.match_odds", "soccer.1x2"),
        ("basketball", "basketball.winner", "basketball.winner"),
        ("tennis", "tennis.winner", "tennis.winner"),
        ("american_football", "american_football.winner", "american_football.winner"),
        ("ice_hockey", "ice_hockey.winner", "ice_hockey.winner"),
        ("baseball", "baseball.winner", "baseball.winner"),
    ],
)
def test_payoff_builder_supports_six_target_sport_winner_families(
    sport,
    market_name,
    market_type,
):
    normalized = MarketNormalizer().normalize(
        betting_instrument(
            sport=sport,
            market_name=market_name,
            market_type=market_type,
            outcome="home",
            params="period=ft",
        ),
    )

    vector = PayoffVectorBuilder().build(normalized)

    assert vector.sport == sport
    assert SettlementState.UNKNOWN.value not in vector.settlement
    assert SettlementState.WIN.value in vector.settlement
    assert SettlementState.LOSE.value in vector.settlement


def test_cloudbet_soccer_quarter_handicap_preserves_arbitrary_line():
    normalizer = MarketNormalizer()
    selection = _load_cloudbet_selection("soccer_selections.json", 2)

    normalized = normalizer.normalize(selection)
    rule = RuleClassifier().classify(
        normalized,
        betting_instrument(
            sport="soccer",
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="away",
            params="line=0.25",
            handicap=0.25,
        ),
    )

    assert normalized.market_type == CanonicalMarketType.ASIAN_HANDICAP.value
    assert normalized.param("line") == "-0.25"
    assert normalized.param("handicap") is None
    assert rule is not None
    assert rule.relationship_type == RelationshipType.PARTIAL_SETTLEMENT_HEDGE.value


def test_cross_provider_event_key_is_provider_agnostic():
    normalizer = MarketNormalizer()
    cloudbet = normalizer.normalize(
        {
            "provider": "CLOUDBET",
            "event_id": "cb-100",
            "event_name": "Team A vs Team B",
            "home_name": "Team A",
            "away_name": "Team B",
            "sport_key": "soccer",
            "cutoff_time": "2026-03-13T18:00:00Z",
            "market_url": "soccer.match_odds/home",
            "outcome": "home",
        },
    )
    sxbet = normalizer.normalize(
        betting_instrument(
            venue="SXBET",
            sport="soccer",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
            info={"outcome_label": "Home"},
        ),
    )

    assert cloudbet.event_key == sxbet.event_key


def test_event_key_strips_provider_market_group_suffix_noise():
    normalizer = MarketNormalizer()
    polymarket = normalizer.normalize(
        betting_instrument(
            venue="POLYMARKET",
            sport="soccer",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
            event_name="Arsenal Exact Score vs West Ham United",
            home_name="Arsenal Exact Score",
            away_name="West Ham United",
            info={"outcome_label": "Home"},
        ),
    )
    sxbet = normalizer.normalize(
        betting_instrument(
            venue="SXBET",
            sport="soccer",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
            event_name="Arsenal vs West Ham United",
            home_name="Arsenal",
            away_name="West Ham United",
            info={"outcome_label": "Home"},
        ),
    )

    assert polymarket.event_key == sxbet.event_key
    assert "exact_score" not in polymarket.event_key


def test_sxbet_negative_three_quarter_handicap_is_supported():
    classifier = RuleClassifier()
    home_minus_three_quarter = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        params="line=-3.25",
        handicap=-3.25,
    )
    # SX.bet stamps one team-one (home) relative line on both legs, so the raw away
    # leg carries the same -3.25 as the home leg; normalization negates it back to the
    # selection-relative +3.25 that completes the partial-settlement hedge.
    away_plus_three_quarter = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="away",
        params="line=-3.25",
        handicap=-3.25,
    )

    rule = classifier.classify(home_minus_three_quarter, away_plus_three_quarter)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.PARTIAL_SETTLEMENT_HEDGE.value
    assert rule.has_partial is True


def test_polymarket_sports_binary_transforms_into_betting_instrument():
    instrument = BinaryOption(
        instrument_id=InstrumentId(Symbol("cond-1"), Venue("POLYMARKET")),
        raw_symbol=Symbol("cond-1"),
        outcome="Yes",
        description="Will Team A beat Team B? Resolves 50-50 if the game is not completed.",
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=Price.from_str("0.001"),
        price_precision=3,
        size_increment=Quantity.from_str("0.000001"),
        size_precision=6,
        activation_ns=0,
        expiration_ns=1,
        max_quantity=None,
        min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
        info={
            "condition_id": "0xabc",
            "feeRateBps": "75",
            "sports_market": {
                "sport": "basketball",
                "market_name": "basketball.moneyline",
                "market_type": "basketball.moneyline",
                "selection_role": "home",
                "event_name": "Team A vs Team B",
                "home_name": "Team A",
                "away_name": "Team B",
                "competition_name": "NBA",
                "price": 1.91,
                "maker_rebate_rate": "0.0025",
                "resolution_policy": {"tie_or_unknown": "50_50"},
            },
        },
    )

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(instrument)
    normalized = MarketNormalizer().normalize(instrument)

    assert transformed is not None
    assert transformed.market_name == "basketball.moneyline"
    assert transformed.outcome == "home"
    assert transformed.info["market_fee_rate"] == "0.0075"
    assert transformed.info["maker_rebate_rate"] == "0.0025"
    assert normalized.market_type == CanonicalMarketType.WINNER.value
    assert normalized.selection == "HOME"
    assert dict(normalized.resolution_policy)["tie_or_unknown"] == "50_50"


def test_market_normalizer_canonicalizes_multi_period_params():
    normalized = MarketNormalizer().normalize(
        {
            "provider": "CLOUDBET",
            "event_id": "evt-1",
            "event_name": "Team A vs Team B",
            "sport_name": "soccer",
            "market_name": "soccer.match_odds",
            "market_type": "soccer.1x2",
            "outcome": "home",
            "submarket_period": "2h",
            "market_url": "soccer.match_odds/home?period=ft&period=1h&period=2h",
        },
    )

    assert normalized.param("period") == "ft|1h|2h"


def test_market_normalizer_supports_single_period_scope_from_p1_param():
    normalized = MarketNormalizer().normalize(
        {
            "provider": "SXBET",
            "event_id": "evt-period-1",
            "event_name": "Team A vs Team B",
            "sport_name": "ice_hockey",
            "market_name": "match_odds",
            "market_type": "match_odds",
            "outcome": "home",
            "params": "period=p1",
        },
    )

    assert normalized.scope == "period_1"


def test_polymarket_inferred_sports_market_preserves_yes_no_semantics():
    base_info = {
        "condition_id": "0xdef",
        "question": "Will the Minnesota Vikings beat the Chicago Bears?",
        "_gamma_original": {
            "sport": "nfl",
            "description": (
                "This market resolves to Yes if the Minnesota Vikings beat the Chicago Bears."
            ),
            "outcomePrices": ["0.14", "0.86"],
            "events": [
                {
                    "title": "Minnesota Vikings vs Chicago Bears",
                    "sport": "american_football",
                    "startDateIso": "2026-09-15T00:00:00Z",
                },
            ],
        },
    }
    yes_instrument = BinaryOption(
        instrument_id=InstrumentId(Symbol("cond-yes"), Venue("POLYMARKET")),
        raw_symbol=Symbol("cond-yes"),
        outcome="Yes",
        description=base_info["question"],
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=Price.from_str("0.001"),
        price_precision=3,
        size_increment=Quantity.from_str("0.000001"),
        size_precision=6,
        activation_ns=0,
        expiration_ns=1,
        max_quantity=None,
        min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
        info=base_info,
    )
    no_instrument = BinaryOption(
        instrument_id=InstrumentId(Symbol("cond-no"), Venue("POLYMARKET")),
        raw_symbol=Symbol("cond-no"),
        outcome="No",
        description=base_info["question"],
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=Price.from_str("0.001"),
        price_precision=3,
        size_increment=Quantity.from_str("0.000001"),
        size_precision=6,
        activation_ns=0,
        expiration_ns=1,
        max_quantity=None,
        min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
        info=base_info,
    )

    transformed_yes = PolymarketSportsTransformer.to_crypto_betting_instrument(yes_instrument)
    transformed_no = PolymarketSportsTransformer.to_crypto_betting_instrument(no_instrument)

    assert transformed_yes is not None
    assert transformed_no is not None
    assert transformed_yes.market_type == "american_football.winner"
    assert transformed_yes.outcome == "home"
    assert "line=" not in transformed_yes.params
    assert transformed_no.outcome == "away"

    rule = RuleClassifier().classify(transformed_yes, transformed_no)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value


def test_polymarket_gamma_token_price_and_team_roles_are_preserved():
    info = {
        "condition_id": "0xnba",
        "question": "Will Los Angeles Lakers win?",
        "tokens": [
            {"token_id": "token-yes", "outcome": "Yes", "price": 0.42},
            {"token_id": "token-no", "outcome": "No", "price": 0.58},
        ],
        "selected_token_id": "token-no",
        "selected_outcome": "No",
        "_gamma_original": {
            "sport": "nba",
            "description": "This market resolves to Yes if the Los Angeles Lakers win.",
            "outcomePrices": '["0.42","0.58"]',
            "events": [
                {
                    "title": "Los Angeles Lakers vs Denver Nuggets",
                    "sport": "basketball",
                    "startDateIso": "2026-05-10T01:00:00Z",
                },
            ],
        },
    }
    no_instrument = BinaryOption(
        instrument_id=InstrumentId(Symbol("token-no"), Venue("POLYMARKET")),
        raw_symbol=Symbol("token-no"),
        outcome="No",
        description=info["question"],
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=Price.from_str("0.001"),
        price_precision=3,
        size_increment=Quantity.from_str("0.000001"),
        size_precision=6,
        activation_ns=0,
        expiration_ns=1,
        max_quantity=None,
        min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
        info=info,
    )

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(no_instrument)
    assert transformed is not None
    assert transformed.price == 0.58
    assert transformed.market_type == "basketball.winner"
    assert transformed.outcome == "away"
    assert transformed.home_name == "Los Angeles Lakers"
    assert transformed.away_name == "Denver Nuggets"

    normalized = MarketNormalizer().normalize(transformed)
    assert normalized.venue == "POLYMARKET"
    assert normalized.market_type == CanonicalMarketType.WINNER.value
    assert normalized.selection == "AWAY"
    assert dict(normalized.resolution_policy)["tie_or_unknown"] == "lose"


def test_polymarket_transform_strips_tournament_prefix_and_maps_token_outcomes():
    info = {
        "condition_id": "0xtennis",
        "question": "Internazionali BNL d'Italia: Jannik Sinner vs Alexei Popyrin",
        "tokens": [
            {"token_id": "sinner-token", "outcome": "Jannik Sinner", "price": 0.71},
            {"token_id": "popyrin-token", "outcome": "Alexei Popyrin", "price": 0.29},
        ],
        "selected_token_id": "sinner-token",
        "selected_outcome": "Jannik Sinner",
        "_gamma_original": {
            "sport": "atp",
            "description": "This market resolves to the match winner.",
            "outcomePrices": '["0.71","0.29"]',
            "events": [
                {
                    "title": "Internazionali BNL d'Italia: Jannik Sinner vs Alexei Popyrin",
                    "sport": "tennis",
                    "startDateIso": "2026-05-11T15:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="sinner-token",
            outcome="Jannik Sinner",
            question=info["question"],
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.home_name == "Jannik Sinner"
    assert transformed.away_name == "Alexei Popyrin"
    assert transformed.market_type == "tennis.winner"
    assert transformed.outcome == "home"

    normalized = MarketNormalizer().normalize(transformed)
    assert normalized.selection == "HOME"
    assert normalized.event_key == "tennis|jannik_sinner|alexei_popyrin|2026-05-11T15:00:00Z"


def test_polymarket_transform_strips_market_suffix_from_fixture_name():
    info = {
        "condition_id": "0xsoccer-corners",
        "question": "Tottenham Hotspur FC vs Leeds United FC - Total Corners",
        "tokens": [
            {"token_id": "over-token", "outcome": "Yes", "price": 0.44},
            {"token_id": "under-token", "outcome": "No", "price": 0.56},
        ],
        "selected_token_id": "over-token",
        "selected_outcome": "Yes",
        "_gamma_original": {
            "sport": "epl",
            "sportsMarketType": "total",
            "description": "This market resolves based on total corners.",
            "outcomePrices": '["0.44","0.56"]',
            "events": [
                {
                    "title": "Tottenham Hotspur FC vs Leeds United FC - Total Corners",
                    "sport": "soccer",
                    "startDateIso": "2026-05-11T19:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="over-token",
            outcome="Yes",
            question=info["question"],
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.home_name == "Tottenham Hotspur FC"
    assert transformed.away_name == "Leeds United FC"


def test_polymarket_transform_keeps_unsupported_score_and_half_markets_audit_only():
    for suffix in ("Exact Score", "Halftime Result"):
        question = f"Saudi Arabia vs. Senegal - {suffix}"
        info = {
            "condition_id": f"0xunsupported-{suffix.lower().replace(' ', '-')}",
            "question": question,
            "tokens": [
                {"token_id": "yes-token", "outcome": "Yes", "price": 0.33},
                {"token_id": "no-token", "outcome": "No", "price": 0.67},
            ],
            "selected_token_id": "yes-token",
            "selected_outcome": "Yes",
            "_gamma_original": {
                "sport": "soccer",
                "description": "This market resolves according to the listed special market.",
                "outcomePrices": '["0.33","0.67"]',
                "events": [
                    {
                        "title": question,
                        "sport": "soccer",
                        "startDate": "2026-05-13",
                    },
                ],
            },
        }

        transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
            polymarket_binary_option(
                symbol=f"{suffix.lower().replace(' ', '-')}-token",
                outcome="Yes",
                question=question,
                info=info,
            ),
        )

        assert transformed is None


def test_polymarket_transform_prefers_market_start_time_over_stale_event_time():
    info = {
        "condition_id": "0xtiafoe-buse",
        "question": "Internazionali BNL d'Italia: Frances Tiafoe vs Ignacio Buse",
        "tokens": [
            {"token_id": "tiafoe-token", "outcome": "Frances Tiafoe", "price": 0.62},
            {"token_id": "buse-token", "outcome": "Ignacio Buse", "price": 0.38},
        ],
        "selected_token_id": "tiafoe-token",
        "selected_outcome": "Frances Tiafoe",
        "_gamma_original": {
            "sport": "atp",
            "startDateIso": "2026-05-13T19:00:00Z",
            "description": "This market resolves to the match winner.",
            "events": [
                {
                    "title": "Frances Tiafoe vs Ignacio Buse",
                    "sport": "tennis",
                    "startDateIso": "2026-03-08T06:09:18.793534Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="tiafoe-token",
            outcome="Frances Tiafoe",
            question=info["question"],
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.start_time == "2026-05-13T19:00:00Z"
    normalized = MarketNormalizer().normalize(transformed)
    assert normalized.event_key == "tennis|frances_tiafoe|ignacio_buse|2026-05-13T19:00:00Z"


def test_polymarket_spread_token_outcomes_map_to_team_roles():
    info = {
        "condition_id": "0xsoccer-spread",
        "question": "Gaziantep FK vs. Rams Başakşehir FK - Spread",
        "tokens": [
            {"token_id": "home-token", "outcome": "Gaziantep FK", "price": 0.49},
            {"token_id": "away-token", "outcome": "Rams Başakşehir FK", "price": 0.51},
        ],
        "selected_token_id": "away-token",
        "selected_outcome": "Rams Başakşehir FK",
        "_gamma_original": {
            "sport": "epl",
            "sportsMarketType": "spread",
            "line": "+0.5",
            "description": "This market resolves based on the spread.",
            "outcomePrices": '["0.49","0.51"]',
            "events": [
                {
                    "title": "Gaziantep FK vs. Rams Başakşehir FK - Spread",
                    "sport": "soccer",
                    "startDateIso": "2026-05-11T14:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="away-token",
            outcome="Rams Başakşehir FK",
            question=info["question"],
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.market_type == "soccer.spread"
    assert transformed.outcome == "away"


def test_polymarket_same_city_team_tokens_use_discriminating_name_parts():
    info = {
        "condition_id": "0xmlb",
        "question": "Chicago Cubs vs Chicago White Sox",
        "tokens": [
            {"token_id": "cubs-token", "outcome": "Chicago Cubs", "price": 0.58},
            {"token_id": "sox-token", "outcome": "Chicago White Sox", "price": 0.42},
        ],
        "selected_token_id": "sox-token",
        "selected_outcome": "Chicago White Sox",
        "_gamma_original": {
            "sport": "mlb",
            "description": "This market resolves to the game winner.",
            "outcomePrices": '["0.58","0.42"]',
            "events": [
                {
                    "title": "Chicago Cubs vs Chicago White Sox",
                    "sport": "baseball",
                    "startDateIso": "2026-05-11T20:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="sox-token",
            outcome="Chicago White Sox",
            question=info["question"],
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.outcome == "away"


def test_polymarket_spread_binary_maps_yes_no_to_team_line_semantics():
    question = "Will the Lakers cover the spread against the Nuggets?"
    info = {
        "condition_id": "0xnba-spread",
        "question": question,
        "tokens": [
            {"token_id": "spread-yes", "outcome": "Yes", "price": 0.49},
            {"token_id": "spread-no", "outcome": "No", "price": 0.51},
        ],
        "selected_token_id": "spread-no",
        "selected_outcome": "No",
        "_gamma_original": {
            "sport": "nba",
            "sportsMarketType": "spread",
            "line": "-4.5",
            "description": "This resolves to Yes if the Lakers cover the spread.",
            "events": [
                {
                    "title": "Los Angeles Lakers vs Denver Nuggets",
                    "sport": "basketball",
                    "startDateIso": "2026-05-10T01:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="spread-no",
            outcome="No",
            question=question,
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.market_type == "basketball.spread"
    assert transformed.outcome == "away"
    normalized = MarketNormalizer().normalize(transformed)
    assert normalized.venue == "POLYMARKET"
    assert normalized.market_type == CanonicalMarketType.POINT_SPREAD.value
    assert normalized.selection == "AWAY"
    assert normalized.param("line") == "4.5"

    # The SX.bet away leg is quoted with the home-relative line (-4.5); normalization
    # negates it to the selection-relative +4.5 that matches the Polymarket away leg.
    sxbet_away_plus = betting_instrument(
        venue="SXBET",
        sport="basketball",
        market_name="spread",
        market_type="spread",
        outcome="away",
        params="line=-4.5",
        handicap=-4.5,
    )
    rule = RuleClassifier().classify(transformed, sxbet_away_plus)
    assert rule is not None
    assert rule.relationship_type == RelationshipType.EQUIVALENT_SELECTION.value


def test_polymarket_totals_binary_maps_over_under_and_extracts_line():
    question = "Will the Lakers vs Nuggets game go over 224.5 total points?"
    info = {
        "condition_id": "0xnba-total",
        "question": question,
        "tokens": [
            {"token_id": "total-yes", "outcome": "Yes", "price": 0.47},
            {"token_id": "total-no", "outcome": "No", "price": 0.53},
        ],
        "selected_token_id": "total-no",
        "selected_outcome": "No",
        "_gamma_original": {
            "sport": "nba",
            "description": "This resolves to Yes if the game total goes over 224.5 points.",
            "events": [
                {
                    "title": "Los Angeles Lakers vs Denver Nuggets",
                    "sport": "basketball",
                    "startDateIso": "2026-05-10T01:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="total-no",
            outcome="No",
            question=question,
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.market_type == "basketball.totals"
    assert transformed.outcome == "under"
    normalized = MarketNormalizer().normalize(transformed)
    assert normalized.market_type == CanonicalMarketType.TOTALS.value
    assert normalized.selection == "UNDER"
    assert normalized.param("line") == "224.5"
    sxbet_under = betting_instrument(
        sport="basketball",
        market_name="totals",
        market_type="totals",
        outcome="under",
        params="line=224.5",
        venue="SXBET",
    )
    rule = RuleClassifier().classify(transformed, sxbet_under)
    assert rule is not None
    assert rule.relationship_type == RelationshipType.EQUIVALENT_SELECTION.value


def test_polymarket_corner_totals_keep_subject_axis_separate_from_goals():
    question = "Will Arsenal vs West Ham United go over 12.5 total corners?"
    info = {
        "condition_id": "0xsoccer-corners",
        "question": question,
        "tokens": [
            {"token_id": "corners-yes", "outcome": "Yes", "price": 0.47},
            {"token_id": "corners-no", "outcome": "No", "price": 0.53},
        ],
        "selected_token_id": "corners-yes",
        "selected_outcome": "Yes",
        "_gamma_original": {
            "sport": "soccer",
            "sportsMarketType": "total_corners",
            "description": "This resolves to Yes if the match has over 12.5 total corners.",
            "events": [
                {
                    "title": "Arsenal Total Corners vs West Ham United",
                    "sport": "soccer",
                    "startDateIso": "2026-05-10T16:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="corners-yes",
            outcome="Yes",
            question=question,
            info=info,
        ),
    )

    assert transformed is not None
    assert transformed.event_key(include_start_time=False) == "soccer:arsenal:west ham united"
    normalized = MarketNormalizer().normalize(transformed)
    assert normalized.market_type == CanonicalMarketType.TOTALS.value
    assert normalized.selection == "OVER"
    assert normalized.param("line") == "12.5"
    assert normalized.param("subject") == "corners"


def test_polymarket_half_point_lines_override_generic_50_50_tie_language():
    question = "Will the Lakers vs Nuggets game go over 224.5 total points?"
    info = {
        "condition_id": "0xnba-total-50-50",
        "question": question,
        "tokens": [
            {"token_id": "total-yes", "outcome": "Yes", "price": 0.47},
            {"token_id": "total-no", "outcome": "No", "price": 0.53},
        ],
        "selected_token_id": "total-yes",
        "selected_outcome": "Yes",
        "_gamma_original": {
            "sport": "nba",
            "sportsMarketType": "total",
            "line": "224.5",
            "description": (
                "This resolves to Yes if the game total goes over 224.5 points. "
                "If the event is not completed the market resolves 50-50."
            ),
            "events": [
                {
                    "title": "Los Angeles Lakers vs Denver Nuggets",
                    "sport": "basketball",
                    "startDateIso": "2026-05-10T01:00:00Z",
                },
            ],
        },
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="total-50-50",
            outcome="Yes",
            question=question,
            info=info,
        ),
    )

    assert transformed is not None
    normalized = MarketNormalizer().normalize(transformed)
    assert dict(normalized.resolution_policy)["tie_or_unknown"] == "lose"


def test_polymarket_infers_team_role_from_nickname_and_beat_question():
    info = {
        "condition_id": "0xnba-beat",
        "question": "Will the Lakers beat the Nuggets?",
        "tokens": [
            {"token_id": "token-yes", "outcome": "Yes", "price": 0.47},
            {"token_id": "token-no", "outcome": "No", "price": 0.53},
        ],
        "selected_token_id": "token-yes",
        "selected_outcome": "Yes",
        "_gamma_original": {
            "sport": "nba",
            "description": "Resolves to Yes if the Lakers beat the Nuggets.",
            "outcomePrices": '["0.47","0.53"]',
            "events": [
                {
                    "title": "Los Angeles Lakers vs Denver Nuggets",
                    "sport": "basketball",
                    "startDateIso": "2026-05-10T01:00:00Z",
                },
            ],
        },
    }
    yes_instrument = BinaryOption(
        instrument_id=InstrumentId(Symbol("token-yes"), Venue("POLYMARKET")),
        raw_symbol=Symbol("token-yes"),
        outcome="Yes",
        description=info["question"],
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=Price.from_str("0.001"),
        price_precision=3,
        size_increment=Quantity.from_str("0.000001"),
        size_precision=6,
        activation_ns=0,
        expiration_ns=1,
        max_quantity=None,
        min_quantity=Quantity.from_int(5),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
        info=info,
    )

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(yes_instrument)
    assert transformed is not None
    assert transformed.market_type == "basketball.winner"
    assert transformed.outcome == "home"
    assert transformed.home_name == "Los Angeles Lakers"
    assert transformed.away_name == "Denver Nuggets"

    normalized = MarketNormalizer().normalize(transformed)
    assert normalized.market_type == CanonicalMarketType.WINNER.value
    assert normalized.selection == "HOME"


def test_polymarket_corpus_accepts_deterministic_team_futures():
    ingestor = SnapshotIngestor(RuleStore(DictCache()))

    class Transformer:
        @staticmethod
        def to_crypto_betting_instrument(_instrument):
            return CryptoBettingInstrument(
                venue=Venue("POLYMARKET"),
                event_id="nfl-champion-2027",
                event_name="NFL Champion 2027",
                home_name="",
                away_name="",
                sport_name="american_football",
                competition_name="NFL Champion 2027",
                market_name="american_football.winner_binary",
                market_type="american_football.winner",
                outcome="yes",
                side=SelectionSide.BACK,
                price=0.14,
                currency=Currency.from_str("USDC"),
                params="subject=minnesota_vikings",
                start_time="2027-02-08T00:00:00Z",
                info={
                    "sports_market": {
                        "sport": "american_football",
                        "market_name": "american_football.winner_binary",
                        "market_type": "american_football.winner",
                        "selection_role": "yes",
                        "event_name": "NFL Champion 2027",
                        "home_name": "",
                        "away_name": "",
                        "price": 0.14,
                        "event_type": "team_future",
                        "params": {"subject": "minnesota_vikings"},
                    },
                },
            )

    records, sports, event_keys, market_names = ingestor._polymarket_normalized_records(
        discovered_markets={"market-1": {"id": "market-1"}},
        normalize_gamma_market_to_clob_format=lambda _market: {
            "tokens": [{"token_id": "token-yes", "outcome": "Yes"}],
        },
        parse_polymarket_instrument=lambda **_kwargs: object(),
        transformer=Transformer,
    )

    assert len(records) == 1
    assert sports == {"american_football"}
    assert event_keys == {"american_football|nfl_champion_2027|2027-02-08T00:00:00Z"}
    assert market_names == {"american_football.winner_binary"}


def test_polymarket_corpus_persists_gamma_fixture_markets_through_real_parser():
    from nautilus_trader.adapters.polymarket.common.gamma_markets import (
        normalize_gamma_market_to_clob_format,
    )
    from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument

    ingestor = SnapshotIngestor(RuleStore(DictCache()))
    market = {
        "id": "gamma-market-1",
        "conditionId": "conditionnba1",
        "questionID": "questionnba1",
        "question": "Will Los Angeles Lakers beat Denver Nuggets?",
        "description": "Resolves to Yes if Los Angeles Lakers win the scheduled NBA game.",
        "slug": "lakers-nuggets",
        "sport": "nba",
        "sportsTag": "nba",
        "clobTokenIds": json.dumps(["yes", "no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.52", "0.48"]),
        "active": True,
        "closed": False,
        "archived": False,
        "endDateIso": "2027-01-03T03:00:00Z",
        "startDateIso": "2027-01-03T01:00:00Z",
        "orderPriceMinTickSize": "0.001",
        "orderMinSize": 5,
        "events": [
            {
                "id": "event-nba-1",
                "title": "Los Angeles Lakers vs Denver Nuggets",
                "slug": "lakers-vs-nuggets",
                "sport": "basketball",
                "startDate": "2027-01-03T01:00:00Z",
                "startDateIso": "2027-01-03T01:00:00Z",
            },
        ],
    }

    records, sports, event_keys, market_names = ingestor._polymarket_normalized_records(
        discovered_markets={"gamma-market-1": market},
        normalize_gamma_market_to_clob_format=normalize_gamma_market_to_clob_format,
        parse_polymarket_instrument=parse_polymarket_instrument,
        transformer=PolymarketSportsTransformer,
    )

    assert len(records) == 2
    assert sports == {"basketball"}
    assert event_keys == {
        "basketball|los_angeles_lakers|denver_nuggets|2027-01-03T01:00:00Z",
    }
    assert market_names == {"basketball.winner"}
    assert {record.selection.selection for record in records} == {"HOME", "AWAY"}


def test_polymarket_corpus_uses_market_start_time_when_event_time_is_stale():
    from nautilus_trader.adapters.polymarket.common.gamma_markets import (
        normalize_gamma_market_to_clob_format,
    )
    from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument

    ingestor = SnapshotIngestor(RuleStore(DictCache()))
    market = {
        "id": "gamma-market-stale-event",
        "conditionId": "conditiontennis1",
        "questionID": "questiontennis1",
        "question": "Internazionali BNL d'Italia: Frances Tiafoe vs Ignacio Buse",
        "description": "This market resolves to the match winner.",
        "slug": "tiafoe-buse",
        "sport": "atp",
        "sportsTag": "atp",
        "clobTokenIds": json.dumps(["yes", "no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.38", "0.62"]),
        "active": True,
        "closed": False,
        "archived": False,
        "startDateIso": "2026-05-13T19:00:00Z",
        "endDateIso": "2026-05-13T23:00:00Z",
        "orderPriceMinTickSize": "0.001",
        "orderMinSize": 5,
        "events": [
            {
                "id": "event-tennis-stale",
                "title": "Frances Tiafoe vs Ignacio Buse",
                "slug": "frances-tiafoe-vs-ignacio-buse",
                "sport": "tennis",
                "startDateIso": "2026-03-08T06:09:18.793534Z",
            },
        ],
    }

    records, sports, event_keys, market_names = ingestor._polymarket_normalized_records(
        discovered_markets={"gamma-market-stale-event": market},
        normalize_gamma_market_to_clob_format=normalize_gamma_market_to_clob_format,
        parse_polymarket_instrument=parse_polymarket_instrument,
        transformer=PolymarketSportsTransformer,
    )

    assert len(records) == 2
    assert sports == {"tennis"}
    assert event_keys == {"tennis|frances_tiafoe|ignacio_buse|2026-05-13T19:00:00Z"}
    assert market_names == {"tennis.winner_binary"}


def test_polymarket_team_future_preserves_subject_specific_yes_no_states():
    info = {
        "condition_id": "0xnfl-vikings",
        "question": "Will the Minnesota Vikings win the 2027 NFL league championship?",
        "tokens": [
            {"token_id": "vikings-yes", "outcome": "Yes", "price": 0.14},
            {"token_id": "vikings-no", "outcome": "No", "price": 0.86},
        ],
        "selected_token_id": "vikings-yes",
        "selected_outcome": "Yes",
        "_gamma_original": {
            "sport": "nfl",
            "description": "This market resolves to Yes if the Vikings win the championship.",
            "events": [
                {
                    "id": "nfl-champion-2027",
                    "title": "NFL Champion 2027",
                    "sport": "american_football",
                    "startDateIso": "2027-02-08T00:00:00Z",
                },
            ],
        },
    }

    yes = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="vikings-yes",
            outcome="Yes",
            question=info["question"],
            info=info,
        ),
    )
    info_no = {**info, "selected_token_id": "vikings-no", "selected_outcome": "No"}
    no = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="vikings-no",
            outcome="No",
            question=info_no["question"],
            info=info_no,
        ),
    )

    assert yes is not None
    assert no is not None
    assert yes.home_name == ""
    assert yes.away_name == ""
    assert yes.event_id == "nfl-champion-2027"
    normalized_yes = MarketNormalizer().normalize(yes)
    normalized_no = MarketNormalizer().normalize(no)
    assert normalized_yes.param("subject") == "minnesota_vikings"
    assert normalized_yes.selection == "YES"
    assert normalized_no.selection == "NO"
    assert normalized_yes.event_key == normalized_no.event_key
    assert PayoffVectorBuilder.build(normalized_yes).result_states == (
        "MINNESOTA_VIKINGS_EVENT_TRUE",
        "MINNESOTA_VIKINGS_EVENT_FALSE",
    )

    rule = RuleClassifier().classify(yes, no)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value


def test_polymarket_transform_skips_non_fixture_sports_futures():
    info = {
        "condition_id": "0xnhl-draft",
        "question": "Will James Hagens be drafted 1st overall in the 2026 NHL Draft?",
        "_gamma_original": {
            "sport": "nhl",
            "description": "Resolves to Yes if James Hagens is drafted first overall.",
            "events": [
                {
                    "title": "2026 NHL Draft",
                    "sport": "ice_hockey",
                    "startDateIso": "2026-06-26T00:00:00Z",
                },
            ],
        },
        "tokens": [
            {"token_id": "future-yes", "outcome": "Yes", "price": 0.47},
            {"token_id": "future-no", "outcome": "No", "price": 0.53},
        ],
        "selected_token_id": "future-yes",
        "selected_outcome": "Yes",
    }

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(
        polymarket_binary_option(
            symbol="future-yes",
            outcome="Yes",
            question=info["question"],
            info=info,
        ),
    )

    assert transformed is None


def test_rule_store_persists_corpus_artifacts():
    cache = DictCache()
    store = RuleStore(cache)
    snapshot = CorpusSnapshot(
        snapshot_id="snapshot-1",
        provider="CLOUDBET",
        endpoint="/pub/v2/odds/events?sport=soccer",
        fetched_at="2026-04-27T00:00:00Z",
        payload=b'{"events":[]}',
    )
    normalized = MarketNormalizer().normalize(
        betting_instrument(
            market_name="basketball.handicap",
            market_type="basketball.handicap",
            outcome="home",
            sport="basketball",
            params="handicap=3.5&period=ot&period=ft",
        ),
    )
    record = NormalizedSelectionRecord(
        record_id="record-1",
        provider="SXBET",
        selection=normalized,
        manifest_id="manifest-1",
    )
    manifest = RuleCorpusManifest(
        manifest_id="manifest-1",
        provider="CLOUDBET",
        fetched_at="2026-04-27T00:00:00Z",
        endpoint_version="feed:v2,trading:v4",
        sport_count=2,
        event_count=10,
        selection_count=20,
        market_taxonomy_hash="abc123",
        source_refs=("snapshot-1",),
    )

    store.save_snapshot(snapshot)
    store.save_normalized_selection(record)
    store.save_manifest(manifest)

    assert store.load_snapshot("snapshot-1") == snapshot
    assert store.load_normalized_selection("record-1") == record
    assert store.load_manifest("manifest-1") == manifest
    assert store.list_snapshot_ids() == ["snapshot-1"]
    assert store.list_normalized_ids() == ["record-1"]
    assert store.list_manifest_ids() == ["manifest-1"]


def test_cloudbet_competition_outright_totals_are_rewritten_to_totals():
    competition = {
        "name": "NFL",
        "key": "american-football-usa-nfl",
        "sport": {"name": "American Football", "key": "american-football"},
        "events": [
            {
                "id": 1,
                "name": "Arizona Cardinals Total Regular Season Wins",
                "status": "TRADING",
                "cutoffTime": "2026-09-05T23:00:00Z",
                "markets": {
                    "american_football.outright.v3": {
                        "submarkets": {
                            "default": {
                                "sequence": "440",
                                "selections": [
                                    {
                                        "outcome": "s-over-4-dot-5",
                                        "params": "",
                                        "marketUrl": "american_football.outright.v3/s-over-4-dot-5",
                                        "price": 1.91,
                                        "minStake": 0.1,
                                        "maxStake": 10,
                                        "probability": 0.5,
                                        "status": "SELECTION_ENABLED",
                                        "side": "BACK",
                                    },
                                    {
                                        "outcome": "s-under-4-dot-5",
                                        "params": "",
                                        "marketUrl": (
                                            "american_football.outright.v3/s-under-4-dot-5"
                                        ),
                                        "price": 1.91,
                                        "minStake": 0.1,
                                        "maxStake": 10,
                                        "probability": 0.5,
                                        "status": "SELECTION_ENABLED",
                                        "side": "BACK",
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        ],
    }

    selections = SnapshotIngestor._cloudbet_competition_to_selections(competition)
    normalized = [MarketNormalizer().normalize(selection) for selection in selections]

    assert len(normalized) == 2
    assert all(item.market_type == CanonicalMarketType.TOTALS.value for item in normalized)
    assert {item.selection for item in normalized} == {"OVER", "UNDER"}
    assert {item.param("line") for item in normalized} == {"4.5"}


def test_rule_miner_persists_candidate_indices():
    cache = DictCache()
    store = RuleStore(cache)
    normalizer = MarketNormalizer()
    home = normalizer.normalize(
        betting_instrument(
            venue="CLOUDBET",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        ),
    )
    away_draw = normalizer.normalize(
        betting_instrument(
            venue="SXBET",
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    store.save_normalized_selection(
        NormalizedSelectionRecord(
            record_id="record-home",
            provider="CLOUDBET",
            selection=home,
            manifest_id="manifest-1",
        ),
    )
    store.save_normalized_selection(
        NormalizedSelectionRecord(
            record_id="record-away-draw",
            provider="SXBET",
            selection=away_draw,
            manifest_id="manifest-1",
        ),
    )

    rules = RuleMiner(store).mine_store(manifest_id="manifest-1")

    assert len(rules) == 1
    assert store.list_candidate_ids() == [rules[0].rule_id]


def test_historical_validator_persists_cloudbet_stats_for_cross_provider_rule():
    classifier = RuleClassifier()
    cache = DictCache()
    store = RuleStore(cache)
    validator = HistoricalRuleValidator(store)

    rule = classifier.classify(
        betting_instrument(
            venue="CLOUDBET",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        ),
        betting_instrument(
            venue="SXBET",
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None
    store.save_candidate(rule)

    items = []
    for event_id in range(25):
        items.extend(
            [
                {
                    "betType": "STRAIGHT",
                    "betId": f"match-{event_id}",
                    "betslipId": f"betslip-match-{event_id}",
                    "positionId": f"position-match-{event_id}",
                    "currency": "USDC",
                    "createTime": "2026-04-27T00:00:00Z",
                    "state": "COMPLETED",
                    "result": "WIN",
                    "selection": {
                        "eventId": str(event_id),
                        "marketUrl": "soccer.match_odds/home",
                        "price": "2.1",
                        "result": "WIN",
                        "marketName": "soccer.match_odds",
                        "outcomeName": "home",
                    },
                },
                {
                    "betType": "STRAIGHT",
                    "betId": f"double-{event_id}",
                    "betslipId": f"betslip-double-{event_id}",
                    "positionId": f"position-double-{event_id}",
                    "currency": "USDC",
                    "createTime": "2026-04-27T00:00:00Z",
                    "state": "COMPLETED",
                    "result": "LOSS",
                    "selection": {
                        "eventId": str(event_id),
                        "marketUrl": "soccer.double_chance/draw_away",
                        "price": "1.8",
                        "result": "LOSS",
                        "marketName": "soccer.double_chance",
                        "outcomeName": "draw_away",
                    },
                },
            ],
        )
    store.save_snapshot(
        CorpusSnapshot(
            snapshot_id="snapshot-bets",
            provider="CLOUDBET",
            endpoint="/pub/v4/bets?offset=0&limit=50",
            fetched_at="2026-04-27T00:00:00Z",
            payload=msgspec.json.encode({"items": items, "hasNext": False}),
        ),
    )

    stats = validator.validate_store(provider="CLOUDBET", persist=True)

    assert len(stats) == 1
    assert stats[0].sample_count == 25
    assert stats[0].match_count == 25
    assert stats[0].mismatch_count == 0
    assert stats[0].confidence == 1.0
    assert store.load_validation(rule.rule_id) == stats[0]


def test_promotion_policy_and_market_matcher_require_promotion_when_store_is_present():
    classifier = RuleClassifier()
    cache = DictCache()
    store = RuleStore(cache)
    policy = RulePromotionPolicy(allowlisted_venue_scopes={("CLOUDBET", "SXBET")})
    matcher = MarketMatcher(rule_store=store)
    home = betting_instrument(
        venue="CLOUDBET",
        market_name="match_odds",
        market_type="match_odds",
        outcome="home",
    )
    away_draw = betting_instrument(
        venue="SXBET",
        market_name="double_chance",
        market_type="double_chance",
        outcome="away_draw",
    )
    rule = classifier.classify(home, away_draw)
    assert rule is not None
    stats = RuleValidationStats(
        rule_id=rule.rule_id,
        venue_id="CLOUDBET|SXBET",
        sport="soccer",
        sample_count=25,
        match_count=25,
        mismatch_count=0,
        confidence=0.99,
        last_validated_at="2026-04-27T00:00:00Z",
    )

    assert matcher.check_arbitrage(home, away_draw) is None

    promoted = policy.promote(store, rule, stats)

    assert promoted is not None
    assert matcher.check_arbitrage(home, away_draw) is not None


def test_catalog_templates_generalize_from_events_without_settled_bets():
    cache = DictCache()
    store = RuleStore(cache)
    normalizer = MarketNormalizer()

    for event_index in range(10):
        home = normalizer.normalize(
            betting_instrument(
                venue="CLOUDBET",
                market_name="match_odds",
                market_type="match_odds",
                outcome="home",
            ),
        )
        away_draw = normalizer.normalize(
            betting_instrument(
                venue="CLOUDBET",
                market_name="double_chance",
                market_type="double_chance",
                outcome="away_draw",
            ),
        )
        home = replace(
            home,
            event_key=f"catalog-event-{event_index}",
            instrument_id=f"home-{event_index}",
        )
        away_draw = replace(
            away_draw,
            event_key=f"catalog-event-{event_index}",
            instrument_id=f"away-draw-{event_index}",
        )
        store.save_normalized_selection(
            NormalizedSelectionRecord(
                record_id=f"home-{event_index}",
                provider="CLOUDBET",
                selection=home,
                manifest_id="manifest-catalog",
            ),
        )
        store.save_normalized_selection(
            NormalizedSelectionRecord(
                record_id=f"away-draw-{event_index}",
                provider="CLOUDBET",
                selection=away_draw,
                manifest_id="manifest-catalog",
            ),
        )

    templates = RuleMiner(store).mine_templates_from_store(manifest_id="manifest-catalog")

    assert len(templates) == 1
    template = templates[0]
    assert template.support.observed_count == 10
    assert template.support.event_count == 10
    assert template.support.catalog_promotable is True
    assert template.promotion_status == "CANDIDATE"
    assert store.list_template_candidate_ids() == [template.template_id]

    promoted = RulePromotionPolicy().promote_template(store, template)

    assert promoted is not None
    assert promoted.promotion_status == "PROMOTED"
    assert promoted.execution_safe is True

    matcher = MarketMatcher(rule_store=store)
    unseen_home = betting_instrument(
        venue="CLOUDBET",
        market_name="match_odds",
        market_type="match_odds",
        outcome="home",
    )
    unseen_away_draw = betting_instrument(
        venue="CLOUDBET",
        market_name="double_chance",
        market_type="double_chance",
        outcome="away_draw",
    )

    opportunity = matcher.check_arbitrage(unseen_home, unseen_away_draw)

    assert opportunity is not None


def test_event_candidates_keep_distinct_fixture_observations_for_same_template_shape():
    cache = DictCache()
    store = RuleStore(cache)
    normalizer = MarketNormalizer()

    for event_index in range(2):
        over = replace(
            normalizer.normalize(
                betting_instrument(
                    venue="CLOUDBET",
                    sport="american_football",
                    market_name="totals",
                    market_type="totals",
                    outcome="over",
                    params="line=45.5",
                ),
            ),
            event_key=f"american-football-event-{event_index}",
            instrument_id=f"over-{event_index}",
        )
        under = replace(
            normalizer.normalize(
                betting_instrument(
                    venue="CLOUDBET",
                    sport="american_football",
                    market_name="totals",
                    market_type="totals",
                    outcome="under",
                    params="line=45.5",
                ),
            ),
            event_key=f"american-football-event-{event_index}",
            instrument_id=f"under-{event_index}",
        )
        store.save_normalized_selection(
            NormalizedSelectionRecord(
                record_id=f"over-{event_index}",
                provider="CLOUDBET",
                selection=over,
                manifest_id="manifest-event-scoped",
            ),
        )
        store.save_normalized_selection(
            NormalizedSelectionRecord(
                record_id=f"under-{event_index}",
                provider="CLOUDBET",
                selection=under,
                manifest_id="manifest-event-scoped",
            ),
        )

    templates = RuleMiner(store).mine_templates_from_store(manifest_id="manifest-event-scoped")
    candidate_ids = store.list_candidate_ids()
    candidates = [store.load_candidate(candidate_id) for candidate_id in candidate_ids]

    assert len(candidate_ids) == 2
    assert all(candidate is not None for candidate in candidates)
    assert len({candidate.rule_id for candidate in candidates if candidate is not None}) == 2
    assert len({candidate.template_id for candidate in candidates if candidate is not None}) == 1
    # Evidence keys are the tolerant fixture-bucket identity (family + scope/anchor);
    # distinct fixtures must still yield two distinct keys, each carrying its event id.
    evidence_keys = {
        candidate.evidence_event_key for candidate in candidates if candidate is not None
    }
    assert len(evidence_keys) == 2
    assert any("american-football-event-0" in key for key in evidence_keys)
    assert any("american-football-event-1" in key for key in evidence_keys)
    assert len(templates) == 1
    assert templates[0].support.observed_count == 2
    # Two distinct fixtures -> event_count 2 (the diversity gate is not inflated).
    assert templates[0].support.event_count == 2
    assert templates[0].support.event_count == 2


def test_rule_miner_precomputes_payoff_vectors_once_per_record(monkeypatch):
    cache = DictCache()
    store = RuleStore(cache)
    normalizer = MarketNormalizer()
    classifier = RuleClassifier()
    miner = RuleMiner(store, classifier=classifier)

    records = [
        NormalizedSelectionRecord(
            record_id="home",
            provider="CLOUDBET",
            manifest_id="manifest-precompute",
            selection=replace(
                normalizer.normalize(
                    betting_instrument(
                        venue="CLOUDBET",
                        market_name="match_odds",
                        market_type="match_odds",
                        outcome="home",
                    ),
                ),
                event_key="precompute-event",
                instrument_id="home",
            ),
        ),
        NormalizedSelectionRecord(
            record_id="away-draw",
            provider="CLOUDBET",
            manifest_id="manifest-precompute",
            selection=replace(
                normalizer.normalize(
                    betting_instrument(
                        venue="CLOUDBET",
                        market_name="double_chance",
                        market_type="double_chance",
                        outcome="away_draw",
                    ),
                ),
                event_key="precompute-event",
                instrument_id="away-draw",
            ),
        ),
        NormalizedSelectionRecord(
            record_id="over",
            provider="CLOUDBET",
            manifest_id="manifest-precompute",
            selection=replace(
                normalizer.normalize(
                    betting_instrument(
                        venue="CLOUDBET",
                        sport="american_football",
                        market_name="totals",
                        market_type="totals",
                        outcome="over",
                        params="line=45.5",
                    ),
                ),
                sport="soccer",
                event_key="precompute-event",
                instrument_id="over",
            ),
        ),
        NormalizedSelectionRecord(
            record_id="under",
            provider="CLOUDBET",
            manifest_id="manifest-precompute",
            selection=replace(
                normalizer.normalize(
                    betting_instrument(
                        venue="CLOUDBET",
                        sport="american_football",
                        market_name="totals",
                        market_type="totals",
                        outcome="under",
                        params="line=45.5",
                    ),
                ),
                sport="soccer",
                event_key="precompute-event",
                instrument_id="under",
            ),
        ),
    ]

    build_count = 0
    classify_count = 0
    original_build = classifier.build_payoff_vector
    original_classify_precomputed = classifier.classify_precomputed

    def counting_build(selection):
        nonlocal build_count
        build_count += 1
        return original_build(selection)

    def counting_classify(selection_a, selection_b, vector_a, vector_b):
        nonlocal classify_count
        classify_count += 1
        return original_classify_precomputed(selection_a, selection_b, vector_a, vector_b)

    monkeypatch.setattr(classifier, "build_payoff_vector", counting_build)
    monkeypatch.setattr(classifier, "classify_precomputed", counting_classify)

    rules = miner.mine_event_candidates(records, persist=False)

    assert build_count == len(records)
    assert classify_count == 2
    assert len(rules) == 2


def test_sparse_catalog_template_does_not_promote_without_settled_bets():
    cache = DictCache()
    store = RuleStore(cache)
    normalizer = MarketNormalizer()
    home = replace(
        normalizer.normalize(
            betting_instrument(
                venue="CLOUDBET",
                market_name="match_odds",
                market_type="match_odds",
                outcome="home",
            ),
        ),
        event_key="sparse-event",
        instrument_id="sparse-home",
    )
    away_draw = replace(
        normalizer.normalize(
            betting_instrument(
                venue="CLOUDBET",
                market_name="double_chance",
                market_type="double_chance",
                outcome="away_draw",
            ),
        ),
        event_key="sparse-event",
        instrument_id="sparse-away-draw",
    )
    store.save_normalized_selection(
        NormalizedSelectionRecord(
            record_id="sparse-home",
            provider="CLOUDBET",
            selection=home,
            manifest_id="manifest-sparse",
        ),
    )
    store.save_normalized_selection(
        NormalizedSelectionRecord(
            record_id="sparse-away-draw",
            provider="CLOUDBET",
            selection=away_draw,
            manifest_id="manifest-sparse",
        ),
    )

    templates = RuleMiner(store).mine_templates_from_store(manifest_id="manifest-sparse")

    assert len(templates) == 1
    assert templates[0].support.catalog_promotable is False
    promoted = RulePromotionPolicy().promote_template(store, templates[0])

    assert promoted is not None
    assert promoted.safety_tier == SafetyTier.TOPOLOGY_SAFE.value
    assert promoted.execution_safe is False


def test_completion_report_distinguishes_candidates_from_promotions():
    cache = DictCache()
    store = RuleStore(cache)
    normalizer = MarketNormalizer()
    providers = ("CLOUDBET", "SXBET", "POLYMARKET")

    for provider in providers:
        manifest = RuleCorpusManifest(
            manifest_id=f"manifest-{provider.lower()}",
            provider=provider,
            fetched_at="2026-04-27T00:00:00Z",
            endpoint_version="test",
            sport_count=1,
            event_count=10,
            selection_count=20,
            market_taxonomy_hash=f"hash-{provider.lower()}",
            source_refs=(),
        )
        store.save_manifest(manifest)

    for event_index in range(10):
        home = replace(
            normalizer.normalize(
                betting_instrument(
                    venue="CLOUDBET",
                    market_name="match_odds",
                    market_type="match_odds",
                    outcome="home",
                ),
            ),
            event_key=f"soccer-event-{event_index}",
            instrument_id=f"home-{event_index}",
        )
        away_draw = replace(
            normalizer.normalize(
                betting_instrument(
                    venue="SXBET",
                    market_name="double_chance",
                    market_type="double_chance",
                    outcome="away_draw",
                ),
            ),
            event_key=f"soccer-event-{event_index}",
            instrument_id=f"away-draw-{event_index}",
        )
        polymarket_home = replace(
            normalizer.normalize(
                betting_instrument(
                    venue="POLYMARKET",
                    market_name="match_odds",
                    market_type="match_odds",
                    outcome="home",
                ),
            ),
            event_key=f"soccer-event-{event_index}",
            instrument_id=f"polymarket-home-{event_index}",
        )
        for provider, selection in (
            ("CLOUDBET", home),
            ("SXBET", away_draw),
            ("POLYMARKET", polymarket_home),
        ):
            store.save_normalized_selection(
                NormalizedSelectionRecord(
                    record_id=f"{provider.lower()}-{event_index}",
                    provider=provider,
                    selection=selection,
                    manifest_id=f"manifest-{provider.lower()}",
                ),
            )

    RuleMiner(store).mine_templates_from_store()
    report = build_completion_report(
        store,
        required_providers=providers,
        target_sports=("soccer",),
        min_candidates=10,
        target_candidates=40,
    )

    assert report.total_event_candidates >= 10
    assert report.total_promoted_templates == 0
    assert report.passed is True
    assert report.sports[0].passed is True
    assert report.sports[0].target_reached is False
    tier_counts = dict(report.safety_tier_counts)
    assert tier_counts.get(SafetyTier.EXECUTION_SAFE.value, 0) >= 1


def test_completion_report_fails_when_required_provider_has_no_candidates():
    cache = DictCache()
    store = RuleStore(cache)
    store.save_manifest(
        RuleCorpusManifest(
            manifest_id="manifest-polymarket",
            provider="POLYMARKET",
            fetched_at="2026-04-27T00:00:00Z",
            endpoint_version="test",
            sport_count=1,
            event_count=1,
            selection_count=1,
            market_taxonomy_hash="hash-polymarket",
            source_refs=(),
        ),
    )

    report = build_completion_report(
        store,
        required_providers=("POLYMARKET",),
        target_sports=("basketball",),
        min_candidates=10,
        target_candidates=20,
    )

    assert report.passed is False
    assert report.providers[0].blockers == ("no_normalized_selections", "no_semantic_candidates")
    assert report.sports[0].blockers == ("no_normalized_selections", "no_semantic_candidates")


def test_completion_report_counts_coverage_candidates_toward_sport_gate(tmp_path):
    cache = FileRuleCache(tmp_path / "semantic-cache")
    store = RuleStore(cache)
    store.save_manifest(
        RuleCorpusManifest(
            manifest_id="manifest-cloudbet",
            provider="CLOUDBET",
            fetched_at="2026-05-06T00:00:00Z",
            endpoint_version="test",
            sport_count=1,
            event_count=1,
            selection_count=3,
            market_taxonomy_hash="hash-cloudbet",
            source_refs=(),
        ),
    )

    for selection in ("SCORE_1_0", "SCORE_2_0", "ANY_OTHER_HOME_WIN"):
        store.save_normalized_selection(
            NormalizedSelectionRecord(
                record_id=f"record-{selection.lower()}",
                provider="CLOUDBET",
                manifest_id="manifest-cloudbet",
                selection=NormalizedSelection(
                    venue="CLOUDBET",
                    instrument_id=f"score-{selection.lower()}",
                    sport="ice_hockey",
                    event_key="ice-hockey-event-1",
                    period="full_time",
                    scope="full_time",
                    market_type=CanonicalMarketType.CORRECT_SCORE.value,
                    market_family=CanonicalMarketType.CORRECT_SCORE.value,
                    selection=selection,
                    params=(),
                    raw_market_name="ice_hockey.correct_score",
                    raw_market_type="ice_hockey.correct_score",
                    raw_outcome=selection.lower(),
                    outcome_key=selection.lower(),
                ),
            ),
        )

    RuleMiner(store).mine_coverage_from_store(persist=True)
    report = build_completion_report(
        store,
        required_providers=("CLOUDBET",),
        target_sports=("ice_hockey",),
        min_candidates=1,
        target_candidates=1,
    )

    assert report.passed is True
    assert report.total_event_candidates == 0
    assert report.total_coverage_proofs >= 1
    assert report.total_semantic_candidates >= 1
    assert report.providers[0].coverage_proof_count >= 1
    assert report.sports[0].coverage_proof_count >= 1
    assert report.sports[0].semantic_candidate_count >= 1
    payload = report.to_dict()
    assert payload["sports"][0]["event_candidate_floor_met"] is False
    assert payload["sports"][0]["semantic_candidate_floor_met"] is True
    assert payload["sports"][0]["event_candidate_shortfall"] == 1
    assert payload["sports"][0]["semantic_candidate_shortfall"] == 0


def test_polymarket_coverage_report_records_selection_counts():
    coverage_report = {
        "provider": "POLYMARKET",
        "sports": {
            "tennis": {"event_count": 2, "market_count": 3, "attempts": []},
        },
    }
    records = [
        NormalizedSelectionRecord(
            record_id="pm-tennis-home",
            provider="POLYMARKET",
            manifest_id=None,
            selection=NormalizedSelection(
                venue="POLYMARKET",
                instrument_id="pm-tennis-home",
                sport="tennis",
                event_key="tennis:frances tiafoe:ignacio buse",
                period="full_time",
                scope="full_time",
                market_type=CanonicalMarketType.WINNER.value,
                market_family=CanonicalMarketType.WINNER.value,
                selection="HOME",
                params=(),
                raw_market_name="tennis.moneyline",
                raw_market_type="tennis.moneyline",
                raw_outcome="home",
                outcome_key="home",
            ),
        ),
        NormalizedSelectionRecord(
            record_id="pm-tennis-away",
            provider="POLYMARKET",
            manifest_id=None,
            selection=NormalizedSelection(
                venue="POLYMARKET",
                instrument_id="pm-tennis-away",
                sport="tennis",
                event_key="tennis:frances tiafoe:ignacio buse",
                period="full_time",
                scope="full_time",
                market_type=CanonicalMarketType.WINNER.value,
                market_family=CanonicalMarketType.WINNER.value,
                selection="AWAY",
                params=(),
                raw_market_name="tennis.moneyline",
                raw_market_type="tennis.moneyline",
                raw_outcome="away",
                outcome_key="away",
            ),
        ),
    ]

    SnapshotIngestor._add_polymarket_selection_counts(
        coverage_report=coverage_report,
        normalized_records=records,
    )

    tennis = coverage_report["sports"]["tennis"]
    assert tennis["selection_count"] == 2
    assert tennis["normalized_event_count"] == 1
    assert tennis["normalized_market_count"] == 1


def test_file_rule_cache_recovers_from_torn_key_index(tmp_path):
    cache_dir = tmp_path / "semantic-cache"
    cache = FileRuleCache(cache_dir)
    key = "betting:semantic_rules:index:manifests"
    payload = b"semantic-cache-payload"

    cache.add(key, payload)
    (cache_dir / "keys.json").write_text("{", encoding="utf-8")

    reloaded = FileRuleCache(cache_dir)

    assert reloaded.get(key) == payload
    assert reloaded._key_index == {}


def test_rule_store_immediate_indexes_are_visible_to_reloaded_file_store(tmp_path):
    cache_dir = tmp_path / "semantic-cache"
    store = RuleStore(FileRuleCache(cache_dir))
    rule = RuleClassifier().classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None
    template = SemanticRuleTemplate.from_rule(
        rule,
        promotion_status="PROMOTED",
        safety_tier=SafetyTier.EXECUTION_SAFE.value,
        eligibility_reasons=("execution_safe_complementary_coverage",),
    )

    store.save_promoted_template(template)

    reloaded = RuleStore(FileRuleCache(cache_dir))
    assert reloaded.list_promoted_template_ids() == [template.template_id]
    assert reloaded.load_promoted_template(template.template_id) == template


def test_rule_store_deferred_indexes_flush_at_bulk_context_exit(tmp_path):
    cache_dir = tmp_path / "semantic-cache"
    store = RuleStore(FileRuleCache(cache_dir))
    rule = RuleClassifier().classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None
    template = SemanticRuleTemplate.from_rule(
        rule,
        promotion_status="PROMOTED",
        safety_tier=SafetyTier.EXECUTION_SAFE.value,
        eligibility_reasons=("execution_safe_complementary_coverage",),
    )

    with store.defer_index_writes():
        store.save_promoted_template(template)
        assert store.list_promoted_template_ids() == [template.template_id]
        assert RuleStore(FileRuleCache(cache_dir)).list_promoted_template_ids() == []

    assert RuleStore(FileRuleCache(cache_dir)).list_promoted_template_ids() == [
        template.template_id,
    ]


def test_semantic_rule_mining_cli_reports_tiered_promotion_counts(tmp_path):
    cache_dir = tmp_path / "semantic-cache"
    store = RuleStore(FileRuleCache(cache_dir))
    normalizer = MarketNormalizer()
    store.save_manifest(
        RuleCorpusManifest(
            manifest_id="manifest-cloudbet",
            provider="CLOUDBET",
            fetched_at="2026-04-27T00:00:00Z",
            endpoint_version="test",
            sport_count=1,
            event_count=3,
            selection_count=15,
            market_taxonomy_hash="test",
            source_refs=(),
        ),
    )

    for event_index in range(3):
        dnb_home = replace(
            normalizer.normalize(
                betting_instrument(
                    venue="CLOUDBET",
                    market_name="draw_no_bet",
                    market_type="draw_no_bet",
                    outcome="home",
                ),
            ),
            event_key=f"cli-event-{event_index}",
            instrument_id=f"dnb-home-{event_index}",
        )
        ah_home = replace(
            normalizer.normalize(
                betting_instrument(
                    venue="CLOUDBET",
                    market_name="asian_handicap",
                    market_type="asian_handicap",
                    outcome="home",
                    params="line=0",
                    handicap=0.0,
                ),
            ),
            event_key=f"cli-event-{event_index}",
            instrument_id=f"ah-home-{event_index}",
        )
        match_odds = tuple(
            replace(
                normalizer.normalize(
                    betting_instrument(
                        venue="CLOUDBET",
                        market_name="match_odds",
                        market_type="match_odds",
                        outcome=outcome.lower(),
                    ),
                ),
                event_key=f"cli-event-{event_index}",
                instrument_id=f"match-{outcome.lower()}-{event_index}",
            )
            for outcome in ("HOME", "DRAW", "AWAY")
        )
        for selection in (dnb_home, ah_home, *match_odds):
            store.save_normalized_selection(
                NormalizedSelectionRecord(
                    record_id=selection.instrument_id,
                    provider="CLOUDBET",
                    selection=selection,
                    manifest_id="manifest-cloudbet",
                ),
            )

    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "betting" / "semantic_rule_mining.py"
    common_args = ["--cache-dir", str(cache_dir)]
    subprocess.run(  # noqa: S603
        [sys.executable, str(script), "generalize-templates", *common_args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    coverage = subprocess.run(  # noqa: S603
        [sys.executable, str(script), "mine-coverage", *common_args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    promote = subprocess.run(  # noqa: S603
        [sys.executable, str(script), "promote-templates", *common_args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    report = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(script),
            "report-coverage",
            *common_args,
            "--required-provider",
            "CLOUDBET",
            "--target-sport",
            "soccer",
            "--min-candidates",
            "1",
            "--target-candidates",
            "1",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(script),
            "verify-completion",
            *common_args,
            "--required-provider",
            "CLOUDBET",
            "--target-sport",
            "soccer",
            "--min-candidates",
            "1",
            "--target-candidates",
            "1",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    promote_payload = json.loads(promote.stdout)
    coverage_payload = json.loads(coverage.stdout)
    report_payload = json.loads(report.stdout)

    assert promote_payload["promoted_template_count"] >= 1
    assert promote_payload["same_venue_execution_eligible_template_count"] >= 1
    assert "safety_tier_counts" in promote_payload
    assert coverage_payload["coverage_hyperedge_count"] >= 1
    assert coverage_payload["coverage_proof_count"] >= coverage_payload["coverage_hyperedge_count"]
    assert "coverage_blocker_samples" in coverage_payload
    assert coverage_payload["coverage_blocker_samples"]
    first_coverage_sample = next(iter(coverage_payload["coverage_blocker_samples"].values()))[0]
    assert {
        "proof_id",
        "sport",
        "scope",
        "provider_scope",
        "market_families",
        "instrument_ids",
        "gap_states",
        "risk_states",
    } <= set(first_coverage_sample)
    assert report_payload["same_venue_execution_eligible_template_count"] >= 1
    assert report_payload["coverage_hyperedge_count"] >= 1
    assert "coverage_blocker_counts" in report_payload
    assert "coverage_blocker_samples" in report_payload
    assert report_payload["coverage_blocker_samples"]
    assert "coverage_proof_breakdown" in report_payload
    assert "candidate_safety_tier_counts" in report_payload
    assert "promoted_safety_tier_counts" in report_payload
    assert "provider_template_breakdown" in report_payload
    assert "sport_template_breakdown" in report_payload
    assert "provider_sport_template_breakdown" in report_payload
    assert "CLOUDBET" in report_payload["provider_template_breakdown"]
    assert "soccer" in report_payload["sport_template_breakdown"]
    assert "safety_tier_counts" in report_payload["provider_template_breakdown"]["CLOUDBET"]
    assert (
        "strict_execution_caveat_counts"
        in report_payload["provider_template_breakdown"]["CLOUDBET"]
    )
    assert "coverage_blocker_counts" in report_payload["sport_template_breakdown"]["soccer"]
    assert "coverage_blocker_samples" in report_payload["provider_template_breakdown"]["CLOUDBET"]
    assert "CLOUDBET|soccer" in report_payload["provider_sport_template_breakdown"]
    assert "promoted_template_strictness" in report_payload
    assert "strict_execution_blocker_counts" in report_payload["promoted_template_strictness"]
    assert report_payload["promoted_template_strictness"]["strict_execution_blocker_counts"]
    assert "same_venue_eligible_breakdown" in report_payload["promoted_template_strictness"]
    assert "caveat_counts" in report_payload["promoted_template_strictness"]
    assert "normalized_market_coverage" in report_payload
    assert "template_coverage" in report_payload
    assert "blocker_samples" in report_payload["template_coverage"]
    assert "provider_coverage" in report_payload


def test_historical_validator_handles_missing_manifest_and_mismatched_outcomes():
    cache = DictCache()
    store = RuleStore(cache)
    validator = HistoricalRuleValidator(store)
    rule = RuleClassifier().classify(
        betting_instrument(
            venue="CLOUDBET",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        ),
        betting_instrument(
            venue="SXBET",
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None
    store.save_candidate(rule)

    assert validator.validate_store(manifest_id="missing-manifest") == []

    signature_a = validator._rule_signature(
        sport=rule.sport,
        scope=rule.scope,
        market_type=rule.market_a,
        selection=rule.selection_a,
        params=rule.params_a,
    )
    signature_b = validator._rule_signature(
        sport=rule.sport,
        scope=rule.scope,
        market_type=rule.market_b,
        selection=rule.selection_b,
        params=rule.params_b,
    )
    mismatch_stats = validator._validate_rule(
        rule,
        {
            "event-1": {
                signature_a: {SettlementState.WIN.value},
                signature_b: {SettlementState.WIN.value},
            },
        },
    )

    assert mismatch_stats is not None
    assert mismatch_stats.sample_count == 1
    assert mismatch_stats.match_count == 0
    assert mismatch_stats.mismatch_count == 1
    assert mismatch_stats.confidence == 0.0
    assert validator._validate_rule(rule, {}) is None


def test_historical_validator_cloudbet_observations_fallback_and_error_paths():
    validator = HistoricalRuleValidator(RuleStore(DictCache()))

    assert validator._cloudbet_observations(b"\xff") == []
    assert validator._cloudbet_observations(b'{"not_items": []}') == []

    payload = json.dumps(
        {
            "items": [
                {
                    "betType": "STRAIGHT",
                    "betId": "home-1",
                    "betslipId": "betslip-home-1",
                    "positionId": "position-home-1",
                    "currency": "USDC",
                    "createTime": "2026-04-27T00:00:00Z",
                    "state": "COMPLETED",
                    "result": "WIN",
                    "selection": {
                        "eventId": "fixture-1",
                        "marketUrl": "soccer.match_odds/home",
                        "price": "2.1",
                        "result": "WIN",
                        "marketName": "soccer.match_odds",
                        "outcomeName": "home",
                    },
                },
            ],
            "has_next": False,
        },
    ).encode("utf-8")
    observations = validator._cloudbet_observations(payload)

    assert len(observations) == 1
    assert observations[0].provider == "CLOUDBET"
    assert observations[0].settlement == SettlementState.WIN.value


def test_cloudbet_settlement_handles_none_and_unknown_results():
    validator = HistoricalRuleValidator(RuleStore(DictCache()))

    assert (
        validator._cloudbet_settlement(
            SimpleNamespace(selection=None, selections=[], result=None),
        )
        == SettlementState.UNKNOWN.value
    )
    assert (
        validator._cloudbet_settlement(
            SimpleNamespace(selection=None, selections=[], result="UNEXPECTED"),
        )
        == SettlementState.UNKNOWN.value
    )


def test_completion_report_uses_candidate_rules_without_templates_and_to_dict():
    cache = DictCache()
    store = RuleStore(cache)
    rule = RuleClassifier().classify(
        betting_instrument(
            venue="CLOUDBET",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        ),
        betting_instrument(
            venue="SXBET",
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None
    normalizer = MarketNormalizer()
    store.save_manifest(
        RuleCorpusManifest(
            manifest_id="manifest-cloudbet",
            provider="CLOUDBET",
            fetched_at="2026-04-27T00:00:00Z",
            endpoint_version="test",
            sport_count=1,
            event_count=1,
            selection_count=1,
            market_taxonomy_hash="hash-cloudbet",
            source_refs=(),
        ),
    )
    store.save_manifest(
        RuleCorpusManifest(
            manifest_id="manifest-sxbet",
            provider="SXBET",
            fetched_at="2026-04-27T00:00:00Z",
            endpoint_version="test",
            sport_count=1,
            event_count=1,
            selection_count=1,
            market_taxonomy_hash="hash-sxbet",
            source_refs=(),
        ),
    )
    store.save_normalized_selection(
        NormalizedSelectionRecord(
            record_id="cloudbet-home",
            provider="CLOUDBET",
            selection=normalizer.normalize(
                betting_instrument(
                    venue="CLOUDBET",
                    market_name="match_odds",
                    market_type="match_odds",
                    outcome="home",
                ),
            ),
            manifest_id="manifest-cloudbet",
        ),
    )
    store.save_normalized_selection(
        NormalizedSelectionRecord(
            record_id="sxbet-away-draw",
            provider="SXBET",
            selection=normalizer.normalize(
                betting_instrument(
                    venue="SXBET",
                    market_name="double_chance",
                    market_type="double_chance",
                    outcome="away_draw",
                ),
            ),
            manifest_id="manifest-sxbet",
        ),
    )
    store.save_candidate(rule)

    report = build_completion_report(
        store,
        required_providers=("CLOUDBET", "SXBET"),
        target_sports=("soccer",),
        min_candidates=1,
        target_candidates=2,
    )

    assert report.total_event_candidates == 1
    assert report.total_template_candidates == 0
    assert report.to_dict()["total_event_candidates"] == 1
    assert report.providers[0].passed is True
    assert report.providers[1].passed is True


def test_completion_helpers_report_blockers_and_normalize_sport_aliases():
    provider_report = completion_module._provider_report(
        provider="SXBET",
        manifest_count=0,
        selection_count=0,
        event_candidate_count=0,
        coverage_proof_count=0,
        coverage_hyperedge_count=0,
        template_candidate_count=0,
        promoted_template_count=0,
        execution_safe_template_count=0,
        sports=(),
    )
    sport_report = completion_module._sport_report(
        sport="soccer",
        selection_count=1,
        event_candidate_count=5,
        coverage_proof_count=0,
        coverage_hyperedge_count=0,
        template_candidate_count=0,
        providers=("SXBET",),
        min_candidates=10,
        target_candidates=20,
    )
    base_rule = RuleClassifier().classify(
        betting_instrument(
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        ),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert base_rule is not None
    template = replace(
        SemanticRuleTemplate.from_rule(
            base_rule,
            support=TemplateSupportStats(
                template_id="blocked-template",
                observed_count=5,
                event_count=2,
                provider_count=1,
                providers=("SXBET",),
                sports=("soccer",),
                confidence=0.99,
            ),
        ),
        relationship_type=RelationshipType.DANGEROUS_NON_EQUIVALENT.value,
        safety_tier=SafetyTier.AUDIT_ONLY.value,
        settlement_a=(SettlementState.UNKNOWN.value, SettlementState.HALF_WIN.value),
        settlement_b=(SettlementState.VOID.value,),
    )
    blockers = completion_module._promotion_blockers([template])

    assert provider_report.blockers == (
        "missing_manifest",
        "no_normalized_selections",
        "no_semantic_candidates",
    )
    assert sport_report.blockers == ("below_min_candidate_count",)
    assert blockers["dangerous_non_equivalent"] == 1
    assert blockers["audit_only"] == 1
    assert blockers["unknown_settlement"] == 1
    assert blockers["void_settlement"] == 1
    assert blockers["partial_settlement"] == 1
    assert blockers["observed_count_below_10"] == 1
    assert blockers["event_count_below_3"] == 1
    assert blockers["single_provider_support"] == 1
    assert completion_module.DEFAULT_TARGET_SPORTS == (
        "soccer",
        "basketball",
        "tennis",
        "american_football",
        "ice_hockey",
        "baseball",
    )
    assert completion_module._normalize_sport("Soccer/Football") == "soccer"
    assert completion_module._normalize_sport("Football") == "american_football"
    assert completion_module._normalize_sport("Hockey") == "ice_hockey"
    assert PolymarketSportsTransformer.canonical_sport("Soccer/Football") == "soccer"
    assert PolymarketSportsTransformer.canonical_sport("Hockey") == "ice_hockey"
    assert (
        MarketNormalizer()
        .normalize(
            {
                "provider": "CLOUDBET",
                "sport_name": "Hockey",
                "event_name": "Team A vs Team B",
                "home_name": "Team A",
                "away_name": "Team B",
                "market_name": "ice_hockey.winner",
                "market_type": "ice_hockey.winner",
                "outcome": "home",
            },
        )
        .sport
        == "ice_hockey"
    )


def test_cloudbet_six_sport_alias_resolution_includes_hockey_and_baseball():
    sports = [
        SimpleNamespace(name="Soccer", key="soccer"),
        SimpleNamespace(name="Basketball", key="basketball"),
        SimpleNamespace(name="Tennis", key="tennis"),
        SimpleNamespace(name="American Football", key="american-football"),
        SimpleNamespace(name="Ice Hockey", key="ice-hockey"),
        SimpleNamespace(name="Baseball", key="baseball"),
    ]

    resolved = SnapshotIngestor._resolve_cloudbet_sports(
        requested_sports=[
            "Soccer/Football",
            "basketball",
            "tennis",
            "American Football",
            "Hockey",
            "baseball",
        ],
        available_sports=sports,
    )

    assert resolved == [
        "soccer",
        "basketball",
        "tennis",
        "american-football",
        "ice-hockey",
        "baseball",
    ]


def test_refresh_cloudbet_backfills_recent_past_for_sparse_sports():  # noqa: C901
    store = RuleStore(DictCache())
    ingestor = SnapshotIngestor(store)

    class FakeSport(msgspec.Struct):
        name: str
        key: str

    class FakeSportsResponse(msgspec.Struct):
        sports: list[FakeSport]

    class FakeEventsResponse(msgspec.Struct):
        competitions: tuple[()] = ()

    future_response = FakeEventsResponse()
    past_response = FakeEventsResponse()

    def _selection(event_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            provider="CLOUDBET",
            sport_name="American Football",
            sport_key="american-football",
            event_id=event_id,
            event_name=f"{event_id} home vs away",
            home_name=f"{event_id} home",
            away_name=f"{event_id} away",
            market_name="american_football.winner",
            market_type="american_football.winner",
            outcome="home",
        )

    future_selections = [_selection(f"future-{index}") for index in range(3)]
    past_selections = [_selection(f"past-{index}") for index in range(4)]

    class FakeClient:
        async def get_sports(self):
            return FakeSportsResponse(
                sports=[
                    FakeSport(name="American Football", key="american-football"),
                ],
            )

        async def get_sport(self, sport_key: str) -> dict[str, str]:
            return {"sport": sport_key}

        async def get_events_for_sport(
            self,
            *,
            sport_key: str,
            from_timestamp: int,
            to_timestamp: int,
            limit: int,
        ) -> FakeEventsResponse:
            if to_timestamp <= 1_000:
                return past_response
            return future_response

        def event_to_selection(self, response: FakeEventsResponse) -> list[SimpleNamespace]:
            if response is past_response:
                return list(past_selections)
            return list(future_selections)

        async def get_event(self, event_id: str) -> None:
            return None

    manifest = asyncio.run(
        ingestor.refresh_cloudbet(
            FakeClient(),
            sports=["american_football"],
            from_timestamp=1_000,
            to_timestamp=1_000 + 86_400,
            limit=10,
            adaptive_window=True,
            max_window_seconds=7 * 24 * 60 * 60,
            min_events_per_sport=1,
            include_recent_past_on_sparse=True,
            include_bets=False,
        ),
    )

    assert manifest.selection_count == 4
    coverage_snapshot = None
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if snapshot is not None and snapshot.endpoint == "/semantic/coverage/cloudbet":
            coverage_snapshot = snapshot
            break
    assert coverage_snapshot is not None
    payload = json.loads(coverage_snapshot.payload.decode("utf-8"))
    coverage = payload["sports"]["american-football"]
    assert coverage["event_count"] == 4
    assert coverage["selection_count"] == 4
    assert coverage["sparse_event_threshold"] == 4
    assert coverage["sparse"] is False


def test_refresh_cloudbet_uses_historical_backfill_when_recent_past_stays_sparse():  # noqa: C901
    store = RuleStore(DictCache())
    ingestor = SnapshotIngestor(store)

    class FakeSport(msgspec.Struct):
        name: str
        key: str

    class FakeSportsResponse(msgspec.Struct):
        sports: list[FakeSport]

    class FakeEventsResponse(msgspec.Struct):
        competitions: tuple[()] = ()

    future_response = FakeEventsResponse()
    recent_past_response = FakeEventsResponse()
    historical_response = FakeEventsResponse()

    def _selection(event_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            provider="CLOUDBET",
            sport_name="American Football",
            sport_key="american-football",
            event_id=event_id,
            event_name=f"{event_id} home vs away",
            home_name=f"{event_id} home",
            away_name=f"{event_id} away",
            market_name="american_football.winner",
            market_type="american_football.winner",
            outcome="home",
        )

    future_selections = [_selection("future-0")]
    historical_selections = [_selection(f"historical-{index}") for index in range(5)]

    class FakeClient:
        async def get_sports(self):
            return FakeSportsResponse(
                sports=[FakeSport(name="American Football", key="american-football")],
            )

        async def get_sport(self, sport_key: str) -> dict[str, str]:
            return {"sport": sport_key}

        async def get_events_for_sport(
            self,
            *,
            sport_key: str,
            from_timestamp: int,
            to_timestamp: int,
            limit: int,
        ) -> FakeEventsResponse:
            if from_timestamp <= -10_000_000:
                return historical_response
            if to_timestamp <= 1_000:
                return recent_past_response
            return future_response

        def event_to_selection(self, response: FakeEventsResponse) -> list[SimpleNamespace]:
            if response is historical_response:
                return list(historical_selections)
            if response is recent_past_response:
                return []
            return list(future_selections)

        async def get_event(self, event_id: str) -> None:
            return None

    manifest = asyncio.run(
        ingestor.refresh_cloudbet(
            FakeClient(),
            sports=["american_football"],
            from_timestamp=1_000,
            to_timestamp=1_000 + 86_400,
            limit=10,
            adaptive_window=True,
            max_window_seconds=7 * 24 * 60 * 60,
            sparse_history_window_seconds=180 * 24 * 60 * 60,
            min_events_per_sport=1,
            include_recent_past_on_sparse=True,
            include_bets=False,
        ),
    )

    assert manifest.selection_count == 5
    coverage_snapshot = None
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if snapshot is not None and snapshot.endpoint == "/semantic/coverage/cloudbet":
            coverage_snapshot = snapshot
            break
    assert coverage_snapshot is not None
    payload = json.loads(coverage_snapshot.payload.decode("utf-8"))
    coverage = payload["sports"]["american-football"]
    assert coverage["event_count"] == 5
    assert coverage["selection_count"] == 5
    assert coverage["attempts"][-1]["direction"] == "historical"
    assert coverage["sparse"] is False


class _ConcurrencyFakeSport(msgspec.Struct):
    name: str
    key: str


class _ConcurrencyFakeSportsResponse(msgspec.Struct):
    sports: list[_ConcurrencyFakeSport]


class _ConcurrencyFakeCompetition(msgspec.Struct):
    key: str


class _ConcurrencyFakeEventsResponse(msgspec.Struct):
    competitions: list[_ConcurrencyFakeCompetition]


class _ConcurrencyFakeClient:
    def __init__(
        self,
        sport_keys: list[str],
        latencies: dict[str, float],
        failing_sports: set[str] | None = None,
    ) -> None:
        self.sport_keys = sport_keys
        self.completion_order: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self._latencies = latencies
        self._failing_sports = failing_sports or set()

    async def _pause(self, sport_key: str) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._latencies.get(sport_key, 0.0))
        finally:
            self.in_flight -= 1

    async def get_sports(self) -> _ConcurrencyFakeSportsResponse:
        return _ConcurrencyFakeSportsResponse(
            sports=[_ConcurrencyFakeSport(name=key, key=key) for key in self.sport_keys],
        )

    async def get_sport(self, sport_key: str) -> dict[str, str]:
        await self._pause(sport_key)
        return {"sport": sport_key}

    async def get_events_for_sport(
        self,
        *,
        sport_key: str,
        from_timestamp: int,
        to_timestamp: int,
        limit: int,
    ) -> _ConcurrencyFakeEventsResponse:
        await self._pause(sport_key)
        self.completion_order.append(sport_key)
        return _ConcurrencyFakeEventsResponse(
            competitions=[_ConcurrencyFakeCompetition(key=sport_key)],
        )

    def event_to_selection(
        self,
        response: _ConcurrencyFakeEventsResponse,
    ) -> list[SimpleNamespace]:
        sport_key = response.competitions[0].key
        if sport_key in self._failing_sports:
            raise RuntimeError(f"boom: {sport_key}")
        return [
            SimpleNamespace(
                provider="CLOUDBET",
                sport_name=sport_key,
                sport_key=sport_key,
                event_id=f"{sport_key}-event",
                event_name=f"{sport_key} home vs away",
                home_name=f"{sport_key} home",
                away_name=f"{sport_key} away",
                market_name=f"{sport_key}.winner",
                market_type=f"{sport_key}.winner",
                outcome="home",
            ),
        ]

    async def get_event(self, event_id: str) -> None:
        return None


def _run_concurrency_refresh(client, *, fetch_concurrency):
    store = RuleStore(DictCache())
    ingestor = SnapshotIngestor(store)
    manifest = asyncio.run(
        ingestor.refresh_cloudbet(
            client,
            sports=list(client.sport_keys),
            from_timestamp=1_000,
            to_timestamp=1_000 + 86_400,
            limit=10,
            adaptive_window=False,
            include_bets=False,
            fetch_concurrency=fetch_concurrency,
        ),
    )
    return manifest, store


def _coverage_payload(store: RuleStore) -> dict:
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if snapshot is not None and snapshot.endpoint == "/semantic/coverage/cloudbet":
            return json.loads(snapshot.payload.decode("utf-8"))
    raise AssertionError("coverage snapshot not found")


def test_refresh_cloudbet_concurrent_fetch_preserves_input_sport_order(monkeypatch):
    monkeypatch.setattr(corpus_module, "_utc_now", lambda: "2026-01-01T00:00:00Z")
    sport_keys = [f"sport-{index}" for index in range(6)]
    latencies = {key: (len(sport_keys) - 1 - index) * 0.01 for index, key in enumerate(sport_keys)}

    concurrent_client = _ConcurrencyFakeClient(sport_keys, latencies)
    concurrent_manifest, concurrent_store = _run_concurrency_refresh(
        concurrent_client,
        fetch_concurrency=len(sport_keys),
    )

    assert concurrent_client.peak_in_flight > 1
    assert concurrent_client.completion_order != sport_keys
    assert list(_coverage_payload(concurrent_store)["sports"]) == sport_keys

    sequential_client = _ConcurrencyFakeClient(sport_keys, latencies)
    sequential_manifest, sequential_store = _run_concurrency_refresh(
        sequential_client,
        fetch_concurrency=1,
    )

    assert sequential_client.peak_in_flight == 1
    assert sequential_client.completion_order == sport_keys
    assert concurrent_manifest.source_refs == sequential_manifest.source_refs
    assert concurrent_manifest == sequential_manifest


def test_refresh_cloudbet_concurrent_fetch_isolates_per_sport_failures(monkeypatch):
    monkeypatch.setattr(corpus_module, "_utc_now", lambda: "2026-01-01T00:00:00Z")
    sport_keys = ["sport-0", "sport-1", "sport-2"]

    client = _ConcurrencyFakeClient(sport_keys, {}, failing_sports={"sport-1"})
    manifest, store = _run_concurrency_refresh(client, fetch_concurrency=len(sport_keys))

    coverage = _coverage_payload(store)["sports"]
    assert list(coverage) == sport_keys
    assert coverage["sport-1"] == {
        "event_count": 0,
        "selection_count": 0,
        "error": "RuntimeError",
    }
    assert manifest.selection_count == 2
    assert manifest.event_count == 2


def test_sxbet_six_sport_alias_resolution_includes_hockey_and_baseball():
    resolved = SnapshotIngestor._resolve_sxbet_sport_ids(
        requested_sports=[
            "Soccer/Football",
            "basketball",
            "tennis",
            "American Football",
            "Hockey",
            "baseball",
        ],
        active_sports={
            1: "Basketball",
            2: "Ice Hockey",
            3: "Baseball",
            5: "Soccer",
            6: "Tennis",
            17: "American Football",
        },
    )

    assert resolved == [1, 2, 3, 5, 6, 17]


def test_sxbet_coverage_sport_names_are_canonical():
    assert SnapshotIngestor._canonical_sport_name("Hockey") == "ice_hockey"
    assert SnapshotIngestor._canonical_sport_name("Ice Hockey") == "ice_hockey"
    assert SnapshotIngestor._canonical_sport_name("Soccer/Football") == "soccer"


def test_refresh_sxbet_uses_requested_sports_and_balances_per_sport_budgets(monkeypatch):
    store = RuleStore(DictCache())
    ingestor = SnapshotIngestor(store)
    provider_calls: list[dict[str, object]] = []
    market_page_sizes: list[tuple[int, int]] = []

    class FakeClient:
        async def get_active_sports(self):
            return {
                "data": [
                    {"sportId": 1, "name": "Basketball"},
                    {"sportId": 5, "name": "Soccer"},
                    {"sportId": 6, "name": "Tennis"},
                ],
            }

        async def get_active_leagues(self, *, sport_id):
            return {"sportId": sport_id, "data": []}

        async def get_fixtures(self, *, sport_id, from_time, to_time):
            return {"sportId": sport_id, "from": from_time, "to": to_time, "data": []}

        async def get_markets(self, *, sport_id, page_size, live_only=None):
            market_page_sizes.append((sport_id, page_size, live_only))
            return {"sportId": sport_id, "data": {"markets": []}}

    instruments_by_sport = {
        1: [
            betting_instrument(
                sport="basketball",
                market_name="winner",
                market_type="winner",
                outcome="home",
            ),
        ],
        5: [
            betting_instrument(
                sport="soccer",
                market_name="match_odds",
                market_type="match_odds",
                outcome="home",
            ),
        ],
        6: [
            betting_instrument(
                sport="tennis",
                market_name="winner",
                market_type="winner",
                outcome="away",
            ),
        ],
    }

    class FakeProvider:
        def __init__(self, *, http_client, config):
            self.config = config
            self._sport_id = next(iter(config.sport_ids or []))
            self._instruments = instruments_by_sport[self._sport_id]

        async def load_all_async(self, filters=None):
            provider_calls.append(
                {
                    "sport_ids": sorted(filters.get("sport_ids") or ()),
                    "instrument_load_limit": self.config.instrument_load_limit,
                    "market_discovery_limit": self.config.market_discovery_limit,
                    "live_only": self.config.live_only,
                    "prefer_liquid_markets": self.config.prefer_liquid_markets,
                    "liquidity_probe_limit": self.config.liquidity_probe_limit,
                    "min_two_sided_markets": self.config.min_two_sided_markets,
                },
            )

        def list_all(self):
            return list(self._instruments)

    monkeypatch.setattr(corpus_module, "SXBetInstrumentProvider", FakeProvider)

    manifest = asyncio.run(
        ingestor.refresh_sxbet(
            FakeClient(),
            sports=["Soccer/Football", "basketball", "tennis", "American Football"],
            instrument_limit=7,
            market_discovery_limit=10,
            prefer_liquid_markets=True,
            liquidity_probe_limit=9,
            min_two_sided_markets=2,
            live_only=True,
        ),
    )

    assert manifest.sport_count == 3
    assert manifest.selection_count == 3
    assert provider_calls == [
        {
            "sport_ids": [1],
            "instrument_load_limit": 3,
            "market_discovery_limit": 4,
            "live_only": True,
            "prefer_liquid_markets": True,
            "liquidity_probe_limit": 9,
            "min_two_sided_markets": 2,
        },
        {
            "sport_ids": [5],
            "instrument_load_limit": 2,
            "market_discovery_limit": 3,
            "live_only": True,
            "prefer_liquid_markets": True,
            "liquidity_probe_limit": 9,
            "min_two_sided_markets": 2,
        },
        {
            "sport_ids": [6],
            "instrument_load_limit": 2,
            "market_discovery_limit": 3,
            "live_only": True,
            "prefer_liquid_markets": True,
            "liquidity_probe_limit": 9,
            "min_two_sided_markets": 2,
        },
    ]
    assert market_page_sizes == [(1, 4, True), (5, 3, True), (6, 3, True)]

    coverage_snapshot = None
    for snapshot_id in store.list_snapshot_ids():
        snapshot = store.load_snapshot(snapshot_id)
        if snapshot is not None and snapshot.endpoint == "/semantic/coverage/sxbet":
            coverage_snapshot = snapshot
            break
    assert coverage_snapshot is not None
    payload = json.loads(coverage_snapshot.payload.decode("utf-8"))
    assert payload["coverage_mode"] == "active_live"
    assert payload["live_only"] is True
    assert payload["prefer_liquid_markets"] is True
    assert payload["liquidity_probe_limit"] == 9
    assert payload["min_two_sided_markets"] == 2
    assert payload["requested_sports"] == [
        "american_football",
        "basketball",
        "soccer",
        "tennis",
    ]
    assert payload["resolved_sports"] == ["basketball", "soccer", "tennis"]
    assert payload["unresolved_requested_sports"] == ["american_football"]
    assert payload["sport_ids"] == [1, 5, 6]
    assert payload["sports"]["american_football"] == {
        "sport_id": None,
        "selection_count": 0,
        "instrument_budget": 0,
        "market_discovery_budget": 0,
        "blocker": "not_in_sxbet_active_sports_catalog",
        "requested": True,
    }
    assert payload["sports"]["basketball"]["selection_count"] == 1
    assert payload["sports"]["soccer"]["selection_count"] == 1
    assert payload["sports"]["tennis"]["selection_count"] == 1


def test_semantic_rule_mining_loads_repo_local_workspace_env(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "betting" / "semantic_rule_mining.py"
    spec = importlib.util.spec_from_file_location("semantic_rule_mining_script", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    env_file = tmp_path / ".env.cloud-workspace.local"
    env_file.write_text(
        "CLOUDBET_API_KEY=cloudbet-key\nSXBET_API_KEY=sxbet-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_DEFAULT_LOCAL_ENV_FILES", (env_file,))
    original_cloudbet = os.environ.pop("CLOUDBET_API_KEY", None)
    original_sxbet = os.environ.pop("SXBET_API_KEY", None)
    try:
        loaded = module._load_local_workspace_env()
        assert loaded == env_file
        assert os.environ["CLOUDBET_API_KEY"] == "cloudbet-key"
        assert os.environ["SXBET_API_KEY"] == "sxbet-key"
    finally:
        os.environ.pop("CLOUDBET_API_KEY", None)
        os.environ.pop("SXBET_API_KEY", None)
        if original_cloudbet is not None:
            os.environ["CLOUDBET_API_KEY"] = original_cloudbet
        if original_sxbet is not None:
            os.environ["SXBET_API_KEY"] = original_sxbet


def test_semantic_rule_mining_env_loader_preserves_existing_shell_env(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "betting" / "semantic_rule_mining.py"
    spec = importlib.util.spec_from_file_location("semantic_rule_mining_script_existing", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    env_file = tmp_path / ".env.cloud-workspace.local"
    env_file.write_text("CLOUDBET_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setattr(module, "_DEFAULT_LOCAL_ENV_FILES", (env_file,))
    original_cloudbet = os.environ.get("CLOUDBET_API_KEY")
    os.environ["CLOUDBET_API_KEY"] = "shell-key"
    try:
        module._load_local_workspace_env()
        assert os.environ["CLOUDBET_API_KEY"] == "shell-key"
    finally:
        if original_cloudbet is None:
            os.environ.pop("CLOUDBET_API_KEY", None)
        else:
            os.environ["CLOUDBET_API_KEY"] = original_cloudbet


def test_semantic_rule_mining_refresh_corpus_passes_sports_to_sxbet(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "betting" / "semantic_rule_mining.py"
    spec = importlib.util.spec_from_file_location("semantic_rule_mining_script_sxbet", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    env_file = tmp_path / ".env.cloud-workspace.local"
    env_file.write_text("SXBET_API_KEY=sxbet-key\n", encoding="utf-8")
    monkeypatch.setattr(module, "_DEFAULT_LOCAL_ENV_FILES", (env_file,))
    monkeypatch.setattr(module, "_maybe_linear_comment", lambda body: None)
    monkeypatch.setattr(module, "_build_cache", lambda persist_cache, cache_dir=None: DictCache())

    refresh_calls: list[dict[str, object]] = []

    class FakeManifest(SimpleNamespace):
        manifest_id = "manifest-sxbet"
        provider = "SXBET"
        fetched_at = "2026-05-06T00:00:00Z"
        endpoint_version = "rest:v1"
        sport_count = 2
        event_count = 2
        selection_count = 4
        market_taxonomy_hash = "taxonomy"
        source_refs = ()

    class FakeIngestor:
        def __init__(self, store):
            self.store = store

        async def refresh_sxbet(self, client, **kwargs):
            refresh_calls.append(kwargs)
            return FakeManifest()

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            self.connected = False
            self.disconnected = False

        async def connect(self):
            self.connected = True

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(module, "SnapshotIngestor", FakeIngestor)
    monkeypatch.setattr(module, "SXBetHttpClient", FakeClient)

    args = SimpleNamespace(
        provider="sxbet",
        sports=["soccer", "basketball"],
        sport_ids=[],
        from_timestamp=111,
        to_timestamp=222,
        max_resolution_horizon_hours=None,
        instrument_limit=333,
        market_discovery_limit=444,
        prefer_liquid_markets=True,
        liquidity_probe_limit=55,
        min_two_sided_markets=2,
        live_only=True,
        persist_cache=False,
        cache_dir=None,
        fixture_dir=None,
        limit=20,
        no_adaptive_cloudbet_window=False,
        max_window_days=7,
        min_events_per_sport=1,
        include_past_on_sparse=False,
        include_bets=False,
        skip_bets=True,
        bet_page_size=50,
        bet_max_pages=5,
        bet_from_date=None,
        bet_to_date=None,
        settled_bets=False,
    )

    asyncio.run(module._refresh_corpus(args))

    assert len(refresh_calls) == 1
    call = refresh_calls[0]
    assert call["sports"] == ["soccer", "basketball"]
    assert call["sport_ids"] is None
    assert call["from_time"] == 111
    assert call["to_time"] == 222
    assert call["instrument_limit"] == 333
    assert call["market_discovery_limit"] == 444
    assert call["prefer_liquid_markets"] is True
    assert call["liquidity_probe_limit"] == 55
    assert call["min_two_sided_markets"] == 2
    assert call["live_only"] is True


def test_semantic_rule_mining_refresh_corpus_applies_resolution_horizon():
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "betting" / "semantic_rule_mining.py"
    spec = importlib.util.spec_from_file_location("semantic_rule_mining_script_window", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = SimpleNamespace(
        from_timestamp=1000,
        to_timestamp=None,
        initial_window_seconds=7 * 24 * 60 * 60,
        max_resolution_horizon_hours=48,
    )

    assert module._refresh_corpus_time_window(args) == (1000, 1000 + 48 * 60 * 60)


def test_semantic_rule_mining_provider_coverage_summary_reports_unresolved_targets():
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "betting" / "semantic_rule_mining.py"
    spec = importlib.util.spec_from_file_location(
        "semantic_rule_mining_script_coverage_summary",
        script,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    summary = module._provider_coverage_summary(
        {
            "/semantic/coverage/sxbet": {
                "provider": "SXBET",
                "coverage_mode": "active_live",
                "live_only": True,
                "prefer_liquid_markets": True,
                "requested_sports": ["soccer", "american_football"],
                "resolved_sports": ["soccer"],
                "unresolved_requested_sports": ["american_football"],
                "sports": {
                    "soccer": {"selection_count": 10, "event_count": 2},
                    "american_football": {
                        "selection_count": 0,
                        "blocker": "not_in_sxbet_active_sports_catalog",
                    },
                },
            },
        },
    )

    assert summary["SXBET"] == {
        "coverage_mode": "active_live",
        "live_only": True,
        "prefer_liquid_markets": True,
        "sport_count": 2,
        "sports_with_selections": 1,
        "total_selection_count": 10,
        "total_event_count": 2,
        "total_market_count": 0,
        "requested_sports": ["american_football", "soccer"],
        "resolved_sports": ["soccer"],
        "unresolved_requested_sports": ["american_football"],
        "zero_selection_sports": ["american_football"],
        "sparse_sports": [],
        "blocker_counts": {"not_in_sxbet_active_sports_catalog": 1},
    }


def _cb_baseball(market_name: str, outcome: str, *, home: str, away: str, start: str) -> dict:
    return {
        "provider": "CLOUDBET",
        "sport_name": "baseball",
        "event_id": "cb-1",
        "event_name": f"{home} vs {away}",
        "home_name": home,
        "away_name": away,
        "cutoff_time": start,
        "market_name": market_name,
        "market_type": market_name,
        "outcome": outcome,
    }


def _sx_baseball(outcome: str, *, home: str, away: str, start: str) -> dict:
    return {
        "provider": "SXBET",
        "sport_name": "baseball",
        "event_id": "sx-1",
        "event_name": f"{home} vs {away}",
        "home_name": home,
        "away_name": away,
        "cutoff_time": start,
        "market_name": "match_odds",
        "market_type": "match_odds",
        "outcome": outcome,
        "raw_market_type": 52,
        "info": {
            "raw_market_type": 52,
            "is_two_way_market": True,
            "sxbet_market_hash": f"h-{outcome}",
        },
    }


def test_cloudbet_first_five_innings_never_matches_full_game_identity():
    # PHANTOM GUARD (XV-FIX finding B, must-never-regress). A CloudBet first-5-innings
    # moneyline carries its settlement span only in the market name (no period param).
    # If that span is not parsed the scope collapses to full_time and the F5 market
    # becomes byte-identical to the full-game market of the same fixture, so a
    # first-5-innings leg would spuriously hedge a full-game leg on a different
    # settlement event.
    normalizer = MarketNormalizer()
    start = "2026-07-12T02:40:00Z"
    home, away = "Cleveland Guardians", "Pittsburgh Pirates"

    cb_f5 = normalizer.normalize(
        _cb_baseball(
            "baseball.moneyline_innings_1_to_5",
            "home",
            home=home,
            away=away,
            start=start,
        ),
    )
    cb_full = normalizer.normalize(
        _cb_baseball("baseball.moneyline", "home", home=home, away=away, start=start),
    )
    sx_full = normalizer.normalize(_sx_baseball("home", home=home, away=away, start=start))

    assert cb_f5.scope == "innings_1_to_5"
    assert cb_full.scope == "full_time"
    assert _semantic_identity(cb_f5) != _semantic_identity(cb_full)
    assert _semantic_identity(cb_f5) != _semantic_identity(sx_full)
    # The handicap and totals F5 submarkets share the same name-only span encoding.
    for market_name in (
        "baseball.handicap_innings_1_to_5",
        "baseball.totals_innings_1_to_5",
    ):
        f5 = normalizer.normalize(
            _cb_baseball(market_name, "home", home=home, away=away, start=start),
        )
        assert f5.scope == "innings_1_to_5"


def test_cloudbet_full_game_matches_sxbet_after_mlb_alias_expansion():
    # (b) A CloudBet full-game moneyline quoted under folded abbreviations and an
    # SXBET full-game two-way market for the same fixture share one semantic identity
    # once the MLB abbreviations expand, so a genuine cross-venue template forms.
    normalizer = MarketNormalizer()
    start = "2026-07-12T02:40:00Z"

    cb_full = normalizer.normalize(
        _cb_baseball(
            "baseball.moneyline",
            "home",
            home="LAD Dodgers",
            away="NYY Yankees",
            start=start,
        ),
    )
    sx_full = normalizer.normalize(
        _sx_baseball("home", home="Los Angeles Dodgers", away="New York Yankees", start=start),
    )
    assert cb_full.scope == "full_time"
    assert cb_full.market_type == CanonicalMarketType.WINNER.value
    assert cb_full.event_key == sx_full.event_key
    assert _semantic_identity(cb_full) == _semantic_identity(sx_full)


def test_mini_mine_yields_cross_venue_template_from_abbreviated_cloudbet_names(tmp_path):
    # (b) End-to-end: the same fixture quoted as CloudBet abbreviations and SXBET full
    # names mines a venue-spanning template. Before alias completion these never
    # co-bucketed, so no cross-venue template could exist.
    normalizer = MarketNormalizer()
    start = "2026-07-12T02:40:00Z"

    def cb_record(outcome: str, index: int) -> NormalizedSelectionRecord:
        return NormalizedSelectionRecord(
            record_id=f"cb-{index}",
            provider="CLOUDBET",
            selection=normalizer.normalize(
                _cb_baseball(
                    "baseball.moneyline",
                    outcome,
                    home="LAD Dodgers",
                    away="NYY Yankees",
                    start=start,
                ),
            ),
        )

    def sx_record(outcome: str, index: int) -> NormalizedSelectionRecord:
        return NormalizedSelectionRecord(
            record_id=f"sx-{index}",
            provider="SXBET",
            selection=normalizer.normalize(
                _sx_baseball(
                    outcome,
                    home="Los Angeles Dodgers",
                    away="New York Yankees",
                    start=start,
                ),
            ),
        )

    records = [
        cb_record("home", 0),
        cb_record("away", 1),
        sx_record("home", 0),
        sx_record("away", 1),
    ]
    miner = RuleMiner(RuleStore(FileRuleCache(tmp_path)))
    templates = miner.mine_templates(records, persist=False, persist_event_candidates=False)

    cross_venue = [
        template for template in templates if set(template.provider_scope) == {"CLOUDBET", "SXBET"}
    ]
    assert cross_venue
    assert all(template.scope == "full_time" for template in cross_venue)


def test_same_venue_first_five_and_full_game_are_scope_distinct():
    # (g) The same-venue first-5-innings vs full-game pair used to collapse to one
    # scope and surface as same_market_params_mismatch; distinct scopes keep them
    # apart at the identity level so they are never treated as the same market.
    normalizer = MarketNormalizer()
    start = "2026-07-12T02:40:00Z"
    home, away = "Cleveland Guardians", "Pittsburgh Pirates"

    cb_f5 = normalizer.normalize(
        _cb_baseball(
            "baseball.moneyline_innings_1_to_5",
            "home",
            home=home,
            away=away,
            start=start,
        ),
    )
    cb_full = normalizer.normalize(
        _cb_baseball("baseball.moneyline", "home", home=home, away=away, start=start),
    )
    assert cb_f5.venue == cb_full.venue == "CLOUDBET"
    assert cb_f5.event_key == cb_full.event_key
    assert cb_f5.scope != cb_full.scope
    assert _semantic_identity(cb_f5) != _semantic_identity(cb_full)
