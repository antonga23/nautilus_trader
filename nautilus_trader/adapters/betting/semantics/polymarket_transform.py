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
Transforms Polymarket sports binary options into betting-style instruments when metadata
is sufficient.
"""

from __future__ import annotations

import json
import re
from typing import Any

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency


class PolymarketSportsTransformer:
    """
    Converts sports-tagged BinaryOption instruments into CryptoBettingInstrument
    snapshots.
    """

    SPORT_PATTERNS = (
        (re.compile(r"\bNBA\b|\bWNBA\b|basketball", re.IGNORECASE), "basketball"),
        (re.compile(r"\bNHL\b|hockey|stanley cup", re.IGNORECASE), "ice_hockey"),
        (re.compile(r"\bNFL\b|super bowl|american football", re.IGNORECASE), "american_football"),
        (re.compile(r"\bMLB\b|world series|baseball", re.IGNORECASE), "baseball"),
        (
            re.compile(
                r"premier league|champions league|la liga|serie a|bundesliga|soccer|football",
                re.IGNORECASE,
            ),
            "soccer",
        ),
        (
            re.compile(r"tennis|wimbledon|us open|australian open|french open", re.IGNORECASE),
            "tennis",
        ),
        (re.compile(r"cricket", re.IGNORECASE), "cricket"),
        (re.compile(r"rugby", re.IGNORECASE), "rugby"),
        (re.compile(r"ufc|mma", re.IGNORECASE), "mma"),
        (re.compile(r"golf", re.IGNORECASE), "golf"),
    )

    SPORT_CODE_MAP = {
        "soccer/football": "soccer",
        "football": "soccer",
        "soccer": "soccer",
        "basketball": "basketball",
        "nba": "basketball",
        "wnba": "basketball",
        "ncaab": "basketball",
        "epl": "soccer",
        "lal": "soccer",
        "bun": "soccer",
        "sea": "soccer",
        "ucl": "soccer",
        "fl1": "soccer",
        "acn": "soccer",
        "afc": "soccer",
        "ofc": "soccer",
        "fif": "soccer",
        "nfl": "american_football",
        "cfb": "american_football",
        "american_football": "american_football",
        "mlb": "baseball",
        "baseball": "baseball",
        "ten": "tennis",
        "atp": "tennis",
        "wta": "tennis",
        "tennis": "tennis",
        "hockey": "ice_hockey",
        "nhl": "ice_hockey",
        "ice_hockey": "ice_hockey",
        "cri": "cricket",
        "ufc": "mma",
        "mma": "mma",
        "rug": "rugby",
        "pga": "golf",
    }
    NO_DRAW_SPORTS = {
        "american_football",
        "baseball",
        "basketball",
        "ice_hockey",
        "tennis",
    }
    NON_FIXTURE_TOKENS = (
        "draft",
        "award",
        "mvp",
        "champion",
        "championship",
        "cup winner",
        "league winner",
        "division winner",
        "top scorer",
        "ballon d",
        "first overall",
        "1st overall",
    )

    @classmethod
    def canonical_sport(cls, raw_sport: str | None) -> str | None:
        if not raw_sport:
            return None
        normalized = raw_sport.strip().lower().replace("-", "_").replace(" ", "_")
        return cls.SPORT_CODE_MAP.get(normalized, normalized or None)

    @classmethod
    def _canonical_sport(cls, raw_sport: str | None) -> str | None:
        return cls.canonical_sport(raw_sport)

    @classmethod
    def _infer_sports_market(  # noqa: C901
        cls,
        instrument: BinaryOption,
        info: dict,
    ) -> dict | None:
        question = str(info.get("question") or getattr(instrument, "description", "")).strip()
        original = info.get("_gamma_original", {})
        event = original.get("events", [{}])[0] if isinstance(original.get("events"), list) else {}
        event_title = str(event.get("title") or "")
        haystacks = [
            question,
            str(info.get("market_slug") or ""),
            event_title,
            str(event.get("slug") or ""),
        ]

        sport = cls.canonical_sport(
            str(original.get("sport") or original.get("sportsTag") or event.get("sport") or ""),
        )
        if sport is None:
            for pattern, candidate in cls.SPORT_PATTERNS:
                if any(pattern.search(text) for text in haystacks if text):
                    sport = candidate
                    break
        if sport is None:
            return None

        selection_target = cls._selection_target(question)

        home_name, away_name = cls._parse_event_participants(event_title)
        if not home_name or not away_name:
            home_name, away_name = cls._parse_event_participants(question)
        if not cls._is_fixture_market(
            question=question,
            event_title=event_title,
            home_name=home_name,
            away_name=away_name,
        ):
            return None
        target_role = cls._participant_role(selection_target, home_name, away_name)
        outcome = str(getattr(instrument, "outcome", "") or "").strip().lower()

        sports_market_type = (
            str(
                original.get("sportsMarketType") or cls._market_type_from_question(question) or "",
            )
            .strip()
            .lower()
        )
        market_family, market_name, market_type, selection_role = cls._winner_market_semantics(
            sport=sport,
            target_role=target_role,
            outcome=outcome,
        )
        params: dict[str, str] = {}
        is_spread_market = "spread" in sports_market_type or "handicap" in sports_market_type
        is_total_market = "total" in sports_market_type
        line = original.get("line")
        if line is None and (is_spread_market or is_total_market):
            line = cls._line_from_question(question)
        if line is not None:
            params["line"] = str(line)
        if is_spread_market:
            market_family, market_name, market_type, selection_role = cls._spread_semantics(
                sport=sport,
                target_role=target_role,
                outcome=outcome,
                line=line,
                params=params,
            )
        elif is_total_market:
            market_family = "totals_binary"
            market_type = f"{sport}.totals"
            market_name = f"{sport}.{market_family}"
            selection_role = cls._total_selection_role(
                question=question,
                outcome=outcome,
            )

        return {
            "sport": sport,
            "market_name": market_name,
            "market_type": market_type,
            "selection_role": selection_role,
            "selection_target": selection_target,
            "home_name": home_name,
            "away_name": away_name,
            "event_name": event_title or question,
            "competition_name": event_title or "Polymarket Sports",
            "event_id": str(info.get("condition_id") or instrument.id.symbol.value),
            "start_time": str(event.get("startDateIso") or event.get("startDate") or ""),
            "params": params,
            "resolution_policy": cls._resolution_policy(question, original),
        }

    @classmethod
    def _is_fixture_market(
        cls,
        *,
        question: str,
        event_title: str,
        home_name: str,
        away_name: str,
    ) -> bool:
        if not home_name or not away_name:
            return False
        combined = " ".join(part for part in (question, event_title) if part).lower()
        if any(token in combined for token in cls.NON_FIXTURE_TOKENS):
            return False
        return not (
            cls._looks_invalid_participant(home_name) or cls._looks_invalid_participant(away_name)
        )

    @staticmethod
    def _looks_invalid_participant(value: str) -> bool:
        normalized = value.strip().lower()
        if not normalized:
            return True
        invalid_prefixes = ("will ", "to ", "be ")
        invalid_tokens = ("draft", "award", "championship", "title", "overall")
        return normalized.startswith(invalid_prefixes) or any(
            token in normalized for token in invalid_tokens
        )

    @classmethod
    def _winner_market_semantics(
        cls,
        *,
        sport: str,
        target_role: str,
        outcome: str,
    ) -> tuple[str, str, str, str]:
        market_family = "winner_binary"
        market_name = f"{sport}.{market_family}"
        market_type = f"{sport}.winner"
        selection_role = outcome
        if target_role not in {"home", "away"}:
            return market_family, market_name, market_type, selection_role

        if outcome == "yes":
            selection_role = target_role
            if sport == "soccer":
                return market_family, f"{sport}.match_odds", f"{sport}.1x2", selection_role
            return market_family, f"{sport}.winner", f"{sport}.winner", selection_role

        if outcome != "no":
            return market_family, market_name, market_type, selection_role
        if sport == "soccer":
            selection_role = "away_draw" if target_role == "home" else "home_draw"
            return (
                "double_chance_binary",
                f"{sport}.double_chance",
                f"{sport}.double_chance",
                selection_role,
            )
        if sport in cls.NO_DRAW_SPORTS:
            selection_role = "away" if target_role == "home" else "home"
            return market_family, f"{sport}.winner", f"{sport}.winner", selection_role
        return market_family, market_name, market_type, selection_role

    @staticmethod
    def _spread_selection_role(*, target_role: str, outcome: str) -> str:
        if target_role not in {"home", "away"}:
            return outcome
        if outcome == "yes":
            return target_role
        if outcome == "no":
            return "away" if target_role == "home" else "home"
        return outcome

    @classmethod
    def _spread_semantics(
        cls,
        *,
        sport: str,
        target_role: str,
        outcome: str,
        line: Any,
        params: dict[str, str],
    ) -> tuple[str, str, str, str]:
        market_family = "spread_binary"
        market_type = f"{sport}.spread"
        market_name = f"{sport}.{market_family}"
        selection_role = cls._spread_selection_role(target_role=target_role, outcome=outcome)
        if outcome == "no" and line is not None:
            params["line"] = cls._invert_numeric_line(line)
        return market_family, market_name, market_type, selection_role

    @staticmethod
    def _total_selection_role(*, question: str, outcome: str) -> str:
        normalized = question.lower()
        target = ""
        if re.search(r"\bover\b", normalized):
            target = "over"
        elif re.search(r"\bunder\b", normalized):
            target = "under"
        if not target:
            return outcome
        if outcome == "yes":
            return target
        if outcome == "no":
            return "under" if target == "over" else "over"
        return outcome

    @staticmethod
    def _line_from_question(question: str) -> str | None:
        match = re.search(r"(?<![A-Za-z0-9])([+-]?\d+(?:\.\d+)?)(?![A-Za-z0-9])", question)
        return match.group(1) if match is not None else None

    @staticmethod
    def _invert_numeric_line(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value)
        inverted = -numeric
        if inverted.is_integer():
            return str(int(inverted))
        return f"{inverted:g}"

    @staticmethod
    def _market_type_from_question(question: str) -> str:
        normalized = question.lower()
        if "cover the spread" in normalized or re.search(r"\bspread\b", normalized):
            return "spread"
        if (re.search(r"\bover\b", normalized) or re.search(r"\bunder\b", normalized)) and (
            " total" in normalized or " points" in normalized or " goals" in normalized
        ):
            return "total"
        return ""

    @staticmethod
    def _selection_target(question: str) -> str:
        for regex in (
            re.compile(r"^Will (.+?) win\b", re.IGNORECASE),
            re.compile(r"^Will (.+?) beat\b", re.IGNORECASE),
            re.compile(r"^Will (.+?) defeat\b", re.IGNORECASE),
            re.compile(r"^Will (.+?) cover\b", re.IGNORECASE),
            re.compile(r"^Will (.+?) be\b", re.IGNORECASE),
            re.compile(r"^Will (.+?) make\b", re.IGNORECASE),
        ):
            match = regex.search(question)
            if match is not None:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _parse_event_participants(event_title: str) -> tuple[str, str]:
        title = event_title.strip()
        if not title:
            return "", ""
        for regex in (
            re.compile(r"^Will (.+?) beat (.+?)\??$", re.IGNORECASE),
            re.compile(r"^Will (.+?) defeat (.+?)\??$", re.IGNORECASE),
        ):
            match = regex.search(title)
            if match is not None:
                return match.group(1).strip(), match.group(2).strip()
        for separator in (" vs. ", " vs ", " v. ", " v "):
            if separator in title.lower():
                parts = re.split(re.escape(separator), title, maxsplit=1, flags=re.IGNORECASE)
                return parts[0].strip(), parts[1].strip()
        for separator in (" @ ", " at "):
            if separator in title.lower():
                parts = re.split(re.escape(separator), title, maxsplit=1, flags=re.IGNORECASE)
                away, home = parts[0].strip(), parts[1].strip()
                return home, away
        return "", ""

    @staticmethod
    def _participant_role(target: str, home_name: str, away_name: str) -> str:
        normalized_target = PolymarketSportsTransformer._normalize_participant(target)
        if not normalized_target:
            return ""
        if PolymarketSportsTransformer._participant_matches(normalized_target, home_name):
            if PolymarketSportsTransformer._participant_matches(normalized_target, away_name):
                return ""
            return "home"
        if PolymarketSportsTransformer._participant_matches(normalized_target, away_name):
            return "away"
        return ""

    @staticmethod
    def _normalize_participant(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        tokens = [
            token
            for token in normalized.split()
            if token not in {"the", "fc", "afc", "sc", "cf", "club"}
        ]
        return " ".join(tokens)

    @classmethod
    def _participant_matches(cls, normalized_target: str, participant: str) -> bool:
        normalized_participant = cls._normalize_participant(participant)
        if not normalized_participant:
            return False
        if normalized_target == normalized_participant:
            return True
        if f" {normalized_target} " in f" {normalized_participant} ":
            return True

        target_tokens = set(normalized_target.split())
        participant_tokens = set(normalized_participant.split())
        if not target_tokens or not participant_tokens:
            return False
        return target_tokens <= participant_tokens or bool(target_tokens & participant_tokens)

    @staticmethod
    def _resolution_policy(question: str, original: dict) -> dict:
        haystacks = [
            question,
            str(original.get("description") or ""),
            str(original.get("resolutionSource") or ""),
        ]
        combined = " ".join(text for text in haystacks if text).lower()
        policy: dict[str, str] = {}
        if "50-50" in combined or "50/50" in combined:
            policy["tie_or_unknown"] = "50_50"
        elif any(token in combined for token in ("void", "refund", "cancelled")):
            policy["tie_or_unknown"] = "void"
        else:
            policy["tie_or_unknown"] = "lose"
        return policy

    @staticmethod
    def to_crypto_betting_instrument(instrument: BinaryOption) -> CryptoBettingInstrument | None:
        info = instrument.info if isinstance(instrument.info, dict) else {}
        sports_market = info.get("sports_market")
        if not isinstance(sports_market, dict):
            sports_market = PolymarketSportsTransformer._infer_sports_market(instrument, info)
        if not isinstance(sports_market, dict):
            return None

        sport = sports_market.get("sport")
        if not sport:
            return None

        price = PolymarketSportsTransformer._token_price(
            info=info,
            sports_market=sports_market,
            outcome=str(getattr(instrument, "outcome", "")),
            token_id=str(getattr(instrument, "raw_symbol", "") or ""),
        )
        if price is None:
            return None
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            return None
        if numeric_price <= 0:
            return None

        market_name = str(sports_market.get("market_name") or "polymarket.sports_winner")
        market_type = str(sports_market.get("market_type") or market_name)
        selection_role = str(
            sports_market.get("selection_role")
            or sports_market.get("team_role")
            or getattr(instrument, "outcome", ""),
        ).lower()
        params = sports_market.get("params") or {}
        if isinstance(params, dict):
            serialized_params = "&".join(
                f"{key}={value}"
                for key, value in sorted((str(key), str(value)) for key, value in params.items())
            )
        else:
            serialized_params = str(params)

        return CryptoBettingInstrument(
            venue=Venue("POLYMARKET"),
            event_id=str(
                sports_market.get("event_id")
                or info.get("condition_id")
                or instrument.id.symbol.value,
            ),
            event_name=str(
                sports_market.get("event_name") or getattr(instrument, "description", ""),
            ),
            home_name=str(sports_market.get("home_name") or ""),
            away_name=str(sports_market.get("away_name") or ""),
            sport_name=str(sport),
            competition_name=str(sports_market.get("competition_name") or "Polymarket Sports"),
            market_name=market_name,
            market_type=market_type,
            outcome=selection_role,
            side=SelectionSide.BACK,
            price=numeric_price,
            currency=Currency.from_str("USDC"),
            params=serialized_params,
            start_time=str(sports_market.get("start_time") or ""),
            info={
                **info,
                "sports_market": sports_market,
                "resolution_policy": sports_market.get("resolution_policy", {}),
            },
        )

    @staticmethod
    def _token_price(
        *,
        info: dict[str, Any],
        sports_market: dict[str, Any],
        outcome: str,
        token_id: str,
    ) -> Any:
        if sports_market.get("price") is not None:
            return sports_market.get("price")
        if info.get("selected_token_price") is not None:
            return info.get("selected_token_price")

        token_price, token_index = PolymarketSportsTransformer._selected_token_price(
            info=info,
            outcome=outcome,
            token_id=token_id,
        )
        if token_price is not None:
            return token_price
        prices = PolymarketSportsTransformer._decode_sequence(
            info.get("_gamma_original", {}).get("outcomePrices") or info.get("outcomePrices"),
        )
        if not prices:
            return None
        if token_index is not None and token_index < len(prices):
            return prices[token_index]
        return prices[0]

    @staticmethod
    def _selected_token_price(
        *,
        info: dict[str, Any],
        outcome: str,
        token_id: str,
    ) -> tuple[Any, int | None]:
        tokens = info.get("tokens")
        selected_token_id = str(info.get("selected_token_id") or token_id or "")
        selected_outcome = str(info.get("selected_outcome") or outcome or "").lower()
        token_index: int | None = None
        if isinstance(tokens, list):
            for index, token in enumerate(tokens):
                if not isinstance(token, dict):
                    continue
                if selected_token_id and str(token.get("token_id") or "") == selected_token_id:
                    token_index = index
                    if token.get("price") is not None:
                        return token.get("price"), token_index
                    break
                if selected_outcome and str(token.get("outcome") or "").lower() == selected_outcome:
                    token_index = index
                    if token.get("price") is not None:
                        return token.get("price"), token_index
        return None, token_index

    @staticmethod
    def _decode_sequence(value: Any) -> list[Any]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return []
            return decoded if isinstance(decoded, list) else []
        return []
