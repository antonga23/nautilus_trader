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

from contextlib import suppress
from datetime import UTC
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Any

from nautilus_trader.adapters.betting.semantics.normalization import MarketNormalizer
from nautilus_trader.adapters.betting.semantics.store import RuleStore
from nautilus_trader.adapters.betting.semantics.types import CorpusSnapshot
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics.types import RuleCorpusManifest
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_payload(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
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

    async def refresh_cloudbet(  # noqa: C901
        self,
        client: CloudbetClient,
        *,
        sports: list[str] | None = None,
        from_timestamp: int,
        to_timestamp: int,
        limit: int = 20,
        adaptive_window: bool = True,
        max_window_seconds: int = 7 * 24 * 60 * 60,
        min_events_per_sport: int = 1,
        include_recent_past_on_sparse: bool = False,
        include_bets: bool = True,
        bet_page_size: int = 50,
        bet_max_pages: int = 5,
        bet_from_date: str | None = None,
        bet_to_date: str | None = None,
        settled_bets_only: bool = False,
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
            "sports": {},
        }

        for sport_key in selected_sports:
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
            selections = []
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
                        f"/pub/v2/odds/events?sport={sport_key}"
                        f"&from={attempt_from}&to={attempt_to}"
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

            if (
                include_recent_past_on_sparse
                and adaptive_window
                and len({selection.event_id for selection in selections}) < min_events_per_sport
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
                    if len({selection.event_id for selection in past_selections}) > len(
                        {selection.event_id for selection in selections},
                    ):
                        events_response = past_response
                        selections = past_selections
                    attempt_reports.append(
                        {
                            "from": attempt_from,
                            "to": attempt_to,
                            "direction": "past",
                            "event_count": len({selection.event_id for selection in past_selections}),
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

            if events_response is None:
                coverage_report["sports"][sport_key] = {
                    "event_count": 0,
                    "selection_count": 0,
                    "attempts": attempt_reports,
                }
                continue

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

            selection_count += len(selections)

            seen_event_ids = {
                event_id
                for selection in selections
                if (event_id := self._cloudbet_selection_field(selection, "event_id")) is not None
            }
            event_count += len(seen_event_ids)
            market_names.update(
                market_name
                for selection in selections
                if (market_name := self._cloudbet_selection_field(selection, "market_name"))
            )
            coverage_report["sports"][sport_key] = {
                "event_count": len(seen_event_ids),
                "selection_count": len(selections),
                "attempts": attempt_reports,
                "sparse": len(seen_event_ids) < min_events_per_sport,
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
                json.dumps(sorted(market_names), separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24],
            source_refs=tuple(source_refs),
        )
        self._persist_normalized_records(normalized_records, manifest.manifest_id)
        self._store.save_manifest(manifest)
        return manifest

    async def refresh_sxbet(
        self,
        client: SXBetHttpClient,
        *,
        sport_ids: list[int] | None = None,
        from_time: int | None = None,
        to_time: int | None = None,
        instrument_limit: int = 250,
        market_discovery_limit: int = 250,
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
        selected_sport_ids = sport_ids or [
            int(sport["sportId"])
            for sport in active_sports.get("data", [])
            if isinstance(sport, dict) and sport.get("sportId") is not None
        ]

        market_names: set[str] = set()
        event_keys: set[str] = set()
        normalized_records: list[NormalizedSelectionRecord] = []

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
                            page_size=min(50, market_discovery_limit),
                        ),
                    ),
                )
            except Exception:  # noqa: S112
                continue

        provider = SXBetInstrumentProvider(
            http_client=client,
            config=SXBetInstrumentProviderConfig(
                load_all=True,
                sport_ids=frozenset(selected_sport_ids) if selected_sport_ids else None,
                instrument_load_limit=instrument_limit,
                market_discovery_limit=market_discovery_limit,
            ),
        )
        await provider.load_all_async(
            filters={
                "sport_ids": frozenset(selected_sport_ids) if selected_sport_ids else None,
            },
        )

        for instrument in provider.list_all():
            normalized = self._normalizer.normalize(instrument)
            market_names.add(normalized.raw_market_name or normalized.market_type)
            event_keys.add(normalized.event_key)
            normalized_records.append(
                NormalizedSelectionRecord(
                    record_id=self._normalized_record_id("SXBET", normalized),
                    provider="SXBET",
                    selection=normalized,
                    manifest_id=None,
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
                json.dumps(sorted(market_names), separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24],
            source_refs=tuple(source_refs),
        )
        self._persist_normalized_records(normalized_records, manifest.manifest_id)
        self._store.save_manifest(manifest)
        return manifest

    async def refresh_polymarket(
        self,
        *,
        sports: list[str] | None = None,
        limit: int = 200,
        http_client=None,
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
            PolymarketSportsTransformer._canonical_sport(sport) or sport
            for sport in (sports or [])
        }
        cafile: str | None = None
        try:
            import certifi  # type: ignore
        except ModuleNotFoundError:
            certifi = None
        if certifi is not None:
            cafile = certifi.where()
        elif os.path.exists("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"
        context = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
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

        market_names: set[str] = set()
        event_keys: set[str] = set()
        discovered_sports: set[str] = set()
        normalized_records: list[NormalizedSelectionRecord] = []
        coverage_report: dict[str, Any] = {
            "provider": "POLYMARKET",
            "sports": {},
        }

        discovered_markets: dict[str, dict[str, Any]] = {}
        for sport_metadata in sports_metadata:
            sport_result = self._refresh_polymarket_sport(
                sport_metadata=sport_metadata,
                target_sports=target_sports,
                limit=limit,
                request=request,
                parse=parse,
                context=context,
                fetched_at=fetched_at,
            )
            if sport_result is None:
                continue
            canonical_sport, sport_markets, sport_coverage, sport_source_refs = sport_result
            coverage_report["sports"][canonical_sport] = sport_coverage
            source_refs.extend(sport_source_refs)
            discovered_markets.update(sport_markets)

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
                transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(instrument)
                if transformed is None:
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
                json.dumps(sorted(market_names), separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24],
            source_refs=tuple(source_refs),
        )
        self._persist_normalized_records(normalized_records, manifest.manifest_id)
        self._store.save_manifest(manifest)
        return manifest

    @staticmethod
    def _polymarket_get_json(*, request, context, endpoint: str) -> Any:
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
        request,
        parse,
        context,
        fetched_at: str,
    ) -> tuple[str, dict[str, dict[str, Any]], dict[str, Any], list[str]] | None:
        from nautilus_trader.adapters.betting.semantics.polymarket_transform import (
            PolymarketSportsTransformer,
        )

        if not isinstance(sport_metadata, dict):
            return None
        sport_code = str(sport_metadata.get("sport") or "").strip()
        canonical_sport = PolymarketSportsTransformer._canonical_sport(sport_code)
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
            tag.strip()
            for tag in str(sport_metadata.get("tags") or "").split(",")
            if tag.strip()
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
        request,
        parse,
        context,
        fetched_at: str,
    ) -> tuple[list[dict[str, Any]] | None, str | None, dict[str, Any]]:
        endpoint = (
            "/events?"
            + parse.urlencode(
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
        )
        try:
            events = self._polymarket_get_json(
                request=request,
                context=context,
                endpoint=endpoint,
            )
        except Exception as exc:
            return None, None, {"tag_id": tag, "sport": canonical_sport, "error": type(exc).__name__}
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
                    len(event.get("markets", []))
                    for event in events
                    if isinstance(event, dict)
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
                market_id = str(market.get("id") or market.get("conditionId") or market.get("slug") or "")
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
                        "startDateIso": event.get("startDate"),
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
    def _normalized_record_id(provider: str, normalized) -> str:
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
        available_sports,
    ) -> list[str]:
        available_by_key = {sport.key: sport.key for sport in available_sports}
        available_by_name = {
            str(sport.name).strip().lower().replace("-", "_").replace(" ", "_"): sport.key
            for sport in available_sports
        }
        aliases = {
            "american_football": "american-football",
            "american-football": "american-football",
            "football": "soccer",
        }
        if not requested_sports:
            return [sport.key for sport in available_sports]

        resolved: list[str] = []
        for sport in requested_sports:
            normalized = sport.strip().lower().replace("-", "_").replace(" ", "_")
            candidate = aliases.get(normalized, sport)
            provider_key = available_by_key.get(candidate) or available_by_name.get(normalized)
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
                submarkets = market_value.get("submarkets") if isinstance(market_value, dict) else None
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
