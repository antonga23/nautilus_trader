# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for betting semantic rule mining.
# -------------------------------------------------------------------------------------------------

from dataclasses import replace
from decimal import Decimal
import logging

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.semantics import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics import MarketNormalizer
from nautilus_trader.adapters.betting.semantics import RelationshipType
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.adapters.betting.semantics import RuleValidationStats
from nautilus_trader.examples.strategies.opportunity_graph import OpportunityGraph
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.test_kit.stubs.data import TestDataStubs


class DictCache:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def add(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


def betting_instrument(
    *,
    market_name: str,
    market_type: str,
    outcome: str,
    sport: str = "soccer",
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


def test_normalizer_reconciles_sxbet_raw_type_3_with_line_metadata():
    normalizer = MarketNormalizer()
    spread = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        info={"raw_market_type": 3, "outcome_label": "Team A +1.5"},
    )
    dnb = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="home",
        info={"raw_market_type": 3},
    )

    normalized_spread = normalizer.normalize(spread)
    normalized_dnb = normalizer.normalize(dnb)

    assert normalized_spread.market_type == CanonicalMarketType.ASIAN_HANDICAP.value
    assert normalized_spread.param("line") == "1.5"
    assert normalized_dnb.market_type == CanonicalMarketType.DRAW_NO_BET.value


def test_normalizer_preserves_sxbet_two_way_match_odds_numeric_type_mapping():
    normalizer = MarketNormalizer()
    selection = betting_instrument(
        market_name="match_odds",
        market_type="match_odds",
        outcome="home",
        info={
            "raw_market_type": 1,
            "is_two_way_market": True,
            "sxbet_market_hash": "market-1",
        },
    )

    normalized = normalizer.normalize(selection)

    assert normalized.market_type == CanonicalMarketType.WINNER.value
    assert normalized.selection == "HOME"


def test_sxbet_soccer_match_odds_home_away_do_not_form_phantom_coverage():
    # A draw-capable soccer 1X2 must normalize three-way (MATCH_ODDS), so backing
    # home and away is NOT complementary coverage: a draw loses both legs. Flagging
    # the market two-way (the pre-fix provider behaviour) instead projects it onto a
    # WINNER two-way partition and mints a phantom COMPLEMENTARY_COVERAGE arb.
    normalizer = MarketNormalizer()
    classifier = RuleClassifier(normalizer=normalizer)

    def sxbet_soccer(outcome: str, *, two_way: bool):
        return betting_instrument(
            market_name="match_odds",
            market_type="match_odds",
            outcome=outcome,
            sport="soccer",
            info={
                "raw_market_type": 1,
                "is_two_way_market": two_way,
                "sxbet_market_hash": f"market-{outcome}",
            },
        )

    home = sxbet_soccer("home", two_way=False)
    away = sxbet_soccer("away", two_way=False)
    assert normalizer.normalize(home).market_type == CanonicalMarketType.MATCH_ODDS.value
    assert classifier.classify(home, away) is None

    # Pre-fix flag: the same fixture is wrongly projected two-way and mints the phantom.
    phantom = classifier.classify(
        sxbet_soccer("home", two_way=True),
        sxbet_soccer("away", two_way=True),
    )
    assert phantom is not None
    assert phantom.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value


def test_dnb_home_and_asian_handicap_zero_home_are_equivalent():
    classifier = RuleClassifier()
    dnb_home = betting_instrument(
        market_name="soccer.draw_no_bet",
        market_type="soccer.draw_no_bet_period=ft",
        outcome="home",
        venue="CLOUDBET",
    )
    ah_home_zero = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        params="line=0",
        handicap=0.0,
    )

    rule = classifier.classify(dnb_home, ah_home_zero)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.EQUIVALENT_SELECTION.value
    assert rule.confidence == 1.0
    assert rule.has_void is True
    assert rule.execution_safe is False


def test_match_odds_home_and_double_chance_away_draw_are_safe_coverage():
    classifier = RuleClassifier()
    match_home = betting_instrument(
        market_name="match_odds",
        market_type="match_odds",
        outcome="home",
    )
    double_chance = betting_instrument(
        market_name="double_chance",
        market_type="double_chance",
        outcome="away_draw",
    )

    rule = classifier.classify(match_home, double_chance)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    assert rule.has_void is False
    assert rule.has_partial is False
    assert rule.execution_safe is False
    assert rule.safety_tier == SafetyTier.AUDIT_ONLY.value


