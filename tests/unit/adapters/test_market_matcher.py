# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for MarketMatcher.
# -------------------------------------------------------------------------------------------------
# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212, PYL-R0903
# skipcq: PYL-R0904, PYL-R0913, PYL-C0302, PYL-E0611
# pylint: disable=duplicate-code,missing-module-docstring,missing-class-docstring
# pylint: disable=missing-function-docstring,protected-access
# pylint: disable=too-few-public-methods,too-many-public-methods
# pylint: disable=too-many-arguments,too-many-lines,no-name-in-module

from decimal import Decimal
from decimal import DivisionByZero

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.semantics import PromotionStatus
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency


class DictCache:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def add(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)


class StubRuleClassifier:
    def __init__(self, rule) -> None:
        self._rule = rule

    def classify(self, *_args, **_kwargs):
        return self._rule


def make_instrument(
    *,
    venue: str = "SXBET",
    event_id: str = "event-123",
    event_name: str = "Team A vs Team B",
    home_name: str = "Team A",
    away_name: str = "Team B",
    market_name: str = "Total Goals",
    market_type: str = "total_goals",
    outcome: str,
    price: float = 2.10,
    params: str = "",
    handicap: float | None = None,
    info: dict | None = None,
    start_time: str | None = "2026-03-13T18:00:00Z",
    sport_name: str = "soccer",
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id=event_id,
        event_name=event_name,
        home_name=home_name,
        away_name=away_name,
        sport_name=sport_name,
        competition_name="Test League",
        market_name=market_name,
        market_type=market_type,
        outcome=outcome,
        side=SelectionSide.BACK,
        price=price,
        currency=Currency.from_str("USDT"),
        params=params,
        handicap=handicap,
        info=info or {},
        start_time=start_time,
    )


