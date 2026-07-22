import asyncio
import hashlib
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from nautilus_trader.adapters.betting.runtime_cache import encode_venue_quote_tiers
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_tiers_key
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import RequestInstrument
from nautilus_trader.data.messages import SubscribeInstruments

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.exceptions import CloudbetAPIError
from nautilus_trader.adapters.cloudbet.client.schema import (
    CompetitionWithCategory,
    EventStatus,
    GetEventResponse,
    GetLatestOddsResponse,
    Identifier,
    MarketModel,
    SelectionModel,
    SelectionSide,
    SelectionStatus,
    SubmarketModel,
    TeamIdentifier,
)
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.market_pollability import MarketPollabilityRegistry
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.model.identifiers import ClientId

REVALIDATE_SECS = 600.0
REVALIDATE_DUE_NS = int((REVALIDATE_SECS + 1) * 1_000_000_000)


def build_event_response(instrument, markets) -> GetEventResponse:
    return GetEventResponse(
        sequence="1",
        id=int(instrument.event_id),
        sport=Identifier(name=instrument.sport_name, key="sport"),
        competition=CompetitionWithCategory(
            category=Identifier(name="category", key="category"),
            key="competition",
            name=instrument.competition_name,
        ),
        home=TeamIdentifier(
            abbreviation="H",
            key="home",
            name=instrument.home_name,
            nationality="",
        ),
        away=TeamIdentifier(
            abbreviation="A",
            key="away",
            name=instrument.away_name,
            nationality="",
        ),
        status=EventStatus.TRADING,
        markets=markets,
        name=instrument.event_name,
        key="event",
        cutoff_time="2026-05-07T12:00:00Z",
        type="EVENT",
        end_time="2026-05-07T14:00:00Z",
        grading_duration=None,
    )


def build_markets_with_selection(data_client, instrument, price=2.72) -> dict:
    submarket_key = data_client._preferred_submarket_key(instrument) or "period=ft"
    return {
        instrument.market_name: MarketModel(
            submarkets={
                submarket_key: SubmarketModel(
                    sequence="1",
                    selections=[
                        SelectionModel(
                            outcome=instrument.outcome,
                            params=instrument.params,
                            price=price,
                            minStake=1,
                            maxStake=321,
                            probability=0.36,
                            status=SelectionStatus.ENABLED.value,
                            side=SelectionSide.BACK.value,
                        ),
                    ],
                ),
            },
        ),
    }


async def wait_for_data_client_state(
    data_client,
    *,
    connected: bool,
    timeout: float = 5.0,
) -> None:
    async def state_matches() -> bool:
        if connected:
            return (
                data_client.instrument_provider._loaded is True
                and data_client.is_connected
                and data_client._update_instruments_task is not None
            )

        return not data_client.is_connected

    async with asyncio.timeout(timeout):
        while not await state_matches():
            await asyncio.sleep(0)