def test_totals_match_same_line_and_safe_alternate_line_coverage():
    classifier = RuleClassifier()
    over_two_five = betting_instrument(
        market_name="total_goals",
        market_type="total_goals",
        outcome="over",
        params="line=2.5",
        handicap=2.5,
    )
    under_two_five = betting_instrument(
        market_name="total_goals",
        market_type="total_goals",
        outcome="under",
        params="line=2.5",
        handicap=2.5,
    )
    under_three_five = betting_instrument(
        market_name="total_goals",
        market_type="total_goals",
        outcome="under",
        params="line=3.5",
        handicap=3.5,
    )
    over_three_five = betting_instrument(
        market_name="total_goals",
        market_type="total_goals",
        outcome="over",
        params="line=3.5",
        handicap=3.5,
    )

    same_line = classifier.classify(over_two_five, under_two_five)

    assert same_line is not None
    assert same_line.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    middle_coverage = classifier.classify(over_two_five, under_three_five)
    assert middle_coverage is not None
    assert middle_coverage.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    assert middle_coverage.has_void is False
    assert middle_coverage.has_partial is False
    assert "overlapping_coverage" in middle_coverage.caveats
    assert classifier.classify(over_two_five, over_three_five) is None
    assert classifier.classify(over_three_five, under_two_five) is None


def test_dnb_home_and_dnb_away_are_void_compatible_not_executable():
    classifier = RuleClassifier()
    dnb_home = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="home",
    )
    dnb_away = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="away",
    )

    rule = classifier.classify(dnb_home, dnb_away)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.VOID_COMPATIBLE_HEDGE.value
    assert rule.has_void is True
    assert rule.execution_safe is False


def test_quarter_asian_handicap_pair_is_partial_settlement():
    classifier = RuleClassifier()
    home_plus_quarter = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        params="line=0.25",
        handicap=0.25,
    )
    away_minus_quarter = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="away",
        params="line=-0.25",
        handicap=-0.25,
    )

    rule = classifier.classify(home_plus_quarter, away_minus_quarter)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.PARTIAL_SETTLEMENT_HEDGE.value
    assert rule.has_partial is True
    assert rule.execution_safe is False


def test_european_handicap_and_asian_handicap_same_line_are_dangerous():
    classifier = RuleClassifier()
    asian_home = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        params="line=1",
        handicap=1.0,
    )
    european_home = betting_instrument(
        market_name="european_handicap",
        market_type="european_handicap",
        outcome="home",
        params="line=1",
        handicap=1.0,
    )

    rule = classifier.classify(asian_home, european_home)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.DANGEROUS_NON_EQUIVALENT.value
    assert rule.execution_safe is False


def test_winner_and_half_point_spread_project_to_equivalent_in_no_draw_sport():
    classifier = RuleClassifier()
    moneyline_home = betting_instrument(
        sport="basketball",
        market_name="moneyline_2way",
        market_type="moneyline_2way",
        outcome="home",
        venue="CLOUDBET",
    )
    spread_home_half = betting_instrument(
        sport="basketball",
        market_name="point_spread",
        market_type="point_spread",
        outcome="home",
        params="line=-0.5",
        handicap=-0.5,
    )

    rule = classifier.classify(moneyline_home, spread_home_half)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.EQUIVALENT_SELECTION.value
    assert rule.confidence == 1.0
    assert rule.result_states == ("HOME_WIN", "AWAY_WIN")
    assert rule.settlement_a == ("WIN", "LOSE")
    assert rule.settlement_b == ("WIN", "LOSE")
    assert "cross_family_partition_projection" in rule.caveats
    assert rule.has_void is False
    assert rule.has_partial is False
    assert rule.execution_safe is False


def test_winner_and_half_point_spread_opposite_sides_are_complementary():
    classifier = RuleClassifier()
    moneyline_home = betting_instrument(
        sport="basketball",
        market_name="moneyline_2way",
        market_type="moneyline_2way",
        outcome="home",
        venue="CLOUDBET",
    )
    spread_away_half = betting_instrument(
        sport="basketball",
        market_name="point_spread",
        market_type="point_spread",
        outcome="away",
        params="line=0.5",
        handicap=0.5,
    )

    rule = classifier.classify(moneyline_home, spread_away_half)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value
    assert rule.result_states == ("HOME_WIN", "AWAY_WIN")
    assert rule.settlement_a == ("WIN", "LOSE")
    assert rule.settlement_b == ("LOSE", "WIN")
    assert "cross_family_partition_projection" in rule.caveats
    assert rule.execution_safe is False


