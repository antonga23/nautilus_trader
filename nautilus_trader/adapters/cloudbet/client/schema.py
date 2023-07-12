# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2023 . All rights reserved.
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
from enum import Enum
from typing import Optional, List, Dict, Any, Union

import msgspec


class Selection(msgspec.Struct):
    """ A selection is a possible outcome of a market and is the primary unit of betting
    In essence a selection is a flattened representation of the GetEventsForSportResponse
    """
    competition_name: Optional[str]
    competition_key: Optional[str]
    sport_name: Optional[str]
    sport_key: Optional[str]
    event_id: Optional[int]
    home_name: Optional[str]
    home_key: Optional[str]
    away_name: Optional[str]
    away_key: Optional[str]
    # ToDo: create enum for status
    status: str
    market_name: Optional[str]
    submarket_name: Optional[str]
    submarket_period: Optional[str]
    sequence: Optional[str]
    outcome: str
    price: float
    min_stake: float
    max_stake: float
    probability: float
    selection_status: str
    side: str
    cutoff_time: Optional[str]
    event_name: Optional[str]


class GetEventForSportResponseSelection(msgspec.Struct):
    outcome: str
    params: str
    price: float
    minStake: float
    maxStake: float
    probability: float
    status: str
    side: str


class GetEventForSportResponseSubmarket(msgspec.Struct):
    sequence: str
    selections: List[GetEventForSportResponseSelection]


class GetEventForSportResponseMarket(msgspec.Struct):
    submarkets: Dict[str, GetEventForSportResponseSubmarket]


class GetEventForSportResponseTeam(msgspec.Struct):
    name: str
    key: str
    abbreviation: str
    nationality: str
    researchId: str


class GetEventForSportResponseCategory(msgspec.Struct):
    name: str
    key: str


def default_team_factory():
    return GetEventForSportResponseTeam(
        name="greyhounds",
        key="greyhounds",
        abbreviation="greyhounds",
        nationality="greyhounds",
        researchId="greyhounds"
    )

class GetEventForSportResponseEvent(msgspec.Struct):
    """ A python class that represents an Event in the GetEventsForSportResponse """
    id: int = msgspec.field(name="id")
    status: str = msgspec.field(name="status")
    markets: Dict[str, GetEventForSportResponseMarket] = msgspec.field(name="markets")
    name: str
    key: str
    cutoff_time: str = msgspec.field(name="cutoffTime")
    event_type: str = msgspec.field(name="type")
    # ToDo: set default only if home or away is None
    # Greyhounds is the only sport that doesn't have a home and away team so we use a factory to set defaults.
    home: Optional[GetEventForSportResponseTeam] = msgspec.field(name="home")
    away: Optional[GetEventForSportResponseTeam] = msgspec.field(name="away")

    def __post_init__(self):
        if self.home is None:
            self.home = GetEventForSportResponseTeam(name="greyhounds", key="greyhounds", abbreviation="greyhounds",
                                                     nationality="greyhounds", researchId="greyhounds")
            # raise ValueError("`low` may not be greater than `high`")
        if self.away is None:
            self.away = GetEventForSportResponseTeam(name="greyhounds", key="greyhounds", abbreviation="greyhounds",
                                                     nationality="greyhounds", researchId="greyhounds")

    # def __init__(self, **kwargs):
    #     super().__init__(**kwargs)
    #     if self.home is None:
    #         self.home = GetEventForSportResponseTeam(name="greyhounds", key="greyhounds", abbreviation="greyhounds",
    #                                                  nationality="greyhounds", researchId="greyhounds")
    #     if self.away is None:
    #         self.away = GetEventForSportResponseTeam(name="greyhounds", key="greyhounds", abbreviation="greyhounds",
    #                                                  nationality="greyhounds", researchId="greyhounds")


class GetEventForSportResponseSport(msgspec.Struct):
    """ A python class that represents a sport in the GetEventsForSportResponse """
    name: str
    key: str


class GetEventForSportResponseCompetition(msgspec.Struct):
    """ A python class that represents a competition in the GetEventsForSportResponse """
    name: str
    key: str
    sport: GetEventForSportResponseSport = msgspec.field(name="sport")
    events: List[GetEventForSportResponseEvent] = msgspec.field(name="events")
    category: GetEventForSportResponseCategory = msgspec.field(name="category")


class GetEventsForSportResponse(msgspec.Struct):
    """
    CloudbetAPIEventSport

    This is the response from the API when calling the /v1/events/sport/{sport_key} endpoint

    Models involved in the response:
    - Competition
    - Event
    - Market
    - Selection
    """
    competitions: List[GetEventForSportResponseCompetition] = msgspec.field(name="competitions")


class GetAccountInfoResponse(msgspec.Struct):
    """GetAccountInfoResponse

    This is the model from the API when calling the /v1/account/info endpoint

    https://www.cloudbet.com/api/?urls.primaryName=Account#/PlayerAccount/accountInfo

    """
    uuid: str
    email: str
    nickname: str


class GetSportsResponseSport(msgspec.Struct):
    """Model for the Sport object in the GetSportsResponse"""
    name: str
    key: str
    competition_count: int = msgspec.field(name="competitionCount")
    event_count: int = msgspec.field(name="eventCount")


class GetSportsResponse(msgspec.Struct):
    """GetSportsResponse

    This is the response from the API when calling the /v1/sports endpoint
    """
    # the field can be optional
    sports: List[GetSportsResponseSport] = msgspec.field(name="sports")


class CloudbetSelection(msgspec.Struct):
    """CloudbetSelection"""

    outcome: str
    params: str
    price: float
    min_stake: float
    max_stake: float
    probability: float
    status: str
    side: str


class CloudbetSide(Enum):
    """CloudbetSide"""

    BACK = "BACK"
    LAY = "LAY"


class ExecutionStatus(Enum):
    """ExecutionStatus"""

    PENDING = "PENDING"
    EXECUTABLE = "EXECUTABLE"
    EXECUTION_COMPLETE = "EXECUTION_COMPLETE"
    EXPIRED = "EXPIRED"


class PersistenceType(Enum):
    """PersistenceType"""

    LAPSE = "LAPSE"
    PERSIST = "PERSIST"
    MARKET_ON_CLOSE = "MARKET_ON_CLOSE"


class OrderType(Enum):
    """OrderType"""

    LIMIT = "LIMIT"
    LIMIT_ON_CLOSE = "LIMIT_ON_CLOSE"
    MARKET_ON_CLOSE = "MARKET_ON_CLOSE"
    # Deprecated
    MARKET_AT_THE_CLOSE = "MARKET_AT_THE_CLOSE"
    LIMIT_AT_THE_CLOSE = "LIMIT_AT_THE_CLOSE"


class BetOutcome(Enum):
    """BetOutcome"""

    WON = "WON"
    LOST = "LOST"


class ClearedOrder(msgspec.Struct):
    """ClearedOrder"""

    eventTypeId: str
    eventId: str
    marketId: str
    selectionId: int
    handicap: float
    betId: str
    placedDate: str
    persistenceType: PersistenceType
    orderType: OrderType
    side: CloudbetSide
    betOutcome: BetOutcome
    priceRequested: float
    settledDate: str
    lastMatchedDate: str
    betCount: int
    priceMatched: float
    priceReduced: bool
    sizeSettled: float
    profit: float
    customerOrderRef: Optional[str] = None
    customerStrategyRef: Optional[str] = None


class ClearedOrdersResponse(msgspec.Struct):
    """ClearedOrdersResponse"""

    clearedOrders: list[ClearedOrder]
    moreAvailable: bool
