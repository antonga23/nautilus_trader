import asyncio
from collections import Counter
from unittest.mock import patch, AsyncMock, MagicMock
from collections import Counter

import msgspec
import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.instruments import Instrument

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import GetLatestOddsResponse, SelectionStatus, GetEventResponse, \
    EventStatus
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.data_client import CloudbetDataClient
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.enums import LogLevel
from nautilus_trader.common.logging import Logger
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import GenericData
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import InstrumentStatusUpdate
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import Ticker
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import InstrumentCloseType
from nautilus_trader.model.enums import MarketStatus
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.objects import Price
from nautilus_trader.model.orderbook import OrderBook
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetResponses

class TestCloudbetDataClient:

    # @pytest.mark.dependency()
    @pytest.mark.asyncio()
    async def test_connect(self, data_client):
        """Test connect """
        # Arrange
        # set the intreval to be 2 seconds
        data_client._update_instrument_interval = 2
        # Act
        data_client.connect()
        await asyncio.sleep(0)
        await asyncio.sleep(
            0)  # _connect uses multiple awaits, => will take time to resolve so multiple sleeps required.
        # Assert that instrument_provider has been initialised
        assert data_client.instrument_provider._loaded is True
        # Assert that data client component is connected
        assert data_client.is_connected
        assert data_client._update_instruments_task is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 552)], indirect=["instruments"])
    async def test_update_instruments_task(self, data_client, instrument_provider, instruments):
        # Arrange: Set the initial update interval and start the update task
        data_client._update_instrument_interval = 1

        # Define an async side effect function for mocking `load_ids_async`
        async def async_side_effect_load_ids(dummy_param):
            # Simulate async behavior
            await asyncio.sleep(0)

        # Start the update task using the context manager for patches
        with patch.object(CloudbetInstrumentProvider, 'load_ids_async', new_callable=AsyncMock,
                          side_effect=async_side_effect_load_ids) as mocked_load_ids, \
            patch.object(data_client, '_send_all_instruments_to_data_engine',
                         new_callable=MagicMock) as mocked_send_all:
            update_task = asyncio.create_task(data_client._update_instruments())

            iterations = 3
            for _ in range(iterations):
                # Wait for the update event to be set, indicating a cycle's completion
                await data_client._update_event.wait()

                # Reset the event for the next cycle
                data_client._update_event.clear()

                # Here, you can make assertions or checks regarding the mocked methods
                # For example, checking call count or inspecting call arguments

            # Clean up: Cancel the update task to prevent it from running indefinitely
            update_task.cancel()
            await update_task

        # Assertions related to the mocked calls
        assert mocked_load_ids.call_count == iterations

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 552)], indirect=["instruments"])
    # @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.connect")
    async def test_update_instruments(self, instrument_provider, data_client, instruments):
        """Test _update_instruments_task"""
        # Arrange,
        data_client._update_instrument_interval = 2 # set the inteval to be 2 seconds
        # Act
        data_client.connect()
        await asyncio.sleep(0)
        await asyncio.sleep(
            0)  # _connect uses multiple awaits, => will take time to resolve so multiple sleeps required.
        # Assert
        # assert the task has been created
        assert data_client._update_instrument_interval == 2
        assert data_client._update_instruments_task is not None

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 552)], indirect=["instruments"])
    async def test_send_instruments_data_engine(self, data_client, instruments, data_engine):
        """Test _send_all_instruments_to_data_engine"""
        # Arrange
        # check cache is empty - excluding instrument provider preloaded instrument
        cache_instruments = data_client._cache.instrument_ids(venue=CLOUDBET_VENUE)
        assert len(data_client._cache.instrument_ids(venue=CLOUDBET_VENUE)) - len(
            cache_instruments) == 0, f"Expected {cache_instruments} instruments, got {len(data_client._cache.instrument_ids(venue=CLOUDBET_VENUE))} instruments"
        # Arrange, Act
        # load instruments into data engine and cache
        data_client._send_all_instruments_to_data_engine(instruments=instruments)
        updated_cache_instruments = data_client._cache.instrument_ids(venue=CLOUDBET_VENUE)
        assert len(instruments) == len(updated_cache_instruments), f"Expected {len(instruments)} instruments, got {len(data_client.instrument_provider._instruments)} instruments"

    # test disconnect
    @pytest.mark.asyncio()
    @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.disconnect")
    async def test_disconnect(self, mock_stream_disconnect, data_client):
        """Test disconnect"""
        # TODO: use pytest dependency or a mock to cause side effecsts, instead of manual connect()
        # @pytest.mark.dependency(depends=["TestCloudbetDataClient::test_connect_without_stream"])
        # Arrange, Act
        data_client.connect()
        await asyncio.sleep(1)
        await asyncio.sleep(1)  # sleeps are required, otherwise AssertionError Task was destroyed but it is pending!
        # check client was connected
        assert data_client.is_connected
        # await asyncio.sleep(30) # sleeps are required, otherwise AssertionError Task was destroyed but it is pending!
        data_client.disconnect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # sleeps are required, otherwise AssertionError Task was destroyed but it is pending!
        # Assert
        assert not data_client.is_connected, f"Expected data client to be disconnected, got {data_client.is_connected}"

    @pytest.mark.asyncio()
    async def test_reset(self, data_client):
        """Test _reset"""
        data_client.reset()
        # TODO: make pythonic
        assert data_client.subscribed_selection_ids == set()
        assert data_client.subscribed_orderbooks == {}
        assert data_client.subscribed_event_ids == {}
        assert data_client.subscribed_market_names == {}
        assert data_client._update_instruments_task == None

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    # @patch('nautilus_trader.adapters.cloudbet.client.core.CloudbetClient.get_latest_odds')
    @patch.object(CloudbetClient, 'get_latest_odds', new_callable=AsyncMock)
    async def test_request_instrument_loaded_instrument(self, mock_get_latest_odds, data_client, data_engine, instruments):
        """Test _request with an instrument already in cache"""
        # Arrange
        request_id = UUID4()  # have to call UUID4(value=None) outside await
        assert request_id is not None, f"Expected request_id to be UUID4, got {request_id}"
        # create session for instrument provider
        if data_client._client.connected is False:
            await data_client._client.connect()
        data_client.instrument_provider.add(instruments[0])
        loaded_instrument = data_client.instrument_provider.find(instruments[0].id)
        assert instruments[0] == loaded_instrument, f"Unable to load or find instrument {instruments[0].id}"
        # Create a mock response
        mock_response = GetLatestOddsResponse(
            max_stake=loaded_instrument.max_size - 1 if loaded_instrument.max_size is not None else 0, # selection may be DISBALED => NO max stake even if the event is trading
            # subtract 1 from the max stake to ensure the max stake is different
            min_stake=loaded_instrument.min_size + 1 if loaded_instrument.min_size is not None else 0,  # add 1 to the min stake to ensure the min stake is different, selection may be DISBALED => NO max stake even if the event is trading
            price=loaded_instrument.price + 1,  # add 1 to the price to ensure the price is different
            status=SelectionStatus.ENABLED,
            outcome=loaded_instrument.outcome,
            params=loaded_instrument.params,
            probability=0.3,  # random probability => required arguemnt
            side=loaded_instrument.side
        )

        mock_get_latest_odds.return_value = mock_response
        # track current DataEngine response count
        current_response_count = data_engine.response_count
        # Act
        # Call the method under test
        data_client.request_instrument(loaded_instrument.id, request_id)
        # assert the mock method was called
        await asyncio.sleep(
            0)  # neccesary sleep to allow async mock to resolve else AssertionError: Expected 'get_latest_odds' to have been called once. Called 0 times.
        mock_get_latest_odds.assert_called_once()
        # Assert
        # we want to assert that the instrument has been updated with the new data
        assert loaded_instrument.max_size == mock_response.max_stake, f"Expected max stake to be {mock_response.max_stake}, got {loaded_instrument.max_size}"
        assert loaded_instrument.min_size == mock_response.min_stake, f"Expected min stake to be {mock_response.min_stake}, got {loaded_instrument.min_size}"
        assert loaded_instrument.price == mock_response.price, f"Expected price to be {mock_response.price}, got {loaded_instrument.price}"

        # assert the instrument has been added to the cache
        assert data_client._cache.load_instrument(loaded_instrument.id) == instruments[0], f"Expected {loaded_instrument.id} to be in cache, got {data_client._cache.load_instrument(loaded_instrument.id)}"
        # check data response was received by the DataEngine
        updated_response_count = data_engine.response_count
        assert updated_response_count - current_response_count == 1, f"Expected {updated_response_count} responses, got {current_response_count}"