def test_winner_and_half_point_spread_stay_rejected_in_draw_capable_sport():
    classifier = RuleClassifier()
    moneyline_home = betting_instrument(
        market_name="moneyline_2way",
        market_type="moneyline_2way",
        outcome="home",
        venue="CLOUDBET",
    )
    spread_home_half = betting_instrument(
        market_name="point_spread",
        market_type="point_spread",
        outcome="home",
        params="line=-0.5",
        handicap=-0.5,
    )

    assert classifier.classify(moneyline_home, spread_home_half) is None


def test_winner_and_spread_projection_requires_exact_half_point_line():
    classifier = RuleClassifier()
    moneyline_home = betting_instrument(
        sport="basketball",
        market_name="moneyline_2way",
        market_type="moneyline_2way",
        outcome="home",
        venue="CLOUDBET",
    )

    for line in ("-1.5", "0", "-0.25"):
        spread = betting_instrument(
            sport="basketball",
            market_name="point_spread",
            market_type="point_spread",
            outcome="home",
            params=f"line={line}",
            handicap=float(line),
        )
        assert classifier.classify(moneyline_home, spread) is None


def test_binary_event_partitions_do_not_project_onto_winner_states():
    classifier = RuleClassifier()
    moneyline_home = betting_instrument(
        sport="basketball",
        market_name="moneyline_2way",
        market_type="moneyline_2way",
        outcome="home",
        venue="CLOUDBET",
    )
    bare_binary_yes = betting_instrument(
        sport="basketball",
        market_name="binary_option",
        market_type="binary_option",
        outcome="yes",
        venue="POLYMARKET",
    )
    team_winner_yes = betting_instrument(
        sport="basketball",
        market_name="moneyline_2way",
        market_type="moneyline_2way",
        outcome="yes",
        params="team=home&period=ft",
    )

    assert classifier.classify(moneyline_home, bare_binary_yes) is None
    assert classifier.classify(moneyline_home, team_winner_yes) is None


def test_rule_store_persists_candidates_promotions_and_validation_stats():
    classifier = RuleClassifier()
    rule = classifier.classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None
    cache = DictCache()
    store = RuleStore(cache)
    stats = RuleValidationStats(
        rule_id=rule.rule_id,
        venue_id="SXBET",
        sport="soccer",
        sample_count=25,
        match_count=25,
        mismatch_count=0,
        confidence=0.99,
        last_validated_at="2026-04-26T00:00:00Z",
    )

    store.save_candidate(rule)
    store.save_promoted(rule)
    store.save_validation(stats)

    assert store.load_candidate(rule.rule_id) == rule
    assert store.load_promoted(rule.rule_id).promotion_status == "PROMOTED"
    assert store.load_validation(rule.rule_id).promotable is True


def test_promotion_policy_marks_equivalent_void_rule_as_same_venue_eligible():
    policy = RulePromotionPolicy()
    rule = RuleClassifier().classify(
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="home"),
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=0",
            handicap=0.0,
        ),
    )
    assert rule is not None

    tier, reasons = policy.classify_rule_tier(
        rule,
        RuleValidationStats(
            rule_id=rule.rule_id,
            venue_id="SXBET",
            sport="soccer",
            sample_count=5,
            match_count=5,
            mismatch_count=0,
            confidence=0.95,
            last_validated_at="2026-04-26T00:00:00Z",
        ),
    )

    assert tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
    assert "provider_scoped_support" in reasons


def test_promotion_policy_marks_complementary_rule_as_execution_safe():
    policy = RulePromotionPolicy(allowlisted_venue_scopes={("SXBET",)})
    rule = RuleClassifier().classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None

    tier, reasons = policy.classify_rule_tier(
        rule,
        RuleValidationStats(
            rule_id=rule.rule_id,
            venue_id="SXBET",
            sport="soccer",
            sample_count=25,
            match_count=25,
            mismatch_count=0,
            confidence=0.99,
            last_validated_at="2026-04-26T00:00:00Z",
        ),
        allowlisted=True,
    )

    assert tier == SafetyTier.EXECUTION_SAFE
    assert "execution_safe_complementary_coverage" in reasons


