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
from nautilus_trader.model.currency import Currency
from nautilus_trader.model.currencies import EUR


# ToDO: use selectionFilters data type/model to filter selections in cloudbet adapter
class SelectionFilters(msgspec.Struct):
    """ Filters to apply to the selection """
    sport_key: Optional[str]
    competition_key: Optional[str]
    event_id: Optional[str]
    market_name: Optional[str]
    submarket_name: Optional[str]
    submarket_period: Optional[str]
    sequence: Optional[str]
    outcome: Optional[str]


class EventId(msgspec.Struct):
    home_team: str
    away_team: str
    sport_key: str
    competition_key: str


class MarketId(msgspec.Struct):
    event_id: EventId
    market_name: str
    submarket_name: Optional[str]
    submarket_period: Optional[str]


class SelectionId(msgspec.Struct):
    event_id: int
    market_name: str
    outcome: str
    params: Optional[str] = None


class EventStatus(Enum):
    """EventStatus"""
    PRE_TRADING = "PRE_TRADING"
    TRADING = "TRADING"
    TRADING_LIVE = "TRADING_LIVE"
    RESULTED = "RESULTED"
    INTERRUPTED = "INTERRUPTED"
    AWAITING_RESULTS = "AWAITING_RESULTS"
    POST_TRADING = "POST_TRADING"
    CANCELLED = "CANCELLED"


class SelectionStatus(Enum):
    DISABLED = "SELECTION_DISABLED"
    ENABLED = "SELECTION_ENABLED"


class SelectionSide(Enum):
    BACK = "BACK"
    LAY = "LAY"
    YES = "YES"
    NO = "NO"
    UNDEFINED = "undefined-side"
    EVEN = "Even"
    ODD = "Odd"


class AcceptPriceChange(Enum):
    NONE, ALL, BETTER = "NONE", "ALL", "BETTER"


class BetStatus(Enum):
    INTERNAL_SERVER_ERROR = "unexpected error at server side. Our engineering team is informed of the issue. Please try again or contact our customer support if this problem persists"
    DUPLICATE_REQUEST = "duplicated request with same Reference ID was posted, this is due to idempotent request handling. If you want to resubmit this bet. Please add a new Reference ID"
    MALFORMED_REQUEST = "the request was not sent as per the expected request structure"
    PRICE_ABOVE_MARKET = "bet price requested was above the current market price. Please reference price from response payload about the corrected value for retry"
    INSUFFICIENT_FUNDS = "account doesn't have sufficient funds in the requested currency"
    STAKE_ABOVE_MAX = "stake requested was above the current maximum stake on a selection. Please reference stake from response payload about the corrected value for retry"
    STAKE_BELOW_MIN = "stake requested was below the current minimum stake on a selection. Please reference stake from response payload about the corrected value for retry"
    LIABILITY_LIMIT_EXCEEDED = "your current liability limit on this event was exceeded. Please reference stake from response payload about the corrected value for retry"
    MARKET_SUSPENDED = "you attempted to bet on an inactive selection"
    ACCEPTED = "your bet was accepted successfully"
    PENDING_ACCEPTANCE = "your bet is being processed by the system. Please check the bet status again periodically to get bet status updates"
    RESTRICTED = "your current account settings don't allow you to bet on this event. Restrictions will be lifted automatically as your account attains tenure and trust. Please contact customer support if you believe you qualify and we will review your account."
    VERIFICATION_REQUIRED = "your account needs to be verified using our KYC procedures. Please contact customer support for more details"
    WIN = "you won the bet"
    LOSS = "you lost the bet"
    PUSH = "market not applicable to result, e.g. draw on 2way, handicap"
    HALF_WIN = "half win, e.g. on a handicap market"
    HALF_LOSS = "half loss, e.g. on a handicap market"
    PARTIAL = "partial win, including dead heat result"


class Selection(msgspec.Struct, kw_only=True):
    """ A selection is a possible outcome of a market and is the primary unit of betting
    In essence a selection is a flattened representation of the GetEventsForSportResponse
    """
    competition_name: Optional[str] = None
    competition_key: Optional[str] = None
    sport_name: Optional[str] = None
    sport_key: Optional[str] = None
    event_id: int
    home_name: Optional[str] = None
    home_key: Optional[str] = None
    away_name: Optional[str] = None
    away_key: Optional[str] = None
    # ToDo: create enum for status
    status: str
    market_name: Optional[str] = None
    submarket_name: Optional[str] = None
    submarket_period: Optional[str] = None
    sequence: Optional[str] = None
    outcome: str
    price: float
    min_stake: float
    max_stake: float
    probability: Optional[float] = None
    selection_status: SelectionStatus
    # ToDo: change type to SelectionSide
    side: str
    cutoff_time: Optional[str] = None
    event_name: Optional[str] = None
    params: Optional[str]
    currency: Currency = EUR

    def to_dict(self):
        return {f: getattr(self, f) for f in self.__struct_fields__}


class TeamIdentifier(msgspec.Struct):
    """ TeamIdentifier identifies a team competitor for a given event"""
    abbreviation: str
    key: str
    name: str
    nationality: str


class Identifier(msgspec.Struct):
    name: str
    key: str


class FixtureListEntry(msgspec.Struct):
    away: TeamIdentifier
    # event cutoff time in string format "2006-01-02T15:04:05Z07:00" (RFC3339)
    cutoff_time: str = msgspec.field(name="cutoffTime")
    home: TeamIdentifier
    # event_id => unqiue identifier for the event
    id: int
    key: str
    name: str
    status: EventStatus


