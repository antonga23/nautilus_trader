# skipcq: PYL-C0114, PYL-C0116

from decimal import Decimal

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.fixture_identity import FixtureIdentityResolver
from nautilus_trader.adapters.betting.fx import FxMarketQuote
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


def test_fixture_identity_allows_cross_venue_start_time_drift_when_participants_are_strong():
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
        event_name="Cleveland Bears v Minnesota Wolves - rescheduled",
        home_name="Cleveland Bears",
        away_name="Minnesota Wolves",
        start_time="2026-05-10T16:00:00Z",
    )

    proof = resolver.resolve(first, second)

    assert proof.same_fixture is True
    assert proof.reason == "canonical_fixture_match_start_time_conflict"
    assert proof.blocker_reason is None
    assert proof.start_time_delta_secs == 4 * 60 * 60


def test_fixture_identity_allows_cross_venue_date_only_start_time():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Baltimore Orioles v Washington Nationals",
        home_name="Baltimore Orioles",
        away_name="Washington Nationals",
        sport_name="baseball",
        start_time="2026-05-15T00:00:00Z",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Baltimore Orioles vs Washington Nationals",
        home_name="Baltimore Orioles",
        away_name="Washington Nationals",
        sport_name="baseball",
        start_time="2026-05-15T22:45:00Z",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.reason == "canonical_fixture_match_start_time_conflict"
    assert proof.blocker_reason is None
    assert proof.start_time_delta_secs == 22.75 * 60 * 60


def test_fixture_identity_blocks_date_only_start_time_on_different_utc_date():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Baltimore Orioles v Washington Nationals",
        home_name="Baltimore Orioles",
        away_name="Washington Nationals",
        sport_name="baseball",
        start_time="2026-05-15T00:00:00Z",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Baltimore Orioles vs Washington Nationals",
        home_name="Baltimore Orioles",
        away_name="Washington Nationals",
        sport_name="baseball",
        start_time="2026-05-16T22:45:00Z",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is False
    assert proof.blocker_reason == "start_time_mismatch"


