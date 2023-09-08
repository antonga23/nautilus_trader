import asyncio
from collections import Counter
from unittest.mock import patch, AsyncMock
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


#
# def test_connect(data_client: ):
#     data_client.connect()
#     assert data_client.is_connected
#
#
# def test_disconnect(data_client: LiveMarketDataClient):
#     data_client.connect()
#     data_client.disconnect()
#     assert not data_client.is_connected
#
#
# def test_reset(data_client: LiveMarketDataClient):
#     pass
#
#
# def test_dispose(data_client: LiveMarketDataClient):
#     pass
#


# @pytest.fixture(scope="session", autouse=True)
# @patch("nautilus_trader.adapters.betfair.providers.load_markets_metadata")
# @patch("load_all_async")
# def instrument_list():
#     """Prefill `INSTRUMENTS` cache for tests"""
#     global INSTRUMENTS
#
#     loop = asyncio.get_event_loop()
#
#     instrument_provider = data_client.instrument_provider
#
#     # Load instruments
#
#     t = loop.create_task(
#         instrument_provider.load_all_async(market_filter={"market_id": market_ids}),
#     )
#     loop.run_until_complete(t)
#
#     # Fill INSTRUMENTS global cache
#     INSTRUMENTS.extend(instrument_provider.list_all())
#     assert INSTRUMENTS


class TestCloudbetDataClient:

    # @pytest.mark.dependency()
    @pytest.mark.asyncio()
    # we patch the import and not the class itself, because the class is used in the import
    @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.connect")
    async def test_connect_without_stream(self, stream_connect, data_client):
        """Test connect without a stream"""
        # TODO: test with a stream
        # Arrange, Act
        data_client.connect()
        await asyncio.sleep(0)
        await asyncio.sleep(
            0)  # _connect uses multiple awaits, => will take time to resolve so multiple sleeps required.
        # Assert that instrument_provider has been initialised
        assert data_client.instrument_provider._loaded is True
        # Assert that data client component is connected
        assert data_client.is_connected

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    async def test_send_instruments_data_engine(self, data_client, instruments, data_engine):
        """Test _send_all_instruments_to_data_engine"""
        # check cache is empty - excluding instrument provider preloaded instrument
        cache_instruments = data_client._cache.instrument_ids(venue=CLOUDBET_VENUE)
        assert len(data_client._cache.instrument_ids(venue=CLOUDBET_VENUE)) - len(
            cache_instruments) == 0, f"Expected {cache_instruments} instruments, got {len(data_client._cache.instrument_ids(venue=CLOUDBET_VENUE)) - 1} instruments"
        # Arrange, Act
        # load instruments into data engine and cache
        data_client._send_all_instruments_to_data_engine(instruments=instruments)
        assert len(instruments) == len(data_client._cache.instrument_ids(venue=CLOUDBET_VENUE)) - len(
            cache_instruments), f"Expected {len(instruments)} instruments, got {len(data_client.instrument_provider._instruments) - 1} instruments"

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
        assert data_client.subscribed_orderbook_delta == {}
        assert data_client.subscribed_selection_ids == set()
        assert data_client.subscribed_orderbooks == {}
        assert data_client.subscribed_instrument_ids == set()

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
            max_stake=loaded_instrument.max_size - 1,
            # subtract 1 from the max stake to ensure the max stake is different
            min_stake=loaded_instrument.min_size + 1,  # add 1 to the min stake to ensure the min stake is different
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


    # @pytest.mark.asyncio()
    # @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 100)], indirect=["instruments"])
    # # @patch.object(CloudbetInstrumentProvider, 'list_all')
    # async def test_request_instruments(self, mock_list_all, data_client, instruments):
    #     # Arrange
    #     venue = CLOUDBET_VENUE
    #     correlation_id = UUID4()
    #     data_client._instrument_provider.add_bulk(instruments)
    #     # mock_list_all.side_effect = lambda: instruments
    #
    #     # Act
    #     data_client.request_instruments(venue, correlation_id)
    #     await asyncio.sleep(0)
    #     await asyncio.sleep(0)
        # Assert
        # mock_list_all.assert_called_once()
        # data_client._handle_instruments.assert_called_once_with(venue, instruments, correlation_id)

    # @pytest.mark.asyncio()
    # @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    # @patch.object(CloudbetClient, 'get_latest_odds', new_callable=AsyncMock)
    # @patch.object(CloudbetClient, 'get_event', new_callable=AsyncMock)
    # async def test_request_instrument_unloaded_instrument(self, mock_get_latest_odds, mock_get_event, data_client,
    #                                                       instruments):
    #     """Test _request with an instrument not already in cache"""
    #     # Arrange
    #     request_id = UUID4()  # have to call UUID4(value=None) outside await
    #     assert request_id is not None, f"Expected request_id to be UUID4, got {request_id}"
    #     # create session for instrument provider
    #     if data_client._client.connected is False:
    #         await data_client._client.connect()
    #
    #     unloaded_instrument = instruments[0]
    #     # Ensure the instrument is not loaded in the provider
    #     assert data_client.instrument_provider.find(
    #         unloaded_instrument.id) is None, f"Instrument {unloaded_instrument.id} should not be loaded"
    #
    #     # Create a mock response for get_latest_odds
    #     mock_odds_response = GetLatestOddsResponse(
    #         status=EventStatus.POST_TRADING,
    #         max_stake=unloaded_instrument.max_size - 1,
    #         min_stake=unloaded_instrument.min_size + 1,
    #         price=unloaded_instrument.price + 1,
    #         outcome=unloaded_instrument.outcome,
    #         params=unloaded_instrument.params,
    #         probability=0.3, # random probability => required arguemnt
    #         side=unloaded_instrument.side
    #     )
    #     mock_get_latest_odds.return_value = mock_odds_response
    #
    #     # Create a mock response for get_event
    #     mock_event_response = GetEventResponse(
    #         sequence="random_string",
    #         id=unloaded_instrument.id.symbol.value.split("|")[0],
    #         sport=unloaded_instrument.sport_name,
    #         competition=unloaded_instrument.competition_name,
    #         home=unloaded_instrument.home_name,
    #         away=unloaded_instrument.away_name,
    #         status=EventStatus.POST_TRADING,
    #         markets=[],
    #         name=unloaded_instrument.event_name,
    #         key="random_event_name",
    #         cutoff_time="2023-01-02T15:04:05Z07:00",
    #         type="random_type",
    #         end_time="2023-01-02T15:06:05Z07:00",
    #         grading_duration="random_duration",
    #     )
    #
    #     # Set up the mock_get_event to return the mock_event_response when called with the specific argument
    #     mock_get_event.side_effect = lambda event_id: mock_event_response if event_id == \
    #                                                                          int(unloaded_instrument.id.symbol.value.split(
    #                                                                              "|")[0]) else None
    #
    #     # Call the method under test
    #     data_client.request_instrument(unloaded_instrument.id, request_id)
    #
    #     # assert the mock methods were called
    #     await asyncio.sleep(
    #         1)  # necessary sleep to allow async mock to resolve else AssertionError: Expected 'get_latest_odds' to have been called once. Called 0 times.
    #     mock_get_latest_odds.assert_called_once()
    #     mock_get_event.assert_called_once_with(unloaded_instrument.id.symbol.value.split("|")[0])
