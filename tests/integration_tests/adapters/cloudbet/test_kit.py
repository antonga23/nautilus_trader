import json
import json
import pathlib
import random
from asyncio import Future
from typing import List
from unittest.mock import MagicMock

import msgspec
from aiohttp import ClientResponse

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import GetAccountInfoResponse, GetSportsResponse, \
    GetEventsForSportResponse, GetBetResponse, GetBetHistoryResponse
from nautilus_trader.adapters.cloudbet.client.util import extract_cloudbet_symbol, cloudbet_instrument_id
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
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
            api_key=test_api_key, # TODO: replace test key with an empty string
            api_url=test_api_url,# TODO: replace test key with an empty string
        )

        # TODO: finish implementing this method and re-run Cloubet Client test suite, handle cases where enpoitn returns Exceptions, failure, success, etc for each endpoint
        # async def request(function_name): # method, url, **kwargs
        #     assert function_name  # required to stop mocks from breaking
        #     responses = {
        #         "login": CloudbetResponses.login(),
        #         "get_sports": CloudbetResponses.get_sports(),
        #         "get_events_for_sport": CloudbetResponses.get_events_for_sport(),
        #         "place_bet_success": CloudbetResponses.place_bet_success(),
        #         "place_bet_failure": CloudbetResponses.place_bet_failure(),
        #         "place_bet_invalid_event_id": CloudbetResponses.place_bet_invalid_event_id(),
        #         "get_bet_history_success": CloudbetResponses.get_bet_history_success(),
        #         "get_bet_status_success": CloudbetResponses.get_bet_status_success(),
        #         "get_account_currencies_success": CloudbetResponses.get_account_currencies_success(),
        #     }
        #     if function_name in responses:
        #         resp = MagicMock(spec=ClientResponse)
        #         resp.data = responses[function_name] # response type => CloudbetResponses.function_name
        #         return resp
        #     raise KeyError(function_name)
        #
        # client.request = MagicMock()  # type: ignore
        # client.request.side_effect = request

        return client

    @staticmethod
    def get_instrument_id(filename: str = "instrument_id_data.json") -> InstrumentId:
        # read the instrument ids from the file
        with open(TEST_PATH / filename) as json_file:
            instrument_ids = json.load(json_file)
        # randomly select an instrument id from the array
        instrument_id_symbol = Symbol(random.sample(instrument_ids, k=1)[0])
        instrument_id = InstrumentId(instrument_id_symbol,CLOUDBET_VENUE)
        # event_id, market_name, outcome, params = extract_cloudbet_symbol(instrument_id)
        # instrument_id = cloudbet_instrument_id(event_id, market_name, outcome, params)
        return instrument_id

    @staticmethod
    def get_instrument_ids(count: int = 100, filename: str = "instrument_id_data.json"):
        with open(TEST_PATH / filename) as json_file:
            instrument_ids = json.load(json_file)
        # randomly select an instrument id from the array
        instrument_ids = random.sample(instrument_ids, k=count)
        instrument_ids = [InstrumentId(**instrument_id) for instrument_id in instrument_ids]
        return instrument_ids

    # TODO: remove this method => TestInstrumentProvider::get_instrument
    @staticmethod
    def get_instrument(filename: str = "instrument.json") -> CryptoBettingInstrument:
        with open(TEST_PATH / filename) as json_file:
            instrument = json.load(json_file)
        instrument = random.choice(instrument)
        instrument = CryptoBettingInstrument(**instrument)
        return instrument


    #TODO: remove this method => TestInstrumentProvider::get_instruments
    # @staticmethod
    # def get_instruments(count=100, filename: str = "instruments.json", **kwargs) -> List[CryptoBettingInstrument]:
    #     """
    #     Returns a list of instruments from the test data file.
    #     """
    #     with open(TEST_PATH / filename) as json_file:
    #         instruments = json.load(json_file)
    #     instruments = random.sample(instruments, k=count)
    #     instruments : List[CryptoBettingInstrument] = [CryptoBettingInstrument(**instrument) for instrument in instruments]
    #     if "sport" in kwargs:
    #         venue = kwargs["sport"]
    #         instruments = [instrument for instrument in instruments if instrument.venue == venue]
    #     return instruments


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

    @staticmethod
    def place_bet_success() -> GetBetResponse:
        return CloudbetResponses.load('place_bet_success.json', response_type=GetBetResponse)

    @staticmethod
    def place_bet_failure(**kwargs) -> GetBetResponse:
        return CloudbetResponses.load('place_bet_failure.json', response_type=GetBetResponse)

    @staticmethod
    def place_bet_invalid_event_id() -> GetBetResponse:
        return CloudbetResponses.load('place_bet_invalid_event.json', response_type=GetBetResponse)

    @staticmethod
    def get_bet_history_success() -> GetBetHistoryResponse:
        return CloudbetResponses.load('get_bet_history.json', response_type=GetBetHistoryResponse)

    @staticmethod
    def get_bet_status_success() -> GetBetResponse:
        return CloudbetResponses.load('get_bet_status.json', response_type=GetBetResponse)





class DataGenerator:
    @staticmethod
    def generate_sport():
        sports = ["soccer", "tennis", "baseball", "basketball"]
        return random.choice(sports)

    # @staticmethod
    # def generate_instrument_ids():
    #     instrument_ids = ["soccer", "tennis", "baseball", "basketball"]
    #     return random.choice(instrument_ids)

# @contextlib.contextmanager
# def mock_client_request(response):
#     """
#     Patch Cloudbet_Client.request with a correctly formatted `response`.
#     """
#     mock_response = MagicMock(ClientResponse)
#     mock_response.data = msgspec.json.encode(response)
#     with patch.object(CloudbetClient, "request", return_value=mock_response) as mock_request:
#         yield mock_request