class TestCloudbetDataClient:
    # @pytest.mark.dependency()
    @pytest.mark.asyncio
    async def test_connect(self, data_client):
        """Test connect"""
        # Arrange
        # set the intreval to be 2 seconds
        data_client._update_instrument_interval = 2
        # Act
        data_client.connect()
        await wait_for_data_client_state(data_client, connected=True)
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
        with (
            patch.object(
                CloudbetInstrumentProvider,
                "load_ids_async",
                new_callable=AsyncMock,
                side_effect=async_side_effect_load_ids,
            ) as mocked_load_ids,
            patch.object(
                data_client, "_send_all_instruments_to_data_engine", new_callable=MagicMock
            ),
        ):
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
        for call in mocked_load_ids.await_args_list:
            assert isinstance(call.args[0], list)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 552)], indirect=["instruments"])
    # @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.connect")
    async def test_update_instruments(self, instrument_provider, data_client, instruments):
        """Test _update_instruments_task"""
        # Arrange,
        data_client._update_instrument_interval = 2  # set the inteval to be 2 seconds
        # Act
        data_client.connect()
        await wait_for_data_client_state(data_client, connected=True)
        # Assert
        # assert the task has been created
        assert data_client._update_instrument_interval == 2
        assert data_client._update_instruments_task is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 552)], indirect=["instruments"])
    async def test_send_instruments_data_engine(self, data_client, instruments, data_engine):
        """Test _send_all_instruments_to_data_engine"""
        # Arrange
        cache_instruments = set(data_client._cache.instrument_ids(venue=CLOUDBET_VENUE))
        instrument_ids = {instrument.id for instrument in instruments}
        expected_cache_instruments = cache_instruments | instrument_ids
        # Arrange, Act
        # load instruments into data engine and cache
        data_client._send_all_instruments_to_data_engine(instruments=instruments)
        updated_cache_instruments = set(data_client._cache.instrument_ids(venue=CLOUDBET_VENUE))
        assert expected_cache_instruments == updated_cache_instruments, (
            "Expected cache to contain the preloaded Cloudbet instruments plus the "
            f"{len(instrument_ids)} instruments sent to the DataEngine, got "
            f"{len(updated_cache_instruments)} instrument ids"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    async def test_subscribe_instruments_republishes_loaded_instruments(
        self,
        data_client,
        instruments,
        monkeypatch,
    ):
        """Venue-level subscriptions must publish loaded instruments to strategies."""
        for instrument in instruments:
            data_client.instrument_provider.add(instrument)
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)

        command = SubscribeInstruments(
            client_id=ClientId(CLOUDBET_VENUE.value),
            venue=CLOUDBET_VENUE,
            command_id=UUID4(),
            ts_init=data_client._clock.timestamp_ns(),
            params=None,
        )

        await data_client._subscribe_instruments(command)

        assert {instrument.id for instrument in handled}.issuperset(
            {instrument.id for instrument in instruments},
        )
        assert set(data_client.subscribed_instruments()).issuperset(
            {instrument.id for instrument in instruments},
        )

    # test disconnect
    @pytest.mark.asyncio
    @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.disconnect")
    async def test_disconnect(self, mock_stream_disconnect, data_client):
        """Test disconnect"""
        # TODO: use pytest dependency or a mock to cause side effecsts, instead of manual connect()
        # @pytest.mark.dependency(depends=["TestCloudbetDataClient::test_connect_without_stream"])
        # Arrange, Act
        data_client.connect()
        await wait_for_data_client_state(data_client, connected=True)
        # check client was connected
        assert data_client.is_connected
        # await asyncio.sleep(30) # sleeps are required, otherwise AssertionError Task was destroyed but it is pending!
        data_client.disconnect()
        await wait_for_data_client_state(data_client, connected=False)
        # Assert
        assert not data_client.is_connected, (
            f"Expected data client to be disconnected, got {data_client.is_connected}"
        )

    @pytest.mark.asyncio
    async def test_reset(self, data_client):
        """Test _reset"""
        data_client._update_instruments_task = asyncio.create_task(asyncio.sleep(3600))
        await data_client._reset()
        # TODO: make pythonic
        assert data_client.subscribed_selection_ids == set()
        assert data_client.subscribed_orderbooks == {}
        assert data_client.subscribed_event_ids == {}
        assert data_client.subscribed_market_names == {}
        assert data_client._update_instruments_task is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_poll_quote_ticks_publishes_latest_cloudbet_price(
        self,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)
        mock_get_latest_odds.return_value = GetLatestOddsResponse(
            max_stake=123,
            min_stake=1,
            price=2.34,
            status=SelectionStatus.ENABLED,
            outcome=instrument.outcome,
            params=instrument.params,
            probability=0.3,
            side=instrument.side,
        )

        published, requested = await data_client._poll_quote_ticks_once()

        assert requested == 1
        assert published == 1
        assert len(handled) == 1
        quote = handled[0]
        assert quote.instrument_id == instrument.id
        assert quote.ask_price.as_decimal() > 0
        assert quote.bid_price.as_decimal() == 0
        assert quote.ts_init >= quote.ts_event
        mock_get_latest_odds.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_event", new_callable=AsyncMock)
    async def test_poll_quote_ticks_uses_event_batch_before_line_fallback(
        self,
        mock_get_event,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)
        submarket_key = data_client._preferred_submarket_key(instrument) or "period=ft"
        mock_get_event.return_value = GetEventResponse(
            sequence="1",
            id=int(instrument.event_id),
            sport=Identifier(name=instrument.sport_name, key="sport"),
            competition=CompetitionWithCategory(
                category=Identifier(name="category", key="category"),
                key="competition",
                name=instrument.competition_name,
            ),
            home=TeamIdentifier(
                abbreviation="H",
                key="home",
                name=instrument.home_name,
                nationality="",
            ),
            away=TeamIdentifier(
                abbreviation="A",
                key="away",
                name=instrument.away_name,
                nationality="",
            ),
            status=EventStatus.TRADING,
            markets={
                instrument.market_name: MarketModel(
                    submarkets={
                        submarket_key: SubmarketModel(
                            sequence="1",
                            selections=[
                                SelectionModel(
                                    outcome=instrument.outcome,
                                    params=instrument.params,
                                    price=2.72,
                                    minStake=1,
                                    maxStake=321,
                                    probability=0.36,
                                    status=SelectionStatus.ENABLED.value,
                                    side=SelectionSide.BACK.value,
                                ),
                            ],
                        ),
                    },
                ),
            },
            name=instrument.event_name,
            key="event",
            cutoff_time="2026-05-07T12:00:00Z",
            type="EVENT",
            end_time="2026-05-07T14:00:00Z",
            grading_duration=None,
        )

        published, requested = await data_client._poll_quote_ticks_once()

        assert requested == 1
        assert published == 1
        assert len(handled) == 1
        assert handled[0].instrument_id == instrument.id
        assert handled[0].ask_price.as_decimal() > 0
        mock_get_event.assert_called_once_with(int(instrument.event_id))
        mock_get_latest_odds.assert_not_called()
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"request_count":1' in stats
        assert b'"event_request_count":1' in stats
        assert b'"line_request_count":0' in stats
        assert b'"fetch_latency_p95_secs"' in stats
        assert b'"pruned_subscription_count":0' in stats

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_event", new_callable=AsyncMock)
    async def test_poll_quote_ticks_prunes_consecutive_missing_cloudbet_selection(
        self,
        mock_get_event,
        mock_get_latest_odds,
        data_client,
        instruments,
    ):
        instrument = instruments[0]
        replacement = instruments[1]
        data_client.instrument_provider.add(instrument)
        data_client.instrument_provider.add(replacement)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._quote_poll_missing_prune_threshold = 1
        mock_get_event.return_value = GetEventResponse(
            sequence="1",
            id=int(instrument.event_id),
            sport=Identifier(name=instrument.sport_name, key="sport"),
            competition=CompetitionWithCategory(
                category=Identifier(name="category", key="category"),
                key="competition",
                name=instrument.competition_name,
            ),
            home=TeamIdentifier(
                abbreviation="H",
                key="home",
                name=instrument.home_name,
                nationality="",
            ),
            away=TeamIdentifier(
                abbreviation="A",
                key="away",
                name=instrument.away_name,
                nationality="",
            ),
            status=EventStatus.TRADING,
            markets={},
            name=instrument.event_name,
            key="event",
            cutoff_time="2026-05-07T12:00:00Z",
            type="EVENT",
            end_time="2026-05-07T14:00:00Z",
            grading_duration=None,
        )
        mock_get_latest_odds.return_value = GetLatestOddsResponse(
            max_stake=0,
            min_stake=0,
            price=0,
            status=SelectionStatus.DISABLED,
            outcome=instrument.outcome,
            params=instrument.params,
            probability=0,
            side=SelectionSide.UNDEFINED,
        )

        published, requested = await data_client._poll_quote_ticks_once()

        assert requested == 1
        assert published == 0
        assert instrument.id not in data_client._subscribed_quote_instruments
        assert len(data_client._subscribed_quote_instruments) == 1
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"pruned_subscription_count":1' in stats
        assert b'"refilled_subscription_count":1' in stats
        assert b'"event_request_count":1' in stats
        assert b'"line_request_count":1' in stats
        mock_get_latest_odds.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_poll_quote_ticks_prunes_delisted_instrument_after_consecutive_404s(
        self,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._quote_poll_event_batching = False
        data_client._quote_poll_missing_prune_threshold = 3
        mock_get_latest_odds.side_effect = CloudbetAPIError(
            message="Failed to retrieve latests odds from the Cloudbet API.",
            code=404,
        )
        prune_events = []
        record_missing = data_client._record_missing_quote_subscription

        def recording_record_missing(instrument_id, **kwargs):
            pruned = record_missing(instrument_id, **kwargs)
            if pruned:
                prune_events.append((instrument_id, kwargs.get("reason")))
            return pruned

        monkeypatch.setattr(
            data_client,
            "_record_missing_quote_subscription",
            recording_record_missing,
        )

        for _ in range(3):
            await data_client._poll_quote_ticks_once()

        assert instrument.id not in data_client._subscribed_quote_instruments
        assert prune_events == [(instrument.id, "consecutive 404s (delisted event)")]
        assert mock_get_latest_odds.await_count == 3
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"failure_count":0' in stats
        assert b'"delisted_count":1' in stats
        assert b'"pruned_subscription_count":1' in stats

        mock_get_latest_odds.reset_mock()
        await data_client._poll_quote_ticks_once()

        polled_event_ids = {
            str(call.kwargs["event_id"]) for call in mock_get_latest_odds.await_args_list
        }
        assert str(instrument.event_id) not in polled_event_ids
        assert instrument.id not in data_client._subscribed_quote_instruments

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_poll_quote_ticks_resets_404_count_on_success(
        self,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._quote_poll_event_batching = False
        data_client._quote_poll_missing_prune_threshold = 3
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)
        delisted_error = CloudbetAPIError(
            message="Failed to retrieve latests odds from the Cloudbet API.",
            code=404,
        )
        odds = GetLatestOddsResponse(
            max_stake=123,
            min_stake=1,
            price=2.34,
            status=SelectionStatus.ENABLED,
            outcome=instrument.outcome,
            params=instrument.params,
            probability=0.3,
            side=instrument.side,
        )
        mock_get_latest_odds.side_effect = [delisted_error, delisted_error, odds]

        for _ in range(3):
            await data_client._poll_quote_ticks_once()

        assert instrument.id in data_client._subscribed_quote_instruments
        assert instrument.id not in data_client._quote_poll_missing_counts
        assert len(handled) == 1
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"pruned_subscription_count":0' in stats

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 5)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_poll_quote_ticks_prunes_delisted_without_affecting_healthy(
        self,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        delisted = instruments[0]
        healthy = [
            instrument
            for instrument in instruments[1:]
            if str(instrument.event_id) != str(delisted.event_id)
        ]
        assert healthy, "expected at least one instrument on another event"
        for instrument in [delisted, *healthy]:
            data_client.instrument_provider.add(instrument)
            data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._quote_poll_event_batching = False
        data_client._quote_poll_missing_prune_threshold = 3
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)

        async def fetch_odds(event_id, market_url):
            if str(event_id) == str(delisted.event_id):
                raise CloudbetAPIError(
                    message="Failed to retrieve latests odds from the Cloudbet API.",
                    code=404,
                )
            return GetLatestOddsResponse(
                max_stake=123,
                min_stake=1,
                price=2.34,
                status=SelectionStatus.ENABLED,
                outcome="outcome",
                params="",
                probability=0.3,
                side=SelectionSide.BACK,
            )

        mock_get_latest_odds.side_effect = fetch_odds

        for _ in range(3):
            await data_client._poll_quote_ticks_once()

        assert delisted.id not in data_client._subscribed_quote_instruments
        for instrument in healthy:
            assert instrument.id in data_client._subscribed_quote_instruments
        assert len(handled) == len(healthy) * 3
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"failure_count":0' in stats

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 5)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_event", new_callable=AsyncMock)
    async def test_poll_quote_ticks_prunes_delisted_event_via_event_batch_fallback(
        self,
        mock_get_event,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        delisted = instruments[0]
        healthy = next(
            instrument
            for instrument in instruments[1:]
            if str(instrument.event_id) != str(delisted.event_id)
        )
        for instrument in (delisted, healthy):
            data_client.instrument_provider.add(instrument)
            data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._quote_poll_missing_prune_threshold = 3
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)

        async def fetch_event(event_id, *args, **kwargs):
            if int(event_id) == int(delisted.event_id):
                raise CloudbetAPIError(
                    message="Failed to retrieve event from the Cloudbet API.",
                    code=404,
                )
            return build_event_response(
                healthy,
                build_markets_with_selection(data_client, healthy),
            )

        async def fetch_odds(event_id, market_url):
            raise CloudbetAPIError(
                message="Failed to retrieve latests odds from the Cloudbet API.",
                code=404,
            )

        mock_get_event.side_effect = fetch_event
        mock_get_latest_odds.side_effect = fetch_odds

        for _ in range(3):
            await data_client._poll_quote_ticks_once()

        assert delisted.id not in data_client._subscribed_quote_instruments
        assert healthy.id in data_client._subscribed_quote_instruments
        assert len(handled) == 3
        assert all(quote.instrument_id == healthy.id for quote in handled)
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"failure_count":0' in stats
        assert b'"pruned_subscription_count":1' in stats

    def test_quote_poll_schedule_adapts_cloudbet_concurrency(self, data_client):
        data_client._quote_poll_concurrency = 4
        data_client._quote_poll_max_concurrency = 16
        data_client._quote_poll_target_cycle_secs = 5.0

        data_client._adapt_quote_poll_schedule(
            instrument_count=80,
            cycle_elapsed=8.0,
            rate_limit_count=0,
            failure_count=0,
        )

        assert data_client._quote_poll_concurrency > 4
        assert data_client._next_quote_poll_sleep_secs == 0.25

        data_client._adapt_quote_poll_schedule(
            instrument_count=80,
            cycle_elapsed=8.0,
            rate_limit_count=1,
            failure_count=1,
        )

        assert data_client._quote_poll_concurrency >= data_client._quote_poll_min_concurrency

    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    def test_auto_subscribe_prioritizes_enabled_liquid_cloudbet_instruments(
        self,
        data_client,
        instruments,
        monkeypatch,
    ):
        disabled_instrument, enabled_instrument = instruments
        disabled_instrument.enabled = False
        disabled_instrument.max_size = 9999
        enabled_instrument.enabled = True
        enabled_instrument.max_size = 10
        data_client.instrument_provider._instruments.clear()
        data_client.instrument_provider.add(disabled_instrument)
        data_client.instrument_provider.add(enabled_instrument)
        data_client._quote_polling_enabled = True
        data_client._config = SimpleNamespace(quote_subscription_limit=1)
        monkeypatch.setattr(data_client, "_start_quote_polling", lambda: None)

        selected = data_client._auto_subscribe_loaded_instruments()

        assert selected == 1
        assert enabled_instrument.id in data_client._subscribed_quote_instruments
        assert disabled_instrument.id not in data_client._subscribed_quote_instruments

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    # @patch('nautilus_trader.adapters.cloudbet.client.core.CloudbetClient.get_latest_odds')
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_request_instrument_loaded_instrument(
        self, mock_get_latest_odds, data_client, data_engine, instruments
    ):
        """Test _request with an instrument already in cache"""
        # Arrange
        request_id = UUID4()  # have to call UUID4(value=None) outside await
        assert request_id is not None, f"Expected request_id to be UUID4, got {request_id}"
        # create session for instrument provider
        if data_client._client.connected is False:
            await data_client._client.connect()
        data_client.instrument_provider.add(instruments[0])
        loaded_instrument = data_client.instrument_provider.find(instruments[0].id)
        assert instruments[0] == loaded_instrument, (
            f"Unable to load or find instrument {instruments[0].id}"
        )
        # Create a mock response
        mock_response = GetLatestOddsResponse(
            max_stake=loaded_instrument.max_size - 1
            if loaded_instrument.max_size is not None
            else 0,  # selection may be DISBALED => NO max stake even if the event is trading
            # subtract 1 from the max stake to ensure the max stake is different
            min_stake=loaded_instrument.min_size + 1
            if loaded_instrument.min_size is not None
            else 0,  # add 1 to the min stake to ensure the min stake is different, selection may be DISBALED => NO max stake even if the event is trading
            price=loaded_instrument.price
            + 1,  # add 1 to the price to ensure the price is different
            status=SelectionStatus.ENABLED,
            outcome=loaded_instrument.outcome,
            params=loaded_instrument.params,
            probability=0.3,  # random probability => required arguemnt
            side=loaded_instrument.side,
        )

        mock_get_latest_odds.return_value = mock_response
        # track current DataEngine response count
        current_response_count = data_engine.response_count
        # Act
        request = RequestInstrument(
            instrument_id=loaded_instrument.id,
            start=None,
            end=None,
            client_id=ClientId(CLOUDBET_VENUE.value),
            venue=CLOUDBET_VENUE,
            callback=lambda x: None,
            request_id=request_id,
            ts_init=data_client._clock.timestamp_ns(),
            params=None,
        )
        await data_client._request_instrument(request)
        mock_get_latest_odds.assert_called_once()
        # Assert
        # we want to assert that the instrument has been updated with the new data
        assert loaded_instrument.max_size == mock_response.max_stake, (
            f"Expected max stake to be {mock_response.max_stake}, got {loaded_instrument.max_size}"
        )
        assert loaded_instrument.min_size == mock_response.min_stake, (
            f"Expected min stake to be {mock_response.min_stake}, got {loaded_instrument.min_size}"
        )
        assert loaded_instrument.price == mock_response.price, (
            f"Expected price to be {mock_response.price}, got {loaded_instrument.price}"
        )

        # assert the instrument has been added to the cache
        assert data_client._cache.load_instrument(loaded_instrument.id) == instruments[0], (
            f"Expected {loaded_instrument.id} to be in cache, got {data_client._cache.load_instrument(loaded_instrument.id)}"
        )
        # check data response was received by the DataEngine
        updated_response_count = data_engine.response_count
        assert updated_response_count - current_response_count == 1, (
            f"Expected {updated_response_count} responses, got {current_response_count}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_event", new_callable=AsyncMock)
    async def test_poll_quote_ticks_skips_line_fallback_for_confirmed_absent_market(
        self,
        mock_get_event,
        mock_get_latest_odds,
        data_client,
        instruments,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._requested_quote_instruments.add(instrument.id)
        data_client._market_pollability = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=REVALIDATE_SECS,
        )
        mock_get_event.return_value = build_event_response(instrument, markets={})
        mock_get_latest_odds.side_effect = CloudbetAPIError("market not found", code="404")

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.call_count == 1
        event_id = int(str(instrument.event_id))
        market_key = str(instrument.market_name)
        assert data_client._market_pollability.is_poll_suppressed(
            event_id,
            market_key,
            data_client._clock.timestamp_ns(),
        )

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.call_count == 1
        assert mock_get_event.call_count == 1
        assert instrument.id in data_client._subscribed_quote_instruments
        assert instrument.id in data_client._requested_quote_instruments
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"event_request_count":0' in stats
        assert b'"line_request_count":0' in stats
        assert b'"tombstone_skipped_count":1' in stats
        assert b'"tombstoned_market_count":1' in stats

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_event", new_callable=AsyncMock)
    async def test_poll_quote_ticks_revalidates_absent_market_on_schedule(
        self,
        mock_get_event,
        mock_get_latest_odds,
        data_client,
        instruments,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._requested_quote_instruments.add(instrument.id)
        data_client._market_pollability = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=REVALIDATE_SECS,
        )
        mock_get_event.return_value = build_event_response(instrument, markets={})
        mock_get_latest_odds.side_effect = CloudbetAPIError("market not found", code="404")

        for _ in range(3):
            await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.call_count == 1

        data_client._clock.set_time(REVALIDATE_DUE_NS)
        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.call_count == 2

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.call_count == 2
        assert data_client._market_pollability.is_poll_suppressed(
            int(str(instrument.event_id)),
            str(instrument.market_name),
            data_client._clock.timestamp_ns(),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_event", new_callable=AsyncMock)
    async def test_poll_quote_ticks_evicts_absent_market_when_it_appears(
        self,
        mock_get_event,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._requested_quote_instruments.add(instrument.id)
        data_client._market_pollability = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=REVALIDATE_SECS,
        )
        event_id = int(str(instrument.event_id))
        market_key = str(instrument.market_name)
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)
        mock_get_event.return_value = build_event_response(instrument, markets={})
        mock_get_latest_odds.side_effect = CloudbetAPIError("market not found", code="404")

        await data_client._poll_quote_ticks_once()

        assert data_client._market_pollability.is_poll_suppressed(
            event_id,
            market_key,
            data_client._clock.timestamp_ns(),
        )

        mock_get_event.return_value = build_event_response(
            instrument,
            markets=build_markets_with_selection(data_client, instrument),
        )

        published, _ = await data_client._poll_quote_ticks_once()

        assert published == 0

        data_client._clock.set_time(REVALIDATE_DUE_NS)
        published, _ = await data_client._poll_quote_ticks_once()

        assert published == 1
        assert handled[0].instrument_id == instrument.id
        assert not data_client._market_pollability.is_poll_suppressed(
            event_id,
            market_key,
            data_client._clock.timestamp_ns(),
        )

        mock_get_event.return_value = build_event_response(instrument, markets={})

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 2)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_event", new_callable=AsyncMock)
    async def test_poll_quote_ticks_absent_cache_publishes_identical_ticks(
        self,
        mock_get_event,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        present, absent = instruments
        for instrument in instruments:
            data_client.instrument_provider.add(instrument)
            data_client._subscribed_quote_instruments.add(instrument.id)
            data_client._requested_quote_instruments.add(instrument.id)
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)
        events = {
            int(present.event_id): build_event_response(
                present,
                markets=build_markets_with_selection(data_client, present),
            ),
        }
        events.setdefault(int(absent.event_id), build_event_response(absent, markets={}))
        mock_get_event.side_effect = lambda event_id: events[int(event_id)]
        mock_get_latest_odds.side_effect = CloudbetAPIError("market not found", code="404")

        cached_published = []
        for _ in range(3):
            handled.clear()
            await data_client._poll_quote_ticks_once()
            cached_published.append(
                sorted((str(q.instrument_id), str(q.ask_price)) for q in handled),
            )

        no_cache_published = []
        for _ in range(3):
            data_client._market_pollability.clear()
            handled.clear()
            await data_client._poll_quote_ticks_once()
            no_cache_published.append(
                sorted((str(q.instrument_id), str(q.ask_price)) for q in handled),
            )

        assert cached_published == no_cache_published
        assert all(published == cached_published[0] for published in cached_published)
        assert [instrument_id for instrument_id, _ in cached_published[0]] == [str(present.id)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_poll_quote_ticks_tombstones_requested_instrument_without_pruning(
        self,
        mock_get_latest_odds,
        data_client,
        instruments,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._requested_quote_instruments.add(instrument.id)
        data_client._quote_poll_event_batching = False
        mock_get_latest_odds.side_effect = CloudbetAPIError(
            message="Failed to retrieve latests odds from the Cloudbet API.",
            code=404,
        )

        for _ in range(3):
            await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 3
        assert instrument.id in data_client._subscribed_quote_instruments
        assert instrument.id in data_client._requested_quote_instruments

        mock_get_latest_odds.reset_mock()
        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 0
        assert instrument.id in data_client._subscribed_quote_instruments
        assert instrument.id in data_client._requested_quote_instruments
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"line_request_count":0' in stats
        assert b'"tombstone_skipped_count":1' in stats
        assert b'"tombstoned_market_count":1' in stats

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 552)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_poll_quote_ticks_shares_revalidation_probe_across_sibling_selections(
        self,
        mock_get_latest_odds,
        data_client,
        instruments,
        monkeypatch,
    ):
        by_market = defaultdict(list)
        for instrument in instruments:
            by_market[(int(str(instrument.event_id)), str(instrument.market_name))].append(
                instrument,
            )
        siblings = next(group for group in by_market.values() if len(group) >= 2)[:2]
        for instrument in siblings:
            data_client.instrument_provider.add(instrument)
            data_client._subscribed_quote_instruments.add(instrument.id)
            data_client._requested_quote_instruments.add(instrument.id)
        data_client._quote_poll_event_batching = False
        data_client._market_pollability = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=REVALIDATE_SECS,
        )
        handled = []
        monkeypatch.setattr(data_client, "_handle_data", handled.append)
        mock_get_latest_odds.side_effect = CloudbetAPIError("market not found", code="404")

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 2

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 2
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"tombstone_skipped_count":2' in stats
        assert b'"tombstoned_market_count":1' in stats

        data_client._clock.set_time(REVALIDATE_DUE_NS)
        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 3
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"revalidation_probe_count":1' in stats
        assert b'"tombstone_skipped_count":1' in stats

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 3

        data_client._clock.set_time(2 * REVALIDATE_DUE_NS)
        mock_get_latest_odds.side_effect = None
        mock_get_latest_odds.return_value = GetLatestOddsResponse(
            max_stake=123,
            min_stake=1,
            price=2.34,
            status=SelectionStatus.ENABLED,
            outcome=siblings[0].outcome,
            params=siblings[0].params,
            probability=0.3,
            side=siblings[0].side,
        )

        published, _ = await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 4
        assert published == 1

        published, _ = await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 6
        assert published == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    @patch.object(CloudbetClient, "get_latest_odds", new_callable=AsyncMock)
    async def test_poll_quote_ticks_treats_malformed_request_as_structurally_unpollable(
        self,
        mock_get_latest_odds,
        data_client,
        instruments,
    ):
        instrument = instruments[0]
        data_client.instrument_provider.add(instrument)
        data_client._subscribed_quote_instruments.add(instrument.id)
        data_client._requested_quote_instruments.add(instrument.id)
        data_client._quote_poll_event_batching = False
        data_client._market_pollability = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=REVALIDATE_SECS,
        )
        mock_get_latest_odds.side_effect = CloudbetAPIError(
            message=(
                "Failed to retrieve latests odds from the Cloudbet API: "
                '{"code":"MALFORMED_REQUEST"}'
            ),
            code=400,
        )

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 1

        await data_client._poll_quote_ticks_once()

        assert mock_get_latest_odds.await_count == 1
        assert instrument.id in data_client._subscribed_quote_instruments
        stats = data_client._cache.get("betting:venue_quote_poll_stats:CLOUDBET")
        assert b'"tombstone_skipped_count":1' in stats
        assert b'"tombstoned_market_count":1' in stats

    def test_inject_pollability_registry_into_provider(self, data_client):
        assert data_client.instrument_provider._pollability_registry is None

        data_client._inject_pollability_registry()

        assert (
            data_client.instrument_provider._pollability_registry
            is data_client._market_pollability
        )

        data_client._inject_pollability_registry()

        assert (
            data_client.instrument_provider._pollability_registry
            is data_client._market_pollability
        )

    def test_inject_pollability_registry_disabled_by_config(self, data_client):
        from nautilus_trader.adapters.cloudbet.config import CloudbetDataClientConfig

        data_client._config = CloudbetDataClientConfig(
            quote_poll_unpollable_discovery_exclusion=False,
        )

        data_client._inject_pollability_registry()

        assert data_client.instrument_provider._pollability_registry is None


