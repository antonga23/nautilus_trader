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
import time
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
import pytest
from nautilus_trader.common.clock import TestClock
from nautilus_trader.common.logging import Logger

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import Selection, GetLatestOddsResponse, SelectionStatus
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetTestStubs, CloudbetResponses
import random


class TestCloudbetInstrumentProvider:

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
        """
        Test the load_all_async method.

        This method tests the functionality of the load_all_async method in the
        current class. It performs the following steps:

        1. Connects to the client (creates a lvie network session)
        2. Calls the load_all_async method of the provider.
        3. Prints the instrument count and the count attribute of the provider.
        4. Asserts that the count attribute of the provider is equal to the
           instrument count.
        """
        await self.client.connect()
        instrument_count = await self.provider.load_all_async()
        print(instrument_count, self.provider.count)
        # # we want to save the instruments to a json for later use as a mock
        instruments: List[CryptoBettingInstrument] = self.provider.list_all()
        # instruments = [CryptoBettingInstrument.to_dict(i) for i in instruments]
        # with open("CryptoBettingInstrument.json", "w") as f:
        #     json.dump(instruments, f)
        assert self.provider.count == instrument_count, f"Instrument count does not match: expected {self.provider.count} but got {instrument_count}"

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
        """
        Test the `load_all_async` method with invalid filters.

        This function is responsible for testing the `load_all_async` method of the class. It checks if the method handles invalid filters correctly.

        Parameters:
        - None

        Returns:
        - None
        """
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

    @pytest.mark.asyncio()
    @patch.object(CloudbetClient, 'get_latest_odds', new_callable=AsyncMock)
    async def test_load_async(self, mock_get_latest_odds):
        # # live instrument setup
        await self.client.connect()
        mock_get_latest_odds.return_value: GetLatestOddsResponse = CloudbetResponses.get_latest_odds()
        sport = random.choice(["soccer", "tennis", "baseball", "basketball", ""])
        selections: List[Selection] = CloudbetTestStubs.get_selections(sport=sport)
        selection = random.choice(selections)
        live_instrument = self.provider.selection_to_instrument(selection)
        await self.provider.load_async(live_instrument.id)

        # check if the instrument is loaded
        loaded_instrument = self.provider.find(live_instrument.id)
        assert isinstance(loaded_instrument, CryptoBettingInstrument)
        assert loaded_instrument.id == live_instrument.id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 10)], indirect=["instruments"])
    @patch.object(CloudbetClient, 'get_latest_odds', new_callable=AsyncMock,
                  return_value=CloudbetResponses.get_latest_odds())
    async def test_fast_load_ids_async(self, mock_get_latest_odds, instrument_provider, instruments):
        # live instrument setup
        await instrument_provider._client.connect()
        live_instruments = instruments
        await instrument_provider.load_ids_async([instrument_id.id for instrument_id in live_instruments])

        #  check if the instrument have been loaded correctly
        loaded_instruments = [instrument_provider.find(instrument_id.id) for instrument_id in live_instruments]
        # Because we're using a patch for get_latest_odds and get_event, we can't assert that the set of loaded_instruments== set(live_instruments) after calling load_ids_async
        # We have to filter out elements in list that are none => beacuse there no matching markets/submarkets in the mock instruments
        loaded_instruments = [instrument for instrument in loaded_instruments if instrument is not None]
        for i in range(len(loaded_instruments)):
            print(loaded_instruments[i])
            assert isinstance(loaded_instruments[i], CryptoBettingInstrument)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 500)], indirect=["instruments"])
    @patch.object(CloudbetClient, 'get_latest_odds', new_callable=AsyncMock,
                  return_value=CloudbetResponses.get_latest_odds())
    @patch.object(CloudbetClient, 'get_event', new_callable=AsyncMock, return_value=CloudbetResponses.get_event())
    async def test_bulk_load_ids_async(self, mock_get_event, mock_get_latest_odds, instrument_provider, instruments):
        """
        Test case for testing the asynchronous bulk load of instrument IDs.

        Parameters:
        - mock_get_event: A mock object for the get_event method of CloudbetClient.
        - mock_get_latest_odds: A mock object for the get_latest_odds method of CloudbetClient.
        - instrument_provider: The instrument provider object.
        - instruments: The list of instruments to be loaded.

        Returns:
        None
        """
        # live instrument setup
        await instrument_provider._client.connect()
        assert instrument_provider._client.connected is True
        live_instruments = instruments
        await instrument_provider.load_ids_async([instrument_id.id for instrument_id in live_instruments])
        #  check if the instrument have been loaded correctly
        loaded_instruments = [instrument_provider.find(instrument_id.id) for instrument_id in live_instruments]
        # Because we're using a patch for get_latest_odds and get_event, we can't assert that the set of loaded_instruments== set(live_instruments) after calling load_ids_async
        # filter out elements in list that are none.
        loaded_instruments = [instrument for instrument in loaded_instruments if instrument is not None]
        for i in range(len(loaded_instruments)):
            assert isinstance(loaded_instruments[i], CryptoBettingInstrument)

    @pytest.mark.parametrize(("param_instrument_filter", "param_search_result"), [
        ({"sport_name": "Soccer"}, True),  # successful single k,v filter
        ({"sport_name": "Invalid Sport Name"}, False),  # unsuccesful single k,v
        ({"sport_name": "Soccer", "market_name": "invalid_market_name"}, False),  # unsuccesful single k,v
        ({"sport_name": "Soccer", "market_name": "soccer.asian_handicap", "event_name": "Torreense U23 V FC Famalicao"},
         True),  # succesful single k,v
    ])
    @patch.object(CloudbetInstrumentProvider, 'list_all')
    @pytest.mark.asyncio()
    async def test_search_instruments(self, mock_provider_list_all, param_instrument_filter, param_search_result):
        # Arrange
        await self.client.connect()
        mock_provider_list_all.return_value = TestInstrumentProvider.crypto_betting_instruments(
            count=552)
        # Act
        filtered_instruments = self.provider.search_instruments(
            instrument_filter=param_instrument_filter,
        )  # NB. for more robustness we could construct the instrument filter based on a random instrument from mock_provider_list_all
        # Assert
        if param_search_result:
            assert len(filtered_instruments) > 0
            for instrument in filtered_instruments:
                for key, value in param_instrument_filter.items():
                    assert getattr(instrument, key) == value
        else:
            assert len(filtered_instruments) == 0

    @pytest.mark.asyncio()
    @patch.object(CloudbetClient, 'get_latest_odds', new_callable=AsyncMock,
                  return_value=CloudbetResponses.get_latest_odds())
    @pytest.mark.skipif(reason="get_betting_instrument() never called so we can skip the test")
    async def test_get_betting_instrument(self):
        await self.provider.load_all_async()
        kw = {
            "market_id": "1.180678317",
            "selection_id": "11313157",
            "handicap": 0.0,
        }
        instrument = self.provider.get_betting_instrument(**kw)
        assert instrument.market_id == "1.180678317"

        # Test throwing warning
        kw["handicap"] = "-1000"
        instrument = self.provider.get_betting_instrument(**kw)
        assert instrument is None

        # Test already in self._subscribed_instruments
        instrument = self.provider.get_betting_instrument(**kw)
        assert instrument is None


