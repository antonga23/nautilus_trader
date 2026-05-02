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
BetDex/Monaco instrument provider.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
import re
import time
from typing import Any

from nautilus_trader.adapters.betdex.config import BetDexInstrumentProviderConfig
from nautilus_trader.adapters.betdex.constants import BETDEX_AGGREGATED_VENUES
from nautilus_trader.adapters.betdex.constants import BETDEX_DEFAULT_CURRENCY
from nautilus_trader.adapters.betdex.constants import BETDEX_VENUE
from nautilus_trader.adapters.betdex.http_client import BetDexHttpClient
from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.common.component import Logger
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency


BETDEX_PRICE_PLACEHOLDER = 2.0
BETDEX_KNOWN_CURRENCIES = frozenset({"USDC", "USD", "EUR", "BTC", "ETH", "SOL"})
BETDEX_MARKET_TYPE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MarketType.DRAW_NO_BET.value, ("draw_no_bet", "tie_no_bet")),
    (MarketType.DOUBLE_CHANCE.value, ("double_chance",)),
    (MarketType.MATCH_ODDS.value, ("match_odds", "full_time_result", "1x2")),
    (MarketType.WINNER.value, ("moneyline", "winner")),
    (MarketType.TEAM_TOTAL_GOALS.value, ("team_total",)),
    (MarketType.TOTAL_GOALS.value, ("total", "over_under")),
    (MarketType.ASIAN_HANDICAP.value, ("asian_handicap", "handicap", "spread", "line")),
)
BETDEX_DIRECT_OUTCOMES = {
    "tie": Outcome.DRAW.value,
    "x": Outcome.DRAW.value,
    "yes": Outcome.YES.value,
    "y": Outcome.YES.value,
    "no": Outcome.NO.value,
    "n": Outcome.NO.value,
}
BETDEX_COMPETITOR_INDEX_MARKETS = frozenset(
    {
        MarketType.MATCH_ODDS.value,
        MarketType.WINNER.value,
        MarketType.ASIAN_HANDICAP.value,
    },
)


