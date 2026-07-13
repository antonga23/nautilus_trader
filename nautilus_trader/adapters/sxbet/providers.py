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
from datetime import timedelta
import hashlib
from math import ceil
import re
import time
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
from nautilus_trader.adapters.sxbet.signing import from_wei
from nautilus_trader.adapters.sxbet.signing import taker_decimal_odds_from_maker_percentage
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
    202: MarketType.MATCH_ODDS.value,
    203: MarketType.MATCH_ODDS.value,
    204: MarketType.MATCH_ODDS.value,
    201: MarketType.ASIAN_HANDICAP.value,
    226: MarketType.MATCH_ODDS.value,
    342: MarketType.ASIAN_HANDICAP.value,
    835: MarketType.TOTAL_GOALS.value,
}
# Sports whose full-time result market is genuinely three-way (a draw can win).
# Their `match_odds` is 1X2, not a two-way money line: SX.bet lists it as a set of
# binary "Team / Not Team" and "Tie / Not tie" markets, so flagging it two-way would
# drop the draw and let the home and away legs be mistaken for a complementary pair.
SXBET_DRAW_CAPABLE_SPORTS = frozenset(
    {
        "soccer",
        "cricket",
        "rugby",
        "rugby_league",
        "australian_rules",
    },
)


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
        self._sport_names_by_id: dict[int, str] = dict(SXBET_SPORT_IDS)
        if logger is not None:
            self._log = logger

    async def load_all_async(self, filters: dict | None = None) -> None:
        """
        Load all instruments from the venue.
        """
        started_at = time.perf_counter()

        filters = filters or {}
        sport_ids = filters.get("sport_ids") or self._sxbet_config.sport_ids
        league_ids = filters.get("league_ids") or self._sxbet_config.league_ids
        instrument_limit = self._sxbet_config.instrument_load_limit
        target_market_count = self._target_market_count(instrument_limit)
        discovery_limit = self._sxbet_config.market_discovery_limit
        if discovery_limit is None and not self._sxbet_config.prefer_liquid_markets:
            discovery_limit = target_market_count

        self._log.info(
            "Loading all SX.bet instruments "
            f"sport_ids={sorted(sport_ids) if sport_ids else 'all'} "
            f"league_ids={sorted(league_ids) if league_ids else 'all'} "
            f"instrument_limit={instrument_limit or 'none'} "
            f"market_discovery_limit={discovery_limit or 'none'} "
            f"prefer_liquid_markets={self._sxbet_config.prefer_liquid_markets} "
            f"liquidity_probe_limit={self._sxbet_config.liquidity_probe_limit} "
            f"min_two_sided_markets={self._sxbet_config.min_two_sided_markets}",
        )
        await self._refresh_sport_names()

        load_started_at = time.perf_counter()
        markets = await self._load_markets(
            sport_ids=sport_ids,
            league_ids=league_ids,
            market_discovery_limit=discovery_limit,
        )
        self._log.info(
            f"SX.bet market discovery completed: markets={len(markets)} "
            f"elapsed={time.perf_counter() - load_started_at:.2f}s",
        )

        selection_started_at = time.perf_counter()
        markets = await self._select_markets_for_processing(markets, target_market_count)
        self._log.info(
            f"SX.bet market selection completed: selected_markets={len(markets)} "
            f"elapsed={time.perf_counter() - selection_started_at:.2f}s",
        )

        hydrate_started_at = time.perf_counter()
        await self._hydrate_best_odds(markets)
        self._log.info(
            f"SX.bet best-odds hydration completed: markets={len(markets)} "
            f"elapsed={time.perf_counter() - hydrate_started_at:.2f}s",
        )

        process_started_at = time.perf_counter()
        processed_markets = 0
        for market in markets:
            if instrument_limit is not None and len(self._instruments) >= instrument_limit:
                break
            await self._process_market(market)
            processed_markets += 1

        self._log.info(
            f"Loaded {len(self._instruments)} instruments from {processed_markets} SX.bet markets "
            f"elapsed={time.perf_counter() - process_started_at:.2f}s "
            f"total_elapsed={time.perf_counter() - started_at:.2f}s",
        )

    @staticmethod
    def _target_market_count(instrument_limit: int | None) -> int | None:
        if instrument_limit is None:
            return None
        return max(1, (int(instrument_limit) + 1) // 2)

    async def _load_markets(
        self,
        sport_ids: frozenset[int] | set[int] | None,
        league_ids: frozenset[int] | set[int] | None,
        market_discovery_limit: int | None,
    ) -> list[dict[str, Any]]:
        markets_by_hash: dict[str, dict[str, Any]] = {}
        sport_filters: tuple[int | None, ...] = tuple(sorted(sport_ids)) if sport_ids else (None,)
        league_filters: tuple[int | None, ...] = (
            tuple(sorted(league_ids)) if league_ids else (None,)
        )
        per_sport_limit = self._per_sport_discovery_limit(
            sport_filters,
            market_discovery_limit,
        )

        for sport_id in sport_filters:
            for league_id in league_filters:
                pagination_key: str | None = None
                page_count = 0
                sport_market_count = 0
                while True:
                    page_started_at = time.perf_counter()
                    markets_data = await self._http_client.get_markets(
                        sport_id=sport_id,
                        league_id=league_id,
                        only_active=True,
                        pagination_key=pagination_key,
                        page_size=SXBET_MARKET_PAGE_SIZE,
                    )
                    page_count += 1
                    previous_count = len(markets_by_hash)
                    added_market_hashes = self._merge_markets(markets_by_hash, markets_data)
                    sport_market_count += len(
                        [
                            market_hash
                            for market_hash in added_market_hashes
                            if self._market_sport_key(markets_by_hash[market_hash]) == sport_id
                        ],
                    )
                    pagination_key = markets_data.get("data", {}).get("nextKey")
                    self._log.info(
                        "SX.bet market page loaded: "
                        f"sport_id={sport_id or 'all'} league_id={league_id or 'all'} "
                        f"page={page_count} added={len(markets_by_hash) - previous_count} "
                        f"total={len(markets_by_hash)} has_next={bool(pagination_key)} "
                        f"elapsed={time.perf_counter() - page_started_at:.2f}s",
                    )
                    if (
                        market_discovery_limit is not None
                        and per_sport_limit is None
                        and len(markets_by_hash) >= market_discovery_limit
                    ):
                        self._log.info(
                            "SX.bet market discovery cap reached: "
                            f"market_discovery_limit={market_discovery_limit}",
                        )
                        return list(markets_by_hash.values())[:market_discovery_limit]
                    if per_sport_limit is not None and sport_market_count >= per_sport_limit:
                        self._log.info(
                            "SX.bet per-sport market discovery cap reached: "
                            f"sport_id={sport_id or 'all'} "
                            f"per_sport_limit={per_sport_limit} "
                            f"market_discovery_limit={market_discovery_limit}",
                        )
                        break
                    if not pagination_key:
                        break

        markets = list(markets_by_hash.values())
        if per_sport_limit is not None:
            markets = self._balanced_market_sequence(
                markets,
                sport_order=sport_filters,
                limit=market_discovery_limit,
            )
            self._log.info(
                "SX.bet balanced multi-sport market discovery completed: "
                f"sports={len(sport_filters)} markets={len(markets)} "
                f"market_discovery_limit={market_discovery_limit}",
            )
        elif market_discovery_limit is not None:
            markets = markets[:market_discovery_limit]
        return markets

    @staticmethod
    def _per_sport_discovery_limit(
        sport_filters: tuple[int | None, ...],
        market_discovery_limit: int | None,
    ) -> int | None:
        if market_discovery_limit is None:
            return None
        concrete_sports = [sport_id for sport_id in sport_filters if sport_id is not None]
        if len(concrete_sports) <= 1:
            return None
        return max(1, ceil(int(market_discovery_limit) / len(concrete_sports)))

    def _balanced_market_sequence(
        self,
        markets: list[dict[str, Any]],
        *,
        sport_order: tuple[int | None, ...],
        limit: int | None,
    ) -> list[dict[str, Any]]:
        grouped: dict[int | None, list[tuple[tuple[int, float, int], dict[str, Any]]]] = {}
        for index, market in enumerate(markets):
            sport_id = self._market_sport_key(market)
            grouped.setdefault(sport_id, []).append(
                (
                    (
                        self._market_resolution_priority(market),
                        self._market_start_timestamp(market) or float("inf"),
                        index,
                    ),
                    market,
                ),
            )
        for bucket in grouped.values():
            bucket.sort(key=lambda item: item[0])

        ordered_sports = [sport_id for sport_id in sport_order if sport_id in grouped]
        ordered_sports.extend(
            sport_id
            for sport_id in sorted(grouped, key=lambda value: value or -1)
            if sport_id not in ordered_sports
        )
        offsets = dict.fromkeys(ordered_sports, 0)
        selected: list[dict[str, Any]] = []
        while True:
            added = False
            for sport_id in ordered_sports:
                offset = offsets[sport_id]
                bucket = grouped.get(sport_id, [])
                if offset >= len(bucket):
                    continue
                selected.append(bucket[offset][1])
                offsets[sport_id] = offset + 1
                added = True
                if limit is not None and len(selected) >= limit:
                    return selected
            if not added:
                return selected

    @classmethod
    def _market_sport_key(cls, market: dict[str, Any]) -> int | None:
        return cls._parse_sport_id(market.get("sportId"))

    def _market_resolution_priority(self, market: dict[str, Any]) -> int:
        horizon_hours = self._sxbet_config.max_resolution_horizon_hours
        if horizon_hours is None:
            return 0
        start_time = self._market_start_datetime(market)
        if start_time is None:
            return 1
        now = datetime.now(UTC)
        if start_time <= now:
            return 0
        if start_time <= now + timedelta(hours=float(horizon_hours)):
            return 0
        return 2

    @classmethod
    def _market_start_timestamp(cls, market: dict[str, Any]) -> float | None:
        start_time = cls._market_start_datetime(market)
        return start_time.timestamp() if start_time is not None else None

    @classmethod
    def _market_start_datetime(cls, market: dict[str, Any]) -> datetime | None:
        normalized = cls._parse_start_time(market.get("gameTime"))
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    async def _select_markets_for_processing(
        self,
        markets: list[dict[str, Any]],
        target_market_count: int | None,
    ) -> list[dict[str, Any]]:
        if target_market_count is None:
            return markets

        target_market_count = max(1, target_market_count)
        if not self._sxbet_config.prefer_liquid_markets:
            selected = markets[:target_market_count]
            self._log.info(
                f"SX.bet selected first {len(selected)} markets without liquidity probing",
            )
            return selected

        probed = 0
        two_sided: list[dict[str, Any]] = []
        one_sided: list[dict[str, Any]] = []
        fallback: list[dict[str, Any]] = []
        empty = 0
        probe_limit = min(len(markets), self._sxbet_config.liquidity_probe_limit)
        for market in markets[:probe_limit]:
            if len(two_sided) >= target_market_count:
                break
            market_hash = market.get("marketHash")
            if not isinstance(market_hash, str) or not market_hash:
                fallback.append(market)
                continue

            probed += 1
            try:
                order_book = await self._http_client.get_order_book(market_hash)
            except SXBetHttpClientError as e:
                self._log.warning(f"Failed to probe SX.bet order book for {market_hash}: {e}")
                fallback.append(market)
                continue

            orders = order_book.get("data", {}).get("orders", [])
            has_outcome_one, has_outcome_two = self._market_order_sides(orders)
            if has_outcome_one and has_outcome_two:
                market["orders"] = orders
                market["_liquidity_depth"] = self._market_two_sided_depth(orders)
                two_sided.append(market)
            elif has_outcome_one or has_outcome_two:
                market["orders"] = orders
                one_sided.append(market)
            else:
                empty += 1
                fallback.append(market)

        depth_ranked = len(two_sided)
        two_sided = self._rank_two_sided_by_depth(two_sided)
        depth_ranked -= len(two_sided)

        unprobed = markets[probe_limit:]
        selected = (two_sided + one_sided + fallback + unprobed)[:target_market_count]
        self._log.info(
            "SX.bet liquidity probe completed: "
            f"probed={probed} two_sided_markets={len(two_sided)} "
            f"one_sided_markets={len(one_sided)} empty_markets={empty} "
            f"depth_filtered={depth_ranked} "
            f"selected_markets={len(selected)} target_markets={target_market_count}",
        )
        if len(two_sided) < self._sxbet_config.min_two_sided_markets:
            self._log.warning(
                "SX.bet liquid market target not met: "
                f"two_sided_markets={len(two_sided)} "
                f"min_two_sided_markets={self._sxbet_config.min_two_sided_markets}",
            )
        return selected

    @staticmethod
    def _market_order_sides(orders: list[dict]) -> tuple[bool, bool]:
        has_outcome_one = False
        has_outcome_two = False
        for order in orders:
            try:
                percentage = int(order.get("percentageOdds", 0))
            except (TypeError, ValueError):
                continue
            if percentage <= 0:
                continue
            if order.get("isMakerBettingOutcomeOne") is True:
                has_outcome_one = True
            elif order.get("isMakerBettingOutcomeOne") is False:
                has_outcome_two = True
        return has_outcome_one, has_outcome_two

    @staticmethod
    def _market_two_sided_depth(orders: list[dict]) -> float:
        # Real fillable depth, mirroring the quote path (data.py ``_best_bid_ask``): a
        # taker backing an outcome matches makers resting on the *opposite* outcome, so
        # the depth backing each outcome is the summed ``totalBetSize`` of the opposite
        # side. A market is only as deep as its thinner side, so the two-sided depth is
        # the smaller of the two outcome depths -- the size an arb can actually lock on
        # both legs.
        depth_backing_outcome_one = 0.0
        depth_backing_outcome_two = 0.0
        for order in orders:
            try:
                percentage = int(order.get("percentageOdds", 0) or 0)
            except (TypeError, ValueError):
                continue
            if percentage <= 0:
                continue
            size = from_wei(int(order.get("totalBetSize", 0) or 0))
            if order.get("isMakerBettingOutcomeOne") is False:
                depth_backing_outcome_one += size
            elif order.get("isMakerBettingOutcomeOne") is True:
                depth_backing_outcome_two += size
        return min(depth_backing_outcome_one, depth_backing_outcome_two)

    def _rank_two_sided_by_depth(
        self,
        two_sided: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        min_depth = self._sxbet_config.min_market_depth
        top_n = self._sxbet_config.top_markets_by_depth
        if min_depth is None and top_n is None:
            # No depth thresholds configured: leave the probe ordering untouched so
            # existing deployments select exactly the markets they did before.
            return two_sided

        if min_depth is not None:
            two_sided = [
                market
                for market in two_sided
                if float(market.get("_liquidity_depth", 0.0) or 0.0) >= float(min_depth)
            ]
        # Stable sort by descending depth: equal-depth markets keep their probe order,
        # so the near-horizon ordering upstream is preserved within a depth tier.
        two_sided = sorted(
            two_sided,
            key=lambda market: float(market.get("_liquidity_depth", 0.0) or 0.0),
            reverse=True,
        )
        if top_n is not None:
            two_sided = two_sided[: int(top_n)]
        return two_sided

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
                    log_api_error=False,
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
    ) -> list[str]:
        added_market_hashes: list[str] = []
        for market in markets_data.get("data", {}).get("markets", []):
            market_hash = market.get("marketHash")
            if isinstance(market_hash, str) and market_hash:
                if market_hash not in markets_by_hash:
                    added_market_hashes.append(market_hash)
                markets_by_hash[market_hash] = market
        return added_market_hashes

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

        sport_name = self._sport_name(sport_id)
        league_name = market.get("leagueLabel") or market.get("leagueName") or "Unknown"
        event_id, event_id_source = self._fixture_event_id(
            market,
            sport_name=sport_name,
            competition_name=league_name,
            team_one=team_one,
            team_two=team_two,
            start_time=start_time,
        )
        normalized_market_type = self._normalize_market_type(market)
        outcome_labels, proposition_subject = self._resolve_outcome_names(
            market,
            normalized_market_type,
        )
        market_params = self._market_params(
            market_type=normalized_market_type,
            raw_market_type=market_type,
            sport_name=sport_name,
            handicap=market.get("line"),
            outcome_labels=outcome_labels,
            proposition_subject=proposition_subject,
            outcome_one_label=str(market.get("outcomeOneName") or ""),
            outcome_two_label=str(market.get("outcomeTwoName") or ""),
        )

        outcome_one_odds = self._extract_best_odds(market, True)
        outcome_two_odds = self._extract_best_odds(market, False)
        liquidity_depth = market.get("_liquidity_depth")
        if liquidity_depth is None:
            orders = market.get("orders")
            liquidity_depth = (
                self._market_two_sided_depth(orders) if isinstance(orders, list) else 0.0
            )
        price_by_outcome = {
            True: outcome_one_odds if outcome_one_odds > 0 else SXBET_PLACEHOLDER_PRICE,
            False: outcome_two_odds if outcome_two_odds > 0 else SXBET_PLACEHOLDER_PRICE,
        }

        # Create instruments for both outcomes
        for outcome_one in (True, False):
            if (
                self._sxbet_config.instrument_load_limit is not None
                and len(self._instruments) >= self._sxbet_config.instrument_load_limit
            ):
                break
            instrument = self._create_instrument(
                market_hash=market_hash,
                event_id=event_id,
                event_id_source=event_id_source,
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
                liquidity_depth=float(liquidity_depth or 0.0),
            )

            if instrument:
                self._instruments[instrument.id] = instrument
                self.add(instrument)

    @staticmethod
    def _best_odds_taker_price(best_odds_entry: dict, outcome_one: bool) -> float:
        # ``bestOdds.outcomeX.percentageOdds`` is the best *maker* implied
        # probability resting on outcome X, so the two sides sum to < 1 by the maker
        # spread (e.g. the API's own 0.5775 / 0.34 example). A taker backing this
        # outcome matches the makers on the *opposite* outcome and receives the
        # complement of their implied odds -- exactly as the raw order path. Reading
        # the same-side field without the complement (``1 / maker_implied``) inflated
        # every hydrated two-sided market into a deep phantom overlay, pricing both
        # legs as longshots.
        opposite_key = "outcomeTwo" if outcome_one else "outcomeOne"
        payload = best_odds_entry.get(opposite_key)
        if not isinstance(payload, dict):
            return 0.0
        percentage = payload.get("percentageOdds")
        if percentage in (None, ""):
            return 0.0
        odds = taker_decimal_odds_from_maker_percentage(int(str(percentage)))
        return odds if odds > 1 else 0.0

    @staticmethod
    def _extract_best_odds(market: dict, outcome_one: bool) -> float:
        """
        Extract the best available taker odds for one outcome of a market.
        """
        best_odds_entry = market.get("bestOdds")
        if isinstance(best_odds_entry, dict):
            odds = SXBetInstrumentProvider._best_odds_taker_price(best_odds_entry, outcome_one)
            if odds > 1:
                return odds

        # Fall back to raw resting orders when no executable best-odds side exists.
        orders = market.get("orders", [])

        best_odds = 0.0
        for order in orders:
            if order.get("isMakerBettingOutcomeOne") != outcome_one:
                # This maker is betting the opposite outcome, so a taker backing
                # our outcome matches it and receives the complement of the maker's
                # odds (not the maker's odds directly).
                odds = taker_decimal_odds_from_maker_percentage(
                    int(order.get("percentageOdds", 0) or 0),
                )
                if odds > 1:
                    best_odds = max(best_odds, odds)

        # If no orders, use implied odds from market
        if best_odds <= 0:
            if outcome_one:
                implied = market.get("outcomeOneProbability", 0)
            else:
                implied = market.get("outcomeTwoProbability", 0)

            if implied and 0 < implied < 1:
                best_odds = 1 / implied

        return best_odds

    @classmethod
    def _fixture_event_id(
        cls,
        market: dict[str, Any],
        *,
        sport_name: str,
        competition_name: str,
        team_one: str,
        team_two: str,
        start_time: str | None,
    ) -> tuple[str, str]:
        for key in ("eventId", "eventID", "fixtureId", "fixtureID", "gameId", "gameID"):
            value = market.get(key)
            if value not in (None, ""):
                return str(value), key

        payload = "|".join(
            cls._fixture_id_component(value)
            for value in (sport_name, competition_name, team_one, team_two, start_time or "")
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return f"sxbet-{digest}", "derived_fixture_key"

    @staticmethod
    def _fixture_id_component(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")

    def _normalize_market_type(self, market: dict[str, Any]) -> str:
        raw_market_type = market.get("type")
        if isinstance(raw_market_type, int) and raw_market_type in SXBET_MARKET_TYPE_MAP:
            return SXBET_MARKET_TYPE_MAP[raw_market_type]

        outcome_one = self._normalize_label(market.get("outcomeOneName"))
        outcome_two = self._normalize_label(market.get("outcomeTwoName"))
        if self._scoped_winner_params(
            raw_market_type=raw_market_type,
            sport_name=self._sport_name(market.get("sportId")),
            outcome_one_label=str(market.get("outcomeOneName") or ""),
            outcome_two_label=str(market.get("outcomeTwoName") or ""),
        ):
            return MarketType.MATCH_ODDS.value
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
        raw_market_type: int | None,
        sport_name: str,
        handicap: float | None,
        outcome_labels: tuple[str, str],
        proposition_subject: str | None,
        outcome_one_label: str,
        outcome_two_label: str,
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
        period_params = cls._scoped_winner_params(
            raw_market_type=raw_market_type,
            sport_name=sport_name,
            outcome_one_label=outcome_one_label,
            outcome_two_label=outcome_two_label,
        )
        for key, value in period_params.items():
            parts.append(f"{key}={value}")
        return ",".join(parts)

    @classmethod
    def _scoped_winner_params(
        cls,
        *,
        raw_market_type: int | None,
        sport_name: str,
        outcome_one_label: str,
        outcome_two_label: str,
    ) -> dict[str, str]:
        sport = cls._canonical_sport_label(sport_name)
        combined = " ".join(part for part in (outcome_one_label, outcome_two_label) if part).lower()
        if not combined and raw_market_type not in {202, 203, 204}:
            return {}

        set_number = cls._extract_scoped_number(combined, "set")
        if sport == "tennis" and set_number is not None:
            return {"set": str(set_number), "period": f"set{set_number}"}

        if raw_market_type in {202, 203, 204}:
            ordinal = {202: 1, 203: 2, 204: 3}[raw_market_type]
        else:
            ordinal = None

        quarter_number = cls._extract_scoped_number(combined, "quarter")
        period_number = cls._extract_scoped_number(combined, "period") or ordinal
        inning_number = cls._extract_scoped_number(combined, "inning")

        if sport == "basketball":
            quarter = quarter_number or period_number
            if quarter is not None:
                return {"period": f"q{quarter}"}
        if sport in {"ice_hockey", "soccer"} and period_number is not None:
            return {"period": f"p{period_number}"}
        if sport == "baseball" and inning_number is not None:
            return {"inning": str(inning_number), "period": f"inning{inning_number}"}
        if period_number is not None:
            return {"period": f"p{period_number}"}
        return {}

    @staticmethod
    def _extract_scoped_number(text: str, scope_word: str) -> int | None:
        patterns = (
            rf"(\d+)(?:st|nd|rd|th)\s+{scope_word}\b",
            rf"{scope_word}\s+(\d+)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _normalize_label(value: Any) -> str:
        if value is None:
            return ""
        normalized = re.sub(r"\s+", " ", str(value).strip().lower())
        return normalized

    async def _refresh_sport_names(self) -> None:
        get_active_sports = getattr(self._http_client, "get_active_sports", None)
        if not callable(get_active_sports):
            return

        try:
            payload = await get_active_sports()
        except Exception as e:  # pragma: no cover - network/client failures are runtime-only
            self._log.warning(
                f"Failed to refresh SX.bet active sports; using fallback mapping: {type(e).__name__}",
            )
            return

        sports = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(sports, list):
            return

        refreshed = 0
        for sport in sports:
            if not isinstance(sport, dict):
                continue
            sport_id = self._parse_sport_id(sport.get("sportId") or sport.get("id"))
            if sport_id is None:
                continue
            label = sport.get("label") or sport.get("name") or sport.get("sport")
            canonical = self._canonical_sport_label(label)
            if canonical:
                self._sport_names_by_id[sport_id] = canonical
                refreshed += 1

        if refreshed:
            self._log.info(f"Refreshed SX.bet active sport labels: sports={refreshed}")

    def _sport_name(self, sport_id: Any) -> str:
        parsed = self._parse_sport_id(sport_id)
        if parsed is None:
            return "unknown"
        return self._sport_names_by_id.get(parsed, SXBET_SPORT_IDS.get(parsed, "unknown"))

    @staticmethod
    def _parse_sport_id(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _canonical_sport_label(cls, value: Any) -> str:
        normalized = cls._fixture_id_component(value)
        aliases = {
            "football": "soccer",
            "soccer": "soccer",
            "basketball": "basketball",
            "hockey": "ice_hockey",
            "ice_hockey": "ice_hockey",
            "baseball": "baseball",
            "tennis": "tennis",
            "mixed_martial_arts": "mma",
            "mma": "mma",
            "e_sports": "esports",
            "esports": "esports",
            "cricket": "cricket",
            "rugby_league": "rugby_league",
            "rugby": "rugby",
            "afl": "australian_rules",
            "australian_rules": "australian_rules",
        }
        return aliases.get(normalized, normalized or "unknown")

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
        if isinstance(value, int | float):
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

        if isinstance(value, int | float):
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
        event_id: str,
        event_id_source: str,
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
        liquidity_depth: float = 0.0,
    ) -> CryptoBettingInstrument | None:
        """
        Create a CryptoBettingInstrument from market data.
        """
        try:
            return CryptoBettingInstrument(
                venue=SXBET_VENUE,
                event_id=event_id,
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
                instrument_key=market_hash,
                info={
                    "outcome_one": is_outcome_one,
                    "raw_market_type": raw_market_type,
                    "is_two_way_market": (
                        market_type == MarketType.MATCH_ODDS.value
                        and self._canonical_sport_label(sport_name) not in SXBET_DRAW_CAPABLE_SPORTS
                    ),
                    "has_best_odds": has_best_odds,
                    "outcome_label": outcome_label,
                    "liquidity_depth": liquidity_depth,
                    "sxbet_market_hash": market_hash,
                    "sxbet_event_id_source": event_id_source,
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
        return [inst for inst in self._instruments.values() if inst.market_id == market_hash]

    def get_market_data(self, market_hash: str) -> dict | None:
        """
        Get cached market data for a market hash.
        """
        return self._market_cache.get(market_hash)
