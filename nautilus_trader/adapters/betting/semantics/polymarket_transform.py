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

import re

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
        "mlb": "baseball",
        "ten": "tennis",
        "atp": "tennis",
        "wta": "tennis",
        "nhl": "ice_hockey",
        "cri": "cricket",
        "ufc": "mma",
        "mma": "mma",
        "rug": "rugby",
        "pga": "golf",
    }

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
    def _infer_sports_market(cls, instrument: BinaryOption, info: dict) -> dict | None:
        question = str(info.get("question") or getattr(instrument, "description", "")).strip()
        original = info.get("_gamma_original", {})
        event = original.get("events", [{}])[0] if isinstance(original.get("events"), list) else {}
        haystacks = [
            question,
            str(info.get("market_slug") or ""),
            str(event.get("title") or ""),
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

        selection_target = ""
        for regex in (
            re.compile(r"^Will (.+?) win\b", re.IGNORECASE),
            re.compile(r"^Will (.+?) be\b", re.IGNORECASE),
            re.compile(r"^Will (.+?) make\b", re.IGNORECASE),
        ):
            match = regex.search(question)
            if match is not None:
                selection_target = match.group(1).strip()
                break

        sports_market_type = str(original.get("sportsMarketType") or "").strip().lower()
        market_family = "winner_binary"
        market_type = f"{sport}.winner"
        params: dict[str, str] = {}
        if original.get("line") is not None:
            params["line"] = str(original.get("line"))
        if "spread" in sports_market_type or "handicap" in sports_market_type:
            market_family = "spread_binary"
            market_type = f"{sport}.spread"
        elif "total" in sports_market_type:
            market_family = "totals_binary"
            market_type = f"{sport}.totals"

        return {
            "sport": sport,
            "market_name": f"{sport}.{market_family}",
            "market_type": market_type,
            "selection_role": getattr(instrument, "outcome", ""),
            "selection_target": selection_target,
            "event_name": str(event.get("title") or question),
            "competition_name": str(event.get("title") or "Polymarket Sports"),
            "event_id": str(info.get("condition_id") or instrument.id.symbol.value),
            "params": params,
            "resolution_policy": cls._resolution_policy(question, original),
        }

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

        price = sports_market.get("price")
        if price is None:
            token_prices = info.get("_gamma_original", {}).get("outcomePrices")
            if isinstance(token_prices, list) and token_prices:
                price = token_prices[0]
        if price is None:
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
            price=float(price),
            currency=Currency.from_str("USDC"),
            params=serialized_params,
            start_time=str(sports_market.get("start_time") or ""),
            info={
                **info,
                "sports_market": sports_market,
                "resolution_policy": sports_market.get("resolution_policy", {}),
            },
        )