class BetDexInstrumentProvider(InstrumentProvider):
    """
    Provides ``CryptoBettingInstrument`` instances from Monaco markets.
    """

    def __init__(
        self,
        http_client: BetDexHttpClient,
        config: BetDexInstrumentProviderConfig,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(config=config)
        self._http_client = http_client
        self._betdex_config = config
        self._instruments: dict[InstrumentId, CryptoBettingInstrument] = {}
        self._market_cache: dict[str, dict[str, Any]] = {}
        self._outcomes_by_market: dict[str, list[dict[str, Any]]] = {}
        if logger is not None:
            self._log = logger

    async def load_all_async(self, filters: dict[str, Any] | None = None) -> None:
        started_at = time.perf_counter()
        filters = filters or {}
        event_ids = self._filter_set(filters, "event_ids") or self._betdex_config.event_ids
        market_limit = self._betdex_config.market_discovery_limit
        instrument_limit = self._betdex_config.instrument_load_limit

        if not event_ids:
            event_ids = await self._discover_event_ids(filters)

        markets = await self._discover_markets(
            event_ids=event_ids,
            filters=filters,
            market_limit=market_limit,
        )

        processed = 0
        for market_payload in markets:
            if instrument_limit is not None and len(self._instruments) >= instrument_limit:
                break
            processed += self._process_market_response(market_payload, instrument_limit)

        self._log.info(
            f"Loaded {len(self._instruments)} BetDex instruments from {processed} markets "
            f"elapsed={time.perf_counter() - started_at:.2f}s",
        )
        return None

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict[str, Any] | None = None,
    ) -> None:
        if not instrument_ids:
            return
        market_ids = {
            str(instrument_id).split(":", 1)[0]
            for instrument_id in instrument_ids
            if str(instrument_id).endswith(f".{BETDEX_VENUE.value}")
        }
        if not market_ids:
            return
        payload = await self._http_client.get_markets(ids=sorted(market_ids), page=0, size=100)
        self._process_market_response(payload, None)

    async def _discover_event_ids(self, filters: dict[str, Any]) -> frozenset[str]:  # skipcq
        event_limit = int(self._betdex_config.event_discovery_limit)
        payload = await self._http_client.get_events(
            **self._event_query_params(filters=filters, event_limit=event_limit),
        )
        allowed_sports = (
            self._filter_set(filters, "sport_keys") or self._betdex_config.sport_keys or frozenset()
        )
        return frozenset(
            self._selected_event_ids(
                payload=payload,
                allowed_sports=allowed_sports,
                event_limit=event_limit,
            ),
        )

    def _event_query_params(
        self,
        *,
        filters: dict[str, Any],
        event_limit: int,
    ) -> dict[str, Any]:
        category_ids = self._filter_set(filters, "category_ids") or self._betdex_config.category_ids
        subcategory_ids = (
            self._filter_set(filters, "subcategory_ids") or self._betdex_config.subcategory_ids
        )
        event_group_ids = (
            self._filter_set(filters, "event_group_ids") or self._betdex_config.event_group_ids
        )
        return {
            "categoryIds": sorted(category_ids or []),
            "subcategoryIds": sorted(subcategory_ids or []),
            "eventGroupIds": sorted(event_group_ids or []),
            "active": True,
            "starting": "Live"
            if self._betdex_config.live_only
            else filters.get("starting") or "Later",
            "page": 0,
            "size": min(int(self._betdex_config.page_size), event_limit),
            "sort": ["expectedStartTime,asc"],
        }

    def _selected_event_ids(
        self,
        *,
        payload: dict[str, Any],
        allowed_sports: frozenset[str],
        event_limit: int,
    ) -> list[str]:
        groups_by_id = self._by_id(payload.get("eventGroups"))
        subcategories_by_id = self._by_id(payload.get("subcategories"))
        categories_by_id = self._by_id(payload.get("categories"))
        event_ids: list[str] = []
        for event in payload.get("events", []):
            if allowed_sports and not self._event_matches_sports(
                event,
                allowed_sports=allowed_sports,
                groups_by_id=groups_by_id,
                subcategories_by_id=subcategories_by_id,
                categories_by_id=categories_by_id,
            ):
                continue
            event_id = event.get("id")
            if isinstance(event_id, str) and event_id:
                event_ids.append(event_id)
            if len(event_ids) >= event_limit:
                break
        return event_ids

    @classmethod
    def _event_matches_sports(
        cls,
        event: dict[str, Any],
        *,
        allowed_sports: frozenset[str],
        groups_by_id: dict[str, dict[str, Any]],
        subcategories_by_id: dict[str, dict[str, Any]],
        categories_by_id: dict[str, dict[str, Any]],
    ) -> bool:
        group = groups_by_id.get(cls._first_ref_id(event.get("eventGroup")), {})
        subcategory = subcategories_by_id.get(cls._first_ref_id(group.get("subcategory")), {})
        category = categories_by_id.get(cls._first_ref_id(subcategory.get("category")), {})
        names = {
            cls._canonical_sport(str(category.get("name") or "")),
            cls._canonical_sport(str(subcategory.get("name") or "")),
        }
        allowed = {cls._canonical_sport(value) for value in allowed_sports}
        return bool((names - {"unknown"}) & allowed)

    async def _discover_markets(
        self,
        *,
        event_ids: frozenset[str] | None,
        filters: dict[str, Any],
        market_limit: int | None,
    ) -> list[dict[str, Any]]:
        limit = (
            market_limit
            or self._betdex_config.instrument_load_limit
            or int(
                self._betdex_config.page_size,
            )
        )
        size = min(int(self._betdex_config.page_size), max(1, int(limit)))
        statuses = filters.get("statuses") or ["Open"]
        in_play_statuses = (
            ["InPlay"] if self._betdex_config.live_only else filters.get("in_play_statuses")
        )
        payload = await self._http_client.get_markets(
            eventIds=sorted(event_ids or []),
            statuses=statuses,
            inPlayStatuses=in_play_statuses,
            published=True,
            page=0,
            size=size,
            sort=["lockAt,asc"],
        )
        return [payload]

    def _process_market_response(
        self,
        payload: dict[str, Any],
        instrument_limit: int | None,
    ) -> int:
        events_by_id = self._by_id(payload.get("events"))
        groups_by_id = self._by_id(payload.get("eventGroups"))
        subcategories_by_id = self._by_id(payload.get("subcategories"))
        categories_by_id = self._by_id(payload.get("categories"))
        outcomes_by_id = self._by_id(payload.get("marketOutcomes"))
        participants_by_id = self._by_id(payload.get("participants"))
        external_refs = payload.get("externalReferences") or []

        processed = 0
        for market in payload.get("markets", []):
            if instrument_limit is not None and len(self._instruments) >= instrument_limit:
                break
            if not self._market_is_tradable(market):
                continue
            market_id = str(market.get("id") or "")
            if not market_id:
                continue
            event = events_by_id.get(self._first_ref_id(market.get("event")), {})
            outcome_ids = self._ref_ids(market.get("marketOutcomes"))
            outcomes = [
                outcomes_by_id[outcome_id]
                for outcome_id in outcome_ids
                if outcome_id in outcomes_by_id
            ]
            if not outcomes:
                continue

            self._market_cache[market_id] = market
            self._outcomes_by_market[market_id] = outcomes
            processed += 1
            context = self._context(
                market=market,
                event=event,
                groups_by_id=groups_by_id,
                subcategories_by_id=subcategories_by_id,
                categories_by_id=categories_by_id,
                participants_by_id=participants_by_id,
                external_refs=external_refs,
            )

            for index, outcome in enumerate(
                sorted(outcomes, key=lambda item: item.get("ordering", 0)),
            ):
                if instrument_limit is not None and len(self._instruments) >= instrument_limit:
                    break
                instrument = self._create_instrument(
                    market=market,
                    event=event,
                    outcome=outcome,
                    outcome_index=index,
                    context=context,
                )
                self._instruments[instrument.id] = instrument
                self.add(instrument)
        return processed

    def _create_instrument(  # skipcq
        self,
        *,
        market: dict[str, Any],
        event: dict[str, Any],
        outcome: dict[str, Any],
        outcome_index: int,
        context: dict[str, Any],
    ) -> CryptoBettingInstrument:
        event_id = str(event.get("id") or self._first_ref_id(market.get("event")) or "")
        market_id = str(market.get("id") or "")
        outcome_id = str(outcome.get("id") or "")
        event_name = str(event.get("name") or market.get("name") or event_id)
        market_name = str(
            market.get("name") or market.get("marketType", {}).get("_ref") or "market",
        )
        market_type = self._normalize_market_type(market_name)
        params, handicap = self._market_params(market)
        home_name, away_name = self._participants(event_name, context, outcome)
        outcome_role = self._normalize_outcome(
            outcome=outcome,
            outcome_index=outcome_index,
            home_name=home_name,
            away_name=away_name,
            market_type=market_type,
        )
        live = market.get("inPlayStatus") == "InPlay"
        currency_id = str(market.get("currencyId") or BETDEX_DEFAULT_CURRENCY)
        currency = self._currency_from_id(currency_id)
        sport_name = str(context.get("sport_name") or "unknown")
        competition_name = str(context.get("competition_name") or "BetDex")
        lock_at = market.get("lockAt") or event.get("expectedStartTime")
        info = self._instrument_info(
            market=market,
            event=event,
            outcome=outcome,
            market_id=market_id,
            outcome_id=outcome_id,
            currency_id=currency_id,
            context=context,
        )

        return CryptoBettingInstrument(
            venue=BETDEX_VENUE,
            event_id=event_id or market_id,
            event_name=event_name,
            home_name=home_name,
            away_name=away_name,
            sport_name=sport_name,
            competition_name=competition_name,
            market_name=market_name,
            market_type=market_type,
            outcome=outcome_role,
            side=SelectionSide.BACK,
            price=BETDEX_PRICE_PLACEHOLDER,
            currency=currency,
            params=params,
            live=live,
            enabled=self._market_is_tradable(market),
            max_size=None,
            min_size=None,
            start_time=str(lock_at) if lock_at else None,
            handicap=handicap,
            trading_status=str(market.get("status") or ""),
            market_id=market_id,
            sport_id=str(context.get("sport_id") or ""),
            competition_id=str(context.get("competition_id") or ""),
            instrument_key=outcome_id,
            info=info,
        )

    def _instrument_info(
        self,
        *,
        market: dict[str, Any],
        event: dict[str, Any],
        outcome: dict[str, Any],
        market_id: str,
        outcome_id: str,
        currency_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "provider": "BETDEX",
            "aggregator": True,
            "aggregated_venues": sorted(BETDEX_AGGREGATED_VENUES),
            "owner_app_id": market.get("ownerAppId") or event.get("ownerAppId"),
            "market_id": market_id,
            "currency_id": currency_id,
            "outcome_id": outcome_id,
            "outcome_title": outcome.get("title"),
            "outcome_ordering": outcome.get("ordering"),
            "raw_market_type": self._first_ref_id(market.get("marketType")),
            "market_value": market.get("marketValue"),
            "market_discriminator": market.get("marketDiscriminator"),
            "in_play_status": market.get("inPlayStatus"),
            "event_start_action": market.get("eventStartAction"),
            "market_lock_action": market.get("marketLockAction"),
            "cross_matching_enabled": market.get("crossMatchingEnabled"),
            "external_references": context.get("external_references", ()),
        }

    @staticmethod
    def _currency_from_id(currency_id: str) -> Currency:
        if currency_id.upper() not in BETDEX_KNOWN_CURRENCIES:
            return Currency.from_str(BETDEX_DEFAULT_CURRENCY)
        try:
            return Currency.from_str(currency_id)
        except ValueError:
            return Currency.from_str(BETDEX_DEFAULT_CURRENCY)

    @staticmethod
    def _market_is_tradable(market: dict[str, Any]) -> bool:
        return (
            market.get("published", True) is True
            and market.get("suspended", False) is not True
            and str(market.get("status") or "").lower() == "open"
        )

    @classmethod
    def _context(
        cls,
        *,
        market: dict[str, Any],
        event: dict[str, Any],
        groups_by_id: dict[str, dict[str, Any]],
        subcategories_by_id: dict[str, dict[str, Any]],
        categories_by_id: dict[str, dict[str, Any]],
        participants_by_id: dict[str, dict[str, Any]],
        external_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        group_id = cls._first_ref_id(event.get("eventGroup"))
        group = groups_by_id.get(group_id, {})
        subcategory_id = cls._first_ref_id(group.get("subcategory"))
        subcategory = subcategories_by_id.get(subcategory_id, {})
        category_id = cls._first_ref_id(subcategory.get("category"))
        category = categories_by_id.get(category_id, {})
        participant_ids = cls._ref_ids(event.get("participants"))
        participants = [
            participants_by_id[item] for item in participant_ids if item in participants_by_id
        ]
        market_ref_ids = set(cls._ref_ids(market.get("externalReferences")))
        event_ref_ids = set(cls._ref_ids(event.get("externalReferences")))
        refs = [
            {
                "source": ref.get("source"),
                "externalReference": ref.get("externalReference"),
            }
            for ref in external_refs
            if str(ref.get("id")) in market_ref_ids | event_ref_ids
        ]
        return {
            "sport_id": category_id or subcategory_id,
            "sport_name": cls._canonical_sport(
                str(category.get("name") or subcategory.get("name") or ""),
            ),
            "competition_id": group_id or subcategory_id or "",
            "competition_name": group.get("name") or subcategory.get("name") or "",
            "participants": participants,
            "external_references": tuple(refs),
        }

    @classmethod
    def _participants(
        cls,
        event_name: str,
        context: dict[str, Any],
        outcome: dict[str, Any],
    ) -> tuple[str, str]:
        participants = context.get("participants") or []
        if len(participants) >= 2:
            names = [str(item.get("name") or "") for item in participants[:2]]
            if all(names):
                return names[0], names[1]

        participant_id = cls._first_ref_id(outcome.get("participant"))
        participant = next(
            (item for item in participants if item.get("id") == participant_id),
            None,
        )
        left, right = cls._split_event_name(event_name)
        if participant and participant.get("name"):
            selected = str(participant["name"])
            if cls._same_name(selected, right):
                return left or selected, selected
            return selected, right
        return left, right

    @staticmethod
    def _split_event_name(event_name: str) -> tuple[str, str]:
        for separator in (" vs ", " v ", " @ ", " at "):
            if separator in event_name.lower():
                pattern = re.compile(re.escape(separator), flags=re.IGNORECASE)
                left, right = pattern.split(event_name, maxsplit=1)
                return left.strip(), right.strip()
        return event_name.strip(), ""

    @classmethod
    def _normalize_market_type(cls, name: str) -> str:  # skipcq
        text = cls._normalize_text(name)
        for market_type, aliases in BETDEX_MARKET_TYPE_ALIASES:
            if any(alias in text for alias in aliases):
                return market_type
        return MarketType.OTHER.value

    @classmethod
    def _normalize_outcome(
        cls,
        *,
        outcome: dict[str, Any],
        outcome_index: int,
        home_name: str,
        away_name: str,
        market_type: str,
    ) -> str:
        title = str(outcome.get("title") or "")
        text = cls._normalize_text(title)
        if "draw" in text:
            return Outcome.DRAW.value
        direct_outcome = BETDEX_DIRECT_OUTCOMES.get(text)
        if direct_outcome is not None:
            return direct_outcome
        totals_outcome = cls._totals_outcome(text)
        if totals_outcome is not None:
            return totals_outcome
        participant_outcome = cls._participant_outcome(title, home_name, away_name)
        if participant_outcome is not None:
            return participant_outcome
        indexed_outcome = cls._indexed_competitor_outcome(market_type, outcome_index)
        if indexed_outcome is not None:
            return indexed_outcome
        return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or Outcome.OTHER.value

    @staticmethod
    def _totals_outcome(text: str) -> str | None:
        if text.startswith("over") or text == "o":
            return Outcome.OVER.value
        if text.startswith("under") or text == "u":
            return Outcome.UNDER.value
        return None

    @classmethod
    def _participant_outcome(
        cls,
        title: str,
        home_name: str,
        away_name: str,
    ) -> str | None:
        if home_name and cls._same_name(title, home_name):
            return Outcome.HOME.value
        if away_name and cls._same_name(title, away_name):
            return Outcome.AWAY.value
        return None

    @staticmethod
    def _indexed_competitor_outcome(market_type: str, outcome_index: int) -> str | None:
        if market_type not in BETDEX_COMPETITOR_INDEX_MARKETS:
            return None
        return {0: Outcome.HOME.value, 1: Outcome.AWAY.value}.get(outcome_index)

    @staticmethod
    def _market_params(market: dict[str, Any]) -> tuple[str, float | None]:
        raw_value = market.get("marketValue")
        handicap: float | None = None
        params: list[str] = []
        if raw_value not in (None, ""):
            parsed = BetDexInstrumentProvider._parse_float(raw_value)
            if parsed is not None:
                handicap = parsed
                params.append(f"line={BetDexInstrumentProvider._format_float(parsed)}")
            else:
                params.append(f"value={raw_value}")
        discriminator = market.get("marketDiscriminator")
        if discriminator not in (None, ""):
            params.append(f"discriminator={discriminator}")
        return "&".join(params), handicap

    @staticmethod
    def _parse_float(value: Any) -> float | None:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_float(value: float) -> str:
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return "0" if text == "-0" else text

    @staticmethod
    def _by_id(items: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(items, list):
            return {}
        return {
            str(item["id"]): item
            for item in items
            if isinstance(item, dict) and item.get("id") is not None
        }

    @staticmethod
    def _ref_ids(ref: Any) -> list[str]:
        if not isinstance(ref, dict):
            return []
        values = ref.get("_ids")
        if isinstance(values, list):
            return [str(value) for value in values if value not in (None, "")]
        value = ref.get("_id")
        return [str(value)] if value not in (None, "") else []

    @classmethod
    def _first_ref_id(cls, ref: Any) -> str:
        ids = cls._ref_ids(ref)
        return ids[0] if ids else ""

    @staticmethod
    def _filter_set(filters: dict[str, Any], key: str) -> frozenset[str] | None:
        value = filters.get(key)
        if not value:
            return None
        if isinstance(value, str):
            return frozenset({value})
        return frozenset(str(item) for item in value)

    @staticmethod
    def _canonical_sport(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        aliases = {
            "football": "soccer",
            "american_football": "american_football",
            "basketball": "basketball",
            "tennis": "tennis",
            "baseball": "baseball",
        }
        return aliases.get(normalized, normalized or "unknown")

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")

    @classmethod
    def _same_name(cls, left: str, right: str) -> bool:
        return cls._normalize_text(left) == cls._normalize_text(right)

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def find_by_market_id(self, market_id: str) -> list[CryptoBettingInstrument]:
        return [
            instrument
            for instrument in self._instruments.values()
            if instrument.market_id == market_id
        ]

    def find_by_outcome_id(self, outcome_id: str) -> CryptoBettingInstrument | None:
        for instrument in self._instruments.values():
            info = instrument.info if isinstance(instrument.info, dict) else {}
            if info.get("outcome_id") == outcome_id:
                return instrument
        return None