def test_deterministic_complementary_partition_is_execution_safe_without_statistical_support():
    rule = RuleClassifier().classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value

    starved_support = TemplateSupportStats(
        template_id="cross-venue-starved",
        observed_count=1,
        event_count=1,
        provider_count=2,
        providers=("CLOUDBET", "POLYMARKET"),
        sports=("soccer",),
        mismatch_count=0,
        confidence=1.0,
    )
    template = SemanticRuleTemplate.from_rule(rule, support=starved_support)

    assert template.support.catalog_promotable is False

    tier, reasons = RulePromotionPolicy().classify_template_tier(template, venue_agnostic=True)

    assert tier == SafetyTier.EXECUTION_SAFE
    assert "deterministic_complementary_partition" in reasons
    assert "execution_safe_complementary_coverage" in reasons


def test_promotion_policy_marks_dangerous_rule_as_audit_only():
    policy = RulePromotionPolicy()
    rule = RuleClassifier().classify(
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=1",
            handicap=1.0,
        ),
        betting_instrument(
            market_name="european_handicap",
            market_type="european_handicap",
            outcome="home",
            params="line=1",
            handicap=1.0,
        ),
    )
    assert rule is not None

    tier, _ = policy.classify_rule_tier(rule, None)

    assert tier == SafetyTier.AUDIT_ONLY


def test_promotion_policy_keeps_mismatched_venue_support_topology_only():
    policy = RulePromotionPolicy()
    rule = RuleClassifier().classify(
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="home"),
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=0",
            handicap=0.0,
        ),
    )
    assert rule is not None

    tier, reasons = policy.classify_rule_tier(
        rule,
        RuleValidationStats(
            rule_id=rule.rule_id,
            venue_id="SXBET",
            sport="soccer",
            sample_count=100,
            match_count=98,
            mismatch_count=2,
            confidence=0.99,
            last_validated_at="2026-04-27T00:00:00Z",
        ),
    )

    assert tier == SafetyTier.TOPOLOGY_SAFE
    assert "provider_scoped_support" not in reasons


def test_template_venue_safe_requires_low_mismatch_rate():
    rule = RuleClassifier().classify(
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="home"),
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=0",
            handicap=0.0,
        ),
    )
    assert rule is not None
    template = SemanticRuleTemplate.from_rule(
        rule,
        support=TemplateSupportStats(
            template_id="mismatched-template",
            observed_count=100,
            event_count=10,
            provider_count=1,
            providers=("SXBET",),
            sports=("soccer",),
            mismatch_count=2,
            confidence=0.99,
        ),
    )

    tier, reasons = RulePromotionPolicy().classify_template_tier(template)

    assert template.support.venue_safe is False
    assert tier == SafetyTier.TOPOLOGY_SAFE
    assert "provider_scoped_support" not in reasons


def test_market_matcher_exposes_void_compatible_rule_but_rejects_arbitrage():
    matcher = MarketMatcher()
    dnb_home = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="home",
    )
    dnb_away = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="away",
    )

    hedges = matcher.find_hedges(dnb_home, [dnb_away])

    assert len(hedges) == 1
    assert hedges[0].relationship_type == RelationshipType.VOID_COMPATIBLE_HEDGE.value
    assert hedges[0].execution_safe is False
    assert hedges[0].same_venue_execution_eligible is False
    assert matcher.check_arbitrage(dnb_home, dnb_away) is None


def test_market_matcher_marks_same_venue_venue_safe_rules_as_same_venue_eligible():
    cache = DictCache()
    store = RuleStore(cache)
    policy = RulePromotionPolicy()
    rule = RuleClassifier().classify(
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="home"),
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=0",
            handicap=0.0,
        ),
    )
    assert rule is not None
    assert (
        policy.promote(
            store,
            rule,
            RuleValidationStats(
                rule_id=rule.rule_id,
                venue_id="SXBET",
                sport="soccer",
                sample_count=5,
                match_count=5,
                mismatch_count=0,
                confidence=0.95,
                last_validated_at="2026-04-26T00:00:00Z",
            ),
        )
        is not None
    )

    matcher = MarketMatcher(rule_store=store)
    hedges = matcher.find_hedges(
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="home"),
        [
            betting_instrument(
                market_name="asian_handicap",
                market_type="asian_handicap",
                outcome="home",
                params="line=0",
                handicap=0.0,
            ),
        ],
    )

    assert len(hedges) == 1
    assert hedges[0].safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value
    assert hedges[0].same_venue_execution_eligible is True
    assert hedges[0].execution_safe is False