def _cap_selection(index: int, max_stake: float) -> Selection:
    return Selection(
        competition_name="Comp",
        sport_name="Soccer",
        event_id=1000 + index,
        home_name="Home",
        away_name="Away",
        status="TRADING",
        market_name="soccer.match_odds",
        submarket_name="match_odds",
        outcome=f"outcome-{index}",
        price=2.0,
        min_stake=1.0,
        max_stake=float(max_stake),
        selection_status=SelectionStatus.ENABLED,
        side="BACK",
        params=None,
        event_name="Home vs Away",
    )


class TestCloudbetInstrumentLoadCap:
    """
    FIX-2: load_all_async enforces instrument_load_limit as a live-subscription hard
    cap, keeping the deepest markets and leaving the API pagination `limit` untouched.
    """

    def _provider(self, filters: dict) -> CloudbetInstrumentProvider:
        from nautilus_trader.config import InstrumentProviderConfig

        return CloudbetInstrumentProvider(
            client=MagicMock(),
            logger=TestComponentStubs.logger(),
            config=InstrumentProviderConfig(filters=filters),
        )

    @pytest.mark.asyncio()
    async def test_cap_keeps_only_the_deepest_markets(self):
        # 10 selections offered, shallow-to-deep max_stake; cap keeps the 4 deepest.
        selections = [_cap_selection(i, max_stake=(i + 1) * 10) for i in range(10)]
        provider = self._provider({"instrument_load_limit": 4, "sport_key": ["soccer"], "limit": 50})
        provider._client.load_selection = AsyncMock(return_value=[selections])

        loaded = await provider.load_all_async()

        assert loaded == 4
        assert provider.count == 4
        kept_depths = sorted(int(inst.max_size) for inst in provider.list_all())
        assert kept_depths == [70, 80, 90, 100]

    @pytest.mark.asyncio()
    async def test_cap_pops_limit_key_but_preserves_pagination_limit(self):
        selections = [_cap_selection(i, max_stake=(i + 1) * 10) for i in range(5)]
        provider = self._provider({"instrument_load_limit": 2, "sport_key": ["soccer"], "limit": 25})
        provider._client.load_selection = AsyncMock(return_value=[selections])

        await provider.load_all_async()

        sent_filter = provider._client.load_selection.await_args.args[0]
        assert "instrument_load_limit" not in sent_filter
        assert sent_filter["limit"] == 25  # pagination limit is preserved

    @pytest.mark.asyncio()
    async def test_no_limit_is_unbounded(self):
        selections = [_cap_selection(i, max_stake=(i + 1) * 10) for i in range(7)]
        provider = self._provider({"sport_key": ["soccer"], "limit": 50})
        provider._client.load_selection = AsyncMock(return_value=[selections])

        loaded = await provider.load_all_async()

        assert loaded == 7
        assert provider.count == 7

    @pytest.mark.asyncio()
    async def test_zero_limit_is_unbounded(self):
        selections = [_cap_selection(i, max_stake=(i + 1) * 10) for i in range(6)]
        provider = self._provider({"instrument_load_limit": 0, "sport_key": ["soccer"], "limit": 50})
        provider._client.load_selection = AsyncMock(return_value=[selections])

        loaded = await provider.load_all_async()

        assert loaded == 6
        assert provider.count == 6


