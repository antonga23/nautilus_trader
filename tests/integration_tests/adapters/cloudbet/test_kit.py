import json
import random
from asyncio import Future
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import msgspec
from aiohttp import ClientResponse

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import (
    GetAccountInfoResponse,
    GetSportsResponse,
    GetEventsForSportResponse,
    GetBetResponse,
    GetBetHistoryResponse,
    GetBetsResponse,
    GetAccountCurrencies,
    GetAccountBalance,
    GetLatestOddsResponse,
    Selection,
    GetEventResponse,
)
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from tests import TESTS_PACKAGE_ROOT

TEST_PATH = TESTS_PACKAGE_ROOT / "integration_tests" / "adapters" / "cloudbet" / "resources"
DATA_PATH = TESTS_PACKAGE_ROOT / "test_data" / "cloudbet"

test_api_key = "test-cloudbet-api-key"
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
            config=InstrumentProviderConfig(load_all=True),
        )

    @staticmethod
    def load(filename, **kwargs):
        # optionally pass the response type as a keyword argument
        if "response_type" in kwargs:
            response_type = kwargs["response_type"]
            return msgspec.json.decode((TEST_PATH / filename).read_bytes(), type=response_type)
        else:
            return msgspec.json.decode((TEST_PATH / filename).read_bytes())

    @staticmethod
    def cloudbet_client(loop, logger) -> CloudbetClient:
        client = CloudbetClient(
            loop=loop,
            logger=logger,
            api_key=test_api_key,  # TODO: replace test key with an empty string
            api_url=test_api_url,  # TODO: replace test key with an empty string
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

        client.get_sports = AsyncMock(return_value=CloudbetResponses.get_sports())  # type: ignore[method-assign]

        async def get_events_for_sport_side_effect(*args, **kwargs):
            sport_key = kwargs.get("sport_key")
            if sport_key is None and args:
                sport_key = args[0]
            return CloudbetResponses.get_events_for_sport(sport_key=sport_key)

        client.get_events_for_sport = AsyncMock(  # type: ignore[method-assign]
            side_effect=get_events_for_sport_side_effect,
        )

        return client

    @staticmethod  # TODO: move to test_kit_providers or create a util function for calling those methods
    def get_instrument_id(filename: str = "instrument_id_data.json") -> InstrumentId:
        """
        Retrieves an instrument ID from a JSON file.

        Params:
            - filename : Optional[str] The name of the JSON file to read the instrument IDs from. Defaults to "instrument_id_data.json".
        Returns: The randomly selected instrument ID.
            - InstrumentId
        """
        # read the instrument ids from the file
        with open(TEST_PATH / filename) as json_file:
            instrument_ids = json.load(json_file)
        # randomly select an instrument id from the array
        instrument_id_symbol = Symbol(random.sample(instrument_ids, k=1)[0])
        instrument_id = InstrumentId(instrument_id_symbol, CLOUDBET_VENUE)
        # event_id, market_name, outcome, params = extract_cloudbet_symbol(instrument_id)
        # instrument_id = cloudbet_instrument_id(event_id, market_name, outcome, params)
        return instrument_id

    @staticmethod  # TODO: move to test_kit_providers or create a util function for calling those methods
    def get_instrument_ids(count: int = 100, filename: str = "instrument_id_data.json"):
        """
        Retrieves a specified number of instrument IDs from a JSON file.

        Args:
            count (int, optional): The number of instrument IDs to retrieve. Defaults to 100.
            filename (str, optional): The name of the JSON file to retrieve the instrument IDs from. Defaults to "instrument_id_data.json".

        Returns:
            List[InstrumentId]: A list of instrument IDs retrieved from the JSON file.
        """
        with open(TEST_PATH / filename) as json_file:
            instrument_ids = json.load(json_file)
        # randomly select an instrument id from the array
        instrument_ids = random.sample(instrument_ids, k=count)
        instrument_ids = [InstrumentId(**instrument_id) for instrument_id in instrument_ids]
        return instrument_ids

    @staticmethod
    # TODO: test the method
    def get_selections(**kwargs) -> List[Selection]:
        """
        Get selections based on the provided kwargs.

        :param kwargs: A dictionary containing the arguments.
            - sport (str): The sport for which selections are needed.
            - count (int): The number of selections to return.

        :return: A list of Selection objects based on the provided kwargs.
        :rtype: List[Selection]
        """
        if kwargs.get("sport"):
            try:
                selections: List[Selection] = CloudbetTestStubs.load(
                    f"{kwargs['sport']}_selections.json", response_type=List[Selection]
                )
            # if a file not found, use the default selections
            except FileNotFoundError:
                selections: List[Selection] = CloudbetTestStubs.load(
                    "default_selections.json", response_type=List[Selection]
                )
            except KeyError:
                selections: List[Selection] = CloudbetTestStubs.load(
                    "default_selections.json", response_type=List[Selection]
                )
            except Exception:
                selections: List[Selection] = CloudbetTestStubs.load(
                    "default_selections.json", response_type=List[Selection]
                )
        else:
            selections: List[Selection] = CloudbetTestStubs.load(
                "default_selections.json", response_type=List[Selection]
            )
        if kwargs.get("count"):
            return selections[: kwargs["count"]]
        else:
            return selections


class CloudbetResponses:
    @staticmethod
    def load(filename, **kwargs):
        # optionally pass the response type as a keyword argument
        if "response_type" in kwargs:
            response_type = kwargs["response_type"]
            return msgspec.json.decode(
                (TEST_PATH / "responses" / filename).read_bytes(), type=response_type
            )
        else:
            return msgspec.json.decode((TEST_PATH / "responses" / filename).read_bytes())

    @staticmethod
    def login() -> GetAccountInfoResponse:
        return CloudbetResponses.load("login.json", response_type=GetAccountInfoResponse)

    @staticmethod
    def get_sports() -> GetSportsResponse:
        return CloudbetResponses.load("get_sports.json", response_type=GetSportsResponse)

    @staticmethod
    def get_events_for_sport(**sport) -> GetEventsForSportResponse:
        # check if a sport key was passed
        if "sport_key" in sport:
            if sport["sport_key"] == "soccer":
                return CloudbetResponses.load(
                    "get_events_for_sport_soccer.json", response_type=GetEventsForSportResponse
                )
            elif sport["sport_key"] == "soccer":
                return CloudbetResponses.load(
                    "get_events_for_sport_soccer.json", response_type=GetEventsForSportResponse
                )
            elif sport["sport_key"] == "tennis":
                return CloudbetResponses.load(
                    "get_events_for_sport_tennis.json", response_type=GetEventsForSportResponse
                )
            elif sport["sport_key"] == "baseball":
                return CloudbetResponses.load(
                    "get_events_for_sport_baseball.json", response_type=GetEventsForSportResponse
                )
            elif sport["sport_key"] == "basketball":
                return CloudbetResponses.load(
                    "get_events_for_sport_basketball.json", response_type=GetEventsForSportResponse
                )
            else:
                return CloudbetResponses.load(
                    "get_events_for_sport_no_events.json", response_type=GetEventsForSportResponse
                )
        else:
            return CloudbetResponses.load(
                "get_events_for_sport.json", response_type=GetEventsForSportResponse
            )

    @staticmethod
    def get_latest_odds(**kwargs) -> GetLatestOddsResponse:
        if kwargs.get("event_id"):
            return CloudbetResponses.load(
                "get_latest_odds.json", response_type=GetLatestOddsResponse
            )
        else:
            return CloudbetResponses.load(
                "get_latest_odds.json", response_type=GetLatestOddsResponse
            )

    @staticmethod
    def place_bet_success() -> GetBetResponse:
        return CloudbetResponses.load("place_bet_success.json", response_type=GetBetResponse)

    @staticmethod
    def place_bet_failure(**kwargs) -> GetBetResponse:
        return CloudbetResponses.load("place_bet_failure.json", response_type=GetBetResponse)

    @staticmethod
    def place_bet_invalid_event_id() -> GetBetResponse:
        return CloudbetResponses.load("place_bet_invalid_event.json", response_type=GetBetResponse)

    @staticmethod
    def get_bet_history_success() -> GetBetHistoryResponse:  # TODO: rename to get_bet_history
        return CloudbetResponses.load("get_bet_history.json", response_type=GetBetHistoryResponse)

    @staticmethod
    def get_bets_success() -> GetBetsResponse:
        return CloudbetResponses.load("get_bets.json", response_type=GetBetsResponse)

    @staticmethod
    def get_bet_status_win() -> GetBetResponse:
        return CloudbetResponses.load("get_bet_status.json", response_type=GetBetResponse)

    @staticmethod
    def get_bet_status_accepted() -> GetBetResponse:
        return CloudbetResponses.load("get_bet_status_accepted.json", response_type=GetBetResponse)

    @staticmethod
    def get_account_currencies_success() -> GetAccountCurrencies:
        return CloudbetResponses.load(
            "get_account_currencies.json", response_type=GetAccountCurrencies
        )

    @staticmethod
    # TODO: handle currencies with different precision eg. BTC
    def get_account_balances(
        currency: Optional[str] = None,
    ) -> GetAccountBalance:  # we optionally pass a currency in case a mock is needed
        return CloudbetResponses.load("get_account_balances.json", response_type=GetAccountBalance)

    @staticmethod
    def get_bet_history_no_bets() -> GetBetHistoryResponse:
        return CloudbetResponses.load(
            "get_bet_history_no_bets.json", response_type=GetBetHistoryResponse
        )

    @staticmethod
    def get_bets_no_bets() -> GetBetsResponse:
        return CloudbetResponses.load("get_bets_no_bets.json", response_type=GetBetsResponse)

    @staticmethod
    def get_bet_history_mixed_status() -> GetBetHistoryResponse:
        return CloudbetResponses.load(
            "get_bet_history_mixed_status.json", response_type=GetBetHistoryResponse
        )

    @staticmethod
    def get_bets_mixed_status() -> GetBetsResponse:
        return CloudbetResponses.load("get_bets_mixed_status.json", response_type=GetBetsResponse)

    @staticmethod
    def get_event(**kwargs) -> GetEventResponse:
        if kwargs.get("event_id"):
            event = CloudbetResponses.load("get_event.json", response_type=GetEventResponse)
            event.event_id = int(kwargs["event_id"])
            return event
        else:
            return CloudbetResponses.load("get_event.json", response_type=GetEventResponse)


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