def test_fixture_identity_keeps_same_venue_start_time_drift_strict():
    resolver = FixtureIdentityResolver()
    first = _instrument(
        venue="SXBET",
        event_name="Cleveland Bears v Minnesota Wolves",
        home_name="Cleveland Bears",
        away_name="Minnesota Wolves",
        start_time="2026-05-10T12:00:00Z",
    )
    second = _instrument(
        venue="SXBET",
        event_name="Cleveland Bears v Minnesota Wolves - rescheduled",
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


def test_fixture_identity_allows_specific_nickname_only_drift_with_start_time_evidence():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Timberwolves v Spurs",
        home_name="Timberwolves",
        away_name="Spurs",
        start_time="2026-05-10T19:00:00Z",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Minnesota Timberwolves vs San Antonio Spurs",
        home_name="Minnesota Timberwolves",
        away_name="San Antonio Spurs",
        start_time="2026-05-10T19:30:00Z",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert "token_subset" in ",".join(proof.alias_hits)


def test_fixture_identity_blocks_generic_subset_alias_without_specific_team_token():
    resolver = FixtureIdentityResolver()
    first = _instrument(
        venue="POLYMARKET",
        event_name="City v United",
        home_name="City",
        away_name="United",
    )
    second = _instrument(
        venue="SXBET",
        event_name="Manchester City v Manchester United",
        home_name="Manchester City",
        away_name="Manchester United",
    )

    proof = resolver.resolve(first, second)

    assert proof.same_fixture is False
    assert proof.blocker_reason == "participant_mismatch"


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


def test_fixture_identity_strips_polymarket_corners_group_without_losing_fixture():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Arsenal Total Corners v West Ham United",
        home_name="Arsenal Total Corners",
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


def test_fixture_identity_expands_aliases_from_event_title_when_team_fields_missing():
    resolver = FixtureIdentityResolver()
    sxbet = _instrument(
        venue="SXBET",
        event_name="CLE Cavaliers @ MIN Timberwolves",
        home_name="",
        away_name="",
        sport_name="basketball",
    )
    cloudbet = _instrument(
        venue="CLOUDBET",
        event_name="Cleveland Cavaliers vs Minnesota Timberwolves",
        home_name="Cleveland Cavaliers",
        away_name="Minnesota Timberwolves",
        sport_name="basketball",
    )

    proof = resolver.resolve(sxbet, cloudbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert "basketball:cleveland cavaliers:minnesota timberwolves" in sxbet.event_alias_keys()
    assert "basketball:cleveland:minnesota" in sxbet.event_alias_keys()


def test_fixture_identity_compacts_dotted_city_abbreviations():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="L.A. Clippers v N.Y. Knicks",
        home_name="L.A. Clippers",
        away_name="N.Y. Knicks",
        sport_name="basketball",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Los Angeles Clippers vs New York Knicks",
        home_name="Los Angeles Clippers",
        away_name="New York Knicks",
        sport_name="basketball",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert proof.canonical_event_key_a == proof.canonical_event_key_b
    assert "basketball:los angeles:new york" in polymarket.event_alias_keys()


def test_fixture_identity_splits_provider_title_separators_when_team_fields_missing():
    resolver = FixtureIdentityResolver()
    sxbet = _instrument(
        venue="SXBET",
        event_name="Minnesota Timberwolves vs. San Antonio Spurs",
        home_name="",
        away_name="",
        sport_name="basketball",
    )
    cloudbet = _instrument(
        venue="CLOUDBET",
        event_name="MIN Timberwolves - SA Spurs",
        home_name="",
        away_name="",
        sport_name="basketball",
    )

    proof = resolver.resolve(sxbet, cloudbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert "basketball:minnesota timberwolves:san antonio spurs" in sxbet.event_alias_keys()
    assert "basketball:minnesota:san antonio" in cloudbet.event_alias_keys()


def test_fixture_identity_expands_common_us_team_abbreviations_from_titles():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="OKC Thunder v LAL Lakers",
        home_name="",
        away_name="",
        sport_name="basketball",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Oklahoma City Thunder vs Los Angeles Lakers",
        home_name="Oklahoma City Thunder",
        away_name="Los Angeles Lakers",
        sport_name="basketball",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert proof.canonical_event_key_a == proof.canonical_event_key_b
    assert "basketball:los angeles lakers:oklahoma city thunder" in polymarket.event_alias_keys()
    assert "basketball:los angeles:oklahoma city" in polymarket.event_alias_keys()


def test_fixture_identity_splits_at_separator_without_confusing_player_hyphens():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Felix Auger-Aliassime at Mariano Navone",
        home_name="",
        away_name="",
        sport_name="tennis",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Felix Auger Aliassime v. Mariano Navone",
        home_name="",
        away_name="",
        sport_name="tennis",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert proof.canonical_event_key_a == proof.canonical_event_key_b


def test_fixture_identity_folds_diacritics_across_provider_names():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="São Paulo v Atlético Mineiro",
        home_name="São Paulo",
        away_name="Atlético Mineiro",
        sport_name="soccer",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Sao Paulo vs Atletico Mineiro",
        home_name="Sao Paulo",
        away_name="Atletico Mineiro",
        sport_name="soccer",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert proof.canonical_event_key_a == proof.canonical_event_key_b


def test_fixture_identity_strips_competition_prefix_when_team_fields_missing():
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Internazionali BNL d'Italia: Frances Tiafoe vs Ignacio Buse",
        home_name="",
        away_name="",
        sport_name="tennis",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Frances Tiafoe v Ignacio Buse",
        home_name="",
        away_name="",
        sport_name="tennis",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert proof.canonical_event_key_a == "tennis:frances tiafoe:ignacio buse"
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


def test_portfolio_policy_uses_fresh_conservative_fx_ask_for_live_cost():
    policy = PortfolioCurrencyPolicy(
        stablecoin_haircut_bps=0,
        fx_quote_max_age_secs=10,
        fx_quotes={
            "EUR/USD": FxMarketQuote(
                pair="EUR/USD",
                rate=Decimal("1.0800"),
                bid=Decimal("1.0790"),
                ask=Decimal("1.0810"),
                source="hyperliquid",
                age_secs=2.0,
            ),
        },
    )

    conversion = policy.convert(Decimal(25), "EUR")

    assert conversion.is_available
    assert conversion.rate == Decimal("1.0810")
    assert conversion.source == "hyperliquid"
    assert conversion.age_secs == 2.0
    assert conversion.converted_amount == Decimal("27.0250")


def test_portfolio_policy_blocks_stale_live_fx_quote():
    policy = PortfolioCurrencyPolicy(
        fx_quote_max_age_secs=10,
        fx_quotes={
            "EUR/USD": FxMarketQuote(
                pair="EUR/USD",
                rate=Decimal("1.0800"),
                source="pyth_hermes",
                age_secs=11.0,
            ),
        },
    )

    conversion = policy.convert(Decimal(25), "EUR")

    assert not conversion.is_available
    assert conversion.blocker_reason == "stale_fx_rate"
    assert conversion.source == "pyth_hermes"
    assert conversion.age_secs == 11.0


def test_portfolio_policy_uses_inverse_bid_for_conservative_fx_cost():
    policy = PortfolioCurrencyPolicy(
        stablecoin_haircut_bps=0,
        fx_quotes={
            "USD/EUR": FxMarketQuote(
                pair="USD/EUR",
                rate=Decimal("0.9250"),
                bid=Decimal("0.9240"),
                ask=Decimal("0.9260"),
                source="binance",
                age_secs=1.0,
            ),
        },
    )

    conversion = policy.convert(Decimal(10), "EUR")

    assert conversion.is_available
    assert conversion.rate == Decimal(1) / Decimal("0.9240")
    assert conversion.converted_amount == Decimal(10) / Decimal("0.9240")


def test_fixture_identity_matches_distinctive_single_token_club_short_name():
    # Issue 221: a venue emits "United" for "Manchester United"; the shared
    # distinctive nickname token must still reach the token_subset path (0.84)
    # instead of being force-rejected as generic.
    resolver = FixtureIdentityResolver()
    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="United v Liverpool",
        home_name="United",
        away_name="Liverpool",
        sport_name="soccer",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Manchester United vs Liverpool",
        home_name="Manchester United",
        away_name="Liverpool",
        sport_name="soccer",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
    assert "token_subset" in ",".join(proof.alias_hits)


def test_fixture_identity_keeps_geographic_single_token_non_distinctive():
    # Issue 221 (conservative bound): a bare geographic descriptor such as "City"
    # must NOT collapse onto "New York City", which would create a same-metro
    # false positive on the token_subset path.
    resolver = FixtureIdentityResolver()
    first = _instrument(
        venue="POLYMARKET",
        event_name="City v Rangers",
        home_name="City",
        away_name="Rangers",
        sport_name="soccer",
    )
    second = _instrument(
        venue="SXBET",
        event_name="New York City v Rangers",
        home_name="New York City",
        away_name="Rangers",
        sport_name="soccer",
    )

    proof = resolver.resolve(first, second)

    assert proof.same_fixture is False
    assert proof.blocker_reason == "participant_mismatch"


def test_fixture_identity_rejects_same_metro_prefix_only_token_overlap():
    # Issue 222: two distinct same-metro teams share only geographic-prefix tokens
    # ("new york") and must not be accepted as the same participant via the
    # token_overlap 0.74 floor.
    resolver = FixtureIdentityResolver()
    knicks_liberty = _instrument(
        venue="POLYMARKET",
        event_name="New York Knicks v New York Liberty",
        home_name="New York Knicks",
        away_name="New York Liberty",
        sport_name="basketball",
    )
    knicks_red_bulls = _instrument(
        venue="SXBET",
        event_name="New York Knicks v New York Red Bulls",
        home_name="New York Knicks",
        away_name="New York Red Bulls",
        sport_name="basketball",
    )

    proof = resolver.resolve(knicks_liberty, knicks_red_bulls)

    assert proof.same_fixture is False
    assert proof.blocker_reason == "participant_mismatch"


def test_fixture_identity_reconciles_venue_specific_soccer_labels():
    # Issue 234: divergent soccer sport labels across venues must normalize to the
    # same sport instead of hard-blocking with sport_mismatch before participants
    # are compared.
    resolver = FixtureIdentityResolver()
    assert resolver.normalize_sport("Football (Soccer)") == "soccer"
    assert resolver.normalize_sport("Association Football") == "soccer"
    assert resolver.normalize_sport("Futbol") == "soccer"

    polymarket = _instrument(
        venue="POLYMARKET",
        event_name="Arsenal v Chelsea",
        home_name="Arsenal",
        away_name="Chelsea",
        sport_name="Football (Soccer)",
    )
    sxbet = _instrument(
        venue="SXBET",
        event_name="Arsenal vs Chelsea",
        home_name="Arsenal",
        away_name="Chelsea",
        sport_name="Soccer",
    )

    proof = resolver.resolve(polymarket, sxbet)

    assert proof.same_fixture is True
    assert proof.blocker_reason is None
