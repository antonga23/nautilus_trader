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

import asyncio
import datetime
import time
import pathlib
import ssl
import uuid
from functools import lru_cache
from typing import Optional, List, Union

import msgspec
from aiohttp import ClientResponse
from aiohttp import ClientResponseError
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.nautilus_pyo3.network import HttpResponse
from nautilus_trader.model.currency import Currency

from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetError
from nautilus_trader.common.logging import Logger
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.network.http import HttpClient
from nautilus_trader.model.identifiers import Venue

from pathlib import Path
from dotenv import dotenv_values

from nautilus_trader.adapters.cloudbet.client.gcra_rate_limit import RateLimitStore
from nautilus_trader.adapters.cloudbet.client.schema import GetSportsResponse, GetEventsForSportResponse, \
    GetSportsResponseSport, GetEventForSportResponseEvent, Selection, GetAccountInfoResponse, default_team_factory, \
    GetLatestOddsResponse, GetEventResponse, GetFixturesResponse, GetBetResponse, GetBetHistoryResponse, SelectionSide, \
    AcceptPriceChange, GetAccountCurrencies, GetAccountBalance
from nautilus_trader.model.currencies import EUR, PLAY_EUR
import json

# load environment variables from .cloudbet_env file
env_path = str(Path().cwd() / '../.cloudbet_env')
cloudbet_secrets = dotenv_values(env_path)

# It's recommended to have one constant for the venue
VENUE = Venue("CLOUDBET")
CLOUDBET_VENUE = Venue("CLOUDBET")


