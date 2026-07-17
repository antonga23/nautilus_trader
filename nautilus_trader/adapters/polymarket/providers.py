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

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import msgspec
from py_clob_client.client import ClobClient

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket.common.gamma_markets import list_markets
from nautilus_trader.adapters.polymarket.common.gamma_markets import (
    normalize_gamma_market_to_clob_format,
)
from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_condition_id
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.adapters.polymarket.http.errors import PolymarketAPIError
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import BinaryOption


POLYMARKET_SPORT_KEYWORDS = {
    "american_football": ("nfl", "cfb", "american football", "super bowl"),
    "baseball": ("mlb", "baseball", "world series"),
    "basketball": ("nba", "wnba", "ncaab", "basketball"),
    "cricket": ("cricket",),
    "golf": ("golf", "pga"),
    "ice_hockey": ("nhl", "hockey", "stanley cup"),
    "mma": ("ufc", "mma"),
    "rugby": ("rugby",),
    "soccer": (
        "soccer",
        "premier league",
        "champions league",
        "la liga",
        "serie a",
        "bundesliga",
        "epl",
        "ucl",
        "football",
    ),
    "tennis": ("tennis", "wimbledon", "us open", "australian open", "french open", "atp", "wta"),
}
POLYMARKET_DEFAULT_MAX_MARKETS_PER_EVENT = 16


def _canonical_polymarket_sport(raw: str) -> str:
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "nfl": "american_football",
        "cfb": "american_football",
        "mlb": "baseball",
        "nba": "basketball",
        "ncaab": "basketball",
        "wnba": "basketball",
        "nhl": "ice_hockey",
        "epl": "soccer",
        "ucl": "soccer",
        "atp": "tennis",
        "wta": "tennis",
        "ufc": "mma",
    }
    return aliases.get(normalized, normalized)


def _market_sport_candidates(market: dict[str, Any]) -> set[str]:
    original_event = {}
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        original_event = events[0]

    explicit_sports = [
        market.get("sport"),
        market.get("sportsTag"),
        market.get("sportsMarketType"),
        original_event.get("sport"),
    ]
    candidates = {
        _canonical_polymarket_sport(str(value))
        for value in explicit_sports
        if value not in (None, "")
    }

    haystack = " ".join(
        str(value)
        for value in (
            market.get("question"),
            market.get("slug"),
            market.get("description"),
            market.get("category"),
            original_event.get("title"),
            original_event.get("slug"),
        )
        if value
    ).lower()
    for sport, keywords in POLYMARKET_SPORT_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            candidates.add(sport)

    return candidates


def _market_matches_sports_filter(market: dict[str, Any], sports: set[str]) -> bool:
    if not sports:
        return True
    return bool(_market_sport_candidates(market) & sports)


def _selected_sports_tag_ids(sport_metadata: dict[str, Any]) -> list[str]:
    tags = [tag.strip() for tag in str(sport_metadata.get("tags") or "").split(",") if tag.strip()]
    return [tag for tag in tags if tag not in {"1", "100639"}] or tags[:1]


def _market_unique_id(market: dict[str, Any]) -> str:
    return str(market.get("conditionId") or market.get("id") or market.get("slug") or "")


def _market_event_key(market: dict[str, Any]) -> str:
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event = events[0]
        return str(
            event.get("id")
            or event.get("slug")
            or "|".join(
                str(value)
                for value in (
                    event.get("title"),
                    event.get("startDate") or event.get("startDateIso"),
                )
                if value
            ),
        )
    return _market_unique_id(market)


def _market_family_priority(market: dict[str, Any]) -> int:
    text = " ".join(
        str(value)
        for value in (
            market.get("sportsMarketType"),
            market.get("question"),
            market.get("slug"),
        )
        if value
    ).lower()
    if any(token in text for token in ("winner", "moneyline", "match odds", " win ")):
        return 0
    if any(token in text for token in ("spread", "handicap", "draw no bet")):
        return 1
    if any(token in text for token in ("total", "over", "under")):
        return 2
    return 3


