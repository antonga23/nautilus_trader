# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2023 Nautech Systems Pty Ltd. All rights reserved.
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
import time
from decimal import Decimal

from typing import Union, Optional, Any

from nautilus_trader.core.correctness import PyCondition
# from nautilus_trader.core.correctness cimport Condition
from nautilus_trader.core.rust.model import AssetClass
from nautilus_trader.core.rust.model import AssetType
from nautilus_trader.model.currency import Currency
from nautilus_trader.model.instruments.base import Instrument
# from nautilus_trader.model.objects cimport Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.objects import Money

from nautilus_trader.adapters.cloudbet.client.schema import SelectionSide
from nautilus_trader.adapters.cloudbet.client.util import cloudbet_instrument_id
from nautilus_trader.model.identifiers import Venue


class CryptoBettingInstrument(Instrument):
    #TODO: test creation using greyhounds and horse racing
    """
    Represents an instrument in a crypto betting market.

    Parameters
    ----------
    home_name : str
        The name of the home team.
    away_name : str
        The name of the away team.
    sport_name : str
        The name of the sport.
    competition_name : str
        The name of the competition.
    price : float
        The price of the selection.
    currency : Currency
        The Currency of the price for this instrument.
    event_name : str
        The name of the event.
    market_name : str
        The name of the market.
    market_type : str
        The type of the market.
    venue : Venue
        The name of the venue.
    live : bool
        Whether the event for the instrument is live.
    enabled : bool
        Whether the instrument is enabled for trading/betting.
    event_id : Optional[str]
        The id of the event.
    selection_id : str
        The id of the selection (Venue specific).
    outcome : str
        The outcome of the selection.
    params : str
        The parameters of the selection.
    market_id : Optional[str]
        The id of the market. (global across Venues) eg 1X2 => ID: 100 , over 2.5 => ID: 101 ; etc
    home_id : Optional[str]
        The id of the home team. (global across Venues)
    away_id : Optional[str]
        The id of the away team. (global across Venues)
    sport_id : Optional[str]
        The id of the sport. (global across Venues)
    competition_id : Optional[str]
        The id of the competition. (global across Venues)
    max_size : Union[float, Decimal, int, None]
        The maximum amount of Money that can be placed on the selection.
    min_size : Union[float, Decimal, int, None]
        The minimum amount of Money that can be placed on the selection.
    start_time : Optional[str]
        The start time of the event.
    end_time : Optional[str]
        The end time of the event.
    fees : Optional[Any]
        The fees specific to the selection.
    handicap : Optional[Any]
        The handicap specific to the selection (if applicable).
    trading_status : Optional[str]
        The trading status of the market.
    """

    def __init__(
        self,
        home_name: str,
        away_name: str,
        sport_name: str,
        competition_name: str,
        price: float,
        currency: Currency,
        event_name: str,
        market_name: str,
        venue: Venue,
        live: bool,
        enabled: bool,
        outcome: str,
        side: SelectionSide,
        params: str,
        market_type: str,
        # ToDo: create enum for all possible market_types eg over +25; under 0.5, match_odds, etc
        market_id: Optional[str] = None,
        home_id: Optional[str] = None,
        away_id: Optional[str] = None,
        sport_id: Optional[str] = None,
        competition_id: Optional[str] = None,
        max_size: Union[float, Decimal, int, None] = None,
        min_size: Union[float, Decimal, int, None] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        fees: Optional[Any] = None,
        handicap: Optional[Any] = None, #TODO: Fix a handicap type...eg. tuple[outcome, handicap_value, etc]
        trading_status: Optional[str] = None,
        # NB: event_id should be a unique per event but the same across all venues
        event_id: Optional[str] = None,
    ):
        # Event level data
        self.home_name = home_name
        self.home_id = home_id
        self.away_name = away_name
        self.away_id = away_id
        self.sport_name = sport_name
        self.sport_id = sport_id
        self.competition_name = competition_name
        self.competition_id = competition_id
        self.event_name = event_name
        self.event_id = event_id
        self.side = side

        # Market level data
        self.market_name = market_name
        self.market_id = market_id
        self.market_type = market_type
        # Selection level data
        self.outcome = outcome
        self.price = price
        self.currency = currency
        # TODO: move this "post-initialization" logic to the model/schema
        # NOTE: this is a temporary fix for the typecasting issue with Quantities. We need to explicitly type these as float or decimal
        self.max_size = float(max_size) if max_size and isinstance(max_size, str) else max_size
        self.min_size = float(min_size) if min_size and isinstance(min_size, str) else min_size
        self.params = params

        # Auxillary data
        self.venue_name = venue
        self.enabled = enabled
        self.start_time = start_time
        self.end_time = end_time
        self.live = live
        self.trading_status = trading_status
        self.fees = fees
        self.handicap = handicap
        self._typed_currency = self.currency if isinstance(self.currency, Currency) else Currency.from_internal_map(currency) #TODO: redundant => currency is already typed as Currency
        instrument_id = cloudbet_instrument_id(event_id=event_id, market_name=market_name, outcome=outcome, params=params)

        super().__init__(
            instrument_id=instrument_id,
            native_symbol=instrument_id.symbol,
            asset_class=AssetClass.SPORTS_BETTING,
            asset_type=AssetType.SPOT,
            quote_currency=self._typed_currency,
            is_inverse=False,
            size_precision=4,
            price_precision=2,
            price_increment=Price(1e-2, precision=2),
            size_increment=Quantity(1e-4, precision=4),
            multiplier=Quantity.from_int(1),
            lot_size=Quantity.from_int(1),
            max_quantity=Quantity(self.max_size, precision=2) if max_size else None, # or float(max_size) != 0 else None,
            min_quantity=Quantity(self.min_size, precision=2) if min_size else None, # or float(min_size) != 0 else None,
            max_notional=Money(self.max_size, self._typed_currency) if max_size else None, # or float(max_size) != 0 else None,
            min_notional=Money(self.min_size, self._typed_currency) if min_size else None, # and float(min_size) != 0 else None,
            max_price=Price.from_int(100),      # Can be None
            min_price=Price.from_str(str(price)),
            margin_init=Decimal(1),
            margin_maint=Decimal(1),
            maker_fee=Decimal(0),
            taker_fee=Decimal(0),
            ts_event=time.time_ns(),
            ts_init=time.time_ns()
        )


    @staticmethod
    def from_dict(values: dict[str, Any]) -> 'CryptoBettingInstrument':
        """
        Creates a new `CryptoBettingInstrument` from a dictionary.

        Parameters
        ----------
        values : dict[str, object]
            The values to initialize the instrument with.
        Returns
        -------
        CryptoBettingInstrument

        Note: dictionary keys must match the names of the `CryptoBettingInstrument` constructor arguments.
        """
        # Condition.not_none(values, "values")
        # check the type of max_size and min_size
        if values.get('max_size'):
            values['max_size'] = float(values['max_size'])
        if values.get('min_size'):
            values['min_size'] = float(values['min_size'])
        # typecast the currency
        if values.get('currency'):
            values['currency'] = Currency.from_internal_map(values['currency'])
        if values.get('side'):
            values['side'] = SelectionSide(values['side'])
        data = values.copy()
        return CryptoBettingInstrument(**{k: v for k, v in data.items() if k not in ('id', 'type')})

    # ToDO: add test
    @staticmethod
    def to_dict(obj: "CryptoBettingInstrument") -> dict[str, Any]: # to allow serialization for Cython use explict type "obj : 'CryptoBettingInstrument'" instead of self
        """
        Converts a `CryptoBettingInstrument` to a dictionary.

        Returns
        -------
        dict[str, object]
        """
        return {
            "type": CryptoBettingInstrument.__name__, # necessary for serialization to cache see: nautilus_trader/serialization/base.pyx
            "home_name": obj.home_name,
            "away_name": obj.away_name,
            "sport_name": obj.sport_name,
            "competition_name": obj.competition_name,
            "price": obj.price,
            "currency": obj.currency.code if obj.currency else None,
            "event_name": obj.event_name,
            "market_name": obj.market_name,
            "market_type": obj.market_type,
            "venue": obj.venue.value if obj.venue else None,
            "live": obj.live,
            "enabled": obj.enabled,
            "event_id": obj.event_id,
            "side": obj.side if obj.side else SelectionSide.UNDEFINED.value,
            "outcome": obj.outcome,
            "params": obj.params,
            "market_id": obj.market_id,
            "home_id": obj.home_id,
            "away_id": obj.away_id,
            "sport_id": obj.sport_id,
            "competition_id": obj.competition_id,
            "max_size": str(obj.max_size) if obj.max_size else None,
            "min_size": str(obj.min_size) if obj.min_size else None,
            "start_time": obj.start_time,
            "end_time": obj.end_time,
            "fees": obj.fees,
            "handicap": obj.handicap,
            "trading_status": obj.trading_status,
        }

    @staticmethod
    # TODO: implement venue specific creation helper method
    def from_venue(venue: Venue, **kwargs) -> 'CryptoBettingInstrument':
        """
        Creates a new `CryptoBettingInstrument` from a venue.

        Parameters
        ----------
        venue : Venue
            The venue to create the instrument from.
        kwargs : dict[str, object]
            The values to initialize the instrument with.
        Returns
        -------
        CryptoBettingInstrument
        """
        PyCondition.not_none(venue, "venue")
        if venue == Venue.CLOUDBET:
            if kwargs.get('evevnt'):
                event = kwargs['event']
            if kwargs.get('odds'):
                odds = kwargs['odds']
            # TODO: implement this
        else:
            raise ValueError(f"Unsupported venue: {venue}")


