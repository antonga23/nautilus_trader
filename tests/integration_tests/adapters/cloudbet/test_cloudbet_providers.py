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
from typing import List

import pytest
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
import pytest
from nautilus_trader.common.clock import TestClock
from nautilus_trader.common.logging import Logger

from nautilus_trader.adapters.cloudbet.client.schema import Selection
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetTestStubs
import random


class TestCloudbetInstrumentProvider:

    # # live instrument fixture
    # @pytest.fixture()
    # @pytest.mark.asyncio()
    # async def live_instrument(self):
    #     await self.client.connect()
    #     # get the current unix timestamp
    #     current_timestamp = int(self.clock.timestamp())
    #     # get the unixtime 48 hours in the future
    #     timestamp_48h = current_timestamp + 172800
    #     # sports = ["soccer", "tennis", "baseball", "basketball"]
    #     sports = ["basketball"]
    #     sport_key = random.choice(sports)
    #     event = await self.client.get_events_for_sport(
    #         sport_key,
    #         current_timestamp,
    #         timestamp_48h,
    #         limit=5
    #     )
    #     selections: List[Selection] = self.client.event_to_selection(event)
    #     selection = Selection(**random.choice(selections))
    #     instrument = self.provider.selection_to_instrument(selection)
    #     return instrument

    def setup(self):
        # Fixture Setup
        self.loop = asyncio.get_event_loop()
        self.clock = LiveClock()
        self.logger = Logger(clock=self.clock, bypass=True)
        self.client = CloudbetTestStubs.cloudbet_client(loop=self.loop, logger=self.logger)
        self.provider = CloudbetInstrumentProvider(
            client=self.client,
            logger=TestComponentStubs.logger(),
        )

    @pytest.mark.asyncio()
    async def test_load_all_async(self):
        await self.client.connect()
        instrument_count = await self.provider.load_all_async()
        print(instrument_count, self.provider.count)
        assert self.provider.count == instrument_count, f"Instrument count does not match: expected {self.provider.count} but got {instrument_count}"

    # @pytest.mark.asyncio()
    # async def test_generate_instrument_data_file(self):
    #     await self.client.connect()
    #     await self.provider.load_all_async()
    #     # Convert the instruments to dictionaries
    #     # instruments_data = [str(instrument.id) for instrument in self.provider.list_all()]
    #     instruments_data: list[CryptoBettingInstrument] = [instrument.to_dict() for instrument in self.provider.list_all()]
    #     # write the instruments to a file
    #     with open('//home/alatha/Desktop/eudaimonia/tests/integration_tests/adapters/cloudbet/test_instrument_id_data.json', 'w') as outfile:
    #         json.dump(instruments_data, outfile)

    @pytest.mark.asyncio()
    async def test_load_all_async_filters(self):
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "tennis", "baseball", "basketball", "football", "ice_hockey", "volleyball", "handball",
                  "american-football", "greyhounds"]
        sport_key_list = random.sample(sports, k=3)
        filters = {
            'sport_key': sport_key_list,
            'from_timestamp': current_timestamp,
            'to_timestamp': timestamp_48h,
            'live': 'false',
            'limit': 10
        }
        instrument_count = await self.provider.load_all_async(filters=filters)
        assert self.provider.count == instrument_count

    @pytest.mark.asyncio()
    async def test_load_all_async_invalid_filters(self):
        await self.client.connect()
        filters = {
            'sport_key': ['non-existing-sport'],
            'from_timestamp': -1,
            'to_timestamp': -1,
            'live': 'not-a-boolean',
            'limit': 'not-an-integer'
        }
        try:
            instrument_count = await self.provider.load_all_async(filters=filters)
            assert self.provider.count == 0
            assert instrument_count == 0
        except ValueError:
            pass

    # @pytest.mark.asyncio()
    # def test_id_to_selection_id(self):
    #     # Replace with a test stub that generates valid instrument ids for cloudbet
    #     instrument_id = CloudbetTestStubs.get_instrument_id()
    #     selection_id = self.provider.id_to_selection_id(instrument_id)
    #     assert isinstance(selection_id, SelectionId)

    @pytest.mark.asyncio()
    async def test_load_async(self):
        # ToDO: refactor live instrument setup into a fixture
        # live instrument setup
        await self.client.connect()
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "tennis", "baseball", "basketball"]
        sport_key = random.choice(sports)
        # ToDO: test multiple events
        event = await self.client.get_events_for_sport(
            sport_key,
            current_timestamp,
            timestamp_48h,
            limit=5
        )
        # ToDO: gracefully handle the case where there are no events as part of live instrument refactor
        selections: List[Selection] = self.client.event_to_selection(event)
        selection = random.choice(selections)
        live_instrument = self.provider.selection_to_instrument(selection)
        await self.provider.load_async(live_instrument.id)

        # check if the instrument is loaded
        loaded_instrument = self.provider.find(live_instrument.id)
        assert isinstance(loaded_instrument, CryptoBettingInstrument)
        assert loaded_instrument.id == live_instrument.id

    @pytest.mark.asyncio
    async def test_fast_load_ids_async(self, instrument_provider):
        # ToDO: refactor live instrument setup into a fixture
        # live instrument setup
        # await self.client.connect()
        await instrument_provider._client.connect()
        assert instrument_provider._client.connected is True
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "basketball"]
        sport_key = random.choice(sports)
        # TODO: test multiple events
        event = await instrument_provider._client.get_events_for_sport(
            sport_key,
            current_timestamp,
            timestamp_48h,
            limit=5
        )
        selections: List[Selection] = instrument_provider._client.event_to_selection(event)
        live_instruments = []
        for i in range(min(10, len(selections))):
            selection = selections[i]
            live_instruments.append(instrument_provider.selection_to_instrument(selection))
            # live_instrument_ids.append(live_instrument.id)
        await instrument_provider.load_ids_async([instrument_id.id for instrument_id in live_instruments])

        #  check if the instrument have been loaded correctly
        loaded_instruments = [instrument_provider.find(instrument_id.id) for instrument_id in live_instruments]
        for i in range(len(loaded_instruments)):
            assert isinstance(loaded_instruments[i], CryptoBettingInstrument)
            assert loaded_instruments[i].id == live_instruments[i].id

    @pytest.mark.asyncio
    async def test_bulk_load_ids_async(self, instrument_provider):
        # ToDO: refactor live instrument setup into a fixture
        # live instrument setup
        # await self.client.connect()
        await instrument_provider._client.connect()
        assert instrument_provider._client.connected is True
        # get the current unix timestamp
        current_timestamp = int(self.clock.timestamp())
        # get the unixtime 48 hours in the future
        timestamp_48h = current_timestamp + 172800
        sports = ["soccer", "basketball"]
        sport_key = random.choice(sports)
        # TODO: test multiple events
        event = await instrument_provider._client.get_events_for_sport(
            sport_key,
            current_timestamp,
            timestamp_48h,
            limit=5
        )
        selections: List[Selection] = instrument_provider._client.event_to_selection(event)
        live_instruments = []
        for i in range(min(100, len(selections))):
            selection = selections[i]
            live_instruments.append(instrument_provider.selection_to_instrument(selection))
            # live_instrument_ids.append(live_instrument.id)
        await instrument_provider.load_ids_async([instrument_id.id for instrument_id in live_instruments])

        #  check if the instrument have been loaded correctly
        loaded_instruments = [instrument_provider.find(instrument_id.id) for instrument_id in live_instruments]
        for i in range(len(loaded_instruments)):
            assert isinstance(loaded_instruments[i], CryptoBettingInstrument)
            assert loaded_instruments[i].id == live_instruments[i].id

    #
    # @pytest.mark.asyncio
    # async def test_load_async(instrument_provider):
    #     instrument_id = InstrumentId("test_instrument_id")
    #     await instrument_provider.load_async(instrument_id)
    #     assert instrument_provider.count == 1
    #
    # def test_selection_to_instrument(instrument_provider):
    #     selection = CloudbetTestStubs.selection()
    #     instrument = instrument_provider.selection_to_instrument(selection)
    #     assert isinstance(instrument, CryptoBettingInstrument)
