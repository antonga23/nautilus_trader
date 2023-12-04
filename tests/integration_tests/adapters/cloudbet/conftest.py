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
from typing import Optional, Union, List

import pytest

from nautilus_trader.model.events.account import AccountState
from nautilus_trader.model.identifiers import Venue, AccountId, TradeId, StrategyId, ClientOrderId

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.common import VENUE
from nautilus_trader.adapters.cloudbet.config import CloudbetDataClientConfig
from nautilus_trader.adapters.cloudbet.data_client import CloudbetDataClient
from nautilus_trader.adapters.cloudbet.execution import CloudbetLiveExecutionClient
from nautilus_trader.adapters.cloudbet.factories import CloudbetLiveDataClientFactory, CloudbetLiveExecClientFactory
from nautilus_trader.adapters.cloudbet.sockets import CloudbetStreamClient
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orders import Order, LimitOrder, MarketOrder
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetTestStubs
from nautilus_trader.common.clock import LiveClock


@pytest.fixture()
def venue() -> Venue:
    return VENUE


@pytest.fixture(autouse=False)
def cloudbet_client(event_loop, logger):
    return CloudbetTestStubs.cloudbet_client(event_loop, logger)


# # live instrument fixture
# @pytest.fixture()
# @pytest.mark.asyncio(autouse=False)
# async def live_instrument(cloudbet_client, clock):
#     await cloudbet_client.connect()
#     # get the current unix timestamp
#
#     clock = LiveClock()
#     current_timestamp = int(clock.timestamp())
#     # get the unixtime 48 hours in the future
#     timestamp_48h = current_timestamp + 172800
#     # sports = ["soccer", "tennis", "baseball", "basketball"]
#     sports = ["basketball"]
#     sport_key = random.choice(sports)
#     event = await cloudbet_client.get_events_for_sport(
#         sport_key,
#         current_timestamp,
#         timestamp_48h,
#         limit=5
#     )
#     selections: List[Selection] = self.client.event_to_selection(event)
#     selection = Selection(**random.choice(selections))
#     instrument = self.provider.selection_to_instrument(selection)
#     return instrument

@pytest.fixture(autouse=True)
def instrument():
    return TestInstrumentProvider.crypto_betting_instrument()


@pytest.fixture(autouse=False)
# request object is a special fixture injected by pytest
def instruments(request) -> list[CryptoBettingInstrument]:
    venue = request.param[0]
    count = request.param[1]
    assert count < 674, f"Can only return a max of  674, got {count}"
    return TestInstrumentProvider.crypto_betting_instruments(venue, count)


@pytest.fixture()
def instrument_provider(cloudbet_client):
    return CloudbetTestStubs.instrument_provider(cloudbet_client=cloudbet_client)


@pytest.fixture()
def data_client(
    mocker,
    event_loop,
    cloudbet_client,
    instrument_provider,
    instrument,
    venue,
    msgbus,
    cache,
    clock,
    logger
) -> CloudbetDataClient:
    mocker.patch("nautilus_trader.adapters.cloudbet.factories.get_cached_cloudbet_client",
                 return_value=cloudbet_client)
    mocker.patch("nautilus_trader.adapters.cloudbet.factories.get_cached_cloudbet_instrument_provider",
                 return_value=instrument_provider)
    instrument_provider.add(instrument)
    data_client = CloudbetLiveDataClientFactory.create(
        loop=event_loop,
        name=venue.value,
        config=CloudbetDataClientConfig(),
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        logger=logger,
    )
    return data_client


@pytest.fixture()
def exec_client(
    mocker,
    event_loop,
    cloudbet_client,
    instrument_provider,
    instrument,
    venue,
    msgbus,
    cache,
    clock,
    logger,
) -> CloudbetLiveExecutionClient:
    mocker.patch("nautilus_trader.adapters.cloudbet.factories.get_cached_cloudbet_client",
                 return_value=cloudbet_client)
    mocker.patch("nautilus_trader.adapters.cloudbet.factories.get_cached_cloudbet_instrument_provider",
                 return_value=instrument_provider)
    instrument_provider.add(instrument)
    exec_client = CloudbetLiveExecClientFactory.create(
        loop=event_loop,
        name=venue.value,
        config=None,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        logger=logger,
        base_currency=None,
    )
    return exec_client


# @pytest.fixture()
# def cloudbet_stream_client(
#     mocker,
#     event_loop,
#     cloudbet_client,
#     instrument_provider,
#     instrument,
#     venue,
#     msgbus,
#     cache,
#     clock,
#     logger,
# ) -> CloudbetStreamClient:
#     mocker.patch("nautilus_trader.adapters.cloudbet.factories.get_cached_cloudbet_client",
#                  return_value=cloudbet_client)
#     mocker.patch("nautilus_trader.adapters.cloudbet.factories.get_cached_cloudbet_instrument_provider",
#                  return_value=instrument_provider)
#     cloudbet_stream_client = CloudbetStreamClient(
#         client=cloudbet_client,
#         logger=logger,
#         message_handler="some callable"

@pytest.fixture()
def account_state() -> AccountState:
    return TestEventStubs.betting_account_state(account_id=AccountId("CLOUDBET-001"))


@pytest.fixture(autouse=False)
def limit_order(instrument_id=None,
                order_side=None,
                price=None,
                quantity=None,
                time_in_force=None,
                trader_id: Optional[TradeId] = None,
                strategy_id: Optional[StrategyId] = None,
                client_order_id: Optional[ClientOrderId] = None,
                expire_time=None,
                tags=None) -> LimitOrder:
    limit_order = TestExecStubs.limit_order(instrument_id=instrument_id,
                                            order_side=order_side,
                                            price=price,
                                            quantity=quantity,
                                            time_in_force=time_in_force,
                                            trader_id=trader_id,
                                            strategy_id=strategy_id,
                                            client_order_id=client_order_id,
                                            expire_time=expire_time,
                                            tags=tags)
    return limit_order


@pytest.fixture(autouse=False)
def market_order(instrument_id=None,
                 order_side=None,
                 quantity=None,
                 trader_id: Optional[TradeId] = None,
                 strategy_id: Optional[StrategyId] = None,
                 client_order_id: Optional[ClientOrderId] = None,
                 time_in_force=None) -> MarketOrder:
    market_order = TestExecStubs.market_order(instrument_id=instrument_id,
                                              order_side=order_side,
                                              quantity=quantity,
                                              trader_id=trader_id,
                                              strategy_id=strategy_id,
                                              client_order_id=client_order_id,
                                              time_in_force=time_in_force)
    return market_order