def _diversify_ranked_markets_by_event(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_groups: dict[str, list[dict[str, Any]]] = {}
    event_order: list[str] = []
    for market in markets:
        event_key = _market_event_key(market)
        if event_key not in event_groups:
            event_groups[event_key] = []
            event_order.append(event_key)
        event_groups[event_key].append(market)
    for group in event_groups.values():
        group.sort(key=lambda market: (_market_family_priority(market), _market_unique_id(market)))

    diversified: list[dict[str, Any]] = []
    while event_order:
        next_order: list[str] = []
        for event_key in event_order:
            group = event_groups[event_key]
            if group:
                diversified.append(group.pop(0))
            if group:
                next_order.append(event_key)
        event_order = next_order
    return diversified


def _market_has_event_metadata(market: dict[str, Any]) -> bool:
    events = market.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("title") or event.get("slug") or event.get("startDate"):
                return True
    return bool(
        market.get("startDate") or market.get("startDateIso") or market.get("gameStartTime"),
    )


def _parse_gamma_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _market_event_or_resolution_times(market: dict[str, Any]) -> list[datetime]:
    times: list[datetime] = []
    events = market.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            for key in ("startDate", "startDateIso", "gameStartTime", "endDate", "endDateIso"):
                parsed = _parse_gamma_datetime(event.get(key))
                if parsed is not None:
                    times.append(parsed)
    for key in ("startDate", "startDateIso", "gameStartTime", "endDate", "endDateIso"):
        parsed = _parse_gamma_datetime(market.get(key))
        if parsed is not None:
            times.append(parsed)
    return times


def _market_horizon_sort_key(
    market: dict[str, Any],
    *,
    now: datetime,
    horizon: timedelta | None,
) -> tuple[int, float, str]:
    if horizon is None:
        return (0, 0.0, _market_unique_id(market))

    stale_grace = timedelta(hours=6)
    times = _market_event_or_resolution_times(market)
    if not times:
        return (2, float("inf"), _market_unique_id(market))

    deltas = [(timestamp - now).total_seconds() for timestamp in times]
    horizon_secs = horizon.total_seconds()
    stale_grace_secs = stale_grace.total_seconds()
    inside = [abs(delta) for delta in deltas if -stale_grace_secs <= delta <= horizon_secs]
    if inside:
        return (0, min(inside), _market_unique_id(market))

    future = [delta for delta in deltas if delta > horizon_secs]
    if future:
        return (1, min(future), _market_unique_id(market))

    return (3, abs(max(deltas)), _market_unique_id(market))


def _rank_markets_by_horizon(
    markets: list[dict[str, Any]],
    *,
    now: datetime,
    horizon: timedelta | None,
) -> list[dict[str, Any]]:
    if horizon is None:
        return markets
    return sorted(
        markets,
        key=lambda market: _market_horizon_sort_key(market, now=now, horizon=horizon),
    )


def _runtime_horizon_candidate(
    market: dict[str, Any],
    *,
    now: datetime,
    horizon: timedelta | None,
) -> bool:
    if horizon is None:
        return True
    bucket = _market_horizon_sort_key(market, now=now, horizon=horizon)[0]
    # Keep markets inside the near-term window and markets with missing timing
    # metadata. Exclude stale resolved markets and known far-future markets so
    # they do not consume live-pilot discovery and quote capacity.
    return bucket in {0, 2}


def _rank_runtime_horizon_markets(
    markets: list[dict[str, Any]],
    *,
    now: datetime,
    horizon: timedelta | None,
) -> list[dict[str, Any]]:
    if horizon is None:
        return markets
    return _diversify_ranked_markets_by_event(
        _rank_markets_by_horizon(
            [
                market
                for market in markets
                if _runtime_horizon_candidate(market, now=now, horizon=horizon)
            ],
            now=now,
            horizon=horizon,
        ),
    )


def _isoformat_z(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _with_horizon_date_filters(
    filters: dict[str, Any],
    *,
    now: datetime,
    horizon: timedelta | None,
) -> dict[str, Any]:
    if horizon is None:
        return filters
    stale_grace = timedelta(hours=6)
    return {
        **filters,
        "start_date_min": _isoformat_z(now - stale_grace),
        "start_date_max": _isoformat_z(now + horizon),
    }


def _horizon_fetch_limit(limit: int | None, horizon: timedelta | None) -> int | None:
    if limit is None or horizon is None:
        return limit
    return min(max(limit * 3, limit + 25), 500)


def _selected_sports_tag_groups(
    sports_metadata: list[Any],
    sports_filter: set[str],
) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for sport_metadata in sports_metadata:
        if not isinstance(sport_metadata, dict):
            continue
        sport_code = str(sport_metadata.get("sport") or "")
        canonical_sport = _canonical_polymarket_sport(sport_code)
        if canonical_sport not in sports_filter:
            continue
        group = groups.setdefault(
            canonical_sport,
            {
                "sport": canonical_sport,
                "sport_codes": [],
                "tag_ids": [],
            },
        )
        if sport_code and sport_code not in group["sport_codes"]:
            group["sport_codes"].append(sport_code)
        for tag_id in _selected_sports_tag_ids(sport_metadata):
            if tag_id not in group["tag_ids"]:
                group["tag_ids"].append(tag_id)
    return groups


def _collect_sports_event_markets(
    *,
    canonical_sport: str,
    sport_code: str,
    selected_tags: list[str],
    events: list[dict[str, Any]],
    discovered_markets: dict[str, dict[str, Any]],
    max_markets_per_event: int | None = None,
) -> None:
    for event in events:
        if not isinstance(event, dict):
            continue
        event_markets = [market for market in event.get("markets", []) if isinstance(market, dict)]
        event_markets.sort(
            key=lambda market: (_market_family_priority(market), _market_unique_id(market)),
        )
        if max_markets_per_event is not None:
            event_markets = event_markets[:max_markets_per_event]
        for market in event_markets:
            if not isinstance(market, dict):
                continue
            market_id = _market_unique_id(market)
            if not market_id:
                continue
            enriched_market = dict(market)
            enriched_market["sport"] = canonical_sport
            enriched_market["sportsTag"] = sport_code
            enriched_market["sportsTagIds"] = tuple(selected_tags)
            enriched_market["events"] = [
                {
                    "id": event.get("id"),
                    "title": event.get("title"),
                    "slug": event.get("slug"),
                    "startDate": event.get("startDate"),
                    "startDateIso": event.get("startDateIso") or event.get("startDate"),
                    "sport": canonical_sport,
                },
            ]
            discovered_markets[market_id] = enriched_market


def _enrich_sport_tag_market(
    market: dict[str, Any],
    *,
    canonical_sport: str,
    sport_code: str,
    selected_tags: list[str],
) -> dict[str, Any]:
    enriched_market = dict(market)
    enriched_market["sport"] = canonical_sport
    enriched_market["sportsTag"] = sport_code
    enriched_market["sportsTagIds"] = tuple(selected_tags)
    events = enriched_market.get("events")
    if isinstance(events, list):
        enriched_events: list[Any] = []
        for event in events:
            if isinstance(event, dict):
                enriched_event = dict(event)
                enriched_event.setdefault("sport", canonical_sport)
                enriched_events.append(enriched_event)
            else:
                enriched_events.append(event)
        enriched_market["events"] = enriched_events
    return enriched_market


def _selected_sports_metadata(
    sports_metadata: list[Any],
    sports_filter: set[str],
) -> list[dict[str, Any]]:
    return [
        sport_metadata
        for sport_metadata in sports_metadata
        if isinstance(sport_metadata, dict)
        and _canonical_polymarket_sport(str(sport_metadata.get("sport") or "")) in sports_filter
    ]


def _balanced_sport_limit(max_results: int | None, selected_sport_count: int) -> int | None:
    if max_results is None:
        return None
    return max(1, (max_results + selected_sport_count - 1) // selected_sport_count)


def _add_balanced_sport_markets(
    *,
    discovered_markets: dict[str, dict[str, Any]],
    overflow_markets: dict[str, dict[str, Any]],
    sport_markets: dict[str, dict[str, Any]],
    sport_quota: int,
    now: datetime,
    horizon: timedelta | None,
) -> None:
    ranked_markets = _rank_runtime_horizon_markets(
        list(sport_markets.values()),
        now=now,
        horizon=horizon,
    )
    for index, market in enumerate(ranked_markets):
        market_id = _market_unique_id(market)
        if not market_id:
            continue
        if index < sport_quota:
            discovered_markets.setdefault(market_id, market)
        else:
            overflow_markets.setdefault(market_id, market)


def _check_clob_response(response: dict[str, Any] | str) -> dict[str, Any]:
    """
    Check CLOB API response and raise exception if error string returned.

    Parameters
    ----------
    response : dict[str, Any] | str
        The response from the CLOB API.

    Returns
    -------
    dict[str, Any]
        The validated response dictionary.

    Raises
    ------
    PolymarketAPIError
        If response is an error string.

    """
    if isinstance(response, str):
        raise PolymarketAPIError(response)
    return response


class PolymarketInstrumentProvider(InstrumentProvider):
    """
    Provides Nautilus instrument definitions from Polymarket.

    Parameters
    ----------
    client : ClobClient
        The Polymarket CLOB HTTP client.
    clock : LiveClock
        The clock instance.
    config : InstrumentProviderConfig, optional
        The instrument provider configuration, by default None.
    http_client : HttpClient, optional
        The HTTP client for Gamma API requests.

    """

    def __init__(
        self,
        client: ClobClient,
        clock: LiveClock,
        config: InstrumentProviderConfig | None = None,
        http_client: HttpClient | None = None,
    ) -> None:
        super().__init__(config=config)
        self._clock = clock
        self._client = client
        self._http_client = http_client or HttpClient(timeout_secs=30)

        self._log_warnings = config.log_warnings if config else True
        self._decoder = msgspec.json.Decoder()
        self._encoder = msgspec.json.Encoder()

    async def load_all_async(self, filters: dict | None = None) -> None:
        if self._config.use_gamma_markets:
            await self._load_markets_using_gamma(filters=filters)
            return
        await self._load_markets([], filters)

    async def _load_ids_using_gamma_markets(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """
        Load instruments using Gamma API markets.
        """
        # Extract unique condition IDs (markets can have multiple tokens/instruments)
        condition_ids = list({get_polymarket_condition_id(inst_id) for inst_id in instrument_ids})

        # Build set of requested token_ids for filtering
        requested_token_ids = {get_polymarket_token_id(inst_id) for inst_id in instrument_ids}

        # Create a copy to avoid mutating the caller's filters
        filters = filters.copy() if filters is not None else {}

        if condition_ids and (
            len(condition_ids) <= 100
        ):  # There is an API limit of max 100 condition_ids in the query string.
            self._log.info(
                f"Loading {len(instrument_ids)} instruments from {len(condition_ids)} markets, using direct condition_id filtering",
            )
            filters["condition_ids"] = condition_ids
        else:
            self._log.info(
                f"Loading {len(instrument_ids)} instruments from {len(condition_ids)} markets, using bulk load of all markets",
            )

        markets = await list_markets(http_client=self._http_client, filters=filters)
        self._log.info(f"Loaded {len(markets)} markets using Gamma API")
        for market in markets:
            condition_id = market.get("conditionId")
            if not condition_id:
                continue

            if condition_ids and condition_id not in condition_ids:
                continue

            normalized_market = normalize_gamma_market_to_clob_format(market)

            for token_info in normalized_market.get("tokens", []):
                token_id = token_info["token_id"]

                # Only load if this specific token was requested
                if requested_token_ids and token_id not in requested_token_ids:
                    continue

                outcome = token_info["outcome"]
                self._load_instrument(normalized_market, token_id, outcome)

    async def _load_markets_using_gamma(self, filters: dict | None = None) -> None:
        """
        Load a bounded active-market sports corpus using Gamma API discovery.
        """
        filters = filters.copy() if filters is not None else {}
        sports_filter = {
            _canonical_polymarket_sport(str(value))
            for value in filters.pop("sports", ()) or ()
            if value
        }
        max_results = filters.pop("max_results", None)
        max_results = int(max_results) if max_results is not None else None
        horizon_hours = filters.pop("max_resolution_horizon_hours", None)
        horizon = timedelta(hours=float(horizon_hours)) if horizon_hours not in (None, "") else None
        now = datetime.fromtimestamp(self._clock.timestamp_ns() / 1_000_000_000, tz=UTC)

        self._log.info(
            "Loading Polymarket instruments using Gamma discovery "
            f"filters={filters} sports={sorted(sports_filter)} max_results={max_results} "
            f"max_resolution_horizon_hours={horizon_hours}",
        )
        loaded_condition_ids: set[str] = set()
        loaded_markets = await self._load_filtered_sports_event_markets(
            sports_filter=sports_filter,
            max_results=max_results,
            loaded_condition_ids=loaded_condition_ids,
            now=now,
            horizon=horizon,
        )

        remaining_results = None
        if max_results is not None:
            remaining_results = max(max_results - loaded_markets, 0)
        loaded_markets += await self._load_filtered_sport_tag_gamma_markets(
            filters=filters,
            sports_filter=sports_filter,
            max_results=remaining_results if remaining_results is not None else max_results,
            loaded_condition_ids=loaded_condition_ids,
            now=now,
            horizon=horizon,
        )
        if max_results is not None:
            remaining_results = max(max_results - loaded_markets, 0)
        loaded_markets += await self._load_filtered_gamma_markets(
            filters=filters,
            sports_filter=sports_filter,
            max_results=remaining_results if remaining_results is not None else max_results,
            loaded_condition_ids=loaded_condition_ids,
            now=now,
            horizon=horizon,
        )
        self._log.info(f"Loaded Polymarket sports markets using Gamma API: {loaded_markets}")

    async def _load_filtered_sports_event_markets(
        self,
        *,
        sports_filter: set[str],
        max_results: int | None,
        loaded_condition_ids: set[str],
        now: datetime,
        horizon: timedelta | None,
    ) -> int:
        if not sports_filter:
            return 0
        event_markets = await self._load_sports_event_markets_using_gamma(
            sports_filter=sports_filter,
            max_results=max_results,
            now=now,
            horizon=horizon,
        )
        self._log.info(
            "Loaded "
            f"{len(event_markets)} candidate Polymarket sports event markets using Gamma API",
        )
        loaded_markets = 0
        for market in event_markets:
            condition_id = str(market.get("conditionId") or "")
            if not condition_id or condition_id in loaded_condition_ids:
                continue
            loaded = self._load_gamma_market_instruments(market)
            if loaded:
                loaded_markets += loaded
                loaded_condition_ids.add(condition_id)
            if max_results is not None and loaded_markets >= max_results:
                break
        return loaded_markets

    async def _load_filtered_gamma_markets(
        self,
        *,
        filters: dict[str, Any],
        sports_filter: set[str],
        max_results: int | None,
        loaded_condition_ids: set[str],
        now: datetime,
        horizon: timedelta | None,
    ) -> int:
        if max_results == 0:
            markets: list[dict[str, Any]] = []
        else:
            markets = await list_markets(
                http_client=self._http_client,
                filters=_with_horizon_date_filters(filters, now=now, horizon=horizon),
                max_results=_horizon_fetch_limit(max_results, horizon),
            )
            markets = _rank_runtime_horizon_markets(markets, now=now, horizon=horizon)
        self._log.info(f"Loaded {len(markets)} candidate Polymarket markets using Gamma API")
        loaded_markets = 0
        for market in markets:
            condition_id = str(market.get("conditionId") or "")
            if condition_id and condition_id in loaded_condition_ids:
                continue
            if not _market_matches_sports_filter(market, sports_filter):
                continue
            loaded = self._load_gamma_market_instruments(market)
            if loaded:
                loaded_markets += loaded
                if condition_id:
                    loaded_condition_ids.add(condition_id)
            if max_results is not None and loaded_markets >= max_results:
                break
        return loaded_markets

    async def _load_filtered_sport_tag_gamma_markets(
        self,
        *,
        filters: dict[str, Any],
        sports_filter: set[str],
        max_results: int | None,
        loaded_condition_ids: set[str],
        now: datetime,
        horizon: timedelta | None,
    ) -> int:
        if not sports_filter or max_results == 0:
            return 0

        tag_markets = await self._load_sport_tag_markets_using_gamma(
            filters=filters,
            sports_filter=sports_filter,
            max_results=max_results,
            now=now,
            horizon=horizon,
        )
        self._log.info(
            f"Loaded {len(tag_markets)} candidate Polymarket sport-tag markets using Gamma API",
        )
        loaded_markets = 0
        for market in tag_markets:
            condition_id = str(market.get("conditionId") or "")
            if not condition_id or condition_id in loaded_condition_ids:
                continue
            if not _market_matches_sports_filter(market, sports_filter):
                continue
            loaded = self._load_gamma_market_instruments(market)
            if loaded:
                loaded_markets += loaded
                loaded_condition_ids.add(condition_id)
            if max_results is not None and loaded_markets >= max_results:
                break
        return loaded_markets

    def _load_gamma_market_instruments(self, market: dict[str, Any]) -> int:
        condition_id = market.get("conditionId")
        if not condition_id:
            return 0

        normalized_market = normalize_gamma_market_to_clob_format(market)
        loaded_tokens = 0
        for token_info in normalized_market.get("tokens", []):
            token_id = token_info.get("token_id")
            if not token_id:
                self._log.warning(f"Market {condition_id} had an empty token")
                continue

            outcome = token_info["outcome"]
            self._load_instrument(normalized_market, token_id, outcome)
            loaded_tokens += 1
        return int(loaded_tokens > 0)

    async def _load_sports_event_markets_using_gamma(
        self,
        *,
        sports_filter: set[str],
        max_results: int | None,
        now: datetime | None = None,
        horizon: timedelta | None = None,
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(tz=UTC)
        sports_metadata = await self._gamma_get_json("/sports")
        if not isinstance(sports_metadata, list):
            return []

        tag_groups = _selected_sports_tag_groups(sports_metadata, sports_filter)
        if not tag_groups:
            return []

        per_sport_limit = _balanced_sport_limit(max_results, len(tag_groups))
        discovered_markets: dict[str, dict[str, Any]] = {}
        overflow_markets: dict[str, dict[str, Any]] = {}
        for canonical_sport, tag_group in tag_groups.items():
            sport_markets = await self._discover_sport_event_markets(
                canonical_sport=canonical_sport,
                sport_codes=list(tag_group["sport_codes"]),
                selected_tags=list(tag_group["tag_ids"]),
                per_sport_limit=per_sport_limit,
                max_results=max_results,
                now=now,
                horizon=horizon,
            )
            _add_balanced_sport_markets(
                discovered_markets=discovered_markets,
                overflow_markets=overflow_markets,
                sport_markets=sport_markets,
                sport_quota=per_sport_limit or len(sport_markets),
                now=now,
                horizon=horizon,
            )
        if max_results is not None:
            for market_id, market in overflow_markets.items():
                if len(discovered_markets) >= max_results:
                    break
                discovered_markets.setdefault(market_id, market)
        return list(discovered_markets.values())[:max_results]

    async def _discover_sport_event_markets(
        self,
        *,
        canonical_sport: str,
        sport_codes: list[str],
        selected_tags: list[str],
        per_sport_limit: int | None,
        max_results: int | None,
        now: datetime,
        horizon: timedelta | None,
    ) -> dict[str, dict[str, Any]]:
        sport_markets: dict[str, dict[str, Any]] = {}
        sport_code = sport_codes[0] if sport_codes else canonical_sport
        for tag_id in selected_tags:
            for order in ("volume24hr", "volume"):
                events = await self._gamma_get_json(
                    "/events",
                    params=_with_horizon_date_filters(
                        {
                            "tag_id": tag_id,
                            "related_tags": "true",
                            "active": "true",
                            "closed": "false",
                            "archived": "false",
                            "limit": _horizon_fetch_limit(
                                per_sport_limit or max_results or 100,
                                horizon,
                            ),
                            "order": order,
                            "ascending": "false",
                        },
                        now=now,
                        horizon=horizon,
                    ),
                )
                if not isinstance(events, list):
                    continue
                _collect_sports_event_markets(
                    canonical_sport=canonical_sport,
                    sport_code=sport_code,
                    selected_tags=selected_tags,
                    events=events,
                    discovered_markets=sport_markets,
                    max_markets_per_event=POLYMARKET_DEFAULT_MAX_MARKETS_PER_EVENT,
                )
        return sport_markets

    async def _load_sport_tag_markets_using_gamma(
        self,
        *,
        filters: dict[str, Any],
        sports_filter: set[str],
        max_results: int | None,
        now: datetime | None = None,
        horizon: timedelta | None = None,
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(tz=UTC)
        sports_metadata = await self._gamma_get_json("/sports")
        if not isinstance(sports_metadata, list):
            return []

        tag_groups = _selected_sports_tag_groups(sports_metadata, sports_filter)
        if not tag_groups:
            return []

        per_sport_limit = _balanced_sport_limit(max_results, len(tag_groups))
        discovered_markets: dict[str, dict[str, Any]] = {}
        overflow_markets: dict[str, dict[str, Any]] = {}
        for canonical_sport, tag_group in tag_groups.items():
            sport_markets = await self._discover_sport_tag_markets(
                filters=filters,
                canonical_sport=canonical_sport,
                sport_codes=list(tag_group["sport_codes"]),
                selected_tags=list(tag_group["tag_ids"]),
                per_sport_limit=per_sport_limit,
                max_results=max_results,
                now=now,
                horizon=horizon,
            )
            _add_balanced_sport_markets(
                discovered_markets=discovered_markets,
                overflow_markets=overflow_markets,
                sport_markets=sport_markets,
                sport_quota=per_sport_limit or len(sport_markets),
                now=now,
                horizon=horizon,
            )
        if max_results is not None:
            for market_id, market in overflow_markets.items():
                if len(discovered_markets) >= max_results:
                    break
                discovered_markets.setdefault(market_id, market)
        return list(discovered_markets.values())[:max_results]

    async def _discover_sport_tag_markets(
        self,
        *,
        filters: dict[str, Any],
        canonical_sport: str,
        sport_codes: list[str],
        selected_tags: list[str],
        per_sport_limit: int | None,
        max_results: int | None,
        now: datetime,
        horizon: timedelta | None,
    ) -> dict[str, dict[str, Any]]:
        sport_markets: dict[str, dict[str, Any]] = {}
        sport_code = sport_codes[0] if sport_codes else canonical_sport
        for tag_id in selected_tags:
            tag_filters = {
                **filters,
                "tag_id": tag_id,
                "related_tags": "true",
                "active": "true",
                "closed": "false",
                "archived": "false",
                "order": "volume24hr",
                "ascending": "false",
            }
            markets = await list_markets(
                http_client=self._http_client,
                filters=_with_horizon_date_filters(
                    tag_filters,
                    now=now,
                    horizon=horizon,
                ),
                max_results=_horizon_fetch_limit(per_sport_limit or max_results, horizon),
            )
            for market in markets:
                market_id = _market_unique_id(market)
                if not market_id:
                    continue
                enriched_market = _enrich_sport_tag_market(
                    market,
                    canonical_sport=canonical_sport,
                    sport_code=sport_code,
                    selected_tags=selected_tags,
                )
                if not _market_has_event_metadata(enriched_market):
                    continue
                if not _market_matches_sports_filter(enriched_market, {canonical_sport}):
                    continue
                sport_markets.setdefault(market_id, enriched_market)
        return sport_markets

    async def _gamma_get_json(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError(f"Invalid Polymarket Gamma endpoint: {endpoint}")
        response = await self._http_client.get(
            f"https://gamma-api.polymarket.com{endpoint}",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "cloudbet-market-maker/polymarket-gamma",
            },
            timeout_secs=30,
        )
        if response.status != 200:
            body = response.body.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gamma API request failed: {response.status} for endpoint {endpoint} body={body}",
            )
        return self._decoder.decode(response.body)

    async def _load_ids_using_clob_api(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """
        Load instruments using CLOB API.
        """
        if len(instrument_ids) > 200:
            self._log.warning(
                f"Loading {len(instrument_ids)} instruments, using bulk load of all markets as a faster alternative",
            )
            await self._load_markets(instrument_ids, filters)
        else:
            await self._load_markets_seq(instrument_ids, filters)

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        if not instrument_ids:
            self._log.info("No instrument IDs given for loading")
            return

        # Check all instrument IDs
        for instrument_id in instrument_ids:
            PyCondition.equal(
                instrument_id.venue,
                POLYMARKET_VENUE,
                "instrument_id.venue",
                "POLYMARKET",
            )

        if self._config.use_gamma_markets:
            await self._load_ids_using_gamma_markets(instrument_ids, filters)
        else:
            await self._load_ids_using_clob_api(instrument_ids, filters)

    async def load_async(self, instrument_id: InstrumentId, filters: dict | None = None) -> None:
        PyCondition.not_none(instrument_id, "instrument_id")
        condition_id = get_polymarket_condition_id(instrument_id)
        token_id = get_polymarket_token_id(instrument_id)

        response = await asyncio.to_thread(self._client.get_market, condition_id)
        response = _check_clob_response(response)

        for token_info in response["tokens"]:
            if token_id != token_info["token_id"]:
                continue

            outcome = token_info["outcome"]

            try:
                self._load_instrument(response, token_id, outcome)
            except ValueError as e:
                self._log.error(f"Unable to parse market: {e}, {response}")

    async def _load_markets_seq(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        filter_is_active = filters.get("is_active", False) if filters else False

        for instrument_id in instrument_ids:
            response: dict[str, Any] | str = await asyncio.to_thread(
                self._client.get_market,
                condition_id=get_polymarket_condition_id(instrument_id),
            )
            response = _check_clob_response(response)

            try:
                active = response["active"]
                closed = response["closed"]

                if filter_is_active and (not active or closed):
                    continue

                condition_id = response["condition_id"]
                if not condition_id:
                    self._log.warning(f"{instrument_id} was archived (no `condition_id`)")
                    continue  # Archived

                for token_info in response["tokens"]:
                    token_id = token_info["token_id"]
                    if not token_id:
                        self._log.warning(f"Market {condition_id} had an empty token")
                        continue
                    outcome = token_info["outcome"]
                    self._load_instrument(response, token_id, outcome)
                    self._log.info(f"Loaded instrument {instrument_id}")
            except ValueError as e:
                self._log.error(f"Unable to parse market: {e}, {response}")

    async def _load_markets(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        # Create a copy to avoid mutating the caller's filters
        filters = filters.copy() if filters is not None else {}

        if instrument_ids:
            instruments_str = "instruments: " + ", ".join([str(x) for x in instrument_ids])
        else:
            instruments_str = "all instruments"
        filters_str = "..." if not filters else f" with filters {filters}..."
        self._log.info(f"Loading {instruments_str}{filters_str}")

        condition_ids = [get_polymarket_condition_id(x) for x in instrument_ids]

        filter_is_active = filters.get("is_active", False)

        markets_visited = 0
        next_cursor = filters.get("next_cursor", "MA==")
        while next_cursor != "LTE=":
            self._log.info(f"Cursor = '{next_cursor}', markets visited = {markets_visited}")
            response: dict[str, Any] | str = await asyncio.to_thread(
                self._client.get_markets,
                next_cursor=next_cursor,
            )
            response = _check_clob_response(response)

            for market_info in response["data"]:
                try:
                    active = market_info["active"]
                    closed = market_info["closed"]

                    if filter_is_active and (not active or closed):
                        continue

                    condition_id = market_info["condition_id"]
                    if not condition_id:
                        continue  # Archived

                    if condition_ids and condition_id not in condition_ids:
                        continue  # Filtering

                    for token_info in market_info["tokens"]:
                        token_id = token_info["token_id"]
                        if not token_id:
                            self._log.warning(f"Market {condition_id} had an empty token")
                            continue

                        outcome = token_info["outcome"]
                        self._load_instrument(market_info, token_id, outcome)
                except ValueError as e:
                    self._log.error(f"Unable to parse market: {e}, {market_info}")
                    continue
            next_cursor = response["next_cursor"]
            markets_visited += len(response["data"])

    def _load_instrument(
        self,
        market_info: dict[str, Any],
        token_id: str,
        outcome: str,
    ) -> BinaryOption:
        token_market_info = dict(market_info)
        token_market_info["selected_token_id"] = token_id
        token_market_info["selected_outcome"] = outcome
        for token in market_info.get("tokens", []):
            if not isinstance(token, dict):
                continue
            if str(token.get("token_id") or "") != str(token_id):
                continue
            if token.get("price") is not None:
                token_market_info["selected_token_price"] = token.get("price")
            break

        instrument = parse_polymarket_instrument(
            market_info=token_market_info,
            token_id=token_id,
            outcome=outcome,
            ts_init=self._clock.timestamp_ns(),
        )
        if market_info["end_date_iso"] is None:
            self._log.warning(f"{instrument.id} expiration is missing, assuming it is still active")

        self.add(instrument)
        return instrument
