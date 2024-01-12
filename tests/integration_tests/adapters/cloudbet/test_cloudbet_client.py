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
import json
import os
import random
from collections import namedtuple
from typing import List
from unittest.mock import patch

import msgspec
import pytest
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.network.http import ClientResponse

from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.core import uuid

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import Selection, GetEventsForSportResponse, GetEventResponse, \
    GetFixturesResponse, GetLatestOddsResponse, SelectionSide, BetStatus, AcceptPriceChange, GetBetResponse, \
    GetBetHistoryResponse, GetAccountCurrencies, GetAccountBalance
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from tests.integration_tests.adapters.cloudbet.conftest import cloudbet_client
# /home/alatha/Desktop/nautilus_trader/tests/integration_tests/adapters/cloudbet/test_kit.py
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetTestStubs, test_api_key, test_api_url, \
    CloudbetResponses, DataGenerator
from asyncmock import AsyncMock
import asynctest


class TestCloudbetClient:
    def setup(self):
        # Fixture Setup
        self.loop = asyncio.get_event_loop()
        self.clock = LiveClock()
        self.logger = Logger(clock=self.clock, bypass=True)
        self.client = CloudbetClient(self.loop, self.logger)
        # we explicitly need to set the api key and secret to test credentials
        self.client._api_key = test_api_key
        self.client._api_url = test_api_url

    async def teardown(self):
        # Fixture Teardown
        await self.client.disconnect()

    @pytest.mark.asyncio()
    async def test_client_init(self):
        await self.client.connect()
        # we need to test if the client is initialized correctly by calling the connect method
        assert self.client.connected is True

    @pytest.mark.asyncio()
    async def test_client_login(self):
        await self.client.connect()
        # assert self.client._api_key is not None
        assert self.client._api_url == test_api_url
        assert self.client._api_key == test_api_key
        result = await self.client.login()
        expected = CloudbetResponses.login()
        # ToDo: assert some known values from the response
        assert result == expected

    @pytest.mark.asyncio()
    async def test_client_get_sports(self):
        await self.client.connect()
        result = await self.client.get_sports()
        expected = CloudbetResponses.get_sports()
        assert type(result) == type(expected)

    @pytest.mark.asyncio()
    async def test_client_get_events_for_sports(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "tennis", "baseball", "basketball"]
        sport_key = random.choice(sports)
        result = await self.client.get_events_for_sport(
            sport_key,
            current_timestamp,
            timestamp_48h,
            limit=5
        )
        expected = CloudbetResponses.get_events_for_sport()
        # we're only interested in verifying the type of the response matches the schema
        assert type(result) == type(expected)

    @pytest.mark.asyncio()
    async def test_event_to_selection(self):
        event = CloudbetResponses.get_events_for_sport()
        result: list[Selection] = CloudbetClient.event_to_selection(event)
        # we're only interested in verifying the type of the response matches the schema
        # for each selection in the result, we want to verify that the type is correct
        for selection in result:
            assert type(selection) == Selection

    @pytest.mark.asyncio()
    async def test_load_selection_when_api_call_fails(self):
        await self.client.connect()
        self.client.get_sports = AsyncMock(side_effect=Exception())
        try:
            result = await self.client.load_selection()
            # assert that an error is thrown
        except Exception:
            pass

    @pytest.mark.asyncio()
    async def test_get_events_for_sport_executed_asynchronously(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        filters = {
            'sport_key': ['soccer', 'basketball', 'baseball', 'tennis'],
            'from_timestamp': current_timestamp,
            'to_timestamp': timestamp_48h,
            'live': 'false',
            'limit': 10
        }

        # Mock get_events_for_sport function
        self.client.get_events_for_sport = asynctest.CoroutineMock(
            return_value=CloudbetResponses.get_events_for_sport())

        # Run load_selection function
        await self.client.load_selection(filters)

        # Verify get_events_for_sport function was called for each sport
        self.client.get_events_for_sport.assert_has_calls([
            asynctest.call('soccer', from_timestamp=filters['from_timestamp'], to_timestamp=filters['to_timestamp'],
                           live=filters['live'], limit=filters['limit']),
            asynctest.call('basketball', from_timestamp=filters['from_timestamp'], to_timestamp=filters['to_timestamp'],
                           live=filters['live'], limit=filters['limit']),
            asynctest.call('baseball', from_timestamp=filters['from_timestamp'], to_timestamp=filters['to_timestamp'],
                           live=filters['live'], limit=filters['limit']),
            asynctest.call('tennis', from_timestamp=filters['from_timestamp'], to_timestamp=filters['to_timestamp'],
                           live=filters['live'], limit=filters['limit'])
        ], any_order=True)  # any_order=True means we don't care about the order of the calls
        assert self.client.get_events_for_sport.call_count == len(filters['sport_key'])

    @pytest.mark.asyncio()
    async def test_get_events_for_sport_market_filter(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "tennis", "baseball", "basketball"]
        sport_key = random.choice(sports)
        markets = ["moneyline", "spread", "total", "handicap", "correct_score", "winner"]
        result = await self.client.get_events_for_sport(
            sport_key,
            current_timestamp,
            timestamp_48h,
            limit=5,
            markets=markets
        )
        expected = CloudbetResponses.get_events_for_sport()
        assert type(result) == type(expected)

    @pytest.mark.asyncio()
    async def test_load_selection(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "tennis", "baseball", "basketball", "football", "ice_hockey", "volleyball", "handball",
                  "american-football", "greyhounds"]
        sport_key_list = random.sample(sports, k=3)
        result: list[list[Selection]] = await self.client.load_selection(
            filters={
                'sport_key': sport_key_list,
                'from_timestamp': current_timestamp,
                'to_timestamp': timestamp_48h,
                'live': 'false',
                'limit': 10
            }
        )
        for selection_list in result:
            # check that the selection list is a non-empty list
            if len(selection_list) > 0:
                # check that the type of the selection is correct
                for selection in selection_list:
                    assert type(selection) == Selection
            else:
                continue

    @pytest.mark.asyncio()
    async def test_load_selection_with_invalid_filters(self):
        await self.client.connect()
        filters = {
            'sport_key': ['non-existing-sport'],
            'from_timestamp': -1,
            'to_timestamp': -1,
            'live': 'not-a-boolean',
            'limit': 'not-an-integer'
        }
        try:
            result = await self.client.load_selection(filters)
            # assert that an error is thrown or result is empty
            assert len(result) == 0
        except ValueError:
            pass

    @pytest.mark.asyncio()
    async def test_load_selection_with_no_filters(self):
        await self.client.connect()
        result = await self.client.load_selection()
        # assert that result contains all selections for all sports and events
        for selection_list in result:
            # check that the selection list is a non-empty list
            if len(selection_list) > 0:
                # check that the type of the selection is correct
                for selection in selection_list:
                    assert type(selection) == Selection
            else:
                continue

    @pytest.mark.asyncio()
    @patch.object(CloudbetClient, 'get_events_for_sport', new_callable=AsyncMock)
    async def test_load_selection_with_market_filters(self, mock_get_events_for_sport):
        # Arrange
        await self.client.connect()
        sport_key = 'baseball'
        filters = {
            'sport_key': sport_key,
            'from_timestamp': 0,
            'to_timestamp': 0,
            'live': 'false',
            'limit': 0,
            'market_name': ['moneyline', 'totals'],
        }
        mock_get_events_for_sport.return_value = CloudbetResponses.get_events_for_sport(sport_key=sport_key)
        # Act
        result: List[List[Selection]] = await self.client.load_selection(filters)
        # assert that result only contains the filtered selections for the specified sport and market name
        for selections in result:
                for selection in selections:
                    assert selection.market_name.split('.')[-1] in filters['market_name']

    @pytest.mark.asyncio()
    async def test_get_fixture_success(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "tennis", "baseball", "basketball"]
        sport_key = random.choice(sports)
        result = await self.client.get_fixtures(sport_key, current_timestamp, timestamp_48h, limit=100)
        # Check that the response is a GetFixtureResponse instance
        assert isinstance(result, GetFixturesResponse)

    @pytest.mark.asyncio()
    async def test_get_event_success(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        # sports = ["soccer", "tennis", "baseball", "basketball"]
        sports = ["soccer"]
        sport_key = random.choice(sports)
        fixtures: GetFixturesResponse = await self.client.get_fixtures(sport_key, current_timestamp, timestamp_48h,
                                                                       limit=100)
        if len(fixtures.competition) > 0:
            current_competition = random.choice(fixtures.competition)
            if len(current_competition.events) > 0:
                current_event = random.choice(current_competition.events)
                assert current_event.id is not None
                result = await self.client.get_event(current_event.id)
                assert isinstance(result, GetEventResponse)
            else:
                print("No events found for competition:", fixtures)

    @pytest.mark.asyncio()
    async def test_get_event_market_filter(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        # sports = ["soccer", "tennis", "baseball", "basketball"]
        sports = ["soccer"]
        sport_key = random.choice(sports)
        market_filters = ["moneyline", "spread", "total", "handicap", "correct_score", "winner"]
        fixtures: GetFixturesResponse = await self.client.get_fixtures(sport_key, current_timestamp, timestamp_48h,
                                                                       limit=100)
        if len(fixtures.competition) > 0:
            current_competition = random.choice(fixtures.competition)
            if len(current_competition.events) > 0:
                current_event = random.choice(current_competition.events)
                assert current_event.id is not None
                result = await self.client.get_event(current_event.id, sport_key, market_filters)
                assert isinstance(result, GetEventResponse)
                if len(result.markets) > 0:
                    for market in result.markets:
                        # check markets fetched are in the market_filters list
                        # market format => {sport}.{market_name}
                        assert market.split('.')[1] in market_filters
                else:
                    print("No markets found for event:", current_event)

            else:
                print("No events found for competition:", fixtures)

    @pytest.mark.asyncio()
    @patch.object(CloudbetClient, 'get_events_for_sport', new_callable=AsyncMock)
    @patch.object(CloudbetClient, 'get_latest_odds', new_callable=AsyncMock)
    async def test_get_latest_odds(self, mock_get_latest_odds, mock_get_events_for_sport):
        # Arrange
        # TODO: refactor this to a fixture
        # live instrument_id setup >>>
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sport_key = random.choice(["soccer", "tennis", "baseball", "basketball"])
        mock_get_events_for_sport.return_value = CloudbetResponses.get_events_for_sport(sport_key=sport_key)
        mock_get_latest_odds.return_value = CloudbetResponses.get_latest_odds()
        ## Act
        event = await self.client.get_events_for_sport(
            sport_key,
            current_timestamp,
            timestamp_48h,
            limit=5
        )
        selections: List[Selection] = self.client.event_to_selection(event)
        selection = random.choice(selections)
        # Replace these with valid event_id and market_url for testing
        event_id = selection.event_id
        market_url = selection.market_name + '/' + selection.outcome + '?' + selection.params if selection.params is not None else selection.market_name + '/' + selection.outcome
        # await self.client.connect()
        result : GetLatestOddsResponse = await self.client.get_latest_odds(event_id, market_url)
        # Check that the response is a GetLatestOddsResponse instance
        assert isinstance(result, GetLatestOddsResponse)

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get_events_for_sport', new_callable=AsyncMock)
    @patch.object(CloudbetClient, 'place_bets', new_callable=AsyncMock)
    async def test_place_bet_accepted(self, mock_place_bet, mock_get_events_for_sport,  cloudbet_client):
        """
        Test the scenario where a bet is successfully placed and the bet status is accepted.
        :param cloudbet_client: An instance of the CloudbetClient class.

        :return: None

        NB: This is a live test and will attempt to connect to Cloudbet to place the bet
        """
        await cloudbet_client.connect()
        # we need to get a tradeable event in the future to place a bet
        #   get the current unix timestamp
        current_timestamp = int(self.clock.timestamp()) + 86400
        #  get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sport_key = random.choice(["soccer", "tennis", "baseball", "basketball"])
        mock_place_bet.return_value: GetBetResponse= CloudbetResponses.place_bet_success()
        mock_get_events_for_sport.return_value = CloudbetResponses.get_events_for_sport(
            sport_key=sport_key
        )
        event: GetEventsForSportResponse = await cloudbet_client.get_events_for_sport(
            sport_key,
        )
        selections: List[Selection] = cloudbet_client.event_to_selection(event)
        selection = random.choice(selections)
        assert isinstance(selection, Selection), f"Expected object of type Selection. received {selection} "
        market_url = selection.market_name + '/' + selection.outcome + '?' + selection.params if selection.params is not None else selection.market_name + '/' + selection.outcome
        result = await cloudbet_client.place_bets(selection.event_id, market_url, selection.price, selection.side,
                                                  selection.min_stake, currency='PLAY_EUR')  # use PLAY_EUR for testing
        assert isinstance(result, GetBetResponse)
        assert result.status is BetStatus.ACCEPTED, f"Expected ACCEPTED BetStatus, instead got: {result.status}"

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'post')
    # ToDO: test why patch fails when explicitly using a co-routine mock
    #  @patch.object(CloudbetClient, 'post', new_callable=AsyncMock)
    async def test_place_bet_mock_required_parameters(self, mock_cloudbet_post, cloudbet_client):
        await cloudbet_client.connect()

        # Set up the mock responses
        success_bet: GetBetResponse = CloudbetResponses.place_bet_success()
        response_data: bytes = json.dumps(success_bet.to_dict()).encode('utf-8')  # encode to binary
        GenericObject = namedtuple('GenericObject', ['status', 'data'])
        mock_cloudbet_post.return_value = GenericObject(status=200, data=response_data)

        # prepare data for place_bet
        json_data = msgspec.json.encode({
            'acceptPriceChange': AcceptPriceChange.NONE,
            'eventId': str(success_bet.event_id),  # have to explictly cast to str
            'marketUrl': success_bet.market_url,
            'price': str(success_bet.price),  # have to explictly cast to str
            'side': str(success_bet.side.value),
            'stake': str(success_bet.stake),  # have to explictly cast to str
            'currency': success_bet.currency,
            'referenceId': success_bet.reference_id
        })
        result = await cloudbet_client.place_bets(success_bet.event_id, market_url=success_bet.market_url,
                                                  price=success_bet.price, side=success_bet.side.value,
                                                  stake=success_bet.stake, reference_id=success_bet.reference_id,
                                                  currency=success_bet.currency)
        # check post method was intercepted, and method successfully serialised mock-return value (response_data)
        assert isinstance(result, GetBetResponse)
        mock_cloudbet_post.assert_called_once()
        mock_cloudbet_post.assert_called_once_with(url=f"{cloudbet_client._api_url}/v3/bets/place",
                                                   headers=cloudbet_client.headers,
                                                   data=json_data)

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'post')
    @patch.object(CloudbetClient, 'get_events_for_sport', new_callable=AsyncMock)
    # @patch.object(CloudbetClient, 'place_bets', new_callable=AsyncMock)
    async def test_fail_to_place_bet_raises_exception(self, mock_get_events_for_sport, mock_cloudbet_post, cloudbet_client):
        """ Test exception is thrown with invalid event_id"""
        # # TODO: replace all calls to connect with a async mock method with app. side effects
        await cloudbet_client.connect()
        # we need to get a tradeable event in the future to place a bet
        #   get the current unix timestamp
        current_timestamp = int(self.clock.timestamp()) + 86400
        #  get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sport_key = random.choice(["soccer", "tennis", "basketball"])
        mock_get_events_for_sport.return_value = CloudbetResponses.get_events_for_sport(sport_key=sport_key)
        event = await cloudbet_client.get_events_for_sport(
            sport_key,
            current_timestamp,
            timestamp_48h,
            limit=5
        )
        selections: List[Selection] = cloudbet_client.event_to_selection(event)
        selection = random.choice(selections)
        assert isinstance(selection, Selection), f"Expected object of type Selection. received {selection} "
        market_url = selection.market_name + '/' + selection.outcome + '?' + selection.params if selection.params is not None else selection.market_name + '/' + selection.outcome

        # Mock the post method
        ClientResponse = namedtuple('ClientResponse', ['status', 'data'])
        mock_cloudbet_post.return_value = ClientResponse(status=500, data=b'')

        # Define the invalid event_id
        event_id = str(selection.event_id) + "random"

        # Call the place_bets method and expect an exception to be raised
        with pytest.raises(CloudbetAPIError):
            await cloudbet_client.place_bets(event_id, market_url, selection.price, selection.side,
                                             selection.min_stake, currency='PLAY_EUR')

        # test to check offset is a positive integer and limitn is within the correct range

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_retrieve_bet_history_required_params(self, mock_cloudbet_get, cloudbet_client):
        # Set up the mock responses
        bet_history: GetBetHistoryResponse = CloudbetResponses.get_bet_history_success()

        from_date = "2023-09-11T00:00:00Z"  # hardcoded since we know these dates have bets placed
        to_date = "2023-09-20T23:59:59Z"  # hardcoded since we know these dates have bets placed
        limit = 100
        offset = 0
        query_params = {
            'fromDate': from_date,
            'toDate': to_date,
            'limit': limit,
            'offset': offset
        }
        response_data: bytes = json.dumps(bet_history.to_dict()).encode('utf-8')  # encode to binary
        GenericObject = namedtuple('GenericObject', ['status', 'data'])
        mock_cloudbet_get.return_value = GenericObject(status=200, data=response_data)
        # result : GetBetHistoryResponse  = await cloudbet_client.get_bet_history()

        # Call the method under test
        result = await cloudbet_client.get_bet_history(from_date=from_date, to_date=to_date)

        # Check the result
        assert isinstance(result, GetBetHistoryResponse)
        mock_cloudbet_get.assert_called_once()
        mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v4/bets/history",
                                                  params=query_params, headers=cloudbet_client.headers)

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_retrieve_bet_history_offset_or_limit_invalid(self, mock_cloudbet_get, cloudbet_client):
        # Arrange
        from_date = "2023-09-11T00:00:00Z"  # hardcoded since we know these dates have bets placed
        to_date = "2023-09-20T23:59:59Z"  # hardcoded since we know these dates have bets placed
        limit = 1001
        offset = -1

        query_params = {
            'fromDate': from_date,
            'toDate': to_date,
            'limit': limit,
            'offset': offset
        }

        # Act and Assert
        with pytest.raises(ValueError):
            await cloudbet_client.get_bet_history(from_date, to_date, limit, offset)
            mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v4/bets/history",
                                                      params=query_params,
                                                      headers=cloudbet_client.headers)  # important sanity check otherwise Error could be thrown for multiple reasons

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_retrieve_bet_history_exception_malformed_date(self, mock_cloudbet_get, cloudbet_client):
        # Arrange
        response_data: bytes = json.dumps(
            "parsing time 2023/09/11 as 2006-01-02T15:04:05Z07:00: cannot parse /09/11 as -").encode(
            'utf-8')  # encode to binary
        GenericObject = namedtuple('GenericObject', ['status', 'data'])
        mock_cloudbet_get.return_value = GenericObject(status=400, data=response_data)
        from_date = "2023/09/11"
        to_date = "2023/09/20"
        limit = 100
        offset = 0
        query_params = {
            'fromDate': from_date,
            'toDate': to_date,
            'limit': limit,
            'offset': offset
        }

        # Act and Assert
        with pytest.raises(CloudbetAPIError):
            await cloudbet_client.get_bet_history(from_date="2023/09/11", to_date="2023/09/20")
        mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v4/bets/history",
                                                  params=query_params,
                                                  headers=cloudbet_client.headers)  # important sanity check otherwise Error could be thrown for multiple reasons

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_get_bet_status_valid_reference_id(self, mock_cloudbet_get, cloudbet_client):
        # Set up the mock response
        bet_status: GetBetResponse = CloudbetResponses.get_bet_status_win()
        response_data: bytes = json.dumps(bet_status.to_dict()).encode('utf-8')  # encode to binary
        GenericObject = namedtuple('GenericObject', ['status', 'data'])
        mock_cloudbet_get.return_value = GenericObject(status=200, data=response_data)

        # Call the method under test
        result = await cloudbet_client.get_bet_status(bet_status.reference_id)

        # Assert the result
        assert isinstance(result, GetBetResponse)
        mock_cloudbet_get.assert_called_once_with(
            url=f"{cloudbet_client._api_url}/v3/bets/{bet_status.reference_id}/status", headers=cloudbet_client.headers)

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_raises_cloudbet_api_error_for_invalid_reference_id(self, mock_cloudbet_get, cloudbet_client):
        # Set up the mock response
        response_data: bytes = json.dumps(" ").encode('utf-8')  # encode to binary
        GenericObject = namedtuple('GenericObject', ['status', 'data'])
        mock_cloudbet_get.return_value = GenericObject(status=404, data=response_data)
        reference_id = "ba319119-cf00-4acb-a4d0-728b3f7234d2"  # invalid_reference_id
        # Call the method under test
        with pytest.raises(CloudbetAPIError):
            await cloudbet_client.get_bet_status(reference_id)

        mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v3/bets/{reference_id}/status",
                                                  headers=cloudbet_client.headers)

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_get_account_currencies_success(self, mock_cloudbet_get, cloudbet_client):
        # Set up the mock response
        account_currencies: GetAccountCurrencies = CloudbetResponses.get_account_currencies_success()
        response_data : bytes = msgspec.json.encode(account_currencies)
        mock_cloudbet_get.return_value.status = 200
        mock_cloudbet_get.return_value.data = response_data


        # Call the method under test
        result = await cloudbet_client.get_account_currencies()

        # Check the result
        assert isinstance(result, GetAccountCurrencies)
        assert result == account_currencies
        mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v1/account/currencies",
                                         headers=cloudbet_client.headers)

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_get_account_currencies_exception_handling(self, mock_cloudbet_get, cloudbet_client):
        # Set up the mock response
        mock_cloudbet_get.return_value.status : int = 404
        mock_cloudbet_get.return_value.data : bytes = "404 page not found".encode('utf-8')
        # Call the method under test
        with pytest.raises(CloudbetAPIError):
            await cloudbet_client.get_account_currencies()

        mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v1/account/currencies",
                                                  headers=cloudbet_client.headers)

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'get')
    async def test_get_balances_success(self, mock_cloudbet_get, cloudbet_client):
        # Set up the test data
        currency = "PLAY_EUR"
        response_data = {"amount": 998.6449}

        # Set up the mock response
        response_data : bytes = msgspec.json.encode({"amount": "998.6449"})
        mock_cloudbet_get.return_value.status = 200
        mock_cloudbet_get.return_value.data = response_data

        # Call the get_balances method
        result = await cloudbet_client.get_balances(currency)

        # Assert that the request was made with the correct URL and headers
        mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v1/account/currencies/{currency}/balance", headers=cloudbet_client.headers)

        # Assert that the response was correctly decoded and returned
        assert isinstance(result, GetAccountBalance)
    #
    #
    @pytest.mark.asyncio
    @patch.object(CloudbetClient,  'get')
    async def test_retrieve_balance_invalid_currency(self, mock_cloudbet_get, cloudbet_client):
        # Set up the test data
        currency : str = "INVALID"
        error_message : str  = "currency doesn't exist"

        # Mock the request method

        mock_cloudbet_get.return_value.status : int = 400
        mock_cloudbet_get.return_value.text : bytes = msgspec.json.encode(error_message)

        # Call the get_balances method and assert that it raises the expected exception
        with pytest.raises(CloudbetAPIError) as exc_info:
            await cloudbet_client.get_balances(currency)

        # Assert that the request was made with the correct URL and headers
        mock_cloudbet_get.assert_called_once_with(url=f"{cloudbet_client._api_url}/v1/account/currencies/{currency}/balance", headers=cloudbet_client.headers)


    # TODO: test this!! - first add a selections fixture
    # @pytest.mark.asyncio()
    # async def test_selections_to_json(self):
    #     await self.client.connect()
    #     # get the current unix timestamp
    #     current_timestamp = int(self.clock.timestamp())
    #     # get the unixtime 48 hours in the future
    #     timestamp_48h = current_timestamp + 172800
    #     sports = ["soccer", "tennis", "baseball", "basketball"]
    #     # sport_key_list = random.sample(sports, k=3)
    #     sport_key_list = ["basketball"]
    #     result: list[list[Selection]] = await self.client.load_selection(
    #         filters={
    #             'sport_key': sport_key_list,
    #             'from_timestamp': current_timestamp,
    #             'to_timestamp': timestamp_48h,
    #             'live': 'false',
    #             'limit': 10
    #         }
    #     )
    #     normalised_selections = []
    #     for selection_list in result:
    #         # check that the selection list is a non-empty list
    #         if len(selection_list) > 0:
    #             # check that the type of the selection is correct
    #             for selection in selection_list:
    #                 selection_json  = selection.to_dict()
    #                 # append  the selections to a json file
    #                 normalised_selections.append(selection_json)
    #         else:
    #             continue
    #     with open('basketball_selections.json', 'a') as outfile:
    #         json.dump(normalised_selections, outfile)
    #     assert os.path.exists('basketball_selections.json')

    # @pytest.mark.asyncio()
    # # NB this test will fail if the client is not initialized correctly
    # async def test_client_disconnect(self):
    #     # we need to test if the client is initialized correctly by calling the connect method
    #     # and checking if the client is connected
    #     self.client = CloudbetClient(self.loop, self.logger)
    #     # we explicitly need to set the api key and secret to test credentials
    #     self.client._api_key = self.test_api_key
    #     self.client._api_url = self.test_api_url
    #     # await self.client.disconnect()
    #     assert self.client.connected is False
