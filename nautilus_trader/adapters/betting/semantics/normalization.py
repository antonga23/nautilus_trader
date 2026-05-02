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
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics.types import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.model.instruments import BinaryOption


LINE_PATTERN = re.compile(r"(?<![a-zA-Z0-9])([+-]?\d+(?:\.\d+)?)(?![a-zA-Z0-9])")
NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")


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
            venue=str(instrument.id.venue),
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
        rules_flags = cls._rules_flags(scope=scope, raw_text=f"{raw_market_name} {raw_market_type}")
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
        venue = str(cls._value(item, "provider") or cls._value(item, "venue") or "CLOUDBET").upper()

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
        normalized_home = MarketNormalizer._normalize_text(home_name)
        normalized_away = MarketNormalizer._normalize_text(away_name)
        normalized_event = MarketNormalizer._normalize_text(event_name)
        normalized_sport = MarketNormalizer._normalize_text(sport)

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

    @staticmethod
    def _parse_params(raw_params: Any) -> dict[str, str]:
        if raw_params in (None, ""):
            return {}
        params: dict[str, str] = {}
        for part in re.split(r"[,&]", str(raw_params)):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if not key or not value:
                continue
            if key in params and value not in params[key].split("|"):
                params[key] = f"{params[key]}|{value}"
            else:
                params[key] = value
        return params

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

    @classmethod
    def _canonical_market_type(  # noqa: C901
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

        if "binary_option" in normalized:
            return CanonicalMarketType.BINARY_OPTION
        if "draw_no_bet" in normalized or "tie_no_bet" in normalized:
            return CanonicalMarketType.DRAW_NO_BET
        if "double_chance" in normalized:
            return CanonicalMarketType.DOUBLE_CHANCE
        if "asian_handicap" in normalized or "levelball" in normalized or "pick_em" in normalized:
            return CanonicalMarketType.ASIAN_HANDICAP
        if "european_handicap" in normalized or "three_way_handicap" in normalized:
            return CanonicalMarketType.EUROPEAN_HANDICAP
        if "team_total" in normalized:
            return CanonicalMarketType.TEAM_TOTALS
        if "both_teams_to_score" in normalized or "btts" in normalized:
            return CanonicalMarketType.BOTH_TEAMS_TO_SCORE
        if "odd_even" in normalized:
            return CanonicalMarketType.ODD_EVEN
        if "correct_score" in normalized or "exact_sets" in normalized:
            return CanonicalMarketType.CORRECT_SCORE
        if "outright" in normalized:
            return CanonicalMarketType.OUTRIGHT
        if any(token in normalized for token in ("moneyline", "winner")):
            return CanonicalMarketType.WINNER
        if normalized.endswith("1x2") or ".1x2" in raw_market_name or "_1x2" in normalized:
            return CanonicalMarketType.MATCH_ODDS
        if "match_odds" in normalized:
            if info.get("is_two_way_market") is True:
                return CanonicalMarketType.WINNER
            return CanonicalMarketType.MATCH_ODDS
        if (
            "total_goals" in normalized
            or "totals" in normalized
            or normalized.endswith(("_total", "_totals"))
        ):
            return CanonicalMarketType.TOTALS
        if "handicap" in normalized or "spread" in normalized or "run_line" in normalized:
            return CanonicalMarketType.POINT_SPREAD
        return CanonicalMarketType.OTHER

    @staticmethod
    def _canonical_sport(raw_sport: str) -> str:
        normalized = raw_sport.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "football": "soccer",
            "futsal": "soccer",
            "american_football": "american_football",
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

        raw = raw_selection.strip().lower()
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
        if normalized_periods in ({"ft", "ot"}, {"ot", "ft"}):
            return "full_time_including_overtime"
        if "wo" in normalized_periods:
            return "winner_only"
        if "ot" in normalized_periods:
            return "overtime"
        if "ft" in normalized_periods or "period_ft" in text:
            return "full_time"
        if "1h" in normalized_periods or "first_half" in text:
            return "first_half"
        if "2h" in normalized_periods or "second_half" in text:
            return "second_half"
        quarter = next((item for item in normalized_periods if item.startswith("q")), None)
        if quarter:
            return f"quarter_{quarter[1:]}"
        set_period = next((item for item in normalized_periods if item.startswith("set")), None)
        if set_period:
            return set_period
        team = params.get("team")
        if team:
            return f"team_{team.lower()}"
        return "full_time"

    @staticmethod
    def _rules_flags(scope: str, raw_text: str) -> tuple[str, ...]:
        flags: set[str] = set()
        normalized = MarketNormalizer._normalize_text(raw_text)
        if "including_overtime" in scope or "overtime" in scope or "period_ot" in normalized:
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