def test_market_matcher_can_price_same_venue_execution_eligible_probe_pairs():
    cache = DictCache()
    store = RuleStore(cache)
    policy = RulePromotionPolicy()
    instrument = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="home",
    )
    hedge = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        params="line=0",
        handicap=0.0,
    )
    rule = RuleClassifier().classify(instrument, hedge)
    assert rule is not None
    assert (
        policy.promote(
            store,
            rule,
            RuleValidationStats(
                rule_id=rule.rule_id,
                venue_id="SXBET",
                sport="soccer",
                sample_count=5,
                match_count=5,
                mismatch_count=0,
                confidence=0.95,
                last_validated_at="2026-04-26T00:00:00Z",
            ),
        )
        is not None
    )

    matcher = MarketMatcher(rule_store=store)

    assert matcher.check_arbitrage(instrument, hedge) is None

    opportunity = matcher.check_arbitrage(
        instrument,
        hedge,
        odds_a=Decimal("2.30"),
        odds_b=Decimal("2.35"),
        allow_same_venue_execution_eligible=True,
    )

    assert opportunity is not None
    assert opportunity.is_same_venue is True
    assert opportunity.profit_margin > 0


def test_opportunity_graph_keeps_void_edges_but_does_not_evaluate_them():
    matcher = MarketMatcher()
    graph = OpportunityGraph(matcher)
    dnb_home = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="home",
    )
    dnb_away = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="away",
        price=2.2,
    )
    graph.build([dnb_home, dnb_away])
    edge = next(iter(graph.edges_by_id.values()))
    tick_home = TestDataStubs.quote_tick(
        instrument=dnb_home,
        bid_price=2.30,
        ask_price=0.0,
        ts_event=10_000_000_000,
    )
    tick_away = TestDataStubs.quote_tick(
        instrument=dnb_away,
        bid_price=2.45,
        ask_price=0.0,
        ts_event=10_500_000_000,
    )

    graph.update_quote(tick_home, odds=Decimal("2.30"), received_ns=11_000_000_000)
    graph.update_quote(tick_away, odds=Decimal("2.45"), received_ns=11_000_000_000)

    assert graph.edge_count == 1
    assert edge.execution_safe is False
    assert edge.same_venue_execution_eligible is False
    assert edge.relationship_type == RelationshipType.VOID_COMPATIBLE_HEDGE.value
    assert (
        graph.evaluate_updated_node(
            str(dnb_away.id),
            min_profit_margin=Decimal("0.01"),
            now_ns=11_000_000_000,
        )
        == []
    )


def test_opportunity_graph_persists_same_venue_execution_eligibility_without_auto_eval():
    cache = DictCache()
    store = RuleStore(cache)
    policy = RulePromotionPolicy()
    rule = RuleClassifier().classify(
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="home"),
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=0",
            handicap=0.0,
        ),
    )
    assert rule is not None
    assert (
        policy.promote(
            store,
            rule,
            RuleValidationStats(
                rule_id=rule.rule_id,
                venue_id="SXBET",
                sport="soccer",
                sample_count=5,
                match_count=5,
                mismatch_count=0,
                confidence=0.95,
                last_validated_at="2026-04-26T00:00:00Z",
            ),
        )
        is not None
    )

    matcher = MarketMatcher(rule_store=store)
    graph = OpportunityGraph(matcher)
    instrument = betting_instrument(
        market_name="draw_no_bet",
        market_type="draw_no_bet",
        outcome="home",
    )
    hedge = betting_instrument(
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome="home",
        params="line=0",
        handicap=0.0,
    )

    graph.build([instrument, hedge])
    edge = next(iter(graph.edges_by_id.values()))

    assert edge.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value
    assert edge.same_venue_execution_eligible is True
    assert edge.execution_safe is False


def test_rule_store_persists_template_safety_tier():
    cache = DictCache()
    store = RuleStore(cache)
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

    assert (
        store.load_promoted_template(template.template_id).safety_tier
        == SafetyTier.EXECUTION_SAFE.value
    )


def test_promotion_policy_rejects_price_correlation_and_unknown_rule_caveats():
    base_rule = RuleClassifier().classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert base_rule is not None
    policy = RulePromotionPolicy()

    correlation_rule = replace(base_rule, caveats=("price_correlation_only",))
    unknown_rule = replace(base_rule, caveats=("unknown_settlement_present",))

    correlation_tier, correlation_reasons = policy.classify_rule_tier(correlation_rule, None)
    unknown_tier, unknown_reasons = policy.classify_rule_tier(unknown_rule, None)

    assert correlation_tier == SafetyTier.AUDIT_ONLY
    assert correlation_reasons == ("price_correlation_only",)
    assert policy.can_promote(correlation_rule, None) is False
    assert unknown_tier == SafetyTier.AUDIT_ONLY
    assert unknown_reasons == ("unknown_settlement_present",)


