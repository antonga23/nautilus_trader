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
from typing import Any

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.sxbet.common import SXBetMarketType
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_SPORT_IDS
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.signing import percentage_to_decimal_odds
from nautilus_trader.common.component import Logger
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency


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
                markets_data = await self._http_client.get_markets(
                    sport_id=sport_id,
                    league_id=league_id,
                    only_active=True,
                )
                self._merge_markets(markets_by_hash, markets_data)

        return list(markets_by_hash.values())

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
        league_name = market.get("leagueName", "Unknown")

        # Get market type
        try:
            sx_market_type = SXBetMarketType(market_type)
            normalized_market_type = sx_market_type.to_normalized()
        except ValueError:
            normalized_market_type = "other"

        # Get best odds from order book if available
        outcome_one_odds = self._extract_best_odds(market, True)
        outcome_two_odds = self._extract_best_odds(market, False)

        # Create instruments for both outcomes
        for outcome_one, odds in [(True, outcome_one_odds), (False, outcome_two_odds)]:
            if odds <= 0:
                continue

            outcome = self._get_outcome_name(market_type, outcome_one)

            instrument = self._create_instrument(
                market_hash=market_hash,
                event_name=f"{team_one} vs {team_two}",
                home_name=team_one,
                away_name=team_two,
                sport_name=sport_name,
                competition_name=league_name,
                market_type=normalized_market_type,
                raw_market_type=market_type,
                outcome=outcome,
                price=odds,
                is_outcome_one=outcome_one,
                handicap=market.get("line"),
                live=is_live,
                start_time=start_time,
            )

            if instrument:
                self._instruments[instrument.id] = instrument
                self.add(instrument)

    @staticmethod
    def _extract_best_odds(market: dict, outcome_one: bool) -> float:
        """
        Extract best available odds from market.
        """
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

    @staticmethod
    def _get_outcome_name(
        market_type: int,
        outcome_one: bool,
    ) -> str:
        """
        Get outcome name based on market type and outcome.
        """
        outcomes = {
            0: "home" if outcome_one else "away",
            1: "home" if outcome_one else "away",
            2: "over" if outcome_one else "under",
            3: "home" if outcome_one else "away",
            4: "yes" if outcome_one else "no",
        }
        return outcomes.get(market_type, "outcome_1" if outcome_one else "outcome_2")

    @staticmethod
    def _market_params(market_type: str, handicap: float | None) -> str:
        if handicap is not None and market_type in {"asian_handicap", "total_goals"}:
            return f"line={handicap}"
        return ""

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
        handicap: float | None = None,
        live: bool = False,
        start_time: str | None = None,
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
                params=self._market_params(market_type, handicap),
                live=live,
                enabled=True,
                start_time=start_time,
                handicap=handicap,
                trading_status="ACTIVE",
                market_id=market_hash,
                info={
                    "outcome_one": is_outcome_one,
                    "raw_market_type": raw_market_type,
                    "is_two_way_market": raw_market_type == SXBetMarketType.MONEY_LINE.value,
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
