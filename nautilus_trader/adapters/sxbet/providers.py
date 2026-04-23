# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
SX.bet Instrument Provider.
"""

from datetime import UTC
from datetime import datetime
import re
from typing import Any

from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_SPORT_IDS
from nautilus_trader.adapters.sxbet.constants import SXBET_TOKENS
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClientError
from nautilus_trader.adapters.sxbet.signing import percentage_to_decimal_odds
from nautilus_trader.common.component import Logger
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency


SXBET_MARKET_BATCH_SIZE = 30
SXBET_MARKET_PAGE_SIZE = 50
SXBET_PLACEHOLDER_PRICE = 2.0
SXBET_MARKET_TYPE_MAP = {
    1: MarketType.MATCH_ODDS.value,
    2: MarketType.TOTAL_GOALS.value,
    3: MarketType.ASIAN_HANDICAP.value,
    52: MarketType.MATCH_ODDS.value,
    88: MarketType.WINNER.value,
    201: MarketType.ASIAN_HANDICAP.value,
    226: MarketType.MATCH_ODDS.value,
    342: MarketType.ASIAN_HANDICAP.value,
    835: MarketType.TOTAL_GOALS.value,
}


class SXBetInstrumentProvider(InstrumentProvider):
    """
    Provides instruments for the SX.bet venue.

    Parameters
    ----------
    http_client : SXBetHttpClient
        The HTTP client for API calls.
    config : SXBetInstrumentProviderConfig
        The provider configuration.
    logger : Logger, optional
        The logger instance.

    """

    def __init__(
        self,
        http_client: SXBetHttpClient,
        config: SXBetInstrumentProviderConfig,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(config=config)

        self._http_client = http_client
        self._sxbet_config = config
        self._instruments: dict[InstrumentId, CryptoBettingInstrument] = {}
        self._market_cache: dict[str, dict] = {}  # market_hash -> market_data
        if logger is not None:
            self._log = logger

    async def load_all_async(self, filters: dict | None = None) -> None:
        """
        Load all instruments from the venue.
        """
        self._log.info("Loading all SX.bet instruments...")

        filters = filters or {}
        sport_ids = filters.get("sport_ids") or self._sxbet_config.sport_ids
        league_ids = filters.get("league_ids") or self._sxbet_config.league_ids

        markets = await self._load_markets(
            sport_ids=sport_ids,
            league_ids=league_ids,
        )
        await self._hydrate_best_odds(markets)
        msg = f"Found {len(markets)} active markets"
        self._log.info(msg)

        for market in markets:
            await self._process_market(market)

        msg = f"Loaded {len(self._instruments)} instruments"
        self._log.info(msg)

    async def _load_markets(
        self,
        sport_ids: frozenset[int] | set[int] | None,
        league_ids: frozenset[int] | set[int] | None,
    ) -> list[dict[str, Any]]:
        markets_by_hash: dict[str, dict[str, Any]] = {}
        sport_filters: tuple[int | None, ...] = tuple(sorted(sport_ids)) if sport_ids else (None,)
        league_filters: tuple[int | None, ...] = (
            tuple(sorted(league_ids)) if league_ids else (None,)
        )

        for sport_id in sport_filters:
            for league_id in league_filters:
                pagination_key: str | None = None
                while True:
                    markets_data = await self._http_client.get_markets(
                        sport_id=sport_id,
                        league_id=league_id,
                        only_active=True,
                        pagination_key=pagination_key,
                        page_size=SXBET_MARKET_PAGE_SIZE,
                    )
                    self._merge_markets(markets_by_hash, markets_data)
                    pagination_key = markets_data.get("data", {}).get("nextKey")
                    if not pagination_key:
                        break

        return list(markets_by_hash.values())

    async def _hydrate_best_odds(self, markets: list[dict[str, Any]]) -> None:
        market_hashes = [
            market_hash
            for market in markets
            if isinstance(market_hash := market.get("marketHash"), str) and market_hash
        ]
        if not market_hashes:
            return

        best_odds_by_hash: dict[str, dict[str, Any]] = {}
        for start in range(0, len(market_hashes), SXBET_MARKET_BATCH_SIZE):
            batch = market_hashes[start : start + SXBET_MARKET_BATCH_SIZE]
            try:
                payload = await self._http_client.get_best_odds(
                    market_hashes=batch,
                    base_token=SXBET_TOKENS["USDC"],
                )
            except SXBetHttpClientError as e:
                self._log.warning(
                    f"Failed to hydrate SX.bet best odds for {len(batch)} markets: {e}",
                )
                continue
            for item in payload.get("data", {}).get("bestOdds", []):
                market_hash = item.get("marketHash")
                if isinstance(market_hash, str) and market_hash:
                    best_odds_by_hash[market_hash] = item

        for market in markets:
            market_hash = market.get("marketHash")
            if isinstance(market_hash, str) and market_hash in best_odds_by_hash:
                market["bestOdds"] = best_odds_by_hash[market_hash]

    @staticmethod
    def _merge_markets(
        markets_by_hash: dict[str, dict[str, Any]],
        markets_data: dict[str, Any],
    ) -> None:
        for market in markets_data.get("data", {}).get("markets", []):
            market_hash = market.get("marketHash")
            if market_hash:
                markets_by_hash[market_hash] = market

    async def _process_market(self, market: dict[str, Any]) -> None:
        """
        Process a market and create instruments.
        """
        market_hash = market.get("marketHash", "")

        # Cache market data
        self._market_cache[market_hash] = market

        # Extract market info
        start_time = self._parse_start_time(market.get("gameTime"))
        team_one = market.get("teamOneName", "Unknown")
        team_two = market.get("teamTwoName", "Unknown")
        sport_id = market.get("sportId", 0)
        market_type = market.get("type", 0)
        is_live = self._is_live_market(market)

        if self._sxbet_config.live_only and not is_live:
            return

        sport_name = SXBET_SPORT_IDS.get(sport_id, "unknown")
        league_name = market.get("leagueLabel") or market.get("leagueName") or "Unknown"
        normalized_market_type = self._normalize_market_type(market)
        outcome_labels, proposition_subject = self._resolve_outcome_names(
            market,
            normalized_market_type,
        )
        market_params = self._market_params(
            market_type=normalized_market_type,
            handicap=market.get("line"),
            outcome_labels=outcome_labels,
            proposition_subject=proposition_subject,
        )

        outcome_one_odds = self._extract_best_odds(market, True)
        outcome_two_odds = self._extract_best_odds(market, False)
        price_by_outcome = {
            True: outcome_one_odds if outcome_one_odds > 0 else SXBET_PLACEHOLDER_PRICE,
            False: outcome_two_odds if outcome_two_odds > 0 else SXBET_PLACEHOLDER_PRICE,
        }

        # Create instruments for both outcomes
        for outcome_one in (True, False):
            instrument = self._create_instrument(
                market_hash=market_hash,
                event_name=f"{team_one} vs {team_two}",
                home_name=team_one,
                away_name=team_two,
                sport_name=sport_name,
                competition_name=league_name,
                market_type=normalized_market_type,
                raw_market_type=market_type,
                outcome=outcome_labels[0] if outcome_one else outcome_labels[1],
                price=price_by_outcome[outcome_one],
                is_outcome_one=outcome_one,
                live=is_live,
                start_time=start_time,
                handicap=market.get("line"),
                params=market_params,
                has_best_odds=(outcome_one_odds > 0 if outcome_one else outcome_two_odds > 0),
                outcome_label=(
                    market.get("outcomeOneName") if outcome_one else market.get("outcomeTwoName")
                ),
            )

            if instrument:
                self._instruments[instrument.id] = instrument
                self.add(instrument)

    @staticmethod
    def _extract_best_odds(market: dict, outcome_one: bool) -> float:
        """
        Extract best available odds from market.
        """
        best_odds_entry = market.get("bestOdds")
        if isinstance(best_odds_entry, dict):
            key = "outcomeOne" if outcome_one else "outcomeTwo"
            payload = best_odds_entry.get(key, {})
            if isinstance(payload, dict):
                percentage_odds = payload.get("percentageOdds")
                if percentage_odds not in (None, ""):
                    return percentage_to_decimal_odds(int(str(percentage_odds)))

        # Check if there are orders in the market
        orders = market.get("orders", [])

        best_odds = 0.0
        for order in orders:
            if order.get("isMakerBettingOutcomeOne") != outcome_one:
                # This order is betting against our desired outcome
                # So we can take the opposite side
                percentage_odds = int(order.get("percentageOdds", 0))
                if percentage_odds > 0:
                    odds = percentage_to_decimal_odds(percentage_odds)
                    best_odds = max(best_odds, odds)

        # If no orders, use implied odds from market
        if best_odds <= 0:
            if outcome_one:
                implied = market.get("outcomeOneProbability", 0)
            else:
                implied = market.get("outcomeTwoProbability", 0)

            if implied and implied > 0:
                best_odds = 1 / implied

        return best_odds

    def _normalize_market_type(self, market: dict[str, Any]) -> str:
        raw_market_type = market.get("type")
        if isinstance(raw_market_type, int) and raw_market_type in SXBET_MARKET_TYPE_MAP:
            return SXBET_MARKET_TYPE_MAP[raw_market_type]

        outcome_one = self._normalize_label(market.get("outcomeOneName"))
        outcome_two = self._normalize_label(market.get("outcomeTwoName"))
        if outcome_one.startswith("over") or outcome_two.startswith("under"):
            return MarketType.TOTAL_GOALS.value
        if self._is_yes_no_pair(outcome_one, outcome_two):
            return MarketType.BOTH_TEAMS_TO_SCORE.value
        if market.get("line") is not None:
            return MarketType.ASIAN_HANDICAP.value
        return MarketType.OTHER.value

    def _resolve_outcome_names(
        self,
        market: dict[str, Any],
        market_type: str,
    ) -> tuple[tuple[str, str], str | None]:
        team_one = self._normalize_label(market.get("teamOneName"))
        team_two = self._normalize_label(market.get("teamTwoName"))
        outcome_one = self._normalize_label(market.get("outcomeOneName"))
        outcome_two = self._normalize_label(market.get("outcomeTwoName"))

        if market_type == MarketType.TOTAL_GOALS.value:
            return ("over", "under"), None
        if self._is_yes_no_pair(outcome_one, outcome_two):
            return ("yes", "no"), None

        team_side_outcomes = self._resolve_team_side_outcomes(
            team_one=team_one,
            team_two=team_two,
            outcome_one=outcome_one,
            outcome_two=outcome_two,
        )
        if team_side_outcomes is not None:
            return team_side_outcomes

        binary_outcomes = self._resolve_binary_team_outcomes(
            team_one=team_one,
            team_two=team_two,
            outcome_one=outcome_one,
            outcome_two=outcome_two,
        )
        if binary_outcomes is not None:
            return binary_outcomes

        return ("outcome_1", "outcome_2"), None

    @staticmethod
    def _resolve_team_side_outcomes(
        *,
        team_one: str,
        team_two: str,
        outcome_one: str,
        outcome_two: str,
    ) -> tuple[tuple[str, str], str | None] | None:
        if outcome_one == team_one and outcome_two == team_two:
            return ("home", "away"), None
        if outcome_one == team_two and outcome_two == team_one:
            return ("away", "home"), None
        if team_one and outcome_one.startswith(team_one):
            return ("home", "away" if outcome_two.startswith(team_two) else "no"), None
        if team_two and outcome_one.startswith(team_two):
            return ("away", "home" if outcome_two.startswith(team_one) else "no"), None
        return None

    @staticmethod
    def _resolve_binary_team_outcomes(
        *,
        team_one: str,
        team_two: str,
        outcome_one: str,
        outcome_two: str,
    ) -> tuple[tuple[str, str], str | None] | None:
        if team_one and outcome_two.startswith(f"not {team_one}"):
            return ("yes", "no"), team_one
        if team_two and outcome_two.startswith(f"not {team_two}"):
            return ("yes", "no"), team_two
        if team_one and outcome_one.startswith(f"not {team_one}"):
            return ("no", "yes"), team_one
        if team_two and outcome_one.startswith(f"not {team_two}"):
            return ("no", "yes"), team_two
        return None

    @classmethod
    def _market_params(
        cls,
        market_type: str,
        handicap: float | None,
        outcome_labels: tuple[str, str],
        proposition_subject: str | None,
    ) -> str:
        parts: list[str] = []
        if handicap is not None and market_type in {
            MarketType.ASIAN_HANDICAP.value,
            MarketType.TOTAL_GOALS.value,
        }:
            parts.append(f"line={handicap}")
        if outcome_labels in {("yes", "no"), ("no", "yes")}:
            parts.append("binary=yes_no")
        if proposition_subject:
            subject_key = re.sub(r"[^a-z0-9]+", "_", proposition_subject).strip("_")
            if subject_key:
                parts.append(f"subject={subject_key}")
        return ",".join(parts)

    @staticmethod
    def _normalize_label(value: Any) -> str:
        if value is None:
            return ""
        normalized = re.sub(r"\s+", " ", str(value).strip().lower())
        return normalized

    @staticmethod
    def _is_yes_no_pair(outcome_one: str, outcome_two: str) -> bool:
        return {outcome_one, outcome_two} == {"yes", "no"}

    @classmethod
    def _is_live_market(cls, market: dict[str, Any]) -> bool:
        for key in ("live", "isLive", "inPlay", "isInPlay"):
            parsed = cls._parse_bool(market.get(key))
            if parsed is not None:
                return parsed

        for key in ("status", "state"):
            value = market.get(key)
            if isinstance(value, str) and value.strip().lower() in {"live", "in_play", "in-play"}:
                return True

        return False

    @staticmethod
    def _parse_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "live", "in_play", "in-play"}:
                return True
            if normalized in {"false", "0", "no", "pre", "prematch", "pre_match"}:
                return False
        return None

    @staticmethod
    def _parse_start_time(value: Any) -> str | None:
        if value in (None, ""):
            return None

        if isinstance(value, (int, float)):
            timestamp = float(value)
        else:
            value_str = str(value).strip()
            if not value_str:
                return None
            if value_str.isdigit():
                timestamp = float(value_str)
            else:
                normalized = value_str.replace("Z", "+00:00")
                try:
                    parsed = datetime.fromisoformat(normalized)
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                else:
                    parsed = parsed.astimezone(UTC)
                return parsed.isoformat().replace("+00:00", "Z")

        if timestamp > 10_000_000_000:
            timestamp /= 1000

        return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")

    def _create_instrument(
        self,
        market_hash: str,
        event_name: str,
        home_name: str,
        away_name: str,
        sport_name: str,
        competition_name: str,
        market_type: str,
        raw_market_type: int,
        outcome: str,
        price: float,
        is_outcome_one: bool,
        live: bool = False,
        start_time: str | None = None,
        handicap: float | None = None,
        params: str = "",
        has_best_odds: bool = False,
        outcome_label: str | None = None,
    ) -> CryptoBettingInstrument | None:
        """
        Create a CryptoBettingInstrument from market data.
        """
        try:
            return CryptoBettingInstrument(
                venue=SXBET_VENUE,
                event_id=market_hash,
                event_name=event_name,
                home_name=home_name,
                away_name=away_name,
                sport_name=sport_name,
                competition_name=competition_name,
                market_name=market_type,
                market_type=market_type,
                outcome=outcome,
                side=SelectionSide.BACK,
                price=price,
                currency=Currency.from_str("USDC"),
                params=params,
                live=live,
                enabled=True,
                start_time=start_time,
                handicap=handicap,
                trading_status="ACTIVE",
                market_id=market_hash,
                info={
                    "outcome_one": is_outcome_one,
                    "raw_market_type": raw_market_type,
                    "is_two_way_market": market_type == MarketType.MATCH_ODDS.value,
                    "has_best_odds": has_best_odds,
                    "outcome_label": outcome_label,
                },
            )
        except (TypeError, ValueError) as e:
            if self._log:
                msg = f"Failed to create instrument: {e}"
                self._log.warning(msg)
            return None

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """
        Load specific instruments by ID.
        """
        if not instrument_ids:
            return

        await self.load_all_async(filters)

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict | None = None,
    ) -> None:
        """
        Load a specific instrument by ID.
        """
        await self.load_ids_async([instrument_id], filters)

    def find(self, instrument_id: InstrumentId) -> CryptoBettingInstrument | None:
        """
        Find an instrument by ID.
        """
        return self._instruments.get(instrument_id)

    def get_all(self) -> dict[InstrumentId, CryptoBettingInstrument]:
        """
        Get all loaded instruments.
        """
        return self._instruments.copy()

    def find_by_market_hash(self, market_hash: str) -> list[CryptoBettingInstrument]:
        """
        Find all instruments for a specific market hash.
        """
        return [inst for inst in self._instruments.values() if inst.event_id == market_hash]

    def get_market_data(self, market_hash: str) -> dict | None:
        """
        Get cached market data for a market hash.
        """
        return self._market_cache.get(market_hash)