def test_promotion_policy_marks_partial_rule_same_venue_eligible_and_promotable():
    cache = DictCache()
    store = RuleStore(cache)
    partial_rule = RuleClassifier().classify(
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=0.25",
            handicap=0.25,
        ),
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="away",
            params="line=-0.25",
            handicap=-0.25,
        ),
    )
    assert partial_rule is not None
    policy = RulePromotionPolicy()
    stats = RuleValidationStats(
        rule_id=partial_rule.rule_id,
        venue_id="SXBET",
        sport="soccer",
        sample_count=5,
        match_count=5,
        mismatch_count=0,
        confidence=0.95,
        last_validated_at="2026-04-26T00:00:00Z",
    )

    tier, reasons = policy.classify_rule_tier(partial_rule, stats)
    promoted = policy.promote(store, partial_rule, stats)

    assert tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
    assert "partial_settlement_present" in reasons
    assert promoted is not None
    assert promoted.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value


def test_promotion_policy_promote_returns_none_for_audit_only_rule():
    cache = DictCache()
    store = RuleStore(cache)
    base_rule = RuleClassifier().classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    assert base_rule is not None
    audit_only_rule = replace(base_rule, caveats=("price_correlation_only",))

    assert RulePromotionPolicy().promote(store, audit_only_rule, None) is None


def test_template_promotion_policy_handles_audit_only_and_partial_templates():
    base_rule = RuleClassifier().classify(
        betting_instrument(market_name="match_odds", market_type="match_odds", outcome="home"),
        betting_instrument(
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
        ),
    )
    partial_rule = RuleClassifier().classify(
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="home",
            params="line=0.25",
            handicap=0.25,
        ),
        betting_instrument(
            market_name="asian_handicap",
            market_type="asian_handicap",
            outcome="away",
            params="line=-0.25",
            handicap=-0.25,
        ),
    )
    assert base_rule is not None
    assert partial_rule is not None
    policy = RulePromotionPolicy()
    cache = DictCache()
    store = RuleStore(cache)

    correlation_template = replace(
        SemanticRuleTemplate.from_rule(
            base_rule,
            support=TemplateSupportStats(
                template_id="corr-template",
                observed_count=12,
                event_count=4,
                provider_count=2,
                providers=("CLOUDBET", "SXBET"),
                sports=("soccer",),
                confidence=0.99,
            ),
        ),
        caveats=("price_correlation_only",),
    )
    unknown_template = replace(correlation_template, caveats=("unknown_settlement_present",))
    partial_template = SemanticRuleTemplate.from_rule(
        partial_rule,
        support=TemplateSupportStats(
            template_id="partial-template",
            observed_count=4,
            event_count=2,
            provider_count=1,
            providers=("SXBET",),
            sports=("soccer",),
            confidence=0.99,
        ),
    )

    correlation_tier, correlation_reasons = policy.classify_template_tier(correlation_template)
    unknown_tier, unknown_reasons = policy.classify_template_tier(unknown_template)
    partial_tier, partial_reasons = policy.classify_template_tier(partial_template)

    assert correlation_tier == SafetyTier.AUDIT_ONLY
    assert correlation_reasons == ("price_correlation_only",)
    assert policy.can_promote_template(correlation_template) is False
    assert policy.promote_template(store, correlation_template) is None
    assert unknown_tier == SafetyTier.AUDIT_ONLY
    assert unknown_reasons == ("unknown_settlement_present",)
    assert partial_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
    assert "partial_settlement_present" in partial_reasons


def test_semantic_node_payload_marks_unnormalized_and_warns(monkeypatch, caplog):
    # Issues #230 / #219: a failed normalization must NOT masquerade as a full_time
    # semantic identity, and the failure must be logged rather than silently swallowed.
    instrument = betting_instrument(
        market_name="match_odds",
        market_type="match_odds",
        outcome="home",
    )

    def _raise(_item):
        raise ValueError("synthetic normalization failure")

    monkeypatch.setattr(MarketNormalizer, "normalize", staticmethod(_raise))

    with caplog.at_level(
        logging.WARNING,
        logger="nautilus_trader.examples.strategies.opportunity_graph",
    ):
        payload = OpportunityGraph._semantic_node_payload(instrument)

    assert payload["semantic_scope"] == "unnormalized"
    assert payload["semantic_scope"] != "full_time"
    assert any(
        record.levelno == logging.WARNING and "normalize failed" in record.getMessage()
        for record in caplog.records
    )


