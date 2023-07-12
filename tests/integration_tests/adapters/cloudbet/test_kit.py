import bz2
import contextlib
import gzip
import pathlib
from asyncio import Future
from ssl import SSLContext
from typing import Optional, Union
from unittest.mock import MagicMock
from unittest.mock import patch

import msgspec
import numpy as np
import pandas as pd
from aiohttp import ClientResponse
from betfair_parser.spec.streaming import MCM
from betfair_parser.spec.streaming import STREAM_DECODER
from betfair_parser.spec.streaming.ocm import OCM
from betfair_parser.spec.streaming.ocm import MatchedOrder
from betfair_parser.spec.streaming.ocm import OrderAccountChange
from betfair_parser.spec.streaming.ocm import OrderChanges
from betfair_parser.spec.streaming.ocm import UnmatchedOrder

from nautilus_trader.adapters.betfair.client.core import BetfairClient
from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.betfair.common import BETFAIR_VENUE
from nautilus_trader.adapters.betfair.data import BetfairParser
from nautilus_trader.adapters.betfair.historic import make_betfair_reader
from nautilus_trader.adapters.betfair.providers import BetfairInstrumentProvider
from nautilus_trader.adapters.betfair.providers import market_definition_to_instruments
from nautilus_trader.adapters.cloudbet.client.schema import GetAccountInfoResponse, GetSportsResponse, \
    GetEventsForSportResponse, Selection
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import BacktestDataConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import BacktestRunConfig
from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import RiskEngineConfig
from nautilus_trader.config import StreamingConfig
from nautilus_trader.model.data.book import OrderBookDelta
from nautilus_trader.model.data.tick import TradeTick
from nautilus_trader.model.instruments.betting import BettingInstrument
from nautilus_trader.persistence.external.readers import LinePreprocessor
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
import random

from tests import TESTS_PACKAGE_ROOT

TEST_PATH = pathlib.Path(TESTS_PACKAGE_ROOT + "/integration_tests/adapters/cloudbet/resources/")
DATA_PATH = pathlib.Path(TESTS_PACKAGE_ROOT + "/test_data/cloudbet")

test_api_key = "eyJhbGciOiJSUzI1NiIsImtpZCI6IkhKcDkyNnF3ZXBjNnF3LU9rMk4zV05pXzBrRFd6cEdwTzAxNlRJUjdRWDAiLCJ0eXAiOiJKV1QifQ.eyJhY2Nlc3NfdGllciI6InRyYWRpbmciLCJleHAiOjIwMDI2MjM5MDgsImlhdCI6MTY4NzI2MzkwOCwianRpIjoiMzlkMTgwODYtNWYxNy00Y2QxLTg5NDEtODU1YzQ4ODAyNWYyIiwic3ViIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3IiwidGVuYW50IjoiY2xvdWRiZXQiLCJ1dWlkIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3In0.Sn7cONVxnz3hmbiWYh8TB0jK_yx86rZ6S-Pd2bw1b0WTA5MK88nHbYmGtHC8Wu8tDegvE5dK_bo-Ra0pcB50Hg-oa_1IkLTh3XwG7aT6tfzg61Qj0_vfkPhw2UPjVrSGw3w8bRxFNXldB3ls1xk2C-5M-f-PA7aPSoG5ebXOGsjmno-rV7HQJ_48xjF8QgLEtt9daxHQAmQ8DNzoAwKJ2ILZHg09GAL2Lfi5m48NMYAUYgInn20QIJVlcDqljltPUG5JQPtWGlVsyMIDz1QwobpcxjdE3zbhHnES64kD3eqjuKX52vMgmeDLgJvth5LbzTgxgHhZl2t9lyr_-x7lig"
test_api_url = "https://sports-api.cloudbet.com/pub"


# monkey patch MagicMock
async def async_magic():
    pass

MagicMock.__await__ = lambda x: async_magic().__await__()


def mock_cloudbet_request(obj, response, attr="request"):
    mock_resp = MagicMock(spec=ClientResponse)
    mock_resp.data = msgspec.json.encode(response)

    setattr(obj, attr, MagicMock(return_value=Future()))
    getattr(obj, attr).return_value.set_result(mock_resp)


class CloudbetTestStubs:

    @staticmethod
    def instrument_provider(cloudbet_client) -> CloudbetInstrumentProvider:
        return CloudbetInstrumentProvider(
            client=cloudbet_client,
            logger=TestComponentStubs.logger(),
        )

    @staticmethod
    def cloudbet_client(loop, logger) -> CloudbetClient:
        client = CloudbetClient(
            loop=loop,
            logger=logger,
            api_key=test_api_key,
            api_url=test_api_url,
        )

        # async def request(method, url, **kwargs):
        #     assert method  # required to stop mocks from breaking
        #     rpc_method = kwargs.get("json", {}).get("method") or url
        #     responses = {
        #         "https://api.betfair.com/exchange/betting/rest/v1/en/navigation/menu.json": BetfairResponses.navigation_list_navigation_response,
        #         "AccountAPING/v1.0/getAccountDetails": BetfairResponses.account_details,
        #         "AccountAPING/v1.0/getAccountFunds": BetfairResponses.account_funds_no_exposure,
        #     }
        #     if rpc_method in responses:
        #         resp = MagicMock(spec=ClientResponse)
        #         resp.data = msgspec.json.encode(responses[rpc_method](**kw))
        #         return resp
        #     raise KeyError(rpc_method)
        #
        # client.request = MagicMock()  # type: ignore
        # client.request.side_effect = request
        return client


class CloudbetResponses:
    @staticmethod
    def load(filename, **kwargs):
        # optionally pass the response type as a keyword argument
        if "response_type" in kwargs:
            response_type = kwargs["response_type"]
            return msgspec.json.decode((TEST_PATH / "responses" / filename).read_bytes(), type=response_type)
        else:
            return msgspec.json.decode((TEST_PATH / "responses" / filename).read_bytes())

    @staticmethod
    def login() -> GetAccountInfoResponse:
        return CloudbetResponses.load("login.json", response_type=GetAccountInfoResponse)

    @staticmethod
    def get_sports() -> GetSportsResponse:
        return CloudbetResponses.load("get_sports.json", response_type=GetSportsResponse)

    @staticmethod
    def get_events_for_sport() -> GetEventsForSportResponse:
        return CloudbetResponses.load("get_events_for_sport.json", response_type=GetEventsForSportResponse)


class DataGenerator:
    @staticmethod
    def generate_sport():
        sports = ["soccer", "tennis", "baseball", "basketball"]
        return random.choice(sports)

# @contextlib.contextmanager
# def mock_client_request(response):
#     """
#     Patch Cloudbet_Client.request with a correctly formatted `response`.
#     """
#     mock_response = MagicMock(ClientResponse)
#     mock_response.data = msgspec.json.encode(response)
#     with patch.object(CloudbetClient, "request", return_value=mock_response) as mock_request:
#         yield mock_request