class CloudbetClient(HttpClient):
    """
    Provides a HTTP client for `Cloudbet`.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop to use for asynchronous operations.
    logger : Logger
        The logger for the provider.

    Attributes
    ----------
    _api_key : Optional[str]
        The API key for the provider.
    _api_url : Optional[str]
        The API URL for the provider.
    """

    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 logger: Logger,
                 api_key: Optional[str] = None,
                 api_url: Optional[str] = None,
                 ):
        super().__init__(loop=loop,
                         logger=logger,
                         connector_kwargs={"enable_cleanup_closed": True, "force_close": True}
                         )
        _api_key_default = cloudbet_secrets['CLOUDBET_API_KEY'] if 'CLOUDBET_API_KEY' in cloudbet_secrets else None
        _api_url_default = cloudbet_secrets['CLOUDBET_API_URL'] if 'CLOUDBET_API_URL' in cloudbet_secrets else None
        self._api_key = api_key if api_key is not None else _api_key_default
        self._api_url = api_url if api_url is not None else _api_url_default
        self._account_uuid: Optional[str, uuid, None] = cloudbet_secrets[
            'CLOUDBET_UUID'] if 'CLOUDBET_UUID' in cloudbet_secrets else None
        self._currency = cloudbet_secrets[
            'CLOUDBET_CURRENCY'] if 'CLOUDBET_CURRENCY' in cloudbet_secrets else None  # in test, generaly PLAY_EUR
        self.rate_limit_store: Optional[RateLimitStore] = RateLimitStore()

    @property
    def headers(self):
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "X-API-Key": self._api_key
        }

    # @property
    # def query_paramters(self, query_param: Optional[dict]):
    # # collect all key-value pairs from the query_param dict
    #     query_paramters = {}
    #     for key, value in query_param.items():
    #         query_paramters[key] = value
    #     return query_paramters

    @property
    def currency(self) -> Union[str, Currency, None]:
        return self._currency

    async def request(self, method, url, **kwargs) -> ClientResponse:
        return await super().request(method=method, url=url, **kwargs)

    async def connect(self) -> None:
        self._log.info("Connecting..")
        await super().connect()
        self._log.info("Connected.")

    async def login(self) -> GetAccountInfoResponse:
        """
        We simulate a login by sending a GET request to the account/info endpoint from the Cloudbet Account API.
        Login to the Cloudbet API.

        https://www.cloudbet.com/api/?urls.primaryName=Account#/PlayerAccount/accountInfo

        """

        resp = await self.get(url=f"{self._api_url}/v1/account/info", headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve account details from the Cloudbet API. Response: {resp}")
            if resp.status == 429:
                time.sleep(1)  # TODO: replace with rate-limit update() and wait rate-limit.inverse
                await self.login()
            raise CloudbetAPIError(f"Failed to login to Cloudbet API. Response: {resp}")
        return msgspec.json.decode(resp.data, type=GetAccountInfoResponse)

    async def disconnect(self) -> None:
        self._log.info("Disconnecting..")
        await super().disconnect()
        self._log.info("Disconnected.")

    async def get_sports(self) -> GetSportsResponse:
        """
        Get a list of sports available for betting.

        https://www.cloudbet.com/api/?urls.primaryName=Feed#/API-Version-2.0/GetSports

        Parameters
        ----------
        """

        resp = await self.get(url=f"{self._api_url}/v2/odds/sports", headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve sports from the Cloudbet API. Response: {resp.text}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_sports()
            raise CloudbetAPIError(message=f"Failed to retrieve sports from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetSportsResponse)

    async def get_events_for_sport(self,
                                   sport_key: str,
                                   from_timestamp: int,
                                   to_timestamp: int,
                                   live: str = "false",
                                   limit: Optional[int] = 1000,
                                   markets: List[str] = None) -> GetEventsForSportResponse:
        """
        Get a list of events for a specific sport.

        https://www.cloudbet.com/api/?urls.primaryName=Feed#/API-Version-2.0/GetEvents

        Parameters
        ----------
        sport_key : str
            The key of the sport to retrieve events for.
        from_timestamp: int
            time range for upcoming events, Unix epoch time
            e.g. 1618997973
        Either live or from + to query params are REQUIRED in your request to specify a valid time-range.
        from can't be sent together with live. Also, from + to must be sent together
        to_timestamp: int
            time range for upcoming events, Unix epoch time
        live: bool
            If true, return all TRADING_LIVE events
        If false, return TRADING + PRE_TRADING event false by default live can't be sent together with from or to query params
        limit: int
            The maximum number of events to return. Default is 1000
        markets: dict
            A list of market filters to apply to the request.
            Specify each market as a separate query param; e.g. ?markets=basketball.handicap&markets=basketball.moneyline
        """
        query_params = {
            'sport': sport_key,
            'live': live,
            'from': from_timestamp,
            'to': to_timestamp,
            'limit': limit,
        }
        if markets:
            for market in markets:
                market = sport_key + "." + market
                query_params.setdefault('markets', []).append(market)
        resp = await self.get(url=f"{self._api_url}/v2/odds/events", params=query_params, headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(
                f"Failed to retrieve events for sport {sport_key} from the Cloudbet API. Response: {resp.text}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_events_for_sport(sport_key, from_timestamp, to_timestamp, live, limit, markets)
            raise CloudbetAPIError(
                f"Failed to retrieve events for sport {sport_key} from the Cloudbet API. Response: {resp.text}")
        return msgspec.json.decode(resp.data, type=GetEventsForSportResponse)

    async def load_selection(self, filters: Optional[dict] = None) -> List[List[Selection]]:
        """
        Load all selections for filtered set of  events. This is a helper function to normalise, flatten and extract
        selection related data. Implementing this function on the Cloudbet client allows us to abstract away the API

        Parameters
        ----------
        filters : dict

        example filter structure and types:
        filters = {
        sport_key: List,
        from_timestamp: int,
        to_timestamp: int,
        live: bool,
        limit: int
        }

        By default, we load all selections for all events for all sports. This is a very expensive operation and should
        be used with caution. We can filter the data by sport, time range, live events and limit the number of events

        """

        # Default filter values
        if filters is None:
            filters = {
                'from_timestamp': int(time.time()),
                'to_timestamp': int(time.time()) + 7200,
                'live': 'false',
                'limit': 2
            }

        filtered_sports: List[GetSportsResponseSport] = []
        self._log.info(f"Loading selections for {filters}")
        get_sports_response_list: GetSportsResponse = await self.get_sports()
        if 'sport_key' in filters and filters['sport_key'] is not None:
            # GetSportsResponse is a list of dicts, we need to iterate over the list and check if the sport key is in the
            # index is the key/number of the list and the value is the dict/sport
            for sport in get_sports_response_list.sports:
                if sport.key in filters['sport_key']:
                    filtered_sports.append(sport.key)
        else:
            # We want all sports
            filtered_sports = [sport.key for sport in get_sports_response_list.sports]

        # We now have a static list of sports and we need to make a request for each sport to get the events in parallel
        tasks = [self.get_events_for_sport(sport_key,
                                           from_timestamp=filters['from_timestamp'],
                                           to_timestamp=filters['to_timestamp'],
                                           live=filters['live'],
                                           limit=filters['limit']
                                           ) for sport_key in filtered_sports]
        # We now have a list of tasks that we can run in parallel
        self._log.info(f"Running {len(tasks)} tasks in parallel")
        events = await asyncio.gather(*tasks)
        # Now we have the events we can extract the selections
        selections_list : List[List[Selection]] = []
        for event in events:
            selections_list.append(self.event_to_selection(event))
        if 'market_name' in filters:
            for selections in selections_list:
                filtered_selections : List[Selection] = [selection for selection in selections if
                              selection.market_name.split('.')[-1] in filters['market_name']]
                selections_list[selections_list.index(selections)] = filtered_selections
        return selections_list

    async def get_latest_odds(self, event_id: Union[str, int], market_url: str) -> GetLatestOddsResponse:
        """
        Obtain the latest odds for a selection based on market key, outcome and params.

        https://www.cloudbet.com/api/?urls.primaryName=Feed#/API-Version-2.0/PostLine

        Note: You can only request odds for events that haven't resulted/still have trading markets

        Parameters
        ----------
        event_id : Union[str, int]
            The event id for the selection
        market_url : str
            The market url  is composed of the marketKey/outcome and where applicable the params
        """

        # Serialise the dictionary to a JSON string Note: We need to manually serialise the dictionary as a JSON
        # string as the HTTP client request doesn't handle serialisation correctly
        data_json = json.dumps({
            "eventId": str(event_id),
            "marketUrl": str(market_url)
        })
        resp = await self.post(url=f"{self._api_url}/v2/odds/lines",
                               headers=self.headers, data=data_json)
        if not (200 <= resp.status < 300):
            self._log.error(
                f"Failed to retrieve latest odds for event {event_id} from the Cloudbet API. Response: {resp.text}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_latest_odds(event_id, market_url)
            raise CloudbetAPIError(message=f"Failed to retrieve latests odds from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetLatestOddsResponse)

    async def get_event(self, event_id: int, sport_key: Optional[str] = None, market_filter: Optional[set[str]] = None):
        """
        Obtain the event data for a given event id.

        https://www.cloudbet.com/api/?urls.primaryName=Feed#/API-Version-2.0/GetEvent

        Parameters
        ----------
        event_id : int
            The event id for the selection
        market_filter : Optional[set[str]]
            A set of market keys to filter the event data by
        """

        query_params = {
        }
        if market_filter and sport_key:
            for market in market_filter:
                market = sport_key + "." + market
                query_params.setdefault('markets', []).append(market)
        resp = await self.get(url=f"{self._api_url}/v2/odds/events/{event_id}", params=query_params,
                              headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve event from the Cloudbet API. Response: {resp.text}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_event(event_id, sport_key, market_filter)
            raise CloudbetAPIError(message=f"Failed to retrieve event from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetEventResponse)

    async def get_fixtures(self, sport_key: str, from_timestamp: int, to_timestamp: int, limit: Optional[int] = 100):
        """
        Obtain the fixtures for a given sport without market & selection level data.

        https://www.cloudbet.com/api/?urls.primaryName=Feed#/API-Version-2.0/GetFixtures

        Parameters
        ----------
        sport_key : str
            The sport key for the selection
        from_timestamp : int
            The start timestamp for the fixtures/events in  Unix epoch time, i.e. seconds since January 1, 1970 midnight UTC; e.g. 1618997973
        to_timestamp : int
            The end timestamp for the fixtures/events in  Unix epoch time, i.e. seconds since January 1, 1970 midnight UTC; e.g. 1618997973
        limit : int
            The maximum number of fixtures/events to return
        """
        query_params = {
            'sport': sport_key,
            'from': from_timestamp,
            'to': to_timestamp,
            'limit': limit
        }
        resp = await self.get(url=f"{self._api_url}/v2/odds/fixtures", params=query_params, headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve fixtures from the Cloudbet API. Response: {resp.text}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_fixtures(sport_key, from_timestamp, to_timestamp, limit)
            raise CloudbetAPIError(message=f"Failed to retrieve fixtures from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetFixturesResponse)

    async def get_bet_status(self, reference_id: str) -> GetBetResponse:
        """
        Obtain the bet data for a given reference id.

        https://www.cloudbet.com/api/?urls.primaryName=Trading#/Trading/BetStatus

        Parameters
        ----------
        reference_id : str
            The reference id for the bet
        """
        resp = await self.get(url=f"{self._api_url}/v3/bets/{reference_id}/status", headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve bet from the Cloudbet API. Response: {resp}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_bet_status(reference_id)
            raise CloudbetAPIError(message=f"Failed to retrieve bet from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetBetResponse)

    async def get_bet_history(self, from_date: str, to_date: str, limit: Optional[int] = 100,
                              offset: Optional[int] = 0) -> GetBetHistoryResponse:
        """
        Obtain the bet history for a given date range.

        https://www.cloudbet.com/api/?urls.primaryName=Trading#/Trading/BetHistory

        Parameters
        ----------
        from_date : str
            The start date for the bet history in ISO RFC3339 format eg. "2023-09-11T00:00:00Z"
            cutoff time in string format "2006-01-02T15:04:05Z"
        to_date : str
            The end date for the bet history in ISO RFC3339 format eg. "2023-09-11T00:00:00Z"
        limit : int, Default is 100
            The maximum number of bets to return in one request, equal or less than 1000
        offset : int, Default is 0
            The offset for the bet history (starting number for latest records)
        """

        PyCondition.in_range_int(limit, 1, 1000, 'limit')
        PyCondition.not_negative_int(offset, 'offset')
        # TODO: check if from_date and to_date are valid dates and start date is before end date
        query_params = {
            'fromDate': from_date,
            'toDate': to_date,
            'limit': limit,
            'offset': offset
        }
        resp = await self.get(url=f"{self._api_url}/v4/bets/history", params=query_params, headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve bet history from the Cloudbet API. Response: {resp}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_bet_history(from_date, to_date, limit, offset)
            raise CloudbetAPIError(message=f"Failed to retrieve bet history from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetBetHistoryResponse)

    async def place_bets(self, event_id: Union[int, str], market_url: str, price: Union[str, float],
                         side: SelectionSide, stake: Union[str, float], reference_id: Optional[str] = None,
                         currency: Optional[str] = None,
                         accept_price_change: AcceptPriceChange = AcceptPriceChange.NONE) -> GetBetResponse:
        """
        Place a bet on a given event id and market url.

        https://www.cloudbet.com/api/?urls.primaryName=Trading#/Trading/PlaceBet
        Parameters
        ----------
        event_id : int
            The event id for the bet
        market_url : str
            The market url  is composed of the marketKey/outcome and where applicable the params
        price : Union[str, float]
            The price for the bet
        side : SelectionSide
            The side for the bet eg BACK or LAY; yes or no
        stake : Union[str, float]
            The stake for the bet (in Currency)
        reference_id : str, optional
            Reference ID, randomly generated request id to allow idempotent calls. Required to be in the UUID format.
            NB: only pass this parameter to retry a failed request.
        currency : Currency, optional
            The currency for the bet
            NB!: only pass this parameter when betting a different currency than the one set in the client.
        accept_price_change : AcceptPriceChange , Default is AcceptPriceChange.NONE
            Accept price change for the bet. ENUM values: NONE, ALL, BETTER
        """
        if reference_id is None:
            reference_id = str(uuid.uuid4())
        if currency is None:
            # currency = self.currency
            currency = PLAY_EUR
        # TODO: replace with BetRequest Type
        json_data = msgspec.json.encode({
            'acceptPriceChange': accept_price_change.value,
            'eventId': str(event_id),  # have to explictly cast to str
            'marketUrl': market_url,
            'price': str(price),  # have to explictly cast to str
            'side': side,
            'stake': str(stake),  # have to explictly cast to str
            'currency': currency,
            'referenceId': reference_id
        })
        resp = await self.post(url=f"{self._api_url}/v3/bets/place", headers=self.headers, data=json_data)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve latests odds from the Cloudbet API. Response: {resp}")
            if resp.status == 429:
                time.sleep(1)
                await self.place_bets(event_id, market_url, price, side, stake, reference_id, currency,
                                      accept_price_change)
            raise CloudbetAPIError(message=f"Failed to retrieve latests from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetBetResponse)

    async def get_account_currencies(self) -> GetAccountCurrencies:
        """
        Get a list of enabled currencies on the account.

        https://www.cloudbet.com/api/?urls.primaryName=Account#/PlayerAccount/accountCurrencies
        """

        resp = await self.get(url=f"{self._api_url}/v1/account/currencies", headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(f"Failed to retrieve account currencies from the Cloudbet API. Response: {resp}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_account_currencies()
            raise CloudbetAPIError(message=f"Failed to retrieve currencies from the Cloudbet API.", code=resp.status)
        return msgspec.json.decode(resp.data, type=GetAccountCurrencies)

    async def get_balances(self, currency: Union[
        str, Currency]):  # TODO: replace str type with a "native" CloudbetCurrencyType
        """
        Get the balance for a requested currency..

        https://www.cloudbet.com/api/?urls.primaryName=Account#/PlayerAccount/accountBalance


        Parameters
        ----------

        currency: Union[str, Currency]
        """

        resp = await self.get(url=f"{self._api_url}/v1/account/currencies/{currency}/balance", headers=self.headers)
        if not (200 <= resp.status < 300):
            self._log.error(
                f"Failed to retrieve balance for currency {currency} from the Cloudbet API. Response: {resp}")
            if resp.status == 429:
                time.sleep(1)
                await self.get_balances(currency)
            raise CloudbetAPIError(
                message=f"Failed to retrieve balance for currency {currency} from the Cloudbet API. Response:",
                code=resp.status)
        return msgspec.json.decode(resp.data, type=GetAccountBalance)

    ############################################################################
    # HELPER METHODS
    ############################################################################

    @staticmethod
    # @lru_cache(maxsize=128)
    # ToDo: Use multithreading to speed up this function, we can extract the data in parallel otherwise we have to
    #  wait for each event to be processed
    def event_to_selection(event: GetEventsForSportResponse) -> List[Selection]:
        selections_list: List = []
        for competition in event.competitions:
            competition_name = competition.name
            competition_key = competition.key
            sport_name = competition.sport.name
            sport_key = competition.sport.key
            for event in competition.events:
                # Default value if home/away team is None
                if event.home is None:
                    event.home = default_team_factory()
                if event.away is None:
                    event.away = default_team_factory()
                event_id = event.id
                home_name = event.home.name
                home_key = event.home.key
                away_name = event.away.name
                away_key = event.away.key
                status = event.status
                event_name = event.name
                cutoff_time = event.cutoff_time
                # Iterate over all the markets
                for market_name, market_value in event.markets.items():
                    # Iterate over all the submarkets in the current market
                    for submarket_period, submarket_value in market_value.submarkets.items():
                        # Iterate over all the selections in the current submarket
                        for selection in submarket_value.selections:
                            # Add a dictionary with the market name, submarket period and selection data to the extracted data list
                            selections_list.append(Selection(
                                competition_name=competition_name,
                                competition_key=competition_key,
                                sport_name=sport_name,
                                sport_key=sport_key,
                                event_id=event_id,
                                home_name=home_name,
                                home_key=home_key,
                                away_name=away_name,
                                away_key=away_key,
                                status=status,
                                market_name=market_name,
                                submarket_name=market_name + "_" + submarket_period,
                                submarket_period=submarket_period,
                                sequence=submarket_value.sequence,
                                outcome=selection.outcome,
                                price=selection.price,
                                min_stake=selection.minStake,
                                max_stake=selection.maxStake,
                                probability=selection.probability,
                                selection_status=selection.status,
                                side=selection.side,
                                cutoff_time=cutoff_time,
                                event_name=event_name,
                                params=selection.params))
        return selections_list