def test_semantic_coverage_summary_logs_malformed_rust_json(caplog):
    # Issue #241: a malformed Rust coverage payload must be logged (with a sample of the
    # raw string) so it is distinguishable from genuinely-empty coverage during diagnostics.
    graph = OpportunityGraph(MarketMatcher(), engine="python")

    class _MalformedRustCore:
        def semantic_coverage_summary_json(self):  # skipcq
            return "{not valid json at all"

    graph._rust_core = _MalformedRustCore()

    with caplog.at_level(
        logging.WARNING,
        logger="nautilus_trader.examples.strategies.opportunity_graph",
    ):
        summary = graph.semantic_coverage_summary()

    assert summary == graph._empty_coverage_summary()
    warning = next(
        (
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "malformed payload" in r.getMessage()
        ),
        None,
    )
    assert warning is not None
    assert "not valid json" in warning.getMessage()


def test_synthetic_line_drops_source_param():
    node = MarketNormalizer.normalize(
        betting_instrument(
            market_name="total_sets",
            market_type="total_sets",
            outcome="over",
            sport="tennis",
            params="total=2.5",
            venue="CLOUDBET",
        ),
    )
    template = MarketNormalizer.normalize(
        betting_instrument(
            market_name="total_sets",
            market_type="total_sets",
            outcome="over",
            sport="tennis",
            params="line=2.5",
            venue="CLOUDBET",
        ),
    )

    assert node.params == (("line", "2.5"),)
    assert node.params == template.params


def test_handicap_source_param_dropped_after_selection_relative_adjustment():
    normalized = MarketNormalizer.normalize(
        betting_instrument(
            market_name="run_line",
            market_type="run_line",
            outcome="away",
            sport="baseball",
            params="handicap=-1",
            handicap=-1.0,
            venue="CLOUDBET",
        ),
    )

    assert normalized.params == (("line", "1"),)


def test_conflicting_total_and_line_params_are_not_collapsed():
    normalized = MarketNormalizer.normalize(
        betting_instrument(
            market_name="total_sets",
            market_type="total_sets",
            outcome="over",
            sport="tennis",
            params="line=2.5&total=3",
            venue="CLOUDBET",
        ),
    )

    assert normalized.params == (("line", "2.5"), ("total", "3"))


def _sxbet_handicap(outcome: str, line: str, *, market_hash: str) -> CryptoBettingInstrument:
    # SX.bet quotes a single team-one (home) relative ``line`` on both outcomes of a
    # handicap market (numeric type 3 -> ASIAN_HANDICAP); the provider stamps it
    # verbatim on both legs, so the raw away leg carries the same sign as the home leg.
    return betting_instrument(
        sport="baseball",
        market_name="asian_handicap",
        market_type="asian_handicap",
        outcome=outcome,
        params=f"line={line}",
        handicap=float(line),
        info={"raw_market_type": 3, "sxbet_market_hash": market_hash},
    )


def test_sxbet_away_handicap_line_is_negated_to_selection_relative():
    # A line=+2.5 SX market: teamOne (home) is +2.5, teamTwo (away) is really -2.5.
    # Both legs arrive with the home-relative "line=2.5"; only the away leg must flip.
    home = MarketNormalizer.normalize(_sxbet_handicap("home", "2.5", market_hash="m1"))
    away = MarketNormalizer.normalize(_sxbet_handicap("away", "2.5", market_hash="m1"))

    assert home.market_type == CanonicalMarketType.ASIAN_HANDICAP.value
    assert home.selection == "HOME"
    assert home.param("line") == "2.5"
    assert away.selection == "AWAY"
    # Pre-fix this stayed "2.5" (home-relative), which wrongly proved the away leg as the
    # complement of a sibling market's "HOME -2.5" and minted a phantom hyperedge.
    assert away.param("line") == "-2.5"


def test_sxbet_away_handicap_phantom_cross_market_edge_does_not_form():
    classifier = RuleClassifier()
    # Observed on the baseball node: an SX "away line=+2.5" leg (really away -2.5) was
    # proven the perfect complement of a sibling market's "home line=-2.5" leg, forming
    # a cross-market edge whose two legs both lose.
    away_plus = _sxbet_handicap("away", "2.5", market_hash="market-a")
    sibling_home_minus = _sxbet_handicap("home", "-2.5", market_hash="market-b")

    # Once the away leg is selection-relative (-2.5) it matches the sibling home leg's
    # side rather than complementing it, so no hedge relationship is proven.
    assert classifier.classify(away_plus, sibling_home_minus) is None


