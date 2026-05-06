# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the Cloudbet-backed semantic mining refresh.
# -------------------------------------------------------------------------------------------------
from dataclasses import replace
from decimal import Decimal
import json
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
    params: str = "",
    venue: str = "SXBET",
    price: float = 2.1,
    handicap: float | None = None,
    info: dict | None = None,
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
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
    assert normalized.scope == "full_time_including_overtime"
    assert normalized.param("handicap") == "3.5"


def test_cloudbet_away_spread_normalizes_home_relative_line_to_selection_line():
    normalizer = MarketNormalizer()
    home = normalizer.normalize(_load_cloudbet_selection("basketball_selections.json", 3))
    away = normalizer.normalize(_load_cloudbet_selection("basketball_selections.json", 4))

    assert away.market_type == CanonicalMarketType.POINT_SPREAD.value
    assert away.selection == "AWAY"
    assert away.param("handicap") == "-3.5"
    assert away.param("line") == "-3.5"
    rule = RuleClassifier().classify(home, away)
    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value


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
    assert normalized.param("handicap") == "-0.25"
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


def test_sxbet_negative_three_quarter_handicap_is_supported():
    classifier = RuleClassifier()
    home_minus_three_quarter = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        params="line=-3.25",
        handicap=-3.25,
    )
    away_plus_three_quarter = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="away",
        params="line=3.25",
        handicap=3.25,
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
                "resolution_policy": {"tie_or_unknown": "50_50"},
            },
        },
    )

    transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(instrument)
    normalized = MarketNormalizer().normalize(instrument)

    assert transformed is not None
    assert transformed.market_name == "basketball.moneyline"
    assert transformed.outcome == "home"
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

    sxbet_away_plus = betting_instrument(
        venue="SXBET",
        sport="basketball",
        market_name="spread",
        market_type="spread",
        outcome="away",
        params="line=4.5",
        handicap=4.5,
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


def test_polymarket_corpus_skips_outrights_without_event_participants():
    ingestor = SnapshotIngestor(RuleStore(DictCache()))

    class Transformer:
        @staticmethod
        def to_crypto_betting_instrument(_instrument):
            return betting_instrument(
                venue="POLYMARKET",
                market_name="american_football.winner_binary",
                market_type="american_football.winner",
                outcome="yes",
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

    assert records == []
    assert sports == set()
    assert event_keys == set()
    assert market_names == set()


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
    assert {candidate.evidence_event_key for candidate in candidates if candidate is not None} == {
        "american-football-event-0",
        "american-football-event-1",
    }
    assert len(templates) == 1
    assert templates[0].support.observed_count == 2
    assert templates[0].support.event_count == 2


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
    assert "promoted_template_strictness" in report_payload
    assert "strict_execution_blocker_counts" in report_payload["promoted_template_strictness"]
    assert report_payload["promoted_template_strictness"]["strict_execution_blocker_counts"]
    assert "same_venue_eligible_breakdown" in report_payload["promoted_template_strictness"]
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
