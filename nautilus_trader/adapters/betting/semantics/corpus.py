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
Provider-backed corpus ingestion for semantic rule mining.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC
from datetime import datetime
from enum import Enum
import hashlib
import json
import logging
from typing import Any

from nautilus_trader.adapters.betting.semantics.normalization import MarketNormalizer
from nautilus_trader.adapters.betting.semantics.store import RuleStore
from nautilus_trader.adapters.betting.semantics.types import CorpusSnapshot
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics.types import RuleCorpusManifest
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_SPORT_IDS
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_payload(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8",
    )
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:24]}"


class SnapshotIngestor:
    """
    Pulls provider snapshots, normalizes selections, and persists corpus manifests.
    """

    def __init__(
        self,
        store: RuleStore,
        normalizer: MarketNormalizer | None = None,
    ) -> None:
        self._store = store
        self._normalizer = normalizer or MarketNormalizer()

    async def refresh_cloudbet(
        self,
        client: CloudbetClient,
        *,
        sports: list[str] | None = None,
        from_timestamp: int,
        to_timestamp: int,
        limit: int = 20,
        adaptive_window: bool = True,
        max_window_seconds: int = 7 * 24 * 60 * 60,
        sparse_history_window_seconds: int = 180 * 24 * 60 * 60,
        min_events_per_sport: int = 1,
        include_recent_past_on_sparse: bool = False,
        include_bets: bool = True,
        bet_page_size: int = 50,
        bet_max_pages: int = 5,
        bet_from_date: str | None = None,
        bet_to_date: str | None = None,
        settled_bets_only: bool = False,
        fetch_concurrency: int = 8,
    ) -> RuleCorpusManifest:
        fetched_at = _utc_now()
        sports_response = await client.get_sports()
        self._save_snapshot(
            provider="CLOUDBET",
            endpoint="/pub/v2/odds/sports",
            fetched_at=fetched_at,
            payload=sports_response,
        )

        selected_sports = self._resolve_cloudbet_sports(
            requested_sports=sports,
            available_sports=sports_response.sports,
        )
        source_refs: list[str] = []
        market_names: set[str] = set()
        selection_count = 0
        event_count = 0
        normalized_records: list[NormalizedSelectionRecord] = []
        coverage_report: dict[str, Any] = {
            "provider": "CLOUDBET",
            "from_timestamp": from_timestamp,
            "to_timestamp": to_timestamp,
            "adaptive_window": adaptive_window,
            "max_window_seconds": max_window_seconds,
            "min_events_per_sport": min_events_per_sport,
            "past_sparse_event_threshold": max(min_events_per_sport, 4),
            "sports": {},
        }

        semaphore = asyncio.Semaphore(max(1, fetch_concurrency))

        async def _bounded_refresh(sport_key: str) -> dict[str, Any]:
            async with semaphore:
                return await self._refresh_cloudbet_sport(
                    client,
                    sport_key,
                    fetched_at=fetched_at,
                    from_timestamp=from_timestamp,
                    to_timestamp=to_timestamp,
                    limit=limit,
                    adaptive_window=adaptive_window,
                    max_window_seconds=max_window_seconds,
                    sparse_history_window_seconds=sparse_history_window_seconds,
                    min_events_per_sport=min_events_per_sport,
                    include_recent_past_on_sparse=include_recent_past_on_sparse,
                )

        sport_results = await asyncio.gather(
            *(_bounded_refresh(sport_key) for sport_key in selected_sports),
            return_exceptions=True,
        )
        for sport_key, sport_result in zip(selected_sports, sport_results, strict=True):
            if isinstance(sport_result, BaseException):
                logger.warning(
                    "Cloudbet corpus refresh failed for sport %s: %r",
                    sport_key,
                    sport_result,
                )
                coverage_report["sports"][sport_key] = {
                    "event_count": 0,
                    "selection_count": 0,
                    "error": type(sport_result).__name__,
                }
                continue
            source_refs.extend(sport_result["source_refs"])
            market_names.update(sport_result["market_names"])
            selection_count += sport_result["selection_count"]
            event_count += sport_result["event_count"]
            normalized_records.extend(sport_result["normalized_records"])
            coverage_report["sports"][sport_key] = sport_result["coverage"]

        source_refs.append(
            self._save_snapshot(
                provider="CLOUDBET",
                endpoint="/semantic/coverage/cloudbet",
                fetched_at=fetched_at,
                payload=coverage_report,
            ),
        )

        if include_bets:
            offset = 0
            for _ in range(max(1, bet_max_pages)):
                try:
                    bets_response = await client.get_bets(
                        from_date=bet_from_date,
                        to_date=bet_to_date,
                        is_settled=True if settled_bets_only else None,
                        limit=bet_page_size,
                        offset=offset,
                    )
                except Exception:
                    break
                source_refs.append(
                    self._save_snapshot(
                        provider="CLOUDBET",
                        endpoint=(
                            f"/pub/v4/bets?offset={offset}&limit={bet_page_size}"
                            f"&isSettled={str(settled_bets_only).lower()}"
                        ),
                        fetched_at=fetched_at,
                        payload=bets_response,
                    ),
                )
                if not bets_response.has_next:
                    break
                offset += bet_page_size

        manifest = RuleCorpusManifest(
            manifest_id=_hash_payload(
                "manifest",
                {
                    "provider": "CLOUDBET",
                    "fetched_at": fetched_at,
                    "sports": selected_sports,
                    "market_names": sorted(market_names),
                },
            ),
            provider="CLOUDBET",
            fetched_at=fetched_at,
            endpoint_version="feed:v2,trading:v4",
            sport_count=len(selected_sports),
            event_count=event_count,
            selection_count=selection_count,
            market_taxonomy_hash=hashlib.sha256(
                json.dumps(sorted(market_names), separators=(",", ":")).encode("utf-8"),
            ).hexdigest()[:24],
            source_refs=tuple(source_refs),
        )
        self._persist_normalized_records(normalized_records, manifest.manifest_id)
        self._store.save_manifest(manifest)
        return manifest

    async def _refresh_cloudbet_sport(  # noqa: C901
        self,
        client: CloudbetClient,
        sport_key: str,
        *,
        fetched_at: str,
        from_timestamp: int,
        to_timestamp: int,
        limit: int,
        adaptive_window: bool,
        max_window_seconds: int,
        sparse_history_window_seconds: int,
        min_events_per_sport: int,
        include_recent_past_on_sparse: bool,
    ) -> dict[str, Any]:
        source_refs: list[str] = []
        market_names: set[str] = set()
        normalized_records: list[NormalizedSelectionRecord] = []

        with suppress(Exception):
            source_refs.append(
                self._save_snapshot(
                    provider="CLOUDBET",
                    endpoint=f"/pub/v2/odds/sports/{sport_key}",
                    fetched_at=fetched_at,
                    payload=await client.get_sport(sport_key),
                ),
            )

        events_response = None
        selections: list[Any] = []
        attempt_reports: list[dict[str, Any]] = []
        base_window_seconds = max(to_timestamp - from_timestamp, 24 * 60 * 60)
        window_seconds = min(base_window_seconds, max_window_seconds)
        while True:
            attempt_from = from_timestamp
            attempt_to = from_timestamp + window_seconds
            try:
                events_response = await client.get_events_for_sport(
                    sport_key=sport_key,
                    from_timestamp=attempt_from,
                    to_timestamp=attempt_to,
                    limit=limit,
                )
            except Exception as exc:
                attempt_reports.append(
                    {
                        "from": attempt_from,
                        "to": attempt_to,
                        "error": type(exc).__name__,
                    },
                )
                events_response = None
                break
            snapshot_id = self._save_snapshot(
                provider="CLOUDBET",
                endpoint=(
                    f"/pub/v2/odds/events?sport={sport_key}&from={attempt_from}&to={attempt_to}"
                ),
                fetched_at=fetched_at,
                payload=events_response,
            )
            source_refs.append(snapshot_id)
            selections = client.event_to_selection(events_response)
            seen_attempt_events = {selection.event_id for selection in selections}
            attempt_reports.append(
                {
                    "from": attempt_from,
                    "to": attempt_to,
                    "event_count": len(seen_attempt_events),
                    "selection_count": len(selections),
                },
            )
            if (
                not adaptive_window
                or len(seen_attempt_events) >= min_events_per_sport
                or window_seconds >= max_window_seconds
            ):
                break
            window_seconds = min(max_window_seconds, window_seconds * 2)

        sparse_event_threshold = max(min_events_per_sport, 4)
        if (
            include_recent_past_on_sparse
            and adaptive_window
            and len({selection.event_id for selection in selections}) < sparse_event_threshold
        ):
            attempt_from = from_timestamp - max_window_seconds
            attempt_to = from_timestamp
            try:
                past_response = await client.get_events_for_sport(
                    sport_key=sport_key,
                    from_timestamp=attempt_from,
                    to_timestamp=attempt_to,
                    limit=limit,
                )
                past_snapshot_id = self._save_snapshot(
                    provider="CLOUDBET",
                    endpoint=(
                        f"/pub/v2/odds/events?sport={sport_key}"
                        f"&from={attempt_from}&to={attempt_to}&direction=past"
                    ),
                    fetched_at=fetched_at,
                    payload=past_response,
                )
                source_refs.append(past_snapshot_id)
                past_selections = client.event_to_selection(past_response)
                current_event_count = len({selection.event_id for selection in selections})
                past_event_count = len({selection.event_id for selection in past_selections})
                if past_event_count > current_event_count or (
                    past_event_count == current_event_count
                    and len(past_selections) > len(selections)
                ):
                    events_response = past_response
                    selections = past_selections
                attempt_reports.append(
                    {
                        "from": attempt_from,
                        "to": attempt_to,
                        "direction": "past",
                        "event_count": len(
                            {selection.event_id for selection in past_selections},
                        ),
                        "selection_count": len(past_selections),
                    },
                )
            except Exception as exc:
                attempt_reports.append(
                    {
                        "from": attempt_from,
                        "to": attempt_to,
                        "direction": "past",
                        "error": type(exc).__name__,
                    },
                )

        if (
            include_recent_past_on_sparse
            and adaptive_window
            and sparse_history_window_seconds > max_window_seconds
            and len({selection.event_id for selection in selections}) < sparse_event_threshold
        ):
            attempt_from = from_timestamp - sparse_history_window_seconds
            attempt_to = from_timestamp
            historical_limit = max(limit, 200)
            try:
                historical_response = await client.get_events_for_sport(
                    sport_key=sport_key,
                    from_timestamp=attempt_from,
                    to_timestamp=attempt_to,
                    limit=historical_limit,
                )
                historical_snapshot_id = self._save_snapshot(
                    provider="CLOUDBET",
                    endpoint=(
                        f"/pub/v2/odds/events?sport={sport_key}"
                        f"&from={attempt_from}&to={attempt_to}&direction=historical"
                    ),
                    fetched_at=fetched_at,
                    payload=historical_response,
                )
                source_refs.append(historical_snapshot_id)
                historical_selections = client.event_to_selection(historical_response)
                current_event_count = len({selection.event_id for selection in selections})
                historical_event_count = len(
                    {selection.event_id for selection in historical_selections},
                )
                if historical_event_count > current_event_count or (
                    historical_event_count == current_event_count
                    and len(historical_selections) > len(selections)
                ):
                    events_response = historical_response
                    selections = historical_selections
                attempt_reports.append(
                    {
                        "from": attempt_from,
                        "to": attempt_to,
                        "direction": "historical",
                        "event_count": historical_event_count,
                        "selection_count": len(historical_selections),
                        "limit": historical_limit,
                    },
                )
            except Exception as exc:
                attempt_reports.append(
                    {
                        "from": attempt_from,
                        "to": attempt_to,
                        "direction": "historical",
                        "error": type(exc).__name__,
                        "limit": historical_limit,
                    },
                )

        if events_response is None:
            return {
                "source_refs": source_refs,
                "market_names": market_names,
                "selection_count": 0,
                "event_count": 0,
                "normalized_records": normalized_records,
                "coverage": {
                    "event_count": 0,
                    "selection_count": 0,
                    "attempts": attempt_reports,
                },
            }

        first_competition = next(iter(events_response.competitions), None)
        competition_payload = None
        if first_competition is not None:
            with suppress(Exception):
                competition_payload = await client.get_competition(first_competition.key)
                source_refs.append(
                    self._save_snapshot(
                        provider="CLOUDBET",
                        endpoint=f"/pub/v2/odds/competitions/{first_competition.key}",
                        fetched_at=fetched_at,
                        payload=competition_payload,
                    ),
                )
        if not selections and competition_payload:
            selections = self._cloudbet_competition_to_selections(competition_payload)
            attempt_reports.append(
                {
                    "source": "competition",
                    "event_count": len(
                        {
                            self._cloudbet_selection_field(selection, "event_id")
                            for selection in selections
                        },
                    ),
                    "selection_count": len(selections),
                },
            )

        seen_event_ids = {
            event_id
            for selection in selections
            if (event_id := self._cloudbet_selection_field(selection, "event_id")) is not None
        }
        market_names.update(
            market_name
            for selection in selections
            if (market_name := self._cloudbet_selection_field(selection, "market_name"))
        )
        coverage = {
            "event_count": len(seen_event_ids),
            "selection_count": len(selections),
            "attempts": attempt_reports,
            "sparse": len(seen_event_ids) < sparse_event_threshold,
            "sparse_event_threshold": sparse_event_threshold,
        }

        for selection in selections:
            normalized = self._normalizer.normalize(selection)
            normalized_records.append(
                NormalizedSelectionRecord(
                    record_id=self._normalized_record_id("CLOUDBET", normalized),
                    provider="CLOUDBET",
                    selection=normalized,
                    manifest_id=None,
                ),
            )

        first_event_id = next(iter(seen_event_ids), None)
        if first_event_id is not None:
            try:
                event_response = await client.get_event(first_event_id)
            except Exception:
                event_response = None
            if event_response is not None:
                event_snapshot_id = self._save_snapshot(
                    provider="CLOUDBET",
                    endpoint=f"/pub/v2/odds/events/{first_event_id}",
                    fetched_at=fetched_at,
                    payload=event_response,
                )
                source_refs.append(event_snapshot_id)
            first_line_selection = next(
                (
                    selection
                    for selection in selections
                    if self._cloudbet_selection_field(selection, "event_id") == first_event_id
                ),
                None,
            )
            market_url = (
                self._cloudbet_selection_field(first_line_selection, "market_url")
                if first_line_selection is not None
                else None
            )
            if market_url:
                with suppress(Exception):
                    source_refs.append(
                        self._save_snapshot(
                            provider="CLOUDBET",
                            endpoint="/pub/v2/odds/lines",
                            fetched_at=fetched_at,
                            payload=await client.get_line(
                                first_event_id,
                                market_url,
                            ),
                        ),
                    )

        return {
            "source_refs": source_refs,
            "market_names": market_names,
            "selection_count": len(selections),
            "event_count": len(seen_event_ids),
            "normalized_records": normalized_records,
            "coverage": coverage,
        }

    async def refresh_sxbet(
        self,
        client: SXBetHttpClient,
        *,
        sports: list[str] | None = None,
        sport_ids: list[int] | None = None,
        from_time: int | None = None,
        to_time: int | None = None,
        instrument_limit: int = 250,
        market_discovery_limit: int = 250,
        prefer_liquid_markets: bool = False,
        liquidity_probe_limit: int = 100,
        min_two_sided_markets: int = 1,
        live_only: bool = False,
    ) -> RuleCorpusManifest:
        fetched_at = _utc_now()
        active_sports = await client.get_active_sports()
        source_refs = [
            self._save_snapshot(
                provider="SXBET",
                endpoint="/sports/active",
                fetched_at=fetched_at,
                payload=active_sports,
            ),
        ]
        active_sport_names = {
            int(sport["sportId"]): str(sport.get("name") or sport.get("label") or "")
            for sport in active_sports.get("data", [])
            if isinstance(sport, dict) and sport.get("sportId") is not None
        }
        requested_sport_names = self._canonical_requested_sport_names(sports)
        selected_sport_ids = sorted({int(sport_id) for sport_id in (sport_ids or [])})
        if not selected_sport_ids:
            selected_sport_ids = self._resolve_sxbet_sport_ids(
                requested_sports=sports,
                active_sports=active_sport_names,
            )
        if not selected_sport_ids:
            selected_sport_ids = sorted(active_sport_names)

        market_names: set[str] = set()
        event_keys: set[str] = set()
        normalized_records: list[NormalizedSelectionRecord] = []
        record_ids_seen: set[str] = set()
        sport_selection_counts: dict[int, int] = {}
        sport_snapshot_errors: dict[int, str] = {}
        sport_provider_errors: dict[int, str] = {}
        instrument_budgets = self._distribute_budget(instrument_limit, selected_sport_ids)
        market_budgets = self._distribute_budget(market_discovery_limit, selected_sport_ids)

        for sport_id in selected_sport_ids:
            try:
                source_refs.append(
                    self._save_snapshot(
                        provider="SXBET",
                        endpoint=f"/leagues/active?sportId={sport_id}",
                        fetched_at=fetched_at,
                        payload=await client.get_active_leagues(sport_id=sport_id),
                    ),
                )
                source_refs.append(
                    self._save_snapshot(
                        provider="SXBET",
                        endpoint=f"/fixture/active?sportId={sport_id}",
                        fetched_at=fetched_at,
                        payload=await client.get_fixtures(
                            sport_id=sport_id,
                            from_time=from_time,
                            to_time=to_time,
                        ),
                    ),
                )
                source_refs.append(
                    self._save_snapshot(
                        provider="SXBET",
                        endpoint=f"/markets/active?sportIds={sport_id}",
                        fetched_at=fetched_at,
                        payload=await client.get_markets(
                            sport_id=sport_id,
                            page_size=min(50, market_budgets.get(sport_id, market_discovery_limit)),
                            live_only=live_only,
                        ),
                    ),
                )
            except Exception as exc:
                sport_snapshot_errors[sport_id] = type(exc).__name__

            provider = SXBetInstrumentProvider(
                http_client=client,
                config=SXBetInstrumentProviderConfig(
                    load_all=True,
                    sport_ids=frozenset({sport_id}),
                    instrument_load_limit=instrument_budgets.get(sport_id, instrument_limit),
                    market_discovery_limit=market_budgets.get(sport_id, market_discovery_limit),
                    prefer_liquid_markets=prefer_liquid_markets,
                    liquidity_probe_limit=liquidity_probe_limit,
                    min_two_sided_markets=min_two_sided_markets,
                    live_only=live_only,
                ),
            )
            try:
                await provider.load_all_async(
                    filters={
                        "sport_ids": frozenset({sport_id}),
                    },
                )
            except Exception as exc:
                sport_provider_errors[sport_id] = type(exc).__name__
                continue

            selection_count_before = len(normalized_records)
            for instrument in provider.list_all():
                normalized = self._normalizer.normalize(instrument)
                record_id = self._normalized_record_id("SXBET", normalized)
                if record_id in record_ids_seen:
                    continue
                record_ids_seen.add(record_id)
                market_names.add(normalized.raw_market_name or normalized.market_type)
                event_keys.add(normalized.event_key)
                normalized_records.append(
                    NormalizedSelectionRecord(
                        record_id=record_id,
                        provider="SXBET",
                        selection=normalized,
                        manifest_id=None,
                    ),
                )
            sport_selection_counts[sport_id] = len(normalized_records) - selection_count_before

        sport_coverage, selected_sport_names, unresolved_requested_sports = (
            self._sxbet_sport_coverage_report(
                requested_sport_names=requested_sport_names,
                selected_sport_ids=selected_sport_ids,
                active_sport_names=active_sport_names,
                sport_selection_counts=sport_selection_counts,
                sport_provider_errors=sport_provider_errors,
                sport_snapshot_errors=sport_snapshot_errors,
                instrument_budgets=instrument_budgets,
                market_budgets=market_budgets,
                default_instrument_limit=instrument_limit,
                default_market_discovery_limit=market_discovery_limit,
            )
        )
        source_refs.append(
            self._save_snapshot(
                provider="SXBET",
                endpoint="/semantic/coverage/sxbet",
                fetched_at=fetched_at,
                payload={
                    "provider": "SXBET",
                    "coverage_mode": "active_live" if live_only else "active_catalog",
                    "from_time": from_time,
                    "to_time": to_time,
                    "live_only": live_only,
                    "prefer_liquid_markets": prefer_liquid_markets,
                    "liquidity_probe_limit": liquidity_probe_limit,
                    "min_two_sided_markets": min_two_sided_markets,
                    "requested_sports": sorted(requested_sport_names),
                    "resolved_sports": sorted(selected_sport_names),
                    "unresolved_requested_sports": unresolved_requested_sports,
                    "sport_ids": selected_sport_ids,
                    "sports": sport_coverage,
                },
            ),
        )

        manifest = RuleCorpusManifest(
            manifest_id=_hash_payload(
                "manifest",
                {
                    "provider": "SXBET",
                    "fetched_at": fetched_at,
                    "sport_ids": selected_sport_ids,
                    "market_names": sorted(market_names),
                },
            ),
            provider="SXBET",
            fetched_at=fetched_at,
            endpoint_version="rest:v1",
            sport_count=len(selected_sport_ids),
            event_count=len(event_keys),
            selection_count=len(normalized_records),
            market_taxonomy_hash=hashlib.sha256(
                json.dumps(sorted(market_names), separators=(",", ":")).encode("utf-8"),
            ).hexdigest()[:24],
            source_refs=tuple(source_refs),
        )
        self._persist_normalized_records(normalized_records, manifest.manifest_id)
        self._store.save_manifest(manifest)
        return manifest

    @staticmethod
    def _normalize_sxbet_sport_name(value: str) -> str:
        return value.strip().lower().replace("-", "_").replace(" ", "_").replace("__", "_")

    @classmethod
    def _canonical_sport_name(cls, value: str) -> str:
        normalized = cls._normalize_sxbet_sport_name(value)
        aliases = {
            "soccer/football": "soccer",
            "soccer_football": "soccer",
            "football": "soccer",
            "american_football": "american_football",
            "american-football": "american_football",
            "hockey": "ice_hockey",
        }
        return aliases.get(normalized, normalized)

    @classmethod
    def _canonical_requested_sport_names(cls, sports: list[str] | None) -> set[str]:
        return {
            cls._canonical_sport_name(str(sport)) for sport in sports or () if str(sport).strip()
        }

    @classmethod
    def _sxbet_sport_coverage_report(
        cls,
        *,
        requested_sport_names: set[str],
        selected_sport_ids: list[int],
        active_sport_names: dict[int, str],
        sport_selection_counts: dict[int, int],
        sport_provider_errors: dict[int, str],
        sport_snapshot_errors: dict[int, str],
        instrument_budgets: dict[int, int],
        market_budgets: dict[int, int],
        default_instrument_limit: int,
        default_market_discovery_limit: int,
    ) -> tuple[dict[str, dict[str, object]], set[str], list[str]]:
        sport_coverage: dict[str, dict[str, object]] = {}
        selected_sport_names: set[str] = set()
        for sport_id in selected_sport_ids:
            sport_name = cls._canonical_sport_name(
                active_sport_names.get(sport_id) or SXBET_SPORT_IDS.get(sport_id, ""),
            )
            if sport_name:
                selected_sport_names.add(sport_name)
            selection_count = sport_selection_counts.get(sport_id, 0)
            blocker = None
            if selection_count == 0:
                blocker = sport_provider_errors.get(
                    sport_id,
                    sport_snapshot_errors.get(sport_id, "no_active_markets_or_provider_data"),
                )
            sport_coverage[sport_name or str(sport_id)] = {
                "sport_id": sport_id,
                "selection_count": selection_count,
                "instrument_budget": instrument_budgets.get(sport_id, default_instrument_limit),
                "market_discovery_budget": market_budgets.get(
                    sport_id,
                    default_market_discovery_limit,
                ),
                "blocker": blocker,
            }
        unresolved_requested_sports = sorted(requested_sport_names - selected_sport_names)
        for sport_name in unresolved_requested_sports:
            sport_coverage.setdefault(
                sport_name,
                {
                    "sport_id": None,
                    "selection_count": 0,
                    "instrument_budget": 0,
                    "market_discovery_budget": 0,
                    "blocker": "not_in_sxbet_active_sports_catalog",
                    "requested": True,
                },
            )
        return sport_coverage, selected_sport_names, unresolved_requested_sports

    @classmethod
    def _resolve_sxbet_sport_ids(
        cls,
        *,
        requested_sports: list[str] | None,
        active_sports: dict[int, str],
    ) -> list[int]:
        if not requested_sports:
            return []

        canonical_by_id: dict[int, str] = {}
        for sport_id, name in active_sports.items():
            canonical_by_id[int(sport_id)] = cls._canonical_sport_name(name)
        for sport_id, fallback_name in SXBET_SPORT_IDS.items():
            canonical_by_id.setdefault(int(sport_id), cls._canonical_sport_name(fallback_name))

        requested = {
            cls._canonical_sport_name(sport) for sport in requested_sports if str(sport).strip()
        }
        return sorted(
            sport_id
            for sport_id, canonical_name in canonical_by_id.items()
            if canonical_name in requested
        )

    @staticmethod
    def _distribute_budget(total: int | None, sport_ids: list[int]) -> dict[int, int]:
        item_count = len(sport_ids)
        if total is None or item_count <= 0:
            return {}
        base, remainder = divmod(max(int(total), item_count), item_count)
        return {
            sport_id: base + (1 if index < remainder else 0)
            for index, sport_id in enumerate(sport_ids)
        }

    async def refresh_polymarket(
        self,
        *,
        sports: list[str] | None = None,
        limit: int = 200,
        http_client: Any = None,
    ) -> RuleCorpusManifest:
        import os
        import ssl
        from urllib import parse
        from urllib import request

        from nautilus_trader.adapters.betting.semantics.polymarket_transform import (
            PolymarketSportsTransformer,
        )
        from nautilus_trader.adapters.polymarket.common.gamma_markets import (
            normalize_gamma_market_to_clob_format,
        )
        from nautilus_trader.adapters.polymarket.common.parsing import (
            parse_polymarket_instrument,
        )

        fetched_at = _utc_now()
        target_sports = {
            PolymarketSportsTransformer.canonical_sport(sport) or sport for sport in (sports or [])
        }
        cafile: str | None = None
        try:
            import certifi
        except ModuleNotFoundError:
            certifi = None
        if certifi is not None:
            cafile = certifi.where()
        elif os.path.exists("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"
        context = (
            ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        )
        sports_metadata = self._polymarket_get_json(
            request=request,
            context=context,
            endpoint="/sports",
        )
        source_refs = [
            self._save_snapshot(
                provider="POLYMARKET",
                endpoint="/gamma/sports",
                fetched_at=fetched_at,
                payload=sports_metadata,
            ),
        ]

        coverage_report: dict[str, Any] = {
            "provider": "POLYMARKET",
            "sports": {},
        }

        discovered_markets: dict[str, dict[str, Any]] = {}
        canonical_market_counts: dict[str, int] = {}
        for sport_metadata in sports_metadata:
            refresh_target = self._polymarket_refresh_target(
                sport_metadata=sport_metadata,
                target_sports=target_sports,
                canonical_market_counts=canonical_market_counts,
                limit=limit,
            )
            if refresh_target is None:
                continue
            canonical_sport, remaining_limit = refresh_target
            sport_result = self._refresh_polymarket_sport(
                sport_metadata=sport_metadata,
                target_sports=target_sports,
                limit=remaining_limit,
                request=request,
                parse=parse,
                context=context,
                fetched_at=fetched_at,
            )
            if sport_result is None:
                continue
            canonical_sport, sport_markets, sport_coverage, sport_source_refs = sport_result
            new_markets = {
                market_id: market
                for market_id, market in sport_markets.items()
                if market_id not in discovered_markets
            }
            canonical_market_counts[canonical_sport] = canonical_market_counts.get(
                canonical_sport,
                0,
            ) + len(new_markets)
            self._merge_polymarket_coverage(
                coverage_report=coverage_report,
                canonical_sport=canonical_sport,
                sport_coverage=sport_coverage,
                market_count=canonical_market_counts[canonical_sport],
            )
            source_refs.extend(sport_source_refs)
            discovered_markets.update(new_markets)

        normalized_records, discovered_sports, event_keys, market_names = (
            self._polymarket_normalized_records(
                discovered_markets=discovered_markets,
                normalize_gamma_market_to_clob_format=normalize_gamma_market_to_clob_format,
                parse_polymarket_instrument=parse_polymarket_instrument,
                transformer=PolymarketSportsTransformer,
            )
        )
        self._add_polymarket_selection_counts(
            coverage_report=coverage_report,
            normalized_records=normalized_records,
        )

        source_refs.append(
            self._save_snapshot(
                provider="POLYMARKET",
                endpoint="/semantic/coverage/polymarket",
                fetched_at=fetched_at,
                payload=coverage_report,
            ),
        )

        manifest = RuleCorpusManifest(
            manifest_id=_hash_payload(
                "manifest",
                {
                    "provider": "POLYMARKET",
                    "fetched_at": fetched_at,
                    "market_names": sorted(market_names),
                    "sports": sorted(discovered_sports),
                },
            ),
            provider="POLYMARKET",
            fetched_at=fetched_at,
            endpoint_version="gamma:v1",
            sport_count=len(discovered_sports),
            event_count=len(event_keys),
            selection_count=len(normalized_records),
            market_taxonomy_hash=hashlib.sha256(
                json.dumps(sorted(market_names), separators=(",", ":")).encode("utf-8"),
            ).hexdigest()[:24],
            source_refs=tuple(source_refs),
        )
        self._persist_normalized_records(normalized_records, manifest.manifest_id)
        self._store.save_manifest(manifest)
        return manifest

    @staticmethod
    def _add_polymarket_selection_counts(
        *,
        coverage_report: dict[str, Any],
        normalized_records: list[NormalizedSelectionRecord],
    ) -> None:
        sports_payload = coverage_report.setdefault("sports", {})
        if not isinstance(sports_payload, dict):
            return
        selection_counts: dict[str, int] = {}
        event_keys_by_sport: dict[str, set[str]] = {}
        market_names_by_sport: dict[str, set[str]] = {}
        for record in normalized_records:
            selection = record.selection
            sport = str(selection.sport or "").strip()
            if not sport:
                continue
            selection_counts[sport] = selection_counts.get(sport, 0) + 1
            event_keys_by_sport.setdefault(sport, set()).add(selection.event_key)
            market_names_by_sport.setdefault(sport, set()).add(
                selection.raw_market_name or selection.market_type,
            )
        for sport, selection_count in selection_counts.items():
            report = sports_payload.get(sport)
            if not isinstance(report, dict):
                report = {}
                sports_payload[sport] = report
            report["selection_count"] = selection_count
            report["normalized_event_count"] = len(event_keys_by_sport.get(sport, set()))
            report["normalized_market_count"] = len(market_names_by_sport.get(sport, set()))

    def _polymarket_normalized_records(
        self,
        *,
        discovered_markets: dict[str, dict[str, Any]],
        normalize_gamma_market_to_clob_format: Any,
        parse_polymarket_instrument: Any,
        transformer: Any,
    ) -> tuple[list[NormalizedSelectionRecord], set[str], set[str], set[str]]:
        market_names: set[str] = set()
        event_keys: set[str] = set()
        discovered_sports: set[str] = set()
        normalized_records: list[NormalizedSelectionRecord] = []
        for market in discovered_markets.values():
            normalized_market = normalize_gamma_market_to_clob_format(market)
            for token in normalized_market.get("tokens", []):
                token_id = token.get("token_id")
                outcome = token.get("outcome")
                if not token_id or not outcome:
                    continue
                instrument = parse_polymarket_instrument(
                    market_info=normalized_market,
                    token_id=str(token_id),
                    outcome=str(outcome),
                )
                transformed = transformer.to_crypto_betting_instrument(instrument)
                if transformed is None:
                    continue
                if not self._polymarket_has_event_participants(transformed):
                    continue
                selection = self._normalizer.normalize(transformed)
                discovered_sports.add(selection.sport)
                event_keys.add(selection.event_key)
                market_names.add(selection.raw_market_name or selection.market_type)
                normalized_records.append(
                    NormalizedSelectionRecord(
                        record_id=self._normalized_record_id("POLYMARKET", selection),
                        provider="POLYMARKET",
                        selection=selection,
                        manifest_id=None,
                    ),
                )
        return normalized_records, discovered_sports, event_keys, market_names

    @staticmethod
    def _polymarket_has_event_participants(instrument: Any) -> bool:
        info = getattr(instrument, "info", {})
        sports_market = info.get("sports_market") if isinstance(info, dict) else {}
        if not isinstance(sports_market, dict):
            return False
        if sports_market.get("home_name") and sports_market.get("away_name"):
            return True
        params = sports_market.get("params")
        return bool(
            sports_market.get("event_type") == "team_future"
            and sports_market.get("event_name")
            and isinstance(params, dict)
            and params.get("subject"),
        )

    @staticmethod
    def _polymarket_refresh_target(
        *,
        sport_metadata: Any,
        target_sports: set[str],
        canonical_market_counts: dict[str, int],
        limit: int,
    ) -> tuple[str, int] | None:
        from nautilus_trader.adapters.betting.semantics.polymarket_transform import (
            PolymarketSportsTransformer,
        )

        if not isinstance(sport_metadata, dict):
            return None
        sport_code = str(sport_metadata.get("sport") or "").strip()
        canonical_sport = PolymarketSportsTransformer.canonical_sport(sport_code)
        if canonical_sport is None:
            return None
        if target_sports and canonical_sport not in target_sports:
            return None
        remaining_limit = max(limit - canonical_market_counts.get(canonical_sport, 0), 0)
        if remaining_limit <= 0:
            return None
        return canonical_sport, remaining_limit

    @staticmethod
    def _merge_polymarket_coverage(
        *,
        coverage_report: dict[str, Any],
        canonical_sport: str,
        sport_coverage: dict[str, Any],
        market_count: int,
    ) -> None:
        coverage = coverage_report["sports"].setdefault(
            canonical_sport,
            {
                "sport_codes": [],
                "tag_ids": [],
                "event_count": 0,
                "market_count": 0,
                "attempts": [],
            },
        )
        coverage["sport_codes"].append(sport_coverage.get("sport_code"))
        coverage["tag_ids"].extend(sport_coverage.get("tag_ids", []))
        coverage["event_count"] += int(sport_coverage.get("event_count") or 0)
        coverage["market_count"] = market_count
        coverage["attempts"].extend(sport_coverage.get("attempts", []))

    @staticmethod
    def _polymarket_get_json(*, request: Any, context: Any, endpoint: str) -> Any:
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError(f"Invalid Polymarket endpoint: {endpoint}")
        req = request.Request(
            f"https://gamma-api.polymarket.com{endpoint}",
            headers={
                "Accept": "application/json",
                "User-Agent": "cloudbet-market-maker/semantic-rule-miner",
            },
            method="GET",
        )
        with request.urlopen(req, timeout=30, context=context) as response:
            return json.loads(response.read().decode("utf-8"))

    def _refresh_polymarket_sport(
        self,
        *,
        sport_metadata: Any,
        target_sports: set[str],
        limit: int,
        request: Any,
        parse: Any,
        context: Any,
        fetched_at: str,
    ) -> tuple[str, dict[str, dict[str, Any]], dict[str, Any], list[str]] | None:
        from nautilus_trader.adapters.betting.semantics.polymarket_transform import (
            PolymarketSportsTransformer,
        )

        if not isinstance(sport_metadata, dict):
            return None
        sport_code = str(sport_metadata.get("sport") or "").strip()
        canonical_sport = PolymarketSportsTransformer.canonical_sport(sport_code)
        if canonical_sport is None:
            return None
        if target_sports and canonical_sport not in target_sports:
            return None

        selected_tags = self._polymarket_selected_tags(sport_metadata)
        sport_events: dict[str, dict[str, Any]] = {}
        discovered_markets: dict[str, dict[str, Any]] = {}
        attempts: list[dict[str, Any]] = []
        source_refs: list[str] = []
        for tag in selected_tags:
            if len(discovered_markets) >= limit:
                break
            events, snapshot_id, attempt = self._polymarket_events_for_tag(
                canonical_sport=canonical_sport,
                sport_code=sport_code,
                tag=tag,
                selected_tags=selected_tags,
                limit=limit,
                request=request,
                parse=parse,
                context=context,
                fetched_at=fetched_at,
            )
            attempts.append(attempt)
            if snapshot_id is not None:
                source_refs.append(snapshot_id)
            if events is None:
                continue
            self._polymarket_collect_event_markets(
                canonical_sport=canonical_sport,
                sport_code=sport_code,
                selected_tags=selected_tags,
                events=events,
                sport_events=sport_events,
                discovered_markets=discovered_markets,
            )
            if len(discovered_markets) >= limit:
                break

        return (
            canonical_sport,
            discovered_markets,
            {
                "sport_code": sport_code,
                "tag_ids": selected_tags,
                "event_count": len(sport_events),
                "market_count": len(discovered_markets),
                "attempts": attempts,
            },
            source_refs,
        )

    @staticmethod
    def _polymarket_selected_tags(sport_metadata: dict[str, Any]) -> list[str]:
        tags = [
            tag.strip() for tag in str(sport_metadata.get("tags") or "").split(",") if tag.strip()
        ]
        return [tag for tag in tags if tag not in {"1", "100639"}] or tags[:1]

    def _polymarket_events_for_tag(
        self,
        *,
        canonical_sport: str,
        sport_code: str,
        tag: str,
        selected_tags: list[str],
        limit: int,
        request: Any,
        parse: Any,
        context: Any,
        fetched_at: str,
    ) -> tuple[list[dict[str, Any]] | None, str | None, dict[str, Any]]:
        endpoint = "/events?" + parse.urlencode(
            {
                "tag_id": tag,
                "related_tags": "true",
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": limit,
                "order": "volume",
                "ascending": "false",
            },
        )
        try:
            events = self._polymarket_get_json(
                request=request,
                context=context,
                endpoint=endpoint,
            )
        except Exception as exc:
            return (
                None,
                None,
                {"tag_id": tag, "sport": canonical_sport, "error": type(exc).__name__},
            )
        snapshot_id = self._save_snapshot(
            provider="POLYMARKET",
            endpoint=f"/gamma{endpoint}",
            fetched_at=fetched_at,
            payload=events,
        )
        return (
            events,
            snapshot_id,
            {
                "tag_id": tag,
                "sport": canonical_sport,
                "sport_code": sport_code,
                "selected_tags": selected_tags,
                "event_count": len([event for event in events if isinstance(event, dict)]),
                "market_count": sum(
                    len(event.get("markets", [])) for event in events if isinstance(event, dict)
                ),
            },
        )

    @staticmethod
    def _polymarket_collect_event_markets(
        *,
        canonical_sport: str,
        sport_code: str,
        selected_tags: list[str],
        events: list[dict[str, Any]],
        sport_events: dict[str, dict[str, Any]],
        discovered_markets: dict[str, dict[str, Any]],
    ) -> None:
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("id") or event.get("slug") or "")
            if not event_id:
                continue
            sport_events[event_id] = event
            for market in event.get("markets", []):
                if not isinstance(market, dict):
                    continue
                market_id = str(
                    market.get("id") or market.get("conditionId") or market.get("slug") or "",
                )
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
                        "endDate": event.get("endDate"),
                        "sport": canonical_sport,
                    },
                ]
                discovered_markets[market_id] = enriched_market

    def _save_snapshot(
        self,
        *,
        provider: str,
        endpoint: str,
        fetched_at: str,
        payload: Any,
    ) -> str:
        raw = json.dumps(payload, default=self._json_default, sort_keys=True).encode("utf-8")
        snapshot_id = _hash_payload(
            "snapshot",
            {"provider": provider, "endpoint": endpoint, "payload": raw.decode("utf-8")},
        )
        self._store.save_snapshot(
            CorpusSnapshot(
                snapshot_id=snapshot_id,
                provider=provider,
                endpoint=endpoint,
                fetched_at=fetched_at,
                payload=raw,
                source_ref=endpoint,
            ),
        )
        return snapshot_id

    @staticmethod
    def _normalized_record_id(provider: str, normalized: Any) -> str:
        return _hash_payload(
            "normalized",
            {
                "provider": provider,
                "instrument_id": normalized.instrument_id,
                "event_key": normalized.event_key,
                "market_type": normalized.market_type,
                "selection": normalized.selection,
                "params": normalized.params,
            },
        )

    def _persist_normalized_records(
        self,
        records: list[NormalizedSelectionRecord],
        manifest_id: str,
    ) -> None:
        for record in records:
            self._store.save_normalized_selection(
                NormalizedSelectionRecord(
                    record_id=record.record_id,
                    provider=record.provider,
                    selection=record.selection,
                    manifest_id=manifest_id,
                ),
            )

    @staticmethod
    def _json_default(value: Any) -> Any:
        if hasattr(value, "__struct_fields__"):
            return {field: getattr(value, field) for field in value.__struct_fields__}
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")

    @staticmethod
    def _cloudbet_selection_field(selection: Any, key: str) -> Any:
        if isinstance(selection, dict):
            return selection.get(key)
        return getattr(selection, key, None)

    @staticmethod
    def _resolve_cloudbet_sports(
        *,
        requested_sports: list[str] | None,
        available_sports: Any,
    ) -> list[str]:
        available_by_key = {sport.key: sport.key for sport in available_sports}
        available_by_name = {
            str(sport.name).strip().lower().replace("-", "_").replace(" ", "_"): sport.key
            for sport in available_sports
        }
        aliases = {
            "soccer/football": "soccer",
            "soccer_football": "soccer",
            "american_football": "american-football",
            "american-football": "american-football",
            "football": "soccer",
            "hockey": "ice_hockey",
        }
        if not requested_sports:
            return [sport.key for sport in available_sports]

        resolved: list[str] = []
        for sport in requested_sports:
            normalized = sport.strip().lower().replace("-", "_").replace(" ", "_")
            candidate = aliases.get(normalized, sport)
            candidate_normalized = (
                str(candidate).strip().lower().replace("-", "_").replace(" ", "_")
            )
            provider_key = (
                available_by_key.get(candidate)
                or available_by_name.get(candidate_normalized)
                or available_by_name.get(normalized)
            )
            if provider_key and provider_key not in resolved:
                resolved.append(provider_key)
        return resolved

    @staticmethod
    def _cloudbet_competition_to_selections(competition: dict[str, Any]) -> list[dict[str, Any]]:
        competition_name = str(competition.get("name") or "")
        competition_key = str(competition.get("key") or "")
        sport = competition.get("sport") or {}
        sport_name = str(sport.get("name") or "")
        sport_key = str(sport.get("key") or "")
        selections: list[dict[str, Any]] = []
        for event in competition.get("events", []):
            if not isinstance(event, dict):
                continue
            event_id = event.get("id")
            event_name = str(event.get("name") or "")
            cutoff_time = str(event.get("cutoffTime") or "")
            status = str(event.get("status") or "")
            for market_name, market_value in (event.get("markets") or {}).items():
                submarkets = (
                    market_value.get("submarkets") if isinstance(market_value, dict) else None
                )
                if not isinstance(submarkets, dict):
                    continue
                for submarket_period, submarket_value in submarkets.items():
                    market_url = None
                    sequence = None
                    if isinstance(submarket_value, dict):
                        market_url = submarket_value.get("marketUrl")
                        sequence = submarket_value.get("sequence")
                        raw_selections = submarket_value.get("selections", [])
                    else:
                        raw_selections = []
                    for selection in raw_selections:
                        if not isinstance(selection, dict):
                            continue
                        normalized_market_name = market_name
                        normalized_market_type = market_name
                        normalized_outcome = selection.get("outcome")
                        normalized_params = selection.get("params") or ""
                        if (
                            "total regular season wins" in event_name.lower()
                            and isinstance(normalized_outcome, str)
                            and normalized_outcome.startswith(("s-over-", "s-under-"))
                        ):
                            is_over = normalized_outcome.startswith("s-over-")
                            line = (
                                normalized_outcome.removeprefix("s-over-")
                                .removeprefix("s-under-")
                                .replace("-dot-", ".")
                            )
                            normalized_market_name = f"{sport_key}.totals"
                            normalized_market_type = f"{sport_key}.totals"
                            normalized_outcome = "over" if is_over else "under"
                            normalized_params = f"line={line}"
                        selections.append(
                            {
                                "competition_name": competition_name,
                                "competition_key": competition_key,
                                "sport_name": sport_name,
                                "sport_key": sport_key,
                                "event_id": event_id,
                                "home_name": None,
                                "home_key": None,
                                "away_name": None,
                                "away_key": None,
                                "status": status,
                                "market_name": normalized_market_name,
                                "market_type": normalized_market_type,
                                "submarket_name": f"{normalized_market_name}_{submarket_period}",
                                "submarket_period": submarket_period,
                                "sequence": sequence,
                                "outcome": normalized_outcome,
                                "price": selection.get("price"),
                                "min_stake": selection.get("minStake"),
                                "max_stake": selection.get("maxStake"),
                                "probability": selection.get("probability"),
                                "selection_status": selection.get("status"),
                                "side": selection.get("side"),
                                "cutoff_time": cutoff_time,
                                "event_name": event_name,
                                "params": normalized_params,
                                "market_url": selection.get("marketUrl") or market_url,
                            },
                        )
        return selections
