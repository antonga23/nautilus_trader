# skipcq: PYL-C0114, PYL-C0116

from decimal import Decimal

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.fixture_identity import FixtureIdentityResolver
from nautilus_trader.adapters.betting.fx import PortfolioCurrencyPolicy
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency


def _instrument(
    *,
    venue: str,
    event_name: str,
    home_name: str,
    away_name: str,
    sport_name: str = "basketball",
    start_time: str = "2026-05-10T18:00:00Z",
    market_type: str = "draw_no_bet",
    outcome: str = "home",
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id=f"{venue.lower()}-{event_name}".replace(" ", "-"),
        event_name=event_name,
        home_name=home_name,
        away_name=away_name,
        sport_name=sport_name,
        competition_name="Test League",
        market_name=market_type,
        market_type=market_type,
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.0,
        currency=Currency.from_str("USDC"),
        start_time=start_time,
    )


def test_fixture_identity_resolves_screenshot_cloudbet_sxbet_aliases():
    resolver = FixtureIdentityResolver()
    cloudbet = _instrument(
        venue="CLOUDBET",
        event_name="MIN Timberwolves v SA Spurs",
        home_name="MIN Timberwolves",
        away_name="SA Spurs",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Minnesota Timberwolves vs San Antonio Spurs",
        home_name="Minnesota Timberwolves",
        away_name="San Antonio Spurs",
    )

    proof = resolver.resolve(cloudbet, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert cloudbet.event_key(include_start_time=False) == (
        "basketball:minnesota timberwolves:san antonio spurs"
    )
    assert cloudbet.matches_event(sxbet)


def test_fixture_identity_resolves_screenshot_polymarket_sxbet_tennis_names():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Felix Auger-Aliassime v Mariano Navone",
        home_name="Felix Auger-Aliassime",
        away_name="Mariano Navone",
        sport_name="tennis",
        market_type="moneyline_2way",
        outcome="no",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Felix Auger Aliassime vs Mariano Navone",
        home_name="Felix Auger Aliassime",
        away_name="Mariano Navone",
        sport_name="tennis",
        market_type="match_odds",
        outcome="home",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.confidence >= 0.9
    assert proof.canonical_event_key_a == proof.canonical_event_key_b


def test_fixture_identity_blocks_ambiguous_cross_sport_match():
    resolver = FixtureIdentityResolver()
    sxbet = _instrument(
        venue="SXBET",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
    )
    cloudbet = _instrument(
        venue="CLOUDBET",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="basketball",
    )

    proof = resolver.resolve(sxbet, cloudbet)

    assert proof.same_fixture is False
    assert proof.blocker_reason == "sport_mismatch"


def test_fixture_identity_blocks_same_teams_when_start_times_are_far_apart():
    resolver = FixtureIdentityResolver()
    first = _instrument(
        venue="CLOUDBET",
        event_name="Cleveland Bears v Minnesota Wolves",
        home_name="Cleveland Bears",
        away_name="Minnesota Wolves",
        start_time="2026-05-10T12:00:00Z",
    )
    second = _instrument(
        venue="SXBET",
        event_name="Cleveland Bears v Minnesota Wolves",
        home_name="Cleveland Bears",
        away_name="Minnesota Wolves",
        start_time="2026-05-10T16:00:00Z",
    )

    proof = resolver.resolve(first, second)

    assert proof.same_fixture is False
    assert proof.blocker_reason == "start_time_mismatch"


def test_fixture_identity_allows_harmless_suffix_drift_with_start_time_evidence():
    resolver = FixtureIdentityResolver()
    cloudbet = _instrument(
        venue="CLOUDBET",
        event_name="Cleveland v Minnesota",
        home_name="Cleveland",
        away_name="Minnesota",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Cleveland Bears v Minnesota Wolves",
        home_name="Cleveland Bears",
        away_name="Minnesota Wolves",
    )

    proof = resolver.resolve(cloudbet, sxbet)

    assert proof.same_fixture is True
    assert proof.confidence >= 0.86
    assert proof.blocker_reason is None
    assert "basketball:cleveland:minnesota" in cloudbet.event_alias_keys()
    assert "basketball:cleveland:minnesota" in sxbet.event_alias_keys()


def test_fixture_identity_strips_provider_market_group_suffix_noise():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Arsenal Exact Score v West Ham United",
        home_name="Arsenal Exact Score",
        away_name="West Ham United",
        sport_name="soccer",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Arsenal vs West Ham United",
        home_name="Arsenal",
        away_name="West Ham United",
        sport_name="soccer",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert proof.canonical_event_key_a == "soccer:arsenal:west ham united"
    assert proof.canonical_event_key_a == proof.canonical_event_key_b


def test_portfolio_policy_treats_stablecoins_as_usd_with_haircut():
    policy = PortfolioCurrencyPolicy(stablecoin_haircut_bps=25)

    conversion = policy.convert(Decimal(15), "USDC")

    assert conversion.is_available
    assert conversion.source == "stablecoin_parity"
    assert conversion.converted_amount == Decimal("15.0375")


def test_portfolio_policy_blocks_play_currency_for_live_settlement():
    policy = PortfolioCurrencyPolicy()

    conversion = policy.convert(Decimal(10), "PLAY_EUR")

    assert not conversion.is_available
    assert conversion.blocker_reason == "sandbox_currency_not_live_settlement"
