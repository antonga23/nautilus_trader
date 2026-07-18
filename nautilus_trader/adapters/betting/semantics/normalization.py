# -------------------------------------------------------------------------------------------------
# skipcq: MC0001, PYL-E0611, PYL-R0903, PYL-R0911, PYL-R0912, PYL-R0913, PYL-R0914
# skipcq: PYL-W0613
# pylint: disable=no-name-in-module,too-few-public-methods,too-many-return-statements
# pylint: disable=too-many-branches,too-many-arguments,too-many-locals,unused-argument
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
Venue-agnostic normalization for betting market selections and sports binary markets.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation
import re
from typing import Any
from urllib import parse

from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.fixture_identity import DEFAULT_FIXTURE_IDENTITY_RESOLVER
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics.polymarket_transform import (
    PolymarketSportsTransformer,
)
from nautilus_trader.adapters.betting.semantics.types import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.model.instruments import BinaryOption


LINE_PATTERN = re.compile(r"(?<![a-zA-Z0-9])([+-]?\d+(?:\.\d+)?)(?![a-zA-Z0-9])")
NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")
# CloudBet whole-name sub-game markets (baseball.moneyline_innings_1_to_5,
# handicap_innings_1_to_5, totals_innings_1_to_5) carry the settlement span only
# in the market name, not in a period param. Left unparsed, the scope collapses to
# full_time and a first-5-innings market becomes byte-identical to the full-game
# market of the same fixture -> a phantom cross-venue edge on different settlement
# events. Read the span from the name so its scope stays distinct.
INNINGS_SPAN_PATTERN = re.compile(r"innings?_(\d+)_to_(\d+)")
TEXT_MARKET_TYPE_RULES: tuple[tuple[CanonicalMarketType, tuple[str, ...]], ...] = (
    (CanonicalMarketType.BINARY_OPTION, ("binary_option",)),
    (CanonicalMarketType.DRAW_NO_BET, ("draw_no_bet", "tie_no_bet")),
    (CanonicalMarketType.DOUBLE_CHANCE, ("double_chance",)),
    (CanonicalMarketType.ASIAN_HANDICAP, ("asian_handicap", "levelball", "pick_em")),
    (CanonicalMarketType.EUROPEAN_HANDICAP, ("european_handicap", "three_way_handicap")),
    (CanonicalMarketType.TEAM_TOTALS, ("team_total",)),
    (CanonicalMarketType.BOTH_TEAMS_TO_SCORE, ("both_teams_to_score", "btts")),
    (CanonicalMarketType.ODD_EVEN, ("odd_even",)),
    (CanonicalMarketType.CORRECT_SCORE, ("correct_score", "exact_sets")),
    (CanonicalMarketType.OUTRIGHT, ("outright",)),
)
WINNER_TEXT_TOKENS = ("moneyline", "winner")
SPREAD_TEXT_TOKENS = ("handicap", "spread", "run_line")
# Venues whose away-side handicap/spread ``line`` is quoted relative to the home
# (team-one) side rather than the away selection, so the away leg must be negated
# to become selection-relative. CloudBet stamps the home-relative line on BOTH
# outcomes of a handicap market: a ``soccer.asian_handicap`` line ``handicap=-0.5``
# carries ``handicap=-0.5`` on its home leg (home -0.5) and the SAME ``handicap=-0.5``
# on its away leg (really away +0.5), so both ASIAN_HANDICAP and POINT_SPREAD (run
# line) away legs need negating. SX.bet quotes a single team-one-relative ``line`` on
# both outcomes of every handicap market (numeric type 3/342 -> ASIAN_HANDICAP), so it
# needs the same treatment.
AWAY_SELECTION_RELATIVE_MARKET_TYPES: dict[str, frozenset[CanonicalMarketType]] = {
    "CLOUDBET": frozenset(
        {CanonicalMarketType.ASIAN_HANDICAP, CanonicalMarketType.POINT_SPREAD},
    ),
    "SXBET": frozenset(
        {CanonicalMarketType.ASIAN_HANDICAP, CanonicalMarketType.POINT_SPREAD},
    ),
}