class FixturesCompetition(msgspec.Struct):
    """ A python class that represents a competition in the GetFixturesResponse """
    category: Identifier
    events: List[FixtureListEntry]
    key: str
    name: str


class CompetitionWithCategory(msgspec.Struct):
    """ CompetitionWithCategory is used for the /sports/events/{id} endpoint to link events with competitions """
    category: Identifier
    key: str
    name: str
    events: Union[List[FixtureListEntry], None] = None


class GetLatestOddsResponse(msgspec.Struct):
    """ A python class that represents the response from the get_latest_odds endpoint
    """
    max_stake: float = msgspec.field(name="maxStake")
    min_stake: float = msgspec.field(name="minStake")
    outcome: str
    params: str
    price: float
    probability: float
    side: SelectionSide
    status: SelectionStatus


class SelectionModel(msgspec.Struct):
    outcome: str
    params: str
    price: float
    # TODO: refactor minStake and maxStake to min_stake and max_stake
    # eg min_stake: float = msgspec.field(name="minStake")
    minStake: float
    maxStake: float
    probability: float
    status: str
    side: str


class SubmarketModel(msgspec.Struct):
    sequence: str
    selections: List[SelectionModel]


class MarketModel(msgspec.Struct):
    submarkets: Dict[str, SubmarketModel]


class GetEventForSportResponseTeam(msgspec.Struct):
    name: str
    key: str
    abbreviation: str
    nationality: str
    researchId: str


class  GetEventResponse(msgspec.Struct):
    """ A python class that represents the response from the get_event endpoint
    """
    sequence: str
    # event_id => unqiue identifier for the event
    id: int
    sport: Identifier
    competition: CompetitionWithCategory
    home: TeamIdentifier
    away: TeamIdentifier
    status: EventStatus
    markets: Dict[str, MarketModel] = msgspec.field(name="markets")
    name: str
    key: str
    # event cutoff time in string format "2006-01-02T15:04:05Z07:00" (RFC3339)
    cutoff_time: str = msgspec.field(name="cutoffTime")
    type: str = msgspec.field(name="type")
    end_time: str = msgspec.field(name="endTime")
    grading_duration: Optional[int] = msgspec.field(name="gradingDuration")


class GetFixturesResponse(msgspec.Struct):
    """ A python class that represents the response from the get_fixtures endpoint
    """
    competition: List[CompetitionWithCategory] = msgspec.field(name="competitions")


class GetBetResponse(msgspec.Struct):
    """ BetResponse presents response upon place bet request"""
    category_key: str = msgspec.field(name="categoryKey")
    competition_id: int = msgspec.field(name="competitionId")
    create_time: str = msgspec.field(name="createTime")
    currency: str = msgspec.field(name="currency")
    customer_reference: str = msgspec.field(name="customerReference")
    error: str = msgspec.field(name="error")
    event_id: int = msgspec.field(name="eventId")
    event_name: str = msgspec.field(name="eventName")
    market_url: str = msgspec.field(name="marketUrl")
    price: Union[float, str] = msgspec.field(name="price")
    reference_id: str = msgspec.field(name="referenceId")
    return_amount: Union[float, str] = msgspec.field(name="returnAmount")
    side: SelectionSide = msgspec.field(name="side")
    sport_key: str = msgspec.field(name="sportsKey")
    stake: Union[float, str] = msgspec.field(name="stake")
    status: BetStatus = msgspec.field(name="status")


class GetBetHistoryResponse(msgspec.Struct):
    """ BetHistoryResponse presents response upon get bet history request"""
    bets: List[GetBetResponse] = msgspec.field(name="bets")
    total_bets: int = msgspec.field(name="totalBets")


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
    # markets: Dict[str, GetEventForSportResponseMarket] = msgspec.field(name="markets")
    markets: Dict[str, MarketModel] = msgspec.field(name="markets")
    name: str
    key: str
    cutoff_time: str = msgspec.field(name="cutoffTime")
    event_type: str = msgspec.field(name="type")
    # ToDo: set default only if home or away is None
    # Greyhounds is the only sport that doesn't have a home and away team so we use a factory to set defaults.
    home: Optional[TeamIdentifier] = msgspec.field(name="home")
    away: Optional[TeamIdentifier] = msgspec.field(name="away")

    def __post_init__(self):
        if self.home is None:
            self.home = TeamIdentifier(name="greyhounds", key="greyhounds", abbreviation="greyhounds",
                                       nationality="greyhounds")
            # raise ValueError("`low` may not be greater than `high`")
        if self.away is None:
            self.away = TeamIdentifier(name="greyhounds", key="greyhounds", abbreviation="greyhounds",
                                       nationality="greyhounds")

    # TODO: test parsing greyhounds events
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
    sport: Identifier = msgspec.field(name="sport")
    events: List[GetEventForSportResponseEvent] = msgspec.field(name="events")
    category: Identifier = msgspec.field(name="category")


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


class CloudbetSide(Enum):
    """CloudbetSide"""

    BACK = "BACK"
    LAY = "LAY"


class TradingStatus(Enum):
    """TradingStatus"""
    # TODO: add all trading status


class ExecutionStatus(Enum):
    """ExecutionStatus"""

    PENDING = "PENDING"
    EXECUTABLE = "EXECUTABLE"
    EXECUTION_COMPLETE = "EXECUTION_COMPLETE"
    EXPIRED = "EXPIRED"


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