class TestMarketMatcher:
    """
    Test MarketMatcher for finding hedges and arbitrage.
    """

    @pytest.fixture
    def market_matcher(self):
        """
        Create a MarketMatcher instance.
        """
        return MarketMatcher()

    @pytest.fixture
    def sample_instrument_a(self):
        """
        Create a sample betting instrument.
        """
        return CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-123",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Total Goals",
            market_type="total_goals",
            outcome="over",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            params="",
            start_time="2026-03-13T18:00:00Z",
        )

    @pytest.fixture
    def sample_instrument_b(self):
        """
        Create a hedging instrument.
        """
        return CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-123",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Total Goals",
            market_type="total_goals",
            outcome="under",
            side=SelectionSide.LAY,
            price=2.05,
            currency=Currency.from_str("USDT"),
            params="",
            start_time="2026-03-13T18:00:00Z",
        )

    def test_find_hedges_same_market(
        self,
        market_matcher,
        sample_instrument_a,
        sample_instrument_b,
    ):
        """
        Test finding hedges in the same market.
        """
        hedges = market_matcher.find_hedges(
            instrument=sample_instrument_a,
            candidates=[sample_instrument_b],
            include_cross_venue=True,
        )

        assert len(hedges) == 1
        assert hedges[0].instrument == sample_instrument_b
        assert hedges[0].match_type == "same_market"

    def test_find_hedges_uses_fixture_aliases_for_cloudbet_sxbet_screenshot_case(
        self,
        market_matcher,
    ):
        """
        Cloudbet/SXBET DNB screenshot names should not fail fixture identity.
        """
        cloudbet = make_instrument(
            venue="CLOUDBET",
            event_id="cloudbet-min-sa",
            event_name="MIN Timberwolves v SA Spurs",
            home_name="MIN Timberwolves",
            away_name="SA Spurs",
            sport_name="basketball",
            market_name="draw_no_bet",
            market_type="draw_no_bet",
            outcome="home",
            price=6.402,
        )
        sxbet = make_instrument(
            venue="SXBET",
            event_id="sxbet-min-sa",
            event_name="Minnesota Timberwolves vs San Antonio Spurs",
            home_name="Minnesota Timberwolves",
            away_name="San Antonio Spurs",
            sport_name="basketball",
            market_name="draw_no_bet",
            market_type="draw_no_bet",
            outcome="away",
            price=1.454,
        )

        hedges = market_matcher.find_hedges(cloudbet, [sxbet])

        assert len(hedges) == 1
        assert hedges[0].match_type == "same_market"
        assert hedges[0].instrument is sxbet

    def test_check_arbitrage_opportunity(
        self,
        market_matcher,
        sample_instrument_a,
        sample_instrument_b,
    ):
        """
        Test checking for arbitrage opportunity.
        """
        opportunity = market_matcher.check_arbitrage(
            instrument_a=sample_instrument_a,
            instrument_b=sample_instrument_b,
        )

        assert opportunity is not None
        assert opportunity.profit_margin > 0
        total_prob = (Decimal(1) / Decimal("2.10")) + (Decimal(1) / Decimal("2.05"))
        expected_margin = (Decimal(1) / total_prob) - Decimal(1)
        assert pytest.approx(float(expected_margin), rel=1e-6) == float(opportunity.profit_margin)
        assert opportunity.odds_a == Decimal("2.10")
        assert opportunity.odds_b == Decimal("2.05")

    def test_check_arbitrage_uses_live_odds_overrides(
        self,
        market_matcher,
        sample_instrument_a,
        sample_instrument_b,
    ):
        """
        Test live quote odds override instrument snapshot prices for arbitrage.
        """
        opportunity = market_matcher.check_arbitrage(
            instrument_a=sample_instrument_a,
            instrument_b=sample_instrument_b,
            odds_a=Decimal("2.40"),
            odds_b=Decimal("2.55"),
        )

        assert opportunity is not None
        assert opportunity.odds_a == Decimal("2.40")
        assert opportunity.odds_b == Decimal("2.55")
        assert opportunity.probability_a == Decimal(1) / Decimal("2.40")
        assert opportunity.probability_b == Decimal(1) / Decimal("2.55")

    def test_check_arbitrage_honors_explicit_zero_override(
        self,
        market_matcher,
        sample_instrument_a,
        sample_instrument_b,
    ):
        """
        Test explicit zero overrides are not replaced by snapshot prices.
        """
        with pytest.raises(DivisionByZero):
            market_matcher.check_arbitrage(
                instrument_a=sample_instrument_a,
                instrument_b=sample_instrument_b,
                odds_a=Decimal(0),
                odds_b=Decimal("2.55"),
            )

    def test_two_way_moneyline_match_odds_can_hedge(self, market_matcher):
        """
        Test two-way moneyline markets can hedge despite match_odds normalization.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="moneyline-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="basketball",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            info={"is_two_way_market": True},
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="moneyline-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="basketball",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.05,
            currency=Currency.from_str("USDT"),
            info={"is_two_way_market": True},
        )

        hedges = market_matcher.find_hedges(inst_a, [inst_b])
        opportunity = market_matcher.check_arbitrage(inst_a, inst_b)

        assert len(hedges) == 1
        assert opportunity is not None

    def test_unknown_outcomes_keep_distinct_selection_keys(self):
        """
        Test venue-specific outcome labels do not collapse into one selection key.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="custom-outcome",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Special Market",
            market_type="other",
            outcome="outcome_1",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="custom-outcome",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Special Market",
            market_type="other",
            outcome="outcome_2",
            side=SelectionSide.BACK,
            price=2.05,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )

        assert inst_a.selection_key() == "outcome_1"
        assert inst_b.selection_key() == "outcome_2"
        assert inst_a.matches_selection(inst_b) is False

    def test_draw_vs_home_away_double_chance_is_supported(self, market_matcher):
        """
        Test draw selections can hedge against double-chance 12 selections.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="draw",
            side=SelectionSide.BACK,
            price=3.40,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Double Chance",
            market_type="double_chance",
            outcome="home_away",
            side=SelectionSide.BACK,
            price=1.50,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )

        hedges = market_matcher.find_hedges(inst_a, [inst_b])

        assert len(hedges) == 1
        assert hedges[0].match_type == "cross_market"

    def test_no_arbitrage_with_poor_odds(self, market_matcher):
        """
        Test that no arbitrage is found with unfavorable odds.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("VENUE1"),
            event_id="test-1",
            event_name="Test Event",
            home_name="Home",
            away_name="Away",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=1.8,
            currency=Currency.from_str("USDT"),
            params="",
        )

        inst_b = CryptoBettingInstrument(
            venue=Venue("VENUE2"),
            event_id="test-1",
            event_name="Test Event",
            home_name="Home",
            away_name="Away",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.LAY,
            price=1.8,
            currency=Currency.from_str("USDT"),
            params="",
        )

        opportunity = market_matcher.check_arbitrage(inst_a, inst_b)

        # Should return None or negative profit margin
        assert opportunity is None or opportunity.profit_margin < 0

    def test_matches_event_cross_venue_with_normalized_teams(self):
        """
        Test cross-venue event matching uses sport and participants, not raw event IDs.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="venue-a-1",
            event_name="Team A vs Team B",
            home_name="Team A FC",
            away_name="Team B",
            sport_name="Soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="venue-b-9",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )

        assert inst_a.matches_event(inst_b) is True
        assert inst_a.is_opposite_outcome(inst_b) is False

    def test_match_events_cross_venue_handles_swapped_home_away(self, market_matcher):
        """
        Test selection matching follows participant identity when venue ordering
        differs.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="venue-a-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="venue-b-9",
            event_name="Team B vs Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )

        matches = market_matcher.match_events_cross_venue([inst_a], [inst_b])

        assert matches == [(inst_a, inst_b)]

    def test_match_events_cross_venue_rejects_swapped_same_label(self, market_matcher):
        """
        Test a same-label HOME selection is not matched when venue ordering is reversed.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="venue-a-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="venue-b-9",
            event_name="Team B vs Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )

        matches = market_matcher.match_events_cross_venue([inst_a], [inst_b])

        assert matches == []

    def test_match_odds_home_vs_away_is_not_treated_as_full_hedge(self, market_matcher):
        """
        Test 1X2 match odds are not treated as a complete two-way hedge.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )

        assert market_matcher.find_hedges(inst_a, [inst_b]) == []
        assert market_matcher.check_arbitrage(inst_a, inst_b) is None

    def test_matches_event_cross_venue_allows_missing_start_time(self):
        """
        Test cross-venue matching falls back to normalized participants when timing
        metadata is missing.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
        )

        assert inst_a.matches_event(inst_b) is True

    def test_match_events_cross_venue_allows_missing_start_time(self, market_matcher):
        """
        Test cross-venue selection matching can proceed when one venue omits the event
        start time.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
        )

        matches = market_matcher.match_events_cross_venue([inst_a], [inst_b])

        assert matches == [(inst_a, inst_b)]

    def test_match_events_cross_venue_skips_ambiguous_missing_start_time(self, market_matcher):
        """
        Test missing-time events do not match when multiple fixture times exist.
        """
        inst_a_early = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_a_late = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-14T18:00:00Z",
        )
        inst_b_missing_time = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-3",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
        )

        matches = market_matcher.match_events_cross_venue(
            [inst_a_early, inst_a_late],
            [inst_b_missing_time],
        )

        assert matches == []

    def test_match_events_cross_venue_skips_same_day_doubleheader_with_start_times(
        self,
        market_matcher,
    ):
        """
        Test a same-day doubleheader does not match even when both legs have times.

        Both source-venue games fall inside the cross-venue soft start-time tolerance of
        the single opposing fixture, so the target cannot be uniquely attributed
        (#231/#237). Prior to the fix the both-start-times branch asserted a match
        against an arbitrary one of the two games.

        """
        game_1 = make_instrument(
            venue="CLOUDBET",
            event_id="cb-dh-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            start_time="2026-03-13T18:00:00Z",
        )
        game_2 = make_instrument(
            venue="CLOUDBET",
            event_id="cb-dh-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            start_time="2026-03-13T21:00:00Z",
        )
        target = make_instrument(
            venue="SXBET",
            event_id="sx-dh",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            start_time="2026-03-13T19:30:00Z",
        )

        matches = market_matcher.match_events_cross_venue([game_1, game_2], [target])

        assert matches == []

    def test_match_events_cross_venue_allows_unique_missing_start_time_without_time_signal(
        self,
        market_matcher,
    ):
        """
        Test missing-time events can match when participant evidence is unique.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
        )

        matches = market_matcher.match_events_cross_venue([inst_a], [inst_b])

        assert matches == [(inst_a, inst_b)]

    def test_find_hedges_skips_ambiguous_cross_venue_missing_start_time(self, market_matcher):
        """
        Test hedge discovery does not pair ambiguous cross-venue fixtures.
        """
        inst_a_early = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_a_late = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-14T18:00:00Z",
        )
        inst_b_missing_time = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-3",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
        )

        hedges = market_matcher.find_hedges(
            inst_b_missing_time,
            [inst_a_early, inst_a_late],
        )

        assert hedges == []
        diagnostics = market_matcher.explain_hedge_event_match(
            inst_b_missing_time,
            inst_a_early,
            [inst_a_late],
        )
        assert diagnostics["matched"] is False
        assert diagnostics["reason"] == "ambiguous_missing_start_time"
        assert diagnostics["sameFixture"] is True
        assert diagnostics["sameVenue"] is False

    def test_find_hedges_allows_cross_venue_missing_start_time_with_single_fixture_cluster(
        self,
        market_matcher,
    ):
        """
        Test hedge discovery still allows unambiguous missing-time cross-venue pairs.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
            info={"is_two_way_market": True},
        )
        inst_b_missing_time = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team B at Team A",
            home_name="Team B",
            away_name="Team A",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            info={"is_two_way_market": True},
        )

        hedges = market_matcher.find_hedges(inst_b_missing_time, [inst_a])

        assert len(hedges) == 1
        diagnostics = market_matcher.explain_hedge_event_match(inst_b_missing_time, inst_a, [])
        assert diagnostics["matched"] is True
        assert diagnostics["reason"] == "cross_venue_unique_missing_start_time"

    def test_find_hedges_uses_fixture_proof_when_missing_start_time_event_keys_drift(
        self,
        market_matcher,
    ):
        """
        Cross-venue discovery must not require exact no-time event keys after the
        fixture resolver has already produced a single unambiguous proof.
        """
        cloudbet = make_instrument(
            venue="CLOUDBET",
            event_id="cloudbet-cle-min",
            event_name="Cleveland v Minnesota",
            home_name="Cleveland",
            away_name="Minnesota",
            sport_name="basketball",
            outcome="over",
            start_time=None,
        )
        sxbet = make_instrument(
            venue="SXBET",
            event_id="sxbet-cle-min",
            event_name="Cleveland Bears v Minnesota Wolves",
            home_name="Cleveland Bears",
            away_name="Minnesota Wolves",
            sport_name="basketball",
            outcome="under",
            start_time="2026-03-13T18:00:00Z",
        )

        hedges = market_matcher.find_hedges(cloudbet, [sxbet])

        assert len(hedges) == 1

    def test_find_hedges_allows_unique_cross_venue_start_time_conflict(
        self,
        market_matcher,
    ):
        cloudbet = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Minnesota Timberwolves vs San Antonio Spurs",
            home_name="Minnesota Timberwolves",
            away_name="San Antonio Spurs",
            sport_name="basketball",
            competition_name="NBA",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T10:00:00Z",
            info={"is_two_way_market": True},
        )
        sxbet = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="MIN Timberwolves v SA Spurs",
            home_name="MIN Timberwolves",
            away_name="SA Spurs",
            sport_name="basketball",
            competition_name="NBA",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T14:00:00Z",
            info={"is_two_way_market": True},
        )

        hedges = market_matcher.find_hedges(sxbet, [cloudbet])

        assert len(hedges) == 1
        diagnostics = market_matcher.explain_hedge_event_match(sxbet, cloudbet, [])
        assert diagnostics["matched"] is True
        assert diagnostics["reason"] == "cross_venue_unique_start_time_conflict"

    def test_find_hedges_allows_unique_cross_venue_date_only_start_time_conflict(
        self,
        market_matcher,
    ):
        polymarket = CryptoBettingInstrument(
            venue=Venue("POLYMARKET"),
            event_id="event-1",
            event_name="Baltimore Orioles vs Washington Nationals",
            home_name="Baltimore Orioles",
            away_name="Washington Nationals",
            sport_name="baseball",
            competition_name="MLB",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDC"),
            start_time="2026-05-15T00:00:00Z",
            info={"is_two_way_market": True},
        )
        sxbet = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Baltimore Orioles v Washington Nationals",
            home_name="Baltimore Orioles",
            away_name="Washington Nationals",
            sport_name="baseball",
            competition_name="MLB",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDC"),
            start_time="2026-05-15T22:45:00Z",
            info={"is_two_way_market": True},
        )

        hedges = market_matcher.find_hedges(sxbet, [polymarket])

        assert len(hedges) == 1
        diagnostics = market_matcher.explain_hedge_event_match(sxbet, polymarket, [])
        assert diagnostics["matched"] is True
        assert diagnostics["reason"] == "cross_venue_unique_start_time_conflict"

    def test_find_hedges_rejects_ambiguous_cross_venue_start_time_conflict(
        self,
        market_matcher,
    ):
        cloudbet_early = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Minnesota Timberwolves vs San Antonio Spurs",
            home_name="Minnesota Timberwolves",
            away_name="San Antonio Spurs",
            sport_name="basketball",
            competition_name="NBA",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T10:00:00Z",
            info={"is_two_way_market": True},
        )
        cloudbet_late = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-2",
            event_name="Minnesota Timberwolves vs San Antonio Spurs",
            home_name="Minnesota Timberwolves",
            away_name="San Antonio Spurs",
            sport_name="basketball",
            competition_name="NBA",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
            info={"is_two_way_market": True},
        )
        sxbet = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-3",
            event_name="MIN Timberwolves v SA Spurs",
            home_name="MIN Timberwolves",
            away_name="SA Spurs",
            sport_name="basketball",
            competition_name="NBA",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.0,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T14:00:00Z",
            info={"is_two_way_market": True},
        )

        hedges = market_matcher.find_hedges(sxbet, [cloudbet_early, cloudbet_late])

        assert hedges == []
        diagnostics = market_matcher.explain_hedge_event_match(
            sxbet,
            cloudbet_early,
            [cloudbet_late],
        )
        assert diagnostics["matched"] is False
        assert diagnostics["reason"] == "ambiguous_start_time_conflict"

    def test_find_hedges_uses_fixture_alias_keys_for_cross_venue_missing_start_time(
        self,
        market_matcher,
    ):
        """
        Test alias-proven fixtures are not rejected by exact event-key drift.
        """
        cloudbet = make_instrument(
            venue="CLOUDBET",
            event_id="cloudbet-event",
            event_name="Cleveland Cavaliers v Minnesota Timberwolves",
            home_name="Cleveland Cavaliers",
            away_name="Minnesota Timberwolves",
            outcome="over",
            params="2.5",
            start_time="2026-03-13T18:00:00Z",
            sport_name="basketball",
        )
        cloudbet_alias = make_instrument(
            venue="CLOUDBET",
            event_id="cloudbet-event-alias",
            event_name="CLE Cavs v MIN Timberwolves",
            home_name="CLE Cavs",
            away_name="MIN Timberwolves",
            outcome="over",
            params="2.5",
            start_time="2026-03-13T18:00:00Z",
            sport_name="basketball",
        )
        sxbet = make_instrument(
            venue="SXBET",
            event_id="sxbet-event",
            event_name="CLE Cavs @ MIN Timberwolves",
            home_name="CLE Cavs",
            away_name="MIN Timberwolves",
            outcome="under",
            params="2.5",
            start_time=None,
            sport_name="basketball",
        )

        hedges = market_matcher.find_hedges(sxbet, [cloudbet, cloudbet_alias])

        assert len(hedges) == 2

    def test_crypto_betting_instrument_min_price_is_not_snapshot_floor(self, sample_instrument_a):
        """
        Test betting instruments do not use the initial odds snapshot as min_price.
        """
        assert sample_instrument_a.min_price is None

    def test_check_arbitrage_rejects_draw_no_bet_pairs(self, market_matcher):
        """
        Test push-capable draw-no-bet markets are not treated as guaranteed arbitrage.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Draw No Bet",
            market_type="draw_no_bet",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Draw No Bet",
            market_type="draw_no_bet",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            start_time="2026-03-13T18:00:00Z",
        )

        hedges = market_matcher.find_hedges(inst_a, [inst_b])

        assert len(hedges) == 1
        assert market_matcher.check_arbitrage(inst_a, inst_b) is None

    def test_check_arbitrage_rejects_asian_handicap_pairs(self, market_matcher):
        """
        Test push-capable asian handicap markets are not treated as guaranteed
        arbitrage.
        """
        inst_a = CryptoBettingInstrument(
            venue=Venue("CLOUDBET"),
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Asian Handicap",
            market_type="asian_handicap",
            outcome="home",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            handicap=0.0,
            start_time="2026-03-13T18:00:00Z",
        )
        inst_b = CryptoBettingInstrument(
            venue=Venue("SXBET"),
            event_id="event-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
            competition_name="Test League",
            market_name="Asian Handicap",
            market_type="asian_handicap",
            outcome="away",
            side=SelectionSide.BACK,
            price=2.10,
            currency=Currency.from_str("USDT"),
            handicap=0.0,
            start_time="2026-03-13T18:00:00Z",
        )

        hedges = market_matcher.find_hedges(inst_a, [inst_b])

        assert len(hedges) == 1
        assert market_matcher.check_arbitrage(inst_a, inst_b) is None

    def test_arbitrage_opportunity_repr_and_negative_margin_property(
        self,
        sample_instrument_a,
        sample_instrument_b,
    ):
        opportunity = ArbitrageOpportunity(
            instrument_a=sample_instrument_a,
            instrument_b=sample_instrument_b,
            probability_a=Decimal("0.50"),
            probability_b=Decimal("0.51"),
            total_probability=Decimal("1.01"),
            profit_margin=Decimal("-0.01"),
            odds_a=Decimal("2.00"),
            odds_b=Decimal("1.96"),
            is_same_venue=False,
            match_type="cross_market",
        )

        assert opportunity.is_arbitrage is False
        assert "profit=-1.00%" in repr(opportunity)

    def test_find_hedges_respects_include_cross_venue_flag(self, market_matcher):
        inst_a = make_instrument(venue="CLOUDBET", outcome="over", params="line=2.5")
        inst_b = make_instrument(
            venue="SXBET",
            outcome="under",
            params="line=2.5",
        )

        assert market_matcher.find_hedges(inst_a, [inst_b], include_cross_venue=False) == []

    def test_find_hedges_filters_semantic_candidates_below_min_confidence(self):
        instrument_a = make_instrument(
            venue="CLOUDBET",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = make_instrument(
            venue="SXBET",
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
            price=1.85,
        )
        rule = RuleClassifier().classify(instrument_a, instrument_b)
        assert rule is not None

        matcher = MarketMatcher(
            min_confidence=1.01,
            rule_classifier=StubRuleClassifier(rule),
        )

        assert matcher.find_hedges(instrument_a, [instrument_b]) == []

    def test_resolve_rule_uses_promoted_template_from_store(self):
        cache = DictCache()
        store = RuleStore(cache)
        instrument_a = make_instrument(
            venue="CLOUDBET",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = make_instrument(
            venue="SXBET",
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
            price=1.85,
        )
        rule = RuleClassifier().classify(instrument_a, instrument_b)
        assert rule is not None
        template = SemanticRuleTemplate.from_rule(
            rule,
            support=TemplateSupportStats(
                template_id="cross-provider-template",
                observed_count=20,
                event_count=8,
                provider_count=2,
                providers=("CLOUDBET", "SXBET"),
                sports=("soccer",),
                confidence=0.99,
            ),
        )
        promoted_template = RulePromotionPolicy().promote_template(store, template)
        assert promoted_template is not None

        matcher = MarketMatcher(rule_store=store)
        resolved = matcher._resolve_rule(instrument_a, instrument_b)

        assert resolved is not None
        assert resolved.template_id == promoted_template.template_id
        assert resolved.promotion_status == PromotionStatus.PROMOTED.value
        assert resolved.safety_tier == SafetyTier.EXECUTION_SAFE.value

    def test_resolve_rule_disallows_unpromoted_topology_when_disabled(self):
        instrument_a = make_instrument(
            venue="CLOUDBET",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = make_instrument(
            venue="SXBET",
            market_name="double_chance",
            market_type="double_chance",
            outcome="away_draw",
            price=1.85,
        )
        matcher = MarketMatcher(allow_unpromoted_topology=False)

        assert matcher._resolve_rule(instrument_a, instrument_b) is None

    def test_hedge_event_match_rejects_event_mismatch(self, market_matcher):
        instrument_a = make_instrument(event_name="Team A vs Team B", outcome="over")
        instrument_b = make_instrument(
            venue="BLACKBET",
            event_name="Team C vs Team D",
            home_name="Team C",
            away_name="Team D",
            outcome="under",
        )

        assert (
            market_matcher._is_hedge_event_match(instrument_a, instrument_b, [instrument_b])
            is False
        )

    def test_same_venue_event_id_mismatch_rejected_before_topology(self, market_matcher):
        instrument_a = make_instrument(event_id="fixture-a", outcome="over")
        instrument_b = make_instrument(event_id="fixture-b", outcome="under")

        assert market_matcher.find_hedges(instrument_a, [instrument_b]) == []
        assert market_matcher.check_arbitrage(instrument_a, instrument_b) is None

    def test_trusted_sxbet_two_way_match_odds_event_id_mismatch_allowed(self, market_matcher):
        instrument_a = make_instrument(
            event_id="market-a",
            market_name="match_odds",
            market_type="match_odds",
            outcome="home",
            info={"is_two_way_market": True},
        )
        instrument_b = make_instrument(
            event_id="market-b",
            market_name="match_odds",
            market_type="match_odds",
            outcome="away",
            info={"is_two_way_market": True},
        )

        hedges = market_matcher.find_hedges(instrument_a, [instrument_b])

        assert len(hedges) == 1
        assert hedges[0].match_type == "same_market"

    def test_find_arbitrage_opportunities_deduplicates_and_sorts(self, market_matcher):
        event_one_over = make_instrument(
            venue="CLOUDBET",
            event_id="event-1",
            outcome="over",
            price=2.30,
            params="line=2.5",
        )
        event_one_under = make_instrument(
            venue="SXBET",
            event_id="event-1",
            outcome="under",
            price=2.45,
            params="line=2.5",
        )
        event_two_over = make_instrument(
            venue="CLOUDBET",
            event_id="event-2",
            outcome="over",
            price=2.05,
            params="line=3.5",
        )
        event_two_under = make_instrument(
            venue="SXBET",
            event_id="event-2",
            outcome="under",
            price=2.20,
            params="line=3.5",
        )

        opportunities = market_matcher.find_arbitrage_opportunities(
            [event_one_over, event_one_under, event_two_over, event_two_under],
            min_profit_margin=Decimal("0.01"),
        )

        assert len(opportunities) == 2
        assert opportunities[0].profit_margin > opportunities[1].profit_margin
        assert {
            tuple(sorted([opportunity.instrument_a.event_id, opportunity.instrument_b.event_id]))
            for opportunity in opportunities
        } == {("event-1", "event-1"), ("event-2", "event-2")}

    def test_event_and_team_name_normalizers(self, market_matcher):
        assert market_matcher.normalize_event_name("Arsenal vs Chelsea") == "arsenal chelsea"
        assert market_matcher.normalize_event_name("Team A @ Team B") == "team a team b"
        assert market_matcher._normalize_team_name("Manchester City FC") == "manchester city"
        assert market_matcher._normalize_team_name("Leeds United") == "leeds united"

    def test_are_matching_selections_rejects_param_and_market_mismatches(self, market_matcher):
        instrument_a = make_instrument(
            market_name="Total Goals",
            market_type="total_goals",
            outcome="over",
            params="line=2.5",
        )
        param_mismatch = make_instrument(
            venue="BLACKBET",
            market_name="Total Goals",
            market_type="total_goals",
            outcome="over",
            params="line=3.5",
        )
        market_mismatch = make_instrument(
            venue="BLACKBET",
            market_name="Match Odds",
            market_type="match_odds",
            outcome="home",
            params="line=2.5",
        )

        assert market_matcher._are_matching_selections(instrument_a, param_mismatch) is False
        assert market_matcher._are_matching_selections(instrument_a, market_mismatch) is False
