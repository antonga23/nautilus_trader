# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for MarketMatcher.
# -------------------------------------------------------------------------------------------------
# pylint: disable=duplicate-code

from decimal import Decimal
from decimal import DivisionByZero

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency


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

    def test_match_events_cross_venue_skips_missing_start_time_without_time_signal(
        self,
        market_matcher,
    ):
        """
        Test missing-time events do not match when no parsed fixture time exists.
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

        assert matches == []

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
