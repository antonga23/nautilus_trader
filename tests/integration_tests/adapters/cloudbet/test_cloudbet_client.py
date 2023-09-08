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
from typing import List

import pytest
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import Selection, GetEventsForSportResponse, GetEventResponse, \
    GetFixturesResponse, GetLatestOddsResponse
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

        # Verify get_events_for_sport function was called concurrently We assume get_events_for_sport function will
        # take more than 0.1 second to complete Therefore if they were called concurrently, the total time should be
        # less than the number of sports * 0.1 second
        assert self.client.get_events_for_sport.call_count == len(filters['sport_key'])
        # assert self.client.get_events_for_sport.total_call_time < len(filters['sport_key']) * 0.1

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
        fixtures: GetFixturesResponse = await self.client.get_fixtures(sport_key, current_timestamp, timestamp_48h, limit=100)
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
    async def test_get_latest_odds(self):

        # TODO: refactor this to a fixture
        # live instrument_id setup >>>
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        # sports = ["soccer", "tennis", "baseball", "basketball"]
        sports = ["basketball"]
        sport_key = random.choice(sports)
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
        result = await self.client.get_latest_odds(event_id, market_url)
        # Check that the response is a GetLatestOddsResponse instance
        assert isinstance(result, GetLatestOddsResponse)

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

# class TimedAsyncMock(AsyncMock):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.total_call_time = 0
#
#     async def __call__(self, *args, **kwargs):
#         start_time = time.perf_counter()
#         result = await super().__call__(*args, **kwargs)
#         end_time = time.perf_counter()
#         self.total_call_time += end_time - start_time
#         return result
