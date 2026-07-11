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
CryptoBettingInstrument - Instrument type for crypto sports betting markets.
"""

import hashlib
import re
import sys
import time
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.betting.common.constants import DEFAULT_PRICE_PRECISION
from nautilus_trader.adapters.betting.common.constants import DEFAULT_SIZE_PRECISION
from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.fixture_identity import DEFAULT_FIXTURE_IDENTITY_RESOLVER
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import InstrumentClass
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments.base import Instrument
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


MAX_BETTING_SYMBOL_LENGTH = 64


def make_crypto_betting_instrument_id(
    venue: Venue,
    event_id: str,
    market_name: str,
    outcome: str,
    params: str | None = None,
) -> InstrumentId:
    """
    Create an InstrumentId for a crypto betting instrument.

    Format: {event_id}:{market_name}:{outcome}[:{params}].{VENUE}

    Parameters
    ----------
    venue : Venue
        The venue identifier.
    event_id : str
        The event/match identifier.
    market_name : str
        The market type name.
    outcome : str
        The selection outcome.
    params : str, optional
        Additional parameters (e.g., handicap value).

    Returns
    -------
    InstrumentId
        The constructed instrument ID.

    """
    # Normalize components for symbol
    event_id = str(event_id).replace(".", "_").replace(" ", "_")
    market_name = market_name.replace(".", "_").replace(" ", "_")
    outcome = outcome.replace(".", "_").replace(" ", "_")

    if params:
        # Canonicalize numeric tokens ("2.50" / "2.500" -> "2.5") so the same betting
        # line always yields the same instrument ID regardless of how the venue
        # payload serialized the float.
        params = re.sub(r"-?\d+\.\d+", lambda m: f"{float(m.group(0)):g}", params)
        params = params.replace(".", "_").replace(" ", "_").replace("=", "_")
        symbol_str = f"{event_id}:{market_name}:{outcome}:{params}"
    else:
        symbol_str = f"{event_id}:{market_name}:{outcome}"

    # Truncate if too long (Symbol has max length)
    if len(symbol_str) > MAX_BETTING_SYMBOL_LENGTH:
        # Use a short hash suffix when the normalized symbol exceeds the model limit.
        hash_suffix = hashlib.sha256(symbol_str.encode()).hexdigest()[:8]
        symbol_str = f"{event_id[:20]}:{market_name[:20]}:{hash_suffix}"

    return InstrumentId(Symbol(symbol_str), venue)


class CryptoBettingInstrument(Instrument):
    """
    Represents an instrument in a crypto sports betting market.

    This extends the base Instrument with betting-specific properties
    for use with cryptocurrency betting venues.

    Parameters
    ----------
    venue : Venue
        The venue where this instrument trades.
    event_id : str
        The unique identifier for the event/match.
    event_name : str
        The human-readable event name.
    home_name : str
        The name of the home team/participant.
    away_name : str
        The name of the away team/participant.
    sport_name : str
        The name of the sport.
    competition_name : str
        The name of the competition/league.
    market_name : str
        The market type name (e.g., "match_odds", "total_goals").
    market_type : str
        The normalized market type.
    outcome : str
        The selection outcome (e.g., "home", "over").
    side : SelectionSide
        The betting side (BACK or LAY).
    price : float
        The current decimal odds/price.
    currency : Currency
        The settlement currency.
    params : str, optional
        Additional market parameters (e.g., "line=2.5").
    live : bool, default False
        Whether this is a live (in-play) market.
    enabled : bool, default True
        Whether trading is enabled for this selection.
    max_size : Decimal, optional
        Maximum stake allowed.
    min_size : Decimal, optional
        Minimum stake allowed.
    start_time : str, optional
        Event start time (ISO format).
    end_time : str, optional
        Event end time (ISO format).
    handicap : float, optional
        Handicap value if applicable.
    trading_status : str, optional
        Current trading status.
    market_id : str, optional
        Venue-specific market identifier.
    home_id : str, optional
        Home team/participant identifier.
    away_id : str, optional
        Away team/participant identifier.
    sport_id : str, optional
        Sport identifier.
    competition_id : str, optional
        Competition/league identifier.
    fees : Any, optional
        Venue-specific fee structure.
    ts_event : int, optional
        UNIX timestamp nanoseconds for event time.
    ts_init : int, optional
        UNIX timestamp nanoseconds for initialization.
    info : dict, optional
        Additional venue-specific information.

    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        venue: Venue,
        event_id: str,
        event_name: str,
        home_name: str,
        away_name: str,
        sport_name: str,
        competition_name: str,
        market_name: str,
        market_type: str,
        outcome: str,
        side: SelectionSide,
        price: float,
        currency: Currency,
        params: str | None = None,
        live: bool = False,
        enabled: bool = True,
        max_size: Decimal | float | None = None,
        min_size: Decimal | float | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        handicap: float | None = None,
        trading_status: str | None = None,
        market_id: str | None = None,
        home_id: str | None = None,
        away_id: str | None = None,
        sport_id: str | None = None,
        competition_id: str | None = None,
        fees: Any | None = None,
        ts_event: int | None = None,
        ts_init: int | None = None,
        instrument_key: str | None = None,
        info: dict | None = None,
    ) -> None:
        # Validate parameters
        PyCondition.not_none(venue, "venue")
        PyCondition.valid_string(event_id, "event_id")
        PyCondition.valid_string(event_name, "event_name")
        PyCondition.valid_string(market_name, "market_name")
        PyCondition.valid_string(outcome, "outcome")
        PyCondition.positive(price, "price")

        # Store betting-specific attributes
        # Intern low-cardinality strings so identical values are deduplicated across the
        # thousands of instruments a graph build materializes. Interning does not change
        # equality or identity semantics for str values. home_name/away_name are left
        # un-interned because they are effectively unique per team.
        self.event_id = sys.intern(event_id)
        self.event_name = sys.intern(event_name)
        self.home_name = home_name
        self.away_name = away_name
        self.sport_name = sys.intern(sport_name)
        self.competition_name = sys.intern(competition_name)
        self.market_name = sys.intern(market_name)
        self.market_type = sys.intern(market_type)
        self.outcome = sys.intern(outcome)
        self.side = side
        self.price = price
        self.params = params or ""
        self.live = live
        self.enabled = enabled
        self.start_time = start_time
        self.end_time = end_time
        self.handicap = handicap
        self.trading_status = trading_status
        self.market_id = sys.intern(market_id) if market_id else market_id
        self.home_id = home_id
        self.away_id = away_id
        self.sport_id = sys.intern(sport_id) if sport_id else sport_id
        self.competition_id = sys.intern(competition_id) if competition_id else competition_id
        self.fees = fees
        self.venue_name = venue
        self.instrument_key = instrument_key

        # Normalize size values
        if max_size is not None:
            max_size = Decimal(str(max_size)) if not isinstance(max_size, Decimal) else max_size
        if min_size is not None:
            min_size = Decimal(str(min_size)) if not isinstance(min_size, Decimal) else min_size

        self.max_size = max_size
        self.min_size = min_size

        instrument_id_params = params
        if instrument_key:
            instrument_id_params = (
                f"{params}|key={instrument_key}" if params else f"key={instrument_key}"
            )

        # Generate instrument ID
        instrument_id = make_crypto_betting_instrument_id(
            venue=venue,
            event_id=event_id,
            market_name=market_name,
            outcome=outcome,
            params=instrument_id_params,
        )

        # Timestamps
        now_ns = time.time_ns()
        ts_event = ts_event or now_ns
        ts_init = ts_init or now_ns

        # Initialize base Instrument
        super().__init__(
            instrument_id=instrument_id,
            raw_symbol=instrument_id.symbol,
            asset_class=AssetClass.ALTERNATIVE,
            instrument_class=InstrumentClass.SPORTS_BETTING,
            quote_currency=currency,
            is_inverse=False,
            size_precision=DEFAULT_SIZE_PRECISION,
            price_precision=DEFAULT_PRICE_PRECISION,
            price_increment=Price(10**-DEFAULT_PRICE_PRECISION, precision=DEFAULT_PRICE_PRECISION),
            size_increment=Quantity(10**-DEFAULT_SIZE_PRECISION, precision=DEFAULT_SIZE_PRECISION),
            multiplier=Quantity.from_int(1),
            lot_size=Quantity.from_int(1),
            max_quantity=Quantity(float(max_size), precision=DEFAULT_SIZE_PRECISION)
            if max_size
            else None,
            min_quantity=Quantity(float(min_size), precision=DEFAULT_SIZE_PRECISION)
            if min_size
            else None,
            max_notional=Money(float(max_size), currency) if max_size else None,
            min_notional=Money(float(min_size), currency) if min_size else None,
            max_price=Price(1000, precision=DEFAULT_PRICE_PRECISION),  # Max odds 1000
            min_price=None,
            margin_init=Decimal(1),
            margin_maint=Decimal(1),
            maker_fee=Decimal(0),
            taker_fee=Decimal(0),
            ts_event=ts_event,
            ts_init=ts_init,
            info=info or {},
        )

    @property
    def implied_probability(self) -> Decimal:
        """
        Get the implied probability from the current odds.
        """
        return Decimal(1) / Decimal(str(self.price))

    @staticmethod
    def from_dict(values: dict[str, Any]) -> "CryptoBettingInstrument":
        """
        Create a CryptoBettingInstrument from a dictionary.

        Parameters
        ----------
        values : dict[str, Any]
            Dictionary with instrument values.

        Returns
        -------
        CryptoBettingInstrument

        """
        data = values.copy()

        # Handle type conversions
        if "venue" in data and isinstance(data["venue"], str):
            data["venue"] = Venue(data["venue"])
        if "currency" in data and isinstance(data["currency"], str):
            data["currency"] = Currency.from_str(data["currency"])
        if "side" in data and isinstance(data["side"], str):
            data["side"] = SelectionSide(data["side"])
        if "max_size" in data and data["max_size"] is not None:
            data["max_size"] = Decimal(str(data["max_size"]))
        if "min_size" in data and data["min_size"] is not None:
            data["min_size"] = Decimal(str(data["min_size"]))

        # Remove keys not in constructor
        for key in ["id", "type", "raw_symbol"]:
            data.pop(key, None)

        return CryptoBettingInstrument(**data)

    @staticmethod
    def to_dict(obj: "CryptoBettingInstrument") -> dict[str, Any]:
        """
        Convert a CryptoBettingInstrument to a dictionary.

        Parameters
        ----------
        obj : CryptoBettingInstrument
            The instrument to convert.

        Returns
        -------
        dict[str, Any]

        """
        return {
            "type": "CryptoBettingInstrument",
            "id": obj.id.value,
            "venue": obj.venue_name.value
            if hasattr(obj.venue_name, "value")
            else str(obj.venue_name),
            "event_id": obj.event_id,
            "event_name": obj.event_name,
            "home_name": obj.home_name,
            "away_name": obj.away_name,
            "sport_name": obj.sport_name,
            "competition_name": obj.competition_name,
            "market_name": obj.market_name,
            "market_type": obj.market_type,
            "outcome": obj.outcome,
            "side": obj.side.value if isinstance(obj.side, SelectionSide) else str(obj.side),
            "price": obj.price,
            "currency": obj.quote_currency.code,
            "params": obj.params,
            "live": obj.live,
            "enabled": obj.enabled,
            "max_size": str(obj.max_size) if obj.max_size else None,
            "min_size": str(obj.min_size) if obj.min_size else None,
            "start_time": obj.start_time,
            "end_time": obj.end_time,
            "handicap": obj.handicap,
            "trading_status": obj.trading_status,
            "market_id": obj.market_id,
            "home_id": obj.home_id,
            "away_id": obj.away_id,
            "sport_id": obj.sport_id,
            "competition_id": obj.competition_id,
            "fees": obj.fees,
            "ts_event": obj.ts_event,
            "ts_init": obj.ts_init,
            "instrument_key": obj.instrument_key,
            # Venue-specific fields (e.g. the SX.bet ``outcome_one`` assignment
            # that drives which order-book side prices each leg) must survive the
            # cache/msgbus round-trip. Without this, ``from_dict`` reconstructs an
            # instrument with empty ``info`` and downstream code silently falls
            # back to a HOME/AWAY name heuristic, mispricing handicap/totals legs
            # whose outcomeOne is not the HOME/OVER selection.
            "info": obj.info,
        }

    @staticmethod
    def _normalize_event_component(value: str | None) -> str:
        return DEFAULT_FIXTURE_IDENTITY_RESOLVER.normalize_event_component(value)

    @classmethod
    def _normalize_team_name(cls, name: str | None) -> str:
        return DEFAULT_FIXTURE_IDENTITY_RESOLVER.normalize_team_name(name)

    def _team_key(self) -> tuple[str, ...]:
        return DEFAULT_FIXTURE_IDENTITY_RESOLVER.team_key(self)

    def _parsed_start_time(self) -> datetime | None:
        if not self.start_time:
            return None

        start_time = self.start_time.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(start_time)
        except ValueError:
            return None

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def parsed_start_time(self) -> datetime | None:
        """
        Return the normalized event start time, if it can be parsed.
        """
        return self._parsed_start_time()

    def event_key(self, include_start_time: bool = True) -> str:
        return DEFAULT_FIXTURE_IDENTITY_RESOLVER.event_key(
            self,
            include_start_time=include_start_time,
        )

    def event_alias_keys(self, include_start_time: bool = False) -> tuple[str, ...]:
        return DEFAULT_FIXTURE_IDENTITY_RESOLVER.event_alias_keys(
            self,
            include_start_time=include_start_time,
        )

    def selection_key(self) -> str:
        outcome = Outcome.from_string(self.outcome)
        if outcome == Outcome.HOME:
            return f"team:{self._normalize_team_name(self.home_name)}"
        if outcome == Outcome.AWAY:
            return f"team:{self._normalize_team_name(self.away_name)}"
        if outcome == Outcome.OTHER:
            return self.outcome.lower().replace(" ", "_").replace("-", "_")
        return outcome.value

    def matches_event(self, other: "CryptoBettingInstrument") -> bool:
        """
        Check if this instrument is for the same event as another.

        Parameters
        ----------
        other : CryptoBettingInstrument
            The other instrument to compare.

        Returns
        -------
        bool
            True if both instruments are for the same event.

        """
        return DEFAULT_FIXTURE_IDENTITY_RESOLVER.resolve(self, other).same_fixture

    def matches_market(self, other: "CryptoBettingInstrument") -> bool:
        """
        Check if this instrument is for the same market as another.

        Parameters
        ----------
        other : CryptoBettingInstrument
            The other instrument to compare.

        Returns
        -------
        bool
            True if both instruments are for the same market.

        """
        return (
            self.matches_event(other)
            and self.market_name == other.market_name
            and self.params == other.params
        )

    def matches_selection(self, other: "CryptoBettingInstrument") -> bool:
        """
        Check if this instrument represents the same real-world selection.

        This accounts for venues that swap home/away ordering but still refer to the
        same participant.

        """
        return self.matches_event(other) and self.selection_key() == other.selection_key()

    def is_opposite_outcome(self, other: "CryptoBettingInstrument") -> bool:
        """
        Check if this instrument has the opposite outcome to another.

        Parameters
        ----------
        other : CryptoBettingInstrument
            The other instrument to compare.

        Returns
        -------
        bool
            True if outcomes are opposite (e.g., over vs under).

        """
        if not self.matches_event(other):
            return False

        outcome_self = Outcome.from_string(self.outcome)
        outcome_other = Outcome.from_string(other.outcome)

        if {outcome_self, outcome_other} <= {Outcome.HOME, Outcome.AWAY}:
            return self.selection_key() != other.selection_key()

        opposite_pairs = {
            frozenset((Outcome.OVER, Outcome.UNDER)),
            frozenset((Outcome.YES, Outcome.NO)),
        }

        return frozenset((outcome_self, outcome_other)) in opposite_pairs

    def __repr__(self) -> str:
        return (
            f"CryptoBettingInstrument("
            f"id={self.id}, "
            f"event={self.event_name!r}, "
            f"market={self.market_name!r}, "
            f"outcome={self.outcome!r}, "
            f"price={self.price}, "
            f"live={self.live})"
        )