def _tier_event_hash(seed: object) -> int:
    return int.from_bytes(
        hashlib.blake2b(str(seed).encode("utf-8"), digest_size=8).digest(),
        "big",
    )


class TestCloudbetQuoteTierScheduling:
    @staticmethod
    def _make_instrument(base, *, event_id, market_name, outcome):
        from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument

        data = dict(CryptoBettingInstrument.to_dict(base))
        data["event_id"] = str(event_id)
        data["market_name"] = market_name
        data["outcome"] = outcome
        return CryptoBettingInstrument.from_dict(data)

    def _register(self, data_client, instruments):
        for instrument in instruments:
            data_client.instrument_provider.add(instrument)
            data_client._subscribed_quote_instruments.add(instrument.id)
        return sorted(data_client._subscribed_quote_instruments, key=str)

    @staticmethod
    def _publish_tiers(data_client, tier_by_instrument_id):
        data_client._cache.add(
            venue_quote_tiers_key("CLOUDBET"),
            encode_venue_quote_tiers(
                venue="CLOUDBET",
                updated_at_ns=1,
                tier_by_instrument_id=tier_by_instrument_id,
                tier_intervals={"hot": 1, "warm": 5, "cold": 30},
            ),
        )

    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    def test_hot_tier_is_due_every_cycle(self, data_client, instruments):
        hot = self._make_instrument(
            instruments[0],
            event_id=910001,
            market_name="soccer.match_odds",
            outcome="home",
        )
        subscribed = self._register(data_client, [hot])
        data_client._quote_tier_scheduling_enabled = True
        self._publish_tiers(data_client, {str(hot.id): "hot"})

        for cycle in range(6):
            data_client._quote_poll_cycle_id = cycle
            selected, _skipped, _probes, tier_counts = (
                data_client._select_pollable_quote_instruments(subscribed, now_ns=1)
            )
            assert selected == subscribed
            assert tier_counts == {"due": 1, "hot": 1, "warm": 0, "cold": 0}

    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    def test_warm_tier_skips_between_due_cycles(self, data_client, instruments):
        event_id = 920002
        warm = self._make_instrument(
            instruments[0],
            event_id=event_id,
            market_name="soccer.totals",
            outcome="over",
        )
        subscribed = self._register(data_client, [warm])
        data_client._quote_tier_scheduling_enabled = True
        self._publish_tiers(data_client, {str(warm.id): "warm"})

        event_hash = _tier_event_hash(event_id)
        due_cycle = (5 - event_hash % 5) % 5  # (due_cycle + event_hash) % 5 == 0
        skip_cycle = due_cycle + 1

        data_client._quote_poll_cycle_id = due_cycle
        selected_due, _s, _p, counts_due = data_client._select_pollable_quote_instruments(
            subscribed,
            now_ns=1,
        )
        assert selected_due == subscribed
        assert counts_due == {"due": 1, "hot": 0, "warm": 1, "cold": 0}

        data_client._quote_poll_cycle_id = skip_cycle
        selected_skip, _s2, _p2, counts_skip = data_client._select_pollable_quote_instruments(
            subscribed,
            now_ns=1,
        )
        assert selected_skip == []
        # The instrument is still tier-counted even on a skipped cycle.
        assert counts_skip == {"due": 0, "hot": 0, "warm": 1, "cold": 0}

    def test_stable_event_hash_and_due_formula_are_deterministic(self, data_client):
        assert data_client._stable_event_hash("42") == _tier_event_hash("42")
        assert data_client._stable_event_hash("42") == data_client._stable_event_hash("42")
        # Distinct events stagger onto distinct phases (not simultaneously due).
        assert _tier_event_hash(111) % 5 != _tier_event_hash(112) % 5

        intervals = {"hot": 1, "warm": 5, "cold": 30}
        for cycle in range(40):
            data_client._quote_poll_cycle_id = cycle
            expected = (cycle + _tier_event_hash(777)) % 30 == 0
            assert data_client._quote_tier_due("cold", intervals, 777) is expected
            assert data_client._quote_tier_due("hot", intervals, 777) is True

    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    def test_event_min_tier_rides_hot_sibling(self, data_client, instruments):
        event_id = 930003
        hot = self._make_instrument(
            instruments[0],
            event_id=event_id,
            market_name="soccer.match_odds",
            outcome="home",
        )
        cold = self._make_instrument(
            instruments[0],
            event_id=event_id,
            market_name="soccer.correct_score",
            outcome="2_1",
        )
        subscribed = self._register(data_client, [hot, cold])
        data_client._quote_tier_scheduling_enabled = True
        self._publish_tiers(data_client, {str(hot.id): "hot", str(cold.id): "cold"})

        # A cycle where the cold instrument on its own (interval 30) would be skipped.
        event_hash = _tier_event_hash(event_id)
        due_cycle = (30 - event_hash % 30) % 30
        data_client._quote_poll_cycle_id = (due_cycle + 1) % 30
        assert (data_client._quote_poll_cycle_id + event_hash) % 30 != 0

        selected, _skipped, _probes, tier_counts = (
            data_client._select_pollable_quote_instruments(subscribed, now_ns=1)
        )

        assert set(selected) == set(subscribed)
        assert tier_counts == {"due": 2, "hot": 1, "warm": 0, "cold": 1}

    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    def test_fail_open_all_hot_when_blob_missing(self, data_client, instruments):
        one = self._make_instrument(
            instruments[0],
            event_id=940004,
            market_name="soccer.match_odds",
            outcome="home",
        )
        two = self._make_instrument(
            instruments[0],
            event_id=940005,
            market_name="soccer.totals",
            outcome="over",
        )
        subscribed = self._register(data_client, [one, two])
        data_client._quote_tier_scheduling_enabled = True
        # No blob published -> fail open, everything treated as hot every cycle.

        for cycle in (0, 3, 29):
            data_client._quote_poll_cycle_id = cycle
            selected, _skipped, _probes, tier_counts = (
                data_client._select_pollable_quote_instruments(subscribed, now_ns=1)
            )
            assert set(selected) == set(subscribed)
            assert tier_counts == {"due": 2, "hot": 2, "warm": 0, "cold": 0}

    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 1)], indirect=["instruments"])
    def test_flag_off_ignores_blob_and_polls_all(self, data_client, instruments):
        event_id = 950006
        cold = self._make_instrument(
            instruments[0],
            event_id=event_id,
            market_name="soccer.correct_score",
            outcome="1_0",
        )
        subscribed = self._register(data_client, [cold])
        # Flag stays off (default); a blob that would defer the instrument is ignored.
        assert data_client._quote_tier_scheduling_enabled is False
        self._publish_tiers(data_client, {str(cold.id): "cold"})
        data_client._quote_poll_cycle_id = 7

        selected, _skipped, _probes, tier_counts = (
            data_client._select_pollable_quote_instruments(subscribed, now_ns=1)
        )

        assert selected == subscribed
        assert tier_counts == {"due": 0, "hot": 0, "warm": 0, "cold": 0}
