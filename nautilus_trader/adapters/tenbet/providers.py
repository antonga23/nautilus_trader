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
10bet instrument provider using browser scraping.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.tenbet.browser_client import TenBetBrowserClient
from nautilus_trader.adapters.tenbet.config import TenBetInstrumentProviderConfig
from nautilus_trader.adapters.tenbet.constants import TENBET_SPORTS
from nautilus_trader.adapters.tenbet.constants import TENBET_VENUE
from nautilus_trader.adapters.tenbet.constants import DEFAULT_SCRAPE_INTERVAL_SECONDS
from nautilus_trader.common.component import Logger
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency


class TenBetInstrumentProvider(InstrumentProvider):
    """
    Provides betting instruments for 10bet via browser scraping.
    """

    def __init__(
        self,
        browser_client: TenBetBrowserClient,
        config: TenBetInstrumentProviderConfig,
        logger: Logger | None = None,
    ) -> None:
        if not isinstance(config, TenBetInstrumentProviderConfig):
            config = TenBetInstrumentProviderConfig(
                load_all=bool(getattr(config, "load_all", False)),
                load_ids=getattr(config, "load_ids", None),
                filters=getattr(config, "filters", None),
                base_url=getattr(config, "base_url", None),
                sports=getattr(config, "sports", None),
                headless=bool(getattr(config, "headless", True)),
                scrape_interval=int(
                    getattr(config, "scrape_interval", DEFAULT_SCRAPE_INTERVAL_SECONDS),
                ),
            )
        super().__init__(config=config)
        self._browser_client = browser_client
        self._config = config
        if logger is not None:
            self._log = logger
        self._market_cache: dict[str, dict[str, Any]] = {}

    async def load_all_async(self, filters: dict | None = None) -> None:
        """
        Load all instruments from 10bet.
        """
        self._log.info("Loading instruments from 10bet...")

        if not self._browser_client.is_connected:
            await self._browser_client.connect()

        filters = filters or {}
        sports = self._config.sports or filters.get("sports") or frozenset({"soccer", "basketball"})

        for sport in sports:
            try:
                self._log.info(f"Loading {sport} markets...")
                markets = await self._browser_client.get_markets_for_sport(str(sport))
                self._log.info(f"Found {len(markets)} markets for {sport}")
                for market in markets:
                    await self._process_market(market)
            except Exception as e:
                self._log.error(f"Error loading {sport} markets: {e}")

        self._loaded = True
        self._log.info(f"Instrument loading complete ({len(self._instruments)} instruments)")

    def load_all(self, filters: dict | None = None) -> None:
        self._log.debug(f"Synchronous load requested with filters={filters!r}")
        raise NotImplementedError("Use load_all_async() for browser-based loading")

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        if not instrument_ids:
            return
        await self.load_all_async(filters)

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict | None = None,
    ) -> None:
        await self.load_ids_async([instrument_id], filters)

    async def _process_market(self, market: dict[str, Any]) -> None:
        market_hash = str(market.get("marketHash", ""))
        if not market_hash:
            self._log.warning("Skipping market without a marketHash")
            return

        self._market_cache[market_hash] = market

        start_time = self._parse_start_time(market.get("gameTime"))
        team_one = str(market.get("teamOneName", "Unknown"))
        team_two = str(market.get("teamTwoName", "Unknown"))
        sport_id = int(market.get("sportId", 0) or 0)
        market_type = int(market.get("type", 0) or 0)
        is_live = self._is_live_market(market)
        sport_name = TENBET_SPORTS.get(sport_id, "unknown")
        league_name = str(market.get("leagueName", "Unknown"))

        outcomes = [
            (True, self._extract_best_odds(market, True)),
            (False, self._extract_best_odds(market, False)),
        ]

        for outcome_one, odds in outcomes:
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
                market_type=self._market_type_name(market_type),
                raw_market_type=market_type,
                outcome=outcome,
                price=odds,
                is_outcome_one=outcome_one,
                handicap=self._extract_handicap(market),
                live=is_live,
                start_time=start_time,
            )

            if instrument is not None:
                self._instruments[instrument.id] = instrument
                self.add(instrument)

    @staticmethod
    def _extract_best_odds(market: dict[str, Any], outcome_one: bool) -> float:
        best_odds = 0.0
        for order in market.get("orders", []):
            if bool(order.get("isMakerBettingOutcomeOne")) != outcome_one:
                percentage_odds = int(order.get("percentageOdds", 0) or 0)
                if percentage_odds > 0:
                    decimal_odds = 10000 / percentage_odds
                    best_odds = max(best_odds, decimal_odds)

        if best_odds <= 0:
            implied = market.get(
                "outcomeOneProbability" if outcome_one else "outcomeTwoProbability",
                0,
            )
            if implied and implied > 0:
                best_odds = 1 / float(implied)

        return best_odds

    @staticmethod
    def _market_type_name(market_type: int) -> str:
        return {
            0: "match_odds",
            1: "match_result",
            2: "total_goals",
            3: "asian_handicap",
            4: "special",
        }.get(market_type, "other")

    @staticmethod
    def _get_outcome_name(market_type: int, outcome_one: bool) -> str:
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

    @staticmethod
    def _extract_handicap(market: dict[str, Any]) -> float | None:
        line = market.get("line")
        if line in (None, ""):
            return None
        try:
            return float(line)
        except (TypeError, ValueError):
            return None

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
        try:
            return CryptoBettingInstrument(
                venue=TENBET_VENUE,
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
                currency=Currency.from_str("ZAR"),
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
                    "sport_id": sport_name,
                },
            )
        except (TypeError, ValueError) as e:
            self._log.warning(f"Failed to create 10bet instrument: {e}")
            return None

    def get_market_data(self, market_hash: str) -> dict | None:
        return self._market_cache.get(market_hash)