class TestCloudbetDiscoveryExclusion:
    """
    WS-B2: load_all_async drops selections whose (event_id, market_key) the injected
    MarketPollabilityRegistry marks unpollable, before the depth-sort/load-limit cap.
    """

    def _provider(self, filters: dict | None = None) -> CloudbetInstrumentProvider:
        from nautilus_trader.config import InstrumentProviderConfig

        return CloudbetInstrumentProvider(
            client=MagicMock(),
            logger=TestComponentStubs.logger(),
            config=InstrumentProviderConfig(filters=filters) if filters else None,
        )

    @staticmethod
    def _tombstoned_registry(event_ids: list[int], market_key: str):
        from nautilus_trader.adapters.cloudbet.market_pollability import (
            MarketPollabilityRegistry,
        )

        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)
        for event_id in event_ids:
            registry.record_miss(event_id, market_key, reason="line_404", now_ns=0)
        return registry

    @pytest.mark.asyncio()
    async def test_registry_excludes_tombstoned_selections(self):
        selections = [_cap_selection(i, max_stake=(i + 1) * 10) for i in range(4)]
        provider = self._provider()
        provider._client.load_selection = AsyncMock(return_value=[selections])
        provider.set_pollability_registry(
            self._tombstoned_registry([1001, 1002], "soccer.match_odds"),
        )

        loaded = await provider.load_all_async()

        assert loaded == 2
        kept_event_ids = {int(str(inst.event_id)) for inst in provider.list_all()}
        assert kept_event_ids == {1000, 1003}

    @pytest.mark.asyncio()
    async def test_exclusion_runs_before_load_limit_cap(self):
        # The deepest selection is tombstoned: the freed slot goes to a pollable one.
        selections = [_cap_selection(i, max_stake=(i + 1) * 10) for i in range(5)]
        provider = self._provider({"instrument_load_limit": 2, "sport_key": ["soccer"]})
        provider._client.load_selection = AsyncMock(return_value=[selections])
        provider.set_pollability_registry(
            self._tombstoned_registry([1004], "soccer.match_odds"),
        )

        loaded = await provider.load_all_async()

        assert loaded == 2
        kept_depths = sorted(int(inst.max_size) for inst in provider.list_all())
        assert kept_depths == [30, 40]

    @pytest.mark.asyncio()
    async def test_without_registry_behavior_is_unchanged(self):
        selections = [_cap_selection(i, max_stake=(i + 1) * 10) for i in range(4)]
        provider = self._provider()
        provider._client.load_selection = AsyncMock(return_value=[selections])

        loaded = await provider.load_all_async()

        assert loaded == 4
        assert provider.count == 4