class MarketNormalizer:
    """
    Normalizes venue-specific betting selections into canonical semantic selections.
    """

    @classmethod
    def normalize(
        cls,
        item: CryptoBettingInstrument | BinaryOption | Mapping[str, Any] | Any,
    ) -> NormalizedSelection:
        if isinstance(item, CryptoBettingInstrument):
            return cls._normalize_betting_instrument(item)
        if isinstance(item, BinaryOption):
            return cls._normalize_binary_option(item)
        return cls._normalize_snapshot(item)

    @classmethod
    def _normalize_betting_instrument(
        cls,
        instrument: CryptoBettingInstrument,
    ) -> NormalizedSelection:
        info = instrument.info if isinstance(instrument.info, dict) else {}
        params = cls._parse_params(getattr(instrument, "params", ""))
        line = cls._extract_line(
            label=info.get("outcome_label"),
            params=params,
            handicap=getattr(instrument, "handicap", None),
        )
        if line is not None:
            params.setdefault("line", cls._format_decimal(line))

        sport = cls._canonical_sport(str(getattr(instrument, "sport_name", "")))
        scope = cls._scope_from_parts(
            raw_market_name=str(getattr(instrument, "market_name", "")),
            raw_market_type=str(getattr(instrument, "market_type", "")),
            params=params,
        )
        rules_flags = cls._rules_flags(scope=scope, raw_text=" ".join([str(info), str(params)]))
        market_type = cls._canonical_market_type(
            raw_market_name=str(getattr(instrument, "market_name", "")),
            raw_market_type=str(getattr(instrument, "market_type", "")),
            raw_market_id=info.get("raw_market_type"),
            selection_text=str(getattr(instrument, "outcome", "")),
            params=params,
            sport=sport,
            info=info,
        )
        selection = cls._canonical_selection(
            raw_selection=str(getattr(instrument, "outcome", "")),
            market_type=market_type,
            sport=sport,
            info=info,
        )
        resolution_policy = cls._normal_resolution_policy(info)
        venue = str(instrument.id.venue)
        params = cls._venue_selection_relative_params(
            params=params,
            venue=venue,
            market_type=market_type,
            selection=selection,
        )
        params = cls._drop_redundant_line_sources(params)

        event_key = cls._event_key_from_fields(
            event_id=str(getattr(instrument, "event_id", instrument.id)),
            home_name=str(getattr(instrument, "home_name", "")),
            away_name=str(getattr(instrument, "away_name", "")),
            cutoff_time=str(getattr(instrument, "start_time", "")),
            event_name=str(getattr(instrument, "event_name", "")),
            sport=sport,
        )
        outcome_key = (
            instrument.selection_key()
            if callable(getattr(instrument, "selection_key", None))
            else selection.lower()
        )

        return NormalizedSelection(
            venue=venue,
            instrument_id=str(instrument.id),
            sport=sport,
            event_key=str(event_key),
            period=scope,
            scope=scope,
            market_type=market_type.value,
            market_family=market_type.value,
            selection=selection,
            params=tuple(sorted((str(key), str(value)) for key, value in params.items())),
            raw_market_name=str(getattr(instrument, "market_name", "")),
            raw_market_type=str(
                info.get("raw_market_type", getattr(instrument, "market_type", "")),
            ),
            raw_outcome=str(getattr(instrument, "outcome", "")),
            outcome_key=str(outcome_key),
            rules_flags=rules_flags,
            resolution_policy=resolution_policy,
            source_ref=str(instrument.id),
        )

    @classmethod
    def _normalize_snapshot(cls, item: Mapping[str, Any] | Any) -> NormalizedSelection:
        market_url = str(cls._value(item, "market_url") or cls._value(item, "marketUrl") or "")
        parsed_market_url = cls._parse_market_url(market_url)
        raw_market_name = str(
            cls._value(item, "market_name")
            or cls._value(item, "marketName")
            or parsed_market_url["market_name"]
            or "",
        )
        raw_market_type = str(
            cls._value(item, "market_type") or parsed_market_url["market_type"] or raw_market_name,
        )
        raw_outcome = str(
            cls._value(item, "outcome")
            or cls._value(item, "selection_name")
            or cls._value(item, "outcome_name")
            or cls._value(item, "outcomeName")
            or parsed_market_url["outcome"]
            or "",
        )
        info = cls._info_dict(item)

        raw_params_parts = [
            str(cls._value(item, "params") or ""),
            str(cls._value(item, "market_params") or ""),
            str(cls._value(item, "submarket_period") or ""),
            str(parsed_market_url["params"] or ""),
        ]
        params = cls._parse_params("&".join(part for part in raw_params_parts if part))
        line = cls._extract_line(
            label=cls._value(item, "outcome_name") or info.get("outcome_label"),
            params=params,
            handicap=cls._value(item, "handicap"),
        )
        if line is not None:
            params.setdefault("line", cls._format_decimal(line))

        sport = cls._canonical_sport(
            str(
                cls._value(item, "sport_name")
                or cls._value(item, "sport_key")
                or info.get("sport", ""),
            ),
        )
        scope = cls._scope_from_parts(
            raw_market_name=raw_market_name,
            raw_market_type=raw_market_type,
            params=params,
        )
        rules_flags = cls._rules_flags(
            scope=scope,
            raw_text=f"{raw_market_name} {raw_market_type} {params}",
        )
        market_type = cls._canonical_market_type(
            raw_market_name=raw_market_name,
            raw_market_type=raw_market_type,
            raw_market_id=info.get("raw_market_type") or cls._value(item, "raw_market_type"),
            selection_text=raw_outcome,
            params=params,
            sport=sport,
            info=info,
        )
        selection = cls._canonical_selection(
            raw_selection=raw_outcome,
            market_type=market_type,
            sport=sport,
            info=info,
        )
        venue = str(cls._value(item, "provider") or cls._value(item, "venue") or "CLOUDBET").upper()
        params = cls._venue_selection_relative_params(
            params=params,
            venue=venue,
            market_type=market_type,
            selection=selection,
        )
        params = cls._drop_redundant_line_sources(params)
        params = cls._strip_whole_game_period(params)

        event_id = (
            cls._value(item, "event_id") or cls._value(item, "eventId") or cls._value(item, "id")
        )
        event_key = cls._event_key_from_fields(
            event_id=str(event_id or ""),
            home_name=str(cls._value(item, "home_name") or cls._value(item, "home") or ""),
            away_name=str(cls._value(item, "away_name") or cls._value(item, "away") or ""),
            cutoff_time=str(
                cls._value(item, "cutoff_time")
                or cls._value(item, "cutoffTime")
                or cls._value(item, "start_time")
                or "",
            ),
            event_name=str(
                cls._value(item, "event_name")
                or cls._value(item, "eventName")
                or cls._value(item, "name")
                or "",
            ),
            sport=sport,
        )

        return NormalizedSelection(
            venue=venue,
            instrument_id=str(
                cls._value(item, "instrument_id") or cls._value(item, "id") or event_key,
            ),
            sport=sport,
            event_key=event_key,
            period=scope,
            scope=scope,
            market_type=market_type.value,
            market_family=market_type.value,
            selection=selection,
            params=tuple(sorted((str(key), str(value)) for key, value in params.items())),
            raw_market_name=raw_market_name,
            raw_market_type=str(cls._value(item, "raw_market_type") or raw_market_type),
            raw_outcome=raw_outcome,
            outcome_key=selection.lower(),
            rules_flags=rules_flags,
            resolution_policy=(),
            source_ref=str(event_id or event_key),
        )

    @classmethod
    def _normalize_binary_option(cls, instrument: BinaryOption) -> NormalizedSelection:
        info = instrument.info if isinstance(instrument.info, dict) else {}
        raw_sports_meta = info.get("sports_market")
        sports_meta: dict[str, Any] = raw_sports_meta if isinstance(raw_sports_meta, dict) else {}
        if sports_meta or info.get("_gamma_original") or info.get("question"):
            transformed = PolymarketSportsTransformer.to_crypto_betting_instrument(instrument)
            if transformed is not None:
                return cls._normalize_betting_instrument(transformed)

        params = cls._parse_params(sports_meta.get("params") or "")
        sport = cls._canonical_sport(str(sports_meta.get("sport") or info.get("sport") or ""))
        scope = cls._scope_from_parts(
            raw_market_name=str(
                sports_meta.get("market_name") or info.get("market_name") or "winner",
            ),
            raw_market_type=str(
                sports_meta.get("market_type") or info.get("market_type") or "winner",
            ),
            params=params,
        )
        resolution_policy = cls._binary_resolution_policy(
            question=str(getattr(instrument, "description", "")),
            info=info,
        )
        rules_flags = cls._rules_flags(
            scope=scope,
            raw_text=" ".join([str(getattr(instrument, "description", "")), str(info)]),
        )
        selection = cls._canonical_selection(
            raw_selection=str(getattr(instrument, "outcome", "")),
            market_type=CanonicalMarketType.BINARY_OPTION,
            sport=sport,
            info=sports_meta or info,
        )
        market_type = CanonicalMarketType.BINARY_OPTION
        condition_id = str(info.get("condition_id") or instrument.id.symbol.value)
        event_key = cls._event_key_from_fields(
            event_id=condition_id,
            home_name=str(sports_meta.get("home_name") or ""),
            away_name=str(sports_meta.get("away_name") or ""),
            cutoff_time=str(sports_meta.get("start_time") or ""),
            event_name=str(
                sports_meta.get("event_name")
                or info.get("question")
                or getattr(instrument, "description", ""),
            ),
            sport=sport,
        )
        return NormalizedSelection(
            venue=str(instrument.id.venue),
            instrument_id=str(instrument.id),
            sport=sport or "unknown",
            event_key=event_key,
            period=scope,
            scope=scope,
            market_type=market_type.value,
            market_family=market_type.value,
            selection=selection,
            params=tuple(sorted((str(key), str(value)) for key, value in params.items())),
            raw_market_name=str(
                sports_meta.get("market_name") or info.get("market_name") or "binary_option",
            ),
            raw_market_type=str(
                sports_meta.get("market_type") or info.get("market_type") or "binary_option",
            ),
            raw_outcome=str(getattr(instrument, "outcome", "")),
            outcome_key=selection.lower(),
            rules_flags=rules_flags,
            resolution_policy=resolution_policy,
            source_ref=condition_id,
        )

    @staticmethod
    def _value(item: Mapping[str, Any] | Any, key: str) -> Any:
        if isinstance(item, Mapping):
            return item.get(key)
        return getattr(item, key, None)

    @staticmethod
    def _info_dict(item: Mapping[str, Any] | Any) -> dict[str, Any]:
        info = MarketNormalizer._value(item, "info")
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _normal_resolution_policy(info: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        raw_sports_meta = info.get("sports_market")
        sports_meta: dict[str, Any] = raw_sports_meta if isinstance(raw_sports_meta, dict) else {}
        raw_policy = info.get("resolution_policy") or sports_meta.get("resolution_policy") or {}
        if isinstance(raw_policy, Mapping):
            return tuple(sorted((str(key), str(value)) for key, value in raw_policy.items()))
        return ()

    @staticmethod
    def _event_key_from_fields(
        *,
        event_id: str,
        home_name: str,
        away_name: str,
        cutoff_time: str,
        event_name: str = "",
        sport: str = "",
    ) -> str:
        normalized_time = MarketNormalizer._normalize_timestamp(cutoff_time)
        normalized_home = MarketNormalizer._normalize_text(
            DEFAULT_FIXTURE_IDENTITY_RESOLVER.normalize_team_name(home_name),
        )
        normalized_away = MarketNormalizer._normalize_text(
            DEFAULT_FIXTURE_IDENTITY_RESOLVER.normalize_team_name(away_name),
        )
        normalized_event = MarketNormalizer._normalize_text(event_name)
        normalized_sport = MarketNormalizer._normalize_text(
            DEFAULT_FIXTURE_IDENTITY_RESOLVER.normalize_sport(sport),
        )

        if normalized_home and normalized_away:
            parts = [
                normalized_sport,
                normalized_home,
                normalized_away,
                normalized_time,
            ]
            return "|".join(part for part in parts if part)

        if normalized_event:
            parts = [normalized_sport, normalized_event, normalized_time]
            return "|".join(part for part in parts if part)

        return "|".join(part for part in (event_id, normalized_time) if part).strip("|")

    @staticmethod
    def _parse_market_url(market_url: str) -> dict[str, str]:
        if not market_url:
            return {"market_name": "", "market_type": "", "outcome": "", "params": ""}

        path, _, query = market_url.partition("?")
        market_name, _, outcome = path.partition("/")
        params = parse.urlencode(parse.parse_qsl(query, keep_blank_values=False))
        return {
            "market_name": market_name,
            "market_type": market_name,
            "outcome": outcome,
            "params": params,
        }

    @classmethod
    def _parse_params(cls, raw_params: Any) -> dict[str, str]:
        if raw_params in (None, ""):
            return {}
        values_by_key: dict[str, list[str]] = {}
        for part in re.split(r"[,&]", str(raw_params)):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if not key or not value:
                continue
            bucket = values_by_key.setdefault(key, [])
            if value not in bucket:
                bucket.append(value)

        params: dict[str, str] = {}
        for key, values in values_by_key.items():
            params[key] = "|".join(cls._canonical_param_values(key, values))
        return params

    @staticmethod
    def _canonical_param_values(key: str, values: list[str]) -> list[str]:
        if len(values) <= 1:
            return values
        if key == "period":
            period_order = {
                "ft": 0,
                "ot": 1,
                "1h": 2,
                "2h": 3,
                "q1": 4,
                "q2": 5,
                "q3": 6,
                "q4": 7,
            }
            return sorted(
                values,
                key=lambda value: (
                    period_order.get(value.strip().lower(), 99),
                    value.strip().lower(),
                ),
            )
        return sorted(values, key=lambda value: value.strip().lower())

    @classmethod
    def _extract_line(
        cls,
        *,
        label: Any,
        params: dict[str, str],
        handicap: Any,
    ) -> Decimal | None:
        if isinstance(label, str):
            match = LINE_PATTERN.search(label.replace(",", "."))
            if match is not None:
                parsed = cls._to_decimal(match.group(1))
                if parsed is not None:
                    return parsed
        for key in ("line", "handicap", "total"):
            raw = params.get(key)
            if raw is None:
                continue
            parsed = cls._to_decimal(raw.split("|", 1)[0])
            if parsed is not None:
                return parsed
        if handicap is not None:
            parsed = cls._to_decimal(str(handicap))
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _drop_redundant_line_sources(params: dict[str, str]) -> dict[str, str]:
        line_value = params.get("line")
        if line_value is None:
            return params
        return {
            key: value
            for key, value in params.items()
            if key not in ("total", "handicap") or value != line_value
        }

    @staticmethod
    def _strip_whole_game_period(params: dict[str, str]) -> dict[str, str]:
        # CloudBet whole-game submarket keys carry period=ft (or period=ot&period=ft);
        # the live instrument carries none. Drop the redundant whole-game period so the
        # corpus params_key matches the live node's (genuine sub-game tokens are kept).
        period = params.get("period")
        if period is None:
            return params
        tokens = {token.strip().lower() for token in period.split("|") if token.strip()}
        if tokens in ({"ft"}, {"ft", "ot"}):
            return {key: value for key, value in params.items() if key != "period"}
        return params

    @classmethod
    def _venue_selection_relative_params(
        cls,
        *,
        params: dict[str, str],
        venue: str,
        market_type: CanonicalMarketType,
        selection: str,
    ) -> dict[str, str]:
        if selection != "AWAY":
            return params
        relative_market_types = AWAY_SELECTION_RELATIVE_MARKET_TYPES.get(venue.upper())
        if relative_market_types is None or market_type not in relative_market_types:
            return params

        adjusted = dict(params)
        for key in ("handicap", "line"):
            parsed = cls._to_decimal(adjusted.get(key, ""))
            if parsed is not None:
                adjusted[key] = cls._format_decimal(-parsed)
        return adjusted

    @classmethod
    def _canonical_market_type(
        cls,
        *,
        raw_market_name: str,
        raw_market_type: str,
        raw_market_id: Any,
        selection_text: str,
        params: dict[str, str],
        sport: str,
        info: dict[str, Any],
    ) -> CanonicalMarketType:
        normalized = cls._normalize_text(
            " ".join([raw_market_name, raw_market_type, str(raw_market_id or "")]),
        )
        try:
            numeric_market_id = int(raw_market_id)
        except (TypeError, ValueError):
            numeric_market_id = raw_market_id
        from_numeric_id = cls._canonical_market_type_from_numeric_id(
            numeric_market_id,
            raw_market_name=raw_market_name,
            raw_market_type=raw_market_type,
            params=params,
            info=info,
        )
        if from_numeric_id is not None:
            return from_numeric_id

        return cls._canonical_market_type_from_text(
            normalized,
            raw_market_name=raw_market_name,
            info=info,
        )

    @classmethod
    def _canonical_market_type_from_numeric_id(
        cls,
        numeric_market_id: Any,
        *,
        raw_market_name: str,
        raw_market_type: str,
        params: dict[str, str],
        info: dict[str, Any],
    ) -> CanonicalMarketType | None:
        if cls._is_sxbet_market(info):
            return cls._sxbet_market_type_from_numeric_id(
                numeric_market_id,
                params=params,
                info=info,
            )
        if numeric_market_id in {0, 52, 226}:
            return (
                CanonicalMarketType.WINNER
                if info.get("is_two_way_market") is True
                else CanonicalMarketType.MATCH_ODDS
            )
        if numeric_market_id in {1, 201, 342}:
            return CanonicalMarketType.ASIAN_HANDICAP
        if numeric_market_id in {2, 835}:
            return CanonicalMarketType.TOTALS
        if numeric_market_id == 3:
            line = params.get("line")
            if line and cls._to_decimal(line.split("|", 1)[0]) not in (None, Decimal(0)):
                return CanonicalMarketType.ASIAN_HANDICAP
            return CanonicalMarketType.DRAW_NO_BET
        if numeric_market_id == 4:
            return CanonicalMarketType.BOTH_TEAMS_TO_SCORE
        if numeric_market_id == 88:
            return CanonicalMarketType.WINNER
        return None

    @staticmethod
    def _is_sxbet_market(info: dict[str, Any]) -> bool:
        return "sxbet_market_hash" in info or "sxbet_event_id_source" in info

    @classmethod
    def _sxbet_market_type_from_numeric_id(
        cls,
        numeric_market_id: Any,
        *,
        params: dict[str, str],
        info: dict[str, Any],
    ) -> CanonicalMarketType | None:
        if numeric_market_id in {0, 1, 52, 63, 202, 203, 204, 226}:
            return (
                CanonicalMarketType.WINNER
                if info.get("is_two_way_market") is True
                else CanonicalMarketType.MATCH_ODDS
            )
        if numeric_market_id in {2, 21, 28, 45, 46, 77, 165, 166, 835}:
            return CanonicalMarketType.TOTALS
        if numeric_market_id in {3, 53, 64, 65, 66, 201, 342}:
            line = params.get("line")
            if line and cls._to_decimal(line.split("|", 1)[0]) not in (None, Decimal(0)):
                return CanonicalMarketType.ASIAN_HANDICAP
            return CanonicalMarketType.DRAW_NO_BET
        if numeric_market_id == 88:
            return CanonicalMarketType.WINNER
        return None

    @classmethod
    def _canonical_market_type_from_text(
        cls,
        normalized: str,
        *,
        raw_market_name: str,
        info: dict[str, Any],
    ) -> CanonicalMarketType:
        candidates = (
            cls._market_type_from_text_rules(normalized),
            cls._winner_market_type_from_text(normalized),
            cls._match_odds_market_type_from_text(
                normalized,
                raw_market_name=raw_market_name,
                info=info,
            ),
            cls._totals_market_type_from_text(normalized),
            cls._spread_market_type_from_text(normalized),
        )
        return next(
            (market_type for market_type in candidates if market_type is not None),
            CanonicalMarketType.OTHER,
        )

    @staticmethod
    def _market_type_from_text_rules(normalized: str) -> CanonicalMarketType | None:
        for market_type, tokens in TEXT_MARKET_TYPE_RULES:
            if any(token in normalized for token in tokens):
                return market_type
        return None

    @staticmethod
    def _winner_market_type_from_text(normalized: str) -> CanonicalMarketType | None:
        if any(
            token in normalized
            for token in (
                "any_set_to_nil",
                "any_team_to_lead_by_points",
                "team_clean_sheet",
                "team_to_lead_by_points",
                "team_win_to_nil",
                "team_to_win_a_set",
                "with_extra_inning",
            )
        ):
            return CanonicalMarketType.WINNER
        if any(token in normalized for token in WINNER_TEXT_TOKENS):
            return CanonicalMarketType.WINNER
        return None

    @staticmethod
    def _match_odds_market_type_from_text(
        normalized: str,
        *,
        raw_market_name: str,
        info: dict[str, Any],
    ) -> CanonicalMarketType | None:
        if normalized.endswith("1x2") or ".1x2" in raw_market_name or "_1x2" in normalized:
            return CanonicalMarketType.MATCH_ODDS
        if "match_odds" not in normalized:
            return None
        return (
            CanonicalMarketType.WINNER
            if info.get("is_two_way_market") is True
            else CanonicalMarketType.MATCH_ODDS
        )

    @staticmethod
    def _totals_market_type_from_text(normalized: str) -> CanonicalMarketType | None:
        if "total_goals" in normalized or "totals" in normalized:
            return CanonicalMarketType.TOTALS
        if normalized.startswith("total_") or "_total_" in normalized:
            return CanonicalMarketType.TOTALS
        if any(
            token in normalized
            for token in (
                "total_games",
                "total_sets",
                "total_period",
                "games_total",
                "sets_total",
            )
        ):
            return CanonicalMarketType.TOTALS
        if normalized.endswith(("_total", "_totals")):
            return CanonicalMarketType.TOTALS
        return None

    @staticmethod
    def _spread_market_type_from_text(normalized: str) -> CanonicalMarketType | None:
        if any(token in normalized for token in SPREAD_TEXT_TOKENS):
            return CanonicalMarketType.POINT_SPREAD
        return None

    @staticmethod
    def _canonical_sport(raw_sport: str) -> str:
        normalized = raw_sport.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "soccer/football": "soccer",
            "soccer_football": "soccer",
            "football": "soccer",
            "futsal": "soccer",
            "american_football": "american_football",
            "hockey": "ice_hockey",
        }
        return aliases.get(normalized, normalized or "unknown")

    @classmethod
    def _canonical_selection(
        cls,
        *,
        raw_selection: str,
        market_type: CanonicalMarketType,
        sport: str,
        info: dict[str, Any],
    ) -> str:
        raw_sports_meta = info.get("sports_market")
        sports_meta: dict[str, Any] = raw_sports_meta if isinstance(raw_sports_meta, dict) else info
        target_role = str(
            sports_meta.get("selection_role") or sports_meta.get("team_role") or "",
        ).upper()

        outcome = Outcome.from_string(raw_selection)
        if outcome != Outcome.OTHER:
            return outcome.value.upper()

        raw = parse.unquote(raw_selection.strip()).lower()
        if raw.startswith("outcome="):
            raw = raw.split("=", 1)[1].strip()
        raw = raw.replace("{{home}}", "home").replace("{{away}}", "away")
        if market_type == CanonicalMarketType.OTHER:
            parts = raw.split("_")
            if len(parts) == 2 and all(part in {"home", "draw", "away"} for part in parts):
                return raw.upper()
        aliases = {
            "1": "HOME",
            "x": "DRAW",
            "2": "AWAY",
            "home_or_draw": "HOME_DRAW",
            "draw_or_away": "AWAY_DRAW",
            "draw_away": "AWAY_DRAW",
            "home_draw": "HOME_DRAW",
            "no_draw": "HOME_AWAY",
            "home_away": "HOME_AWAY",
            "yes": target_role or "YES",
            "no": "NO",
            "pk": "PICK",
        }
        if raw in aliases:
            return aliases[raw]
        if raw.startswith("score="):
            return raw.replace(":", "_").replace("=", "_").upper()
        raw = raw.replace("+", "_plus")
        return NON_WORD_PATTERN.sub("_", raw).strip("_").upper() or "OTHER"

    @classmethod
    def _scope_from_parts(
        cls,
        *,
        raw_market_name: str,
        raw_market_type: str,
        params: dict[str, str],
    ) -> str:
        periods = params.get("period", "").split("|") if params.get("period") else []
        normalized_periods = {period.strip().lower() for period in periods if period.strip()}
        text = cls._normalize_text(" ".join([raw_market_name, raw_market_type, str(params)]))
        explicit_scope = cls._explicit_scope_from_periods(
            normalized_periods=normalized_periods,
            text=text,
            params=params,
        )
        if explicit_scope is not None:
            return explicit_scope
        team = params.get("team")
        if team:
            return f"team_{team.lower()}"
        return "full_time"

    @staticmethod
    def _explicit_scope_from_periods(  # noqa: C901
        *,
        normalized_periods: set[str],
        text: str,
        params: dict[str, str],
    ) -> str | None:
        direct_scope = MarketNormalizer._direct_period_scope(
            normalized_periods=normalized_periods,
            text=text,
        )
        if direct_scope is not None:
            return direct_scope
        quarter_periods = sorted(
            item for item in normalized_periods if item.startswith("q") and item[1:].isdigit()
        )
        if len(quarter_periods) == 1:
            return f"quarter_{quarter_periods[0][1:]}"
        if len(quarter_periods) > 1:
            return "full_time_including_overtime" if "ot" in normalized_periods else "full_time"
        quarter = next(
            (item for item in normalized_periods if item.startswith("q")),
            None,
        )
        if quarter is not None:
            return f"quarter_{quarter[1:]}"
        single_period_scope = MarketNormalizer._single_period_scope(normalized_periods)
        if single_period_scope is not None:
            return single_period_scope
        set_or_inning_scope = MarketNormalizer._set_or_inning_scope(
            normalized_periods=normalized_periods,
            params=params,
        )
        if set_or_inning_scope is not None:
            return set_or_inning_scope
        if "wo" in normalized_periods and normalized_periods.issubset({"wo", "default"}):
            return "winner_only"
        if "ot" in normalized_periods:
            return "overtime"
        if "default" in normalized_periods:
            return "full_time"
        set_period = next((item for item in normalized_periods if item.startswith("set")), None)
        if set_period:
            return set_period
        return None

    @staticmethod
    def _single_period_scope(normalized_periods: set[str]) -> str | None:
        period = next(
            (item for item in normalized_periods if item.startswith("p") and item[1:].isdigit()),
            None,
        )
        if period is None:
            return None
        return f"period_{period[1:]}"

    @staticmethod
    def _direct_period_scope(*, normalized_periods: set[str], text: str) -> str | None:
        if "team_to_win_a_set" in text:
            return "winner_only"
        innings_span = INNINGS_SPAN_PATTERN.search(text)
        if innings_span is not None:
            return f"innings_{int(innings_span.group(1))}_to_{int(innings_span.group(2))}"
        if "first_half" in text or ("1h" in normalized_periods and "2h" not in normalized_periods):
            return "first_half"
        if "second_half" in text or "2h" in normalized_periods:
            return "second_half"
        if normalized_periods in ({"ft", "ot"}, {"ot", "ft"}):
            # Regulation+overtime spanning the whole game IS the full-time market. The
            # live feed carries no period token, so folding overtime into the scope
            # here would orphan every corpus record against its live node; the overtime
            # nuance rides the includes_overtime rules_flag instead (see _rules_flags).
            return "full_time"
        if "ft" in normalized_periods or "period_ft" in text:
            return "full_time"
        return None

    @staticmethod
    def _set_or_inning_scope(
        *,
        normalized_periods: set[str],
        params: dict[str, str],
    ) -> str | None:
        set_value = params.get("set", "").strip().lower()
        if set_value.isdigit():
            return f"set{set_value}"
        set_periods = sorted(
            item for item in normalized_periods if item.startswith("set") and item[3:].isdigit()
        )
        if len(set_periods) == 1:
            return set_periods[0]
        inning_value = params.get("inning", "").strip().lower()
        if inning_value.isdigit():
            return f"inning_{inning_value}"
        inning_numbers = sorted(
            int(item[6:])
            for item in normalized_periods
            if item.startswith("inning") and item[6:].isdigit()
        )
        if not inning_numbers:
            return None
        if len(inning_numbers) == 1:
            return f"inning_{inning_numbers[0]}"
        # A multi-inning submarket (e.g. innings 1-5) must map to one stable scope
        # regardless of set iteration order, otherwise the same market drifts between
        # inning_2/inning_4 run-to-run. Contiguous spans read as innings_{lo}_to_{hi}.
        if inning_numbers == list(range(inning_numbers[0], inning_numbers[-1] + 1)):
            return f"innings_{inning_numbers[0]}_to_{inning_numbers[-1]}"
        return "innings_" + "_".join(str(number) for number in inning_numbers)

    @staticmethod
    def _rules_flags(scope: str, raw_text: str) -> tuple[str, ...]:
        flags: set[str] = set()
        normalized = MarketNormalizer._normalize_text(raw_text)
        tokens = {token for token in normalized.split("_") if token}
        if (
            "including_overtime" in scope
            or "overtime" in scope
            or "period_ot" in normalized
            or "ot" in tokens
        ):
            flags.add("includes_overtime")
        if scope == "winner_only":
            flags.add("winner_only_scope")
        if "penalties" in normalized or "shootout" in normalized:
            flags.add("includes_penalties")
        return tuple(sorted(flags))

    @staticmethod
    def _binary_resolution_policy(
        question: str,
        info: dict[str, Any],
    ) -> tuple[tuple[str, str], ...]:
        text = " ".join(
            [
                question,
                str(info.get("description", "")),
                str(info.get("rules", "")),
                str(info.get("_gamma_original", {}).get("resolutionSource", "")),
            ],
        ).lower()
        policy: dict[str, str] = {}
        if "50-50" in text or "50/50" in text:
            policy["tie_or_unknown"] = "50_50"
        if "void" in text or "refund" in text:
            policy["unplayed"] = "void"
        if "extra time" in text or "overtime" in text:
            policy["includes_overtime"] = "true"
        return tuple(sorted(policy.items()))

    @staticmethod
    def _normalize_text(value: str) -> str:
        return NON_WORD_PATTERN.sub("_", value.lower()).strip("_")

    @staticmethod
    def _normalize_timestamp(value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return raw
        return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _to_decimal(value: str) -> Decimal | None:
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
        if parsed == Decimal("-0"):
            return Decimal(0)
        return parsed

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