def test_sxbet_same_market_handicap_complement_still_classifies():
    classifier = RuleClassifier()
    # The true two-outcome hedge inside ONE SX market (line=-1.5): home -1.5 vs away
    # +1.5. Both legs arrive raw as "line=-1.5"; negating only the away leg restores
    # the genuine complementary pair (guards against over-negating real hedges).
    home_minus = _sxbet_handicap("home", "-1.5", market_hash="same-market")
    away_plus = _sxbet_handicap("away", "-1.5", market_hash="same-market")

    rule = classifier.classify(home_minus, away_plus)

    assert rule is not None
    assert rule.relationship_type == RelationshipType.COMPLEMENTARY_COVERAGE.value


def _cross_venue_dnb_middle():
    return RuleClassifier().classify(
        betting_instrument(
            market_name="draw_no_bet",
            market_type="draw_no_bet",
            outcome="home",
            venue="CLOUDBET",
        ),
        betting_instrument(
            market_name="draw_no_bet",
            market_type="draw_no_bet",
            outcome="away",
            venue="SXBET",
        ),
    )


def _venue_safe_stats(rule_id: str) -> RuleValidationStats:
    return RuleValidationStats(
        rule_id=rule_id,
        venue_id="CLOUDBET",
        sport="soccer",
        sample_count=5,
        match_count=5,
        mismatch_count=0,
        confidence=0.95,
        last_validated_at="2026-04-26T00:00:00Z",
    )


def test_void_compatible_middle_shared_predicate():
    from nautilus_trader.adapters.betting.semantics import has_only_void_push_settlement_risk
    from nautilus_trader.adapters.betting.semantics import is_void_compatible_middle

    assert is_void_compatible_middle(
        RelationshipType.VOID_COMPATIBLE_HEDGE.value,
        ("void_states_present", "price_correlation_not_proof"),
    )
    # A non-void settlement risk disqualifies it regardless of the flag.
    assert not is_void_compatible_middle(
        RelationshipType.VOID_COMPATIBLE_HEDGE.value,
        ("void_states_present", "partial_states_present"),
    )
    # Only a VOID_COMPATIBLE_HEDGE is a middle; a complementary book with void is not.
    assert not is_void_compatible_middle(
        RelationshipType.COMPLEMENTARY_COVERAGE.value,
        ("void_states_present",),
    )
    assert has_only_void_push_settlement_risk(("void_settlement",))
    assert not has_only_void_push_settlement_risk(("void_settlement", "unknown_settlement"))
    assert not has_only_void_push_settlement_risk(("overlapping_coverage",))


def test_cross_venue_void_middle_stays_same_venue_eligible_without_flag():
    rule = _cross_venue_dnb_middle()
    assert rule is not None
    assert rule.relationship_type == RelationshipType.VOID_COMPATIBLE_HEDGE.value

    tier, reasons = RulePromotionPolicy().classify_rule_tier(
        rule,
        _venue_safe_stats(rule.rule_id),
    )

    assert tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
    assert "execution_safe_void_compatible_middle" not in reasons


def test_cross_venue_void_middle_is_execution_safe_under_flag():
    rule = _cross_venue_dnb_middle()
    assert rule is not None

    tier, reasons = RulePromotionPolicy().classify_rule_tier(
        rule,
        _venue_safe_stats(rule.rule_id),
        allow_void_compatible_middles=True,
    )

    assert tier == SafetyTier.EXECUTION_SAFE
    assert "execution_safe_void_compatible_middle" in reasons


def test_same_venue_void_middle_unchanged_under_flag():
    # A single-venue void middle stays same-venue-eligible even with the flag on: the
    # cross-venue elevation only fires for a genuinely multi-venue scope.
    rule = RuleClassifier().classify(
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="home"),
        betting_instrument(market_name="draw_no_bet", market_type="draw_no_bet", outcome="away"),
    )
    assert rule is not None
    assert rule.venue_scope == ("SXBET",)

    tier, _ = RulePromotionPolicy().classify_rule_tier(
        rule,
        _venue_safe_stats(rule.rule_id),
        allow_void_compatible_middles=True,
    )

    assert tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE
