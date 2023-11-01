import asyncio
from collections import Counter
from datetime import datetime
from typing import Optional, Union
from unittest import mock
from unittest.mock import patch, AsyncMock, PropertyMock
from collections import Counter
import pandas as pd
import msgspec
import pytest
from nautilus_trader.core.rust.model import OrderType, ContingencyType, TimeInForce
from nautilus_trader.core.uuid import UUID4

from nautilus_trader.accounting.accounts.base import Account

from nautilus_trader.adapters.cloudbet.client.util import cb_bet_to_order_status_report, \
    cloudbet_timestamp_to_unix_nanos, make_symbol, cloudbet_instrument_id, bet_to_trade_report
from nautilus_trader.adapters.cloudbet.execution import CloudbetLiveExecutionClient
from nautilus_trader.model.currency import Currency

from nautilus_trader.execution.reports import OrderStatusReport, TradeReport

from nautilus_trader.common.factories import OrderFactory

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.instruments import Instrument

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import GetLatestOddsResponse, SelectionStatus, GetEventResponse, \
    EventStatus, GetAccountInfoResponse, GetAccountCurrencies, GetAccountBalance, GetBetResponse, GetBetHistoryResponse
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.model.enums import AccountType
from nautilus_trader.adapters.cloudbet.data_client import CloudbetDataClient
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.common.clock import LiveClock, TestClock
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
from nautilus_trader.model.identifiers import InstrumentId, AccountId, VenueOrderId, ClientOrderId, PositionId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.objects import Price, Money, AccountBalance, Quantity

from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orderbook import OrderBook
from nautilus_trader.model.orders import Order
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetResponses


class TestCloudbetExecutionClient:
    @pytest.mark.asyncio()
    async def test_exec_client_fixture(self, exec_client):
        assert exec_client is not None, f"Expected exec client to be not None, got {exec_client}"
        assert isinstance(exec_client,
                          CloudbetLiveExecutionClient), f"Expected exec client to be CloudbetLiveExecutionClient, got {type(exec_client)}"

    @pytest.mark.asyncio
    @patch.object(CloudbetClient, 'login', new_callable=AsyncMock, return_value=CloudbetResponses.login())
    async def test_set_account_id_not_none(self, mock_login, exec_client):
        # Call the method under test
        await exec_client.set_account_id(account_id=None)

        expected_account_id: AccountId = AccountId(
            f"{CLOUDBET_VENUE.value}-{mock_login.return_value.uuid.split('-')[0]}")
        # Assert that the account ID is set correctly
        assert exec_client.account_id == expected_account_id

    @pytest.mark.asyncio
    async def test_retrieves_account_info_and_balances(self, mocker, exec_client):
        """
        Test case for succesfully retrieving account state and all balances.

        Args:
            mocker: The mocker object for patching methods and responses.
            exec_client: The exec_client object for executing the client connection.

        Returns:
            None
        """
        # Mock the necessary methods and responses
        mock_login = mocker.patch.object(CloudbetClient, 'login')
        mock_login.return_value = CloudbetResponses.login()

        mock_get_account_currencies = mocker.patch.object(CloudbetClient, 'get_account_currencies')
        mock_get_account_currencies.return_value = CloudbetResponses.get_account_currencies_success()

        mock_get_balances = mocker.patch.object(CloudbetClient, 'get_balances')
        mock_get_balances.side_effect = [
            CloudbetResponses.get_account_balances(),
            CloudbetResponses.get_account_balances()
        ]

        expected_account_id = AccountId(f"{CLOUDBET_VENUE.value}-{mock_login.return_value.uuid.split('-')[0]}")
        # Assert that no account exists in cache yet
        assert exec_client._cache.account(expected_account_id) is None

        # Call the method under test
        await exec_client.connection_account_state()

        # Assert that the necessary methods were called with the correct arguments
        # mock_login.assert_called_once()
        mock_get_account_currencies.assert_called_once()
        mock_get_balances.assert_has_calls([
            mocker.call('PLAY_EUR'),
            mocker.call('USDT')
        ])
        # when _send_account_state is called, the account state will be sent to the client and an account will be added to the cache
        # we need to check that the account state was sent belonging to the correct account id
        cached_account: Account = exec_client._cache.account(expected_account_id)
        assert cached_account is not None
        for currency in mock_get_account_currencies.return_value.currencies:
            typed_currency = Currency.from_str(currency)
            balance_amount: str = CloudbetResponses.get_account_balances().amount
            assert cached_account.balance(Currency.from_str(currency)) == AccountBalance(
                total=Money(balance_amount, typed_currency),
                locked=Money(0, typed_currency),
                free=Money(balance_amount, typed_currency),
            )

    @pytest.mark.asyncio
    async def test_retrieves_account_info_and_balances_with_error(self, mocker, exec_client):
        """
        Test case for retrieving account state and balances, gracefully handling any errors.

        Args:
            mocker: The mocker object for patching methods and responses.
            exec_client: The instance of the CloudbetClient class.

        Returns:
            None
        """
        # Mock the necessary methods and responses
        mock_login = mocker.patch.object(CloudbetClient, 'login')
        mock_login.return_value = CloudbetResponses.login()

        mock_get_account_currencies = mocker.patch.object(CloudbetClient, 'get_account_currencies')
        mock_get_account_currencies.return_value = CloudbetResponses.get_account_currencies_success()

        mock_get_balances = mocker.patch.object(CloudbetClient, 'get_balances')
        mock_get_balances.side_effect = [
            CloudbetResponses.get_account_balances(),
            Exception("Unable to retrieve balance for currency")
        ]

        expected_account_id = AccountId(f"{CLOUDBET_VENUE.value}-{mock_login.return_value.uuid.split('-')[0]}")
        # Assert that no account exists in cache yet
        assert exec_client._cache.account(expected_account_id) is None
        _ = exec_client._cache.account(expected_account_id)

        # assert function terminated with exception
        try:
            await exec_client.connection_account_state()
        except Exception as ex:
            assert str(ex) == "Unable to retrieve balance for currency"

        _ = exec_client._cache.account(expected_account_id)
        assert (exec_client._cache.account(expected_account_id)).id == expected_account_id
        typed_currency = Currency.from_str(mock_get_account_currencies.return_value.currencies[0])
        balance_amount: str = CloudbetResponses.get_account_balances().amount
        assert exec_client._cache.account(expected_account_id).balance(typed_currency) == AccountBalance(
            total=Money(balance_amount, typed_currency),
            locked=Money(0, typed_currency),
            free=Money(balance_amount, typed_currency),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("login_result, currencies_result, balances_result", [
        (Exception("Unable to login"), CloudbetResponses.get_account_currencies_success(),
         CloudbetResponses.get_account_balances()),
        (CloudbetResponses.login(), Exception("Unable to get account currencies"),
         CloudbetResponses.get_account_balances()),
        (CloudbetResponses.login(), Exception("Unable to get account currencies"),
         Exception("Unable to get balances currencies")),
        (CloudbetResponses.login(), CloudbetResponses.get_account_balances(),
         Exception("Unable to retrieve balance for currency")),
    ])
    @patch.object(CloudbetClient, 'login', new_callable=AsyncMock)
    @patch.object(CloudbetClient, 'get_account_currencies', new_callable=AsyncMock)
    @patch.object(CloudbetClient, 'get_balances', new_callable=AsyncMock)
    async def test_fails_retrieves_account_info_client_exceptions(self, login, get_account_currencies, get_balances,
                                                                  login_result, currencies_result, balances_result,
                                                                  exec_client, mocker):
        """
        Test case for retrieving account state and balances, gracefully handling any errors.

        This test checks that the `connection_account_state` method of the `CloudbetClient` class handles exceptions correctly.
        If any of the `login`, `get_account_currencies`, or `get_balances` methods raises an exception, the `connection_account_state` method should return `None`.
        If none of these methods raises an exception, the `connection_account_state` method should return a non-`None` value.

        Args:
            mocker: The mocker object for patching methods and responses.
            exec_client: The instance of the CloudbetClient class.
            login_result: The result to be returned when the `login` method is called. This can be a successful response or an exception.
            currencies_result: The result to be returned when the `get_account_currencies` method is called. This can be a successful response or an exception.
            balances_result: The result to be returned when the `get_balances` method is called. This can be a successful response or an exception.

        Returns:
            None
        """
        # Mock the necessary methods and responses
        login.side_effect = login_result
        get_account_currencies.side_effect = currencies_result
        get_balances.side_effect = balances_result

        # Call the method under test
        result = await exec_client.connection_account_state()

        # Check if the method returned None when it was supposed to
        if isinstance(login_result, Exception) or isinstance(currencies_result, Exception) or isinstance(
            balances_result, Exception):
            assert result is None
        else:
            assert result is not None


class TestCloudbetExecutionReports:

    def setup(self):
        # Fixture Setup
        self.trader_id = TestIdStubs.trader_id()
        self.strategy_id = TestIdStubs.strategy_id()

        clock = TestClock()
        clock.set_time(0)

        self.order_factory = OrderFactory(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            clock=clock,
        )

    async def cache_valid_order(self, exec_client: Union[mock.MagicMock, CloudbetLiveExecutionClient],
                                instruments: list[CryptoBettingInstrument], instrument_id: Optional[InstrumentId],
                                cached_order: str, bet_history: GetBetHistoryResponse):
        """
        Caches a valid order for each instrument in the given list of instruments.

        Args:
            exec_client (Union[mock.MagicMock, CloudbetLiveExecutionClient]): The execution client to use for caching the order.
            instruments (list[CryptoBettingInstrument]): The list of instruments to cache the order for.
            instrument_id (Optional[InstrumentId]): The ID of the instrument to use for the order. If not provided, the ID of each instrument in the list will be used.
            cached_order (str): The type of the cached order.
            bet_history (GetBetHistoryResponse): The response containing the bet history.

        Returns:
            None
        """
        for instr, bet in zip(instruments, bet_history.bets):
            if cached_order in ["valid_order", "valid_order_no_venue_id"]:
                order = self.order_factory.limit(
                    instrument_id if instrument_id else instr.id,
                    OrderSide.BUY,
                    Quantity.from_int(10),
                    Price.from_str("8.835"),
                )

                if cached_order == "valid_order_no_venue_id":
                    order.apply(TestEventStubs.order_submitted(order=order))
                else:
                    # Use the reference_id from the bet as the VenueOrderId
                    cached_venue_order_id = VenueOrderId(bet.reference_id)
                    order.apply(TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id))

                position_id = PositionId(f"{instr.id}-{CLOUDBET_VENUE.value}")
                exec_client._cache.add_order(order=order, position_id=position_id)

    @pytest.fixture()
    def account_id(self) -> AccountId:
        """
        A fixture that generates and returns an AccountId object.

        Parameters:
            self (TestClass): The instance of the TestClass that the fixture is associated with.

        Returns:
            AccountId: The generated AccountId object.

        Raises:
            None.
        """
        account_uuid: GetAccountInfoResponse = CloudbetResponses.login()
        account_id = AccountId(f"{CLOUDBET_VENUE.value}-{account_uuid.uuid.split('-')[0]}")
        return account_id

    # -- ORDER STATUS REPORTS ------------------------------------------------------------------------

    def test_cloudbet_betstatus_to_order_status_report(self, account_id, instrument, exec_client, clock):
        """
        Test case for converting a Cloudbet bet status to an Order Status Report. Assumes the bet has been accepted by Cloudbet but the status needs to be mapped to internal order representation.

        This test validates the `cb_bet_to_order_status_report` function by checking its ability to convert a given bet status from Cloudbet into an internal Order Status Report object. The test initializes required objects and a sample bet response, then invokes the function with these parameters.

        Args:
            account_id: The account ID fixture.
            instrument: The instrument fixture.
            exec_client: The execution client fixture.
            clock: The clock fixture.

        Returns:
            None

        Assertions:
            - Asserts that the returned object is an instance of OrderStatusReport.
            - Additional assertions to validate type conversions and attribute mappings.
        """
        # Initialize the required objects
        instrument_id = instrument.id
        ts_init = clock.timestamp_ns()
        # Create a sample bet response object
        bet_response: GetBetResponse = CloudbetResponses.get_bet_status_accepted()
        venue_order_id: VenueOrderId = VenueOrderId(bet_response.reference_id)
        client_order_id: Optional[ClientOrderId] = None
        report_id = UUID4()

        # Invoke the cb_bet_to_order_status_report  utility function
        order_status_report = cb_bet_to_order_status_report(
            account_id=account_id,
            instrument_id=instrument_id,
            ts_init=ts_init,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            report_id=report_id,
            bet_response=bet_response
        )

        # # Assert the generated order status report
        assert isinstance(order_status_report, OrderStatusReport)
        # these checks are important to ensure type conversions we expect are happening
        assert order_status_report.order_side == bet_response.side.get_order_side()
        assert order_status_report.order_type == OrderType.LIMIT
        assert order_status_report.contingency_type == ContingencyType.NO_CONTINGENCY
        assert order_status_report.time_in_force == TimeInForce.GTC
        assert order_status_report.order_status == bet_response.status.get_order_status()
        assert order_status_report.quantity == Quantity.from_str(str(bet_response.stake))
        assert order_status_report.ts_init == ts_init
        assert order_status_report.ts_accepted == cloudbet_timestamp_to_unix_nanos(bet_response.create_time)
        assert order_status_report.ts_last == cloudbet_timestamp_to_unix_nanos(bet_response.create_time)

    def test_cached_order_to_order_status_report(self, account_id, instrument, exec_client, clock, trader_id,
                                                 venue_order_id):
        """
        Test case for converting a cached order to an Order Status Report. This assumes the order has reached the exchange and has been accepted, but we are unable to retrieve the order status from the exchange.

        This test checks the functionality of the `cb_bet_to_order_status_report` function, ensuring that it correctly converts a given limit order and other parameters to an Order Status Report object. The test initializes a limit order with sample data, including a specified venue_order_id, and then applies the function.

        Args:
            account_id: The account ID related to the order.
            instrument: The instrument object related to the order.
            exec_client: The execution client instance for order processing.
            clock: The clock object providing the current timestamp.
            trader_id: The trader ID related to the order.
            venue_order_id: The venue-specific order ID.

        Returns:
            None

        Assertions:
            - Asserts that the returned object is an instance of OrderStatusReport.
        """
        # Initialize the required objects
        instrument_id = instrument.id
        ts_init = clock.timestamp_ns()
        bet_response = None
        report_id = UUID4()
        # create a Sample order object
        limit_order = self.order_factory.limit(
            instrument.id,
            OrderSide.BUY,
            Quantity.from_int(10),
            Price.from_str("2.38"),
        )

        limit_order.apply(TestEventStubs.order_accepted(order=limit_order,
                                                        venue_order_id=venue_order_id))  # we have to explicitly set the venue order id as it is not set by default

        # Invoke the cb_bet_to_order_status_report function
        order_status_report = cb_bet_to_order_status_report(
            account_id=account_id,
            instrument_id=instrument_id,
            ts_init=ts_init,
            client_order_id=limit_order.client_order_id,
            venue_order_id=limit_order.venue_order_id,
            report_id=report_id,
            bet_response=bet_response,
            order=limit_order
        )
        assert isinstance(order_status_report, OrderStatusReport)

    def test_raise_assertion_error_if_neither_order_nor_bet_response_provided(self, account_id, instrument, exec_client,
                                                                              clock, venue_order_id):
        """
        Test whether an `AssertionError` is raised when neither `order` nor `bet_response` is provided.

        Args:
            account_id (int): The account ID fixture.
            instrument (Instrument): The instrument fixture.
            exec_client (ExecClient): The execution client fixture.
            clock (Clock): The clock fixture.
            venue_order_id (str): The venue order id fixture.

        Assertions:
            - Asserts that an `AssertionError` is raised.
        """
        # Initialize the required objects
        ts_init = clock.timestamp_ns()
        # Create a sample bet response object
        bet_response = None
        order = None
        client_order_id: Optional[ClientOrderId] = None
        report_id = UUID4()

        # Invoke the cb_bet_to_order_status_report function without providing order or bet response
        with pytest.raises(AssertionError) as e:
            cb_bet_to_order_status_report(
                account_id=account_id,
                instrument_id=instrument.id,
                ts_init=ts_init,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                report_id=report_id
            )

        # Assert an Assertion Error was raised
        assert e.type == AssertionError

    # -- HAPPY PATH GENERATE ORDER STATUS REPORTS ------------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "get_bet_status_result, is_exception, client_order_id_is_none, cached_order, venue_order_id", [
            (CloudbetResponses.get_bet_status_accepted(), False, True, None, "some_venue_id"),  # Test 1
            (CloudbetResponses.get_bet_status_accepted(), False, False, None, "some_venue_id"),  # Test 2
            ("No bet status response received from Cloudbet", True, False, "valid_order", "some_venue_id"),  # Test 3
            (None, False, False, "valid_order", "None"),  # Test 4
        ])
    @patch.object(CloudbetClient, 'login', new_callable=AsyncMock, return_value=CloudbetResponses.login())
    @patch.object(CloudbetClient, 'get_bet_status', new_callable=AsyncMock)
    async def test_successful_order_status_reports(self,
                                                   get_bet_status, login,
                                                   get_bet_status_result, is_exception, client_order_id_is_none,
                                                   cached_order, venue_order_id,
                                                   exec_client, instrument,
                                                   limit_order):  # Replace exec_client, instrument, and limit_order with your actual fixtures or objects
        """
        General Overview:
        -----------------
        Test case for generating Order Status Reports using various scenarios encapsulated by the parametrized inputs.
        This test aims to cover multiple edge cases in a single function using different combinations of patched data
        and assumptions.

        Parameters:
        ---------------------
            - get_bet_status (AsyncMock): A patched version of the 'get_bet_status' method.
            - login (AsyncMock): A patched version of the 'login' method.
            - get_bet_status_result: Mock result for 'get_bet_status'.
            - is_exception (bool): If 'get_bet_status' should raise an exception.
            - client_order_id_is_none (bool): If the client_order_id should be None.
            - cached_order (str): Specifies if a valid order should be cached.
            - venue_order_id (str): The venue_order_id to use in the test.
            - exec_client: The CloudbetLiveExecutionClient fixture.
            - instrument: The instrument fixture.

        Test Cases Explained:
        ---------------------
        Test 1/Param set 1:
            - Tests when 'get_bet_status' returns a valid response.
            - No client ID is provided.
            - Assumes successful login and valid instrument.
            - Assertions: Expect an OrderStatusReport.

        Test 2/Param set 2:
            - Tests when 'get_bet_status' returns a valid response.
            - A valid client ID is provided.
            - Assumes successful login and valid instrument.
            - Assertions: Expect an OrderStatusReport.

        Test 3/Param set 3:
            - Tests when 'get_bet_status' raises an exception.
            - A valid client ID is derived from a cached order.
            - Assumes successful login and valid instrument.
            - Assertions: Expect an OrderStatusReport.

        Test 4/Param set 4:
            - Tests when 'get_bet_status' returns None.
            - A valid client ID is provided, but venue_order_id is None.
            - Assumes successful login and valid instrument.
            - Assertions: Expect an OrderStatusReport.

        Returns:
            None
        """

        if is_exception:
            get_bet_status.side_effect = Exception(get_bet_status_result)
        else:
            get_bet_status.return_value = get_bet_status_result

        # Common setup code
        instrument_id = cloudbet_instrument_id(event_id=20254973, market_name="soccer.team_win_to_nil", outcome="yes",
                                               params="team=away")  # Replace with your actual method to create an InstrumentId

        client_order_id = None if client_order_id_is_none else ClientOrderId(
            "some_id")  # Replace with your actual method to create a ClientOrderId

        if cached_order == "valid_order":
            order = self.order_factory.limit(
                instrument_id,
                OrderSide.BUY,
                Quantity.from_int(10),
                Price.from_str("8.835"),
            )
            cached_venue_order_id = VenueOrderId("some_venue_order_id")
            order.apply(TestEventStubs.order_accepted(order=order,
                                                      venue_order_id=cached_venue_order_id))  # Replace with your actual method to apply an order accepted event

            position_id = PositionId(
                f"{instrument.id}-{CLOUDBET_VENUE.value}")
            exec_client._cache.add_order(order=order, position_id=position_id)

            client_order_id = order.client_order_id
        else:
            order = None

        # Dynamically determine the venue_order_id to use in the function call
        if venue_order_id == "cached_venue_order_id":
            actual_venue_order_id = cached_venue_order_id
        elif venue_order_id == "None":
            actual_venue_order_id = None
        else:
            actual_venue_order_id = VenueOrderId(venue_order_id)

        # Call the function under test
        order_status_report = await exec_client.generate_order_status_report(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=actual_venue_order_id
        )

        # Common assertions
        assert isinstance(order_status_report, OrderStatusReport)
        # TODO: add additional assertion depending on params

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "get_bet_history_result, get_bet_status_result, is_exception, instrument_id_is_none, start, end, open_only, cached_order",
        [
            (CloudbetResponses.get_bet_history_success(), CloudbetResponses.get_bet_status_accepted(), False, False, None, None,
             True, "valid_order"),  # Test 1
            (CloudbetResponses.get_bet_history_success(), CloudbetResponses.get_bet_status_accepted(), False, False,
             None, None, False, "valid_order"),  # Test 2
            (CloudbetResponses.get_bet_history_success(), CloudbetResponses.get_bet_status_accepted(), False, True,
             "2021-10-01",
             "2021-10-02", False, None),  # Test 3
            (None, None, True, False, None, None, False, "valid_order"),  # Test 5
            (CloudbetResponses.get_bet_history_success(), None, True, True, "20-10-01", "2021-10-02", True, "valid_order"),
            # Test 5
        ])
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 10)], indirect=["instruments"])  # return 10 instruments
    @patch.object(CloudbetClient, 'login', new_callable=AsyncMock, return_value=CloudbetResponses.login())
    @patch.object(CloudbetClient, 'get_bet_history', new_callable=AsyncMock)
    @patch.object(CloudbetClient, 'get_bet_status', new_callable=AsyncMock)
    async def test_successful_generate_order_status_multi_reports(self,
                                                                  get_bet_status, get_bet_history, login,
                                                                  get_bet_history_result, get_bet_status_result,
                                                                  is_exception,
                                                                  instrument_id_is_none, start, end, open_only,
                                                                  cached_order,
                                                                  exec_client, instrument, instruments):
        """
        General Overview:
        -----------------
        Test case for generating a list of Order Status Reports using the `generate_order_status_reports` method.
        This test aims to cover multiple scenarios encapsulated by the parametrized inputs.

        Parameters Explained:
        ---------------------
            - get_bet_history (AsyncMock): A patched version of the 'get_bet_history' method.
            - get_bet_status (AsyncMock): A patched version of the 'get_bet_status' method.
            - login (AsyncMock): A patched version of the 'login' method.
            - get_bet_history_result: Mock result for 'get_bet_history'.
            - get_bet_status_result: Mock result for 'get_bet_status'.
            - is_exception (bool): If 'get_bet_history' or 'get_bet_status' should raise an exception.
            - instrument_id_is_none (bool): If the instrument_id should be None.
            - start (str): The start date in string format.
            - end (str): The end date in string format.
            - open_only (bool): If only open orders should be returned.
            - cached_order (str): Specifies if a valid order should be cached.
            - exec_client: The CloudbetLiveExecutionClient fixture.
            - instrument: The instrument fixture.

        Test Cases Explained:
        ---------------------
        Test 1/Param set 1:
            - 'get_bet_history' and 'get_bet_status' return valid responses.
            - Only instrument_id is provided.
            - no start and end date provided so a cached order will be used
            - `open_only` is True.
            - Expected: A list of OrderStatusReports generated from cached orders that are open.
            - Assertions: Expect a list of OrderStatusReports.

        Test 1/Param set 2:
            - 'get_bet_history' and 'get_bet_status' return valid responses.
            - Only instrument_id is provided.
            - no start and end date provided so a cached order will be used
            - `open_only` is False.
            - Expected: A list of OrderStatusReports generated from cached orders, regardless of their status.
            - Assertions: Expect a list of OrderStatusReports.

       Test 3/Param set 3:
            - 'get_bet_history' and 'get_bet_status' return valid responses.
            - No instrument_id, but start and end dates are provided.
            - `open_only` is False.
            - Expected: A list of OrderStatusReports generated for all orders within the specified time frame.

        Test 4/Param set 4:
            - 'get_bet_history' and 'get_bet_status' raise exceptions.
            - Only instrument_id is provided.
            - `open_only` is False.
            - Assumes successful login and valid instrument.
            - Assertions: Expect an empty list.
            - Expected: An empty list since both get_bet_history and get_bet_status raise exceptions.


        Test 5/Param set 5:
            - 'get_bet_history' returns valid response, but 'get_bet_status' raises exception.
            - Both start and end dates are provided.
            - instrument_id is not provided
            - `open_only` is True.
            - Assumes successful login and valid instrument.
            - Expected: An empty list since 'get_bet_status' fails.
            - Assertions: Expect an empty list.

        Returns:
            None
        """
        # Mock the side effects or return values based on `is_exception`
        if is_exception:
            get_bet_history.side_effect = Exception("Failed to fetch bet history")
            get_bet_status.side_effect = Exception("Failed to fetch bet status")
        else:
            get_bet_history.return_value = get_bet_history_result
            get_bet_status.return_value = get_bet_status_result

        # Create an instrument_id if it is not supposed to be None
        instrument_id = None if instrument_id_is_none else instrument.id

        if get_bet_history_result is not None:
            await self.cache_valid_order(exec_client, instruments, instrument_id, cached_order, get_bet_history_result)

        # Convert string date to pandas Timestamp if start and end are not None
        start_ts = pd.Timestamp(start) if start else None
        end_ts = pd.Timestamp(end) if end else None

        # Call the function under test
        try:
            order_status_reports = await exec_client.generate_order_status_reports(
                instrument_id=instrument_id,
                start=start_ts,
                end=end_ts,
                open_only=open_only
            )
        except Exception as e:
            if is_exception:
                assert str(e) == "Failed to fetch bet history" or str(e) == "Failed to fetch bet status"
                return
            else:
                raise AssertionError(f"Unexpected exception: {e}")

        # Assertions based on the test parameters
        if not is_exception:
            assert isinstance(order_status_reports, list)
            if not order_status_reports:
                assert order_status_reports == []
            else:
                assert all(isinstance(report, OrderStatusReport) for report in order_status_reports)

    # -- HAPPY PATH GENERATE ORDER STATUS REPORTS ------------------------------------------------------------------------

    # -- UNHAPPY PATH GENERATE ORDER STATUS REPORTS ------------------------------------------------------------------------
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "get_bet_status_result, is_exception, client_order_id_is_none, cached_order, venue_order_id", [
            ("ExceptionMessage", True, True, None, "some_venue_id"),
            # # Test 1: get_bet_status raises an exception, no client ID, no cached order
            ("ExceptionMessage", True, True, "valid_order", "some_venue_id"),
            # # Test 2: get_bet_status raises an exception, client ID from cached order, no venue_order_id
            (CloudbetResponses.get_bet_status_accepted(), True, False, "valid_order_no_venue_id", None),
            # Test 3: get_bet_status raises an exception, client ID from cached order, venue_order_id is None
            (CloudbetResponses.get_bet_status_accepted(), False, False, None, None),
            # Test 4: order with client ID is not found in cached. venue_order_id is None
        ])
    @patch.object(CloudbetClient, 'login', new_callable=AsyncMock, return_value=CloudbetResponses.login())
    @patch.object(CloudbetClient, 'get_bet_status', new_callable=AsyncMock)
    async def test_unsuccessful_order_status_reports(self,
                                                     get_bet_status, login,
                                                     get_bet_status_result, is_exception, client_order_id_is_none,
                                                     cached_order, venue_order_id,
                                                     exec_client,
                                                     instrument):  # Replace exec_client, instrument with your actual fixtures or objects
        """
        General Overview:
        -----------------
        Test case for unsuccessful scenarios in generating Order Status Reports.
        This test focuses on edge cases where exceptions are raised, or `None` is expected to be returned.

        Parameters Explained:
        ---------------------
        Refer to the previous docstring for similar parameters.

        Test Cases Explained:
        ---------------------
        Test 1/Param set 1:
            - 'get_bet_status' raises an exception.
            - No client ID or cached order is provided.
            - Expect None or appropriate exception handling.

        Test 2/Param set 2:
            - 'get_bet_status' raises an exception.
            - A client ID is derived from a cached order, but no venue_order_id is provided.
            - Expect None or appropriate exception handling.

        Test 3/Param set 3:
            - 'get_bet_status' raises an exception.
            - A client ID is derived from a cached order, but venue_order_id is None.
            - Expect None or appropriate exception handling.

        Test 4/Param set 4:
            - 'get_bet_status' returns a valid response.
            - A client ID is derived from a cached order, but venue_order_id is None.
            - Expect None or appropriate exception handling.

        Returns:
            None
        """
        if is_exception:
            get_bet_status.side_effect = Exception(get_bet_status_result)
        else:
            get_bet_status.return_value = get_bet_status_result

        # Common setup code similar to the happy path test
        instrument_id = cloudbet_instrument_id(event_id=20254973, market_name="soccer.team_win_to_nil", outcome="yes",
                                               params="team=away")  # Replace with your actual method to create an InstrumentId
        client_order_id = None if client_order_id_is_none else ClientOrderId("some_client_order_id")

        if cached_order == "valid_order" or cached_order == "valid_order_no_venue_id":
            order = self.order_factory.limit(
                instrument_id,
                OrderSide.BUY,
                Quantity.from_int(10),
                Price.from_str("8.835"),
            )

            if cached_order == "valid_order_no_venue_id":
                # Apply the order_submitted event, which should not have a venue_order_id
                order.apply(TestEventStubs.order_submitted(order=order))  # No venue_order_id here
            else:
                # Apply the order_accepted event, which should have a venue_order_id
                cached_venue_order_id = VenueOrderId("some_venue_order_id")
                order.apply(TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id))

            position_id = PositionId(f"{instrument.id}-{CLOUDBET_VENUE.value}")
            exec_client._cache.add_order(order=order, position_id=position_id)

            # Override the client_order_id to None, to simulate the user not providing it
            if client_order_id_is_none:
                client_order_id = None
            else:
                client_order_id = order.client_order_id  # Ensure client_order_id is set to a valid value

        actual_venue_order_id = VenueOrderId(venue_order_id) if venue_order_id is not None else None
        # Call the function under test
        order_status_report = await exec_client.generate_order_status_report(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=actual_venue_order_id
        )

        # Assertions for unsuccessful scenarios
        assert order_status_report is None

    # ---------------------------- UNHAPPY PATH GENERATE ORDER STATUS REPORTS ------------------------------------------


    #------------------------------------------ TRADE REPORT----------------------------------------------------
    @pytest.mark.parametrize(
        "bet_response, cached_order, is_exception, client_order_id, venue_order_id, report_id, ts_init", [
            (CloudbetResponses.get_bet_status_win(), None, False, None, "some_venue_id", UUID4(), 123456789),
            # Test 1
            (None, "valid_order", False, "client_order_1", "some_venue_id", UUID4(), 123456789),  # Test 2
            ("No bet status response received from Cloudbet", None, True, "client_order_1", "some_venue_id", UUID4(),
             123456789),  # Test 3
            (CloudbetResponses.get_bet_status_win(), "valid_order", False, "client_order_1", "some_venue_id",
             UUID4(), 123456789)  # Test 4
        ])
    @patch.object(CloudbetClient, 'get_bet_status', new_callable=AsyncMock)
    def test_bet_to_trade_report(self,
        get_bet_status,
        bet_response, cached_order, is_exception, client_order_id, venue_order_id, report_id, ts_init,
        account_id, instrument, exec_client):  # Replace account_id, instrument_id with your actual fixtures or objects
        """
        General Overview:
        -----------------
        Test case for converting various bet responses and orders into Trade Reports.
        This test aims to cover multiple edge cases in a single function using different combinations of patched data and assumptions.

        Parameters:
        -----------
        get_bet_status (AsyncMock): A patched version of the 'get_bet_status' method.
        bet_response: Mock result for 'bet_response'.
        order: Mock result for 'order'.
        is_exception (bool): If 'get_bet_status' should raise an exception.
        client_order_id (str): The client_order_id to use in the test.
        venue_order_id (str): The venue_order_id to use in the test.
        report_id (UUID4): The report_id to use in the test.
        ts_init (int): The timestamp for initialization.
        account_id: The account_id fixture.
        instrument_id: The instrument_id fixture.

        Test Cases Explained:
        ---------------------
        Test 1/Param set 1:
            - Tests when 'bet_response' is valid, and no 'order' is provided.
            - Assertions: Expect a TradeReport.

        Test 2/Param set 2:
            - Tests when no 'bet_response' is provided, but a valid 'order' is.
            - Assertions: Expect a TradeReport.

        Test 3/Param set 3:
            - Tests when 'bet_response' is invalid and no 'order' is provided.
            - Assertions: Expect an exception to be raised.

        Test 4/Param set 4:
            - Tests when both 'bet_response' and 'order' are provided.
            - Assertions: Expect a TradeReport.

        Returns:
            None
        """
        if is_exception:
            get_bet_status.side_effect = Exception("Failed to fetch bet status")
        else:
            get_bet_status.return_value = bet_response

        # Create an instrument_id if it is not supposed to be None
        instrument_id = instrument.id
        cached_venue_order_id : Optional[VenueOrderId] = None
        if cached_order in ["valid_order", "valid_order_no_venue_id"]:
            order = self.order_factory.limit(
                instrument_id,
                OrderSide.BUY,
                Quantity.from_int(10),
                Price.from_str("8.835"),
            )

            if cached_order == "valid_order_no_venue_id":
                order.apply(TestEventStubs.order_submitted(order=order))
            else:
                # Use the reference_id from the bet as the VenueOrderId
                cached_venue_order_id = VenueOrderId(CloudbetResponses.get_bet_status_win().reference_id) #have to patch this even if it's not used
                order.apply(TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id))

            position_id = PositionId(f"{instrument_id}-{CLOUDBET_VENUE.value}")
            exec_client._cache.add_order(order=order, position_id=position_id)
            cached_order = order

        typed_venue_order_id = VenueOrderId(venue_order_id) if cached_venue_order_id is None else cached_venue_order_id
        typed_client_order_id = ClientOrderId(client_order_id) if client_order_id is not None else None

        # Invoke the bet_to_trade_report function
        try:
            trade_report = bet_to_trade_report(
                order=cached_order,
                account_id=account_id,
                instrument_id=instrument_id,
                bet_response=bet_response,
                ts_init=ts_init,
                venue_order_id=typed_venue_order_id,
                report_id=report_id,
                client_order_id=typed_client_order_id
            )
            assert isinstance(trade_report, TradeReport)
            # TODO: Replace with more specific assertions for a valid bet response or cached order

        except Exception as e:
            if is_exception:
                assert isinstance(e, Exception)  # Replace with the expected exception type
            else:
                assert False, f"Unexpected exception: {e}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "instrument_id, venue_order_id, start, end, get_bet_status_response, get_bet_history_response, cached_order, is_exception, expected_reports_count",
        [
            # ("some_instrument_id", None, "20-10-01", "2021-10-02", None, CloudbetResponses.get_bet_history_success(), "valid_order", False, 2),  # Test 1
            # (None, "some_venue_order_id", None, None, CloudbetResponses.get_bet_status_win(), CloudbetResponses.get_bet_history_success(), "valid_order", False, 1),  # Test 2
            # (None, None, "20-10-01", "2021-10-02", None, CloudbetResponses.get_bet_history_success(), "valid_order", False, 1),  # Test 3
            # (None, None, "20-10-01", "2021-10-02", None, CloudbetResponses.get_bet_history_success(), "invalid_order",
            #  False, 0),  # Test 3.2 # invalid order # TODO: test again and see which paths are traversed
            ("some_instrument_id", None, None, None, None, None, None, True, 0),  # Test 4 : Exception case
            ("some_instrument_id", None, None, None, None, CloudbetResponses.get_bet_history_success(), "valid_order", True, 10),  # Test 4.2
            ("some_instrument_id", "some_venue_order_id", None, None, CloudbetResponses.get_bet_status_win(), CloudbetResponses.get_bet_history_success(), "valid_order", False, 1),  # Test 5
            ("some_instrument_id", "some_venue_order_id", None, None, None, None, None, True, 0)  # Test 6: Exception case
        ]
    )
    @pytest.mark.parametrize("instruments", [(CLOUDBET_VENUE, 10)], indirect=["instruments"])  # return 10 instruments
    @patch.object(CloudbetClient, 'get_bet_status', new_callable=AsyncMock)
    @patch.object(CloudbetClient, 'get_bet_history', new_callable=AsyncMock, return_value=CloudbetResponses.get_bet_history_success())
    async def test_generate_trade_reports(self, get_bet_history, get_bet_status, instrument_id, venue_order_id, start,
                                          end, get_bet_status_response, get_bet_history_response, cached_order, is_exception,
                                          expected_reports_count, account_id, instrument, instruments, exec_client):
        """
        General Overview:
        -----------------
        Test case for generating trade reports under various conditions.
        This test aims to cover multiple edge cases using different combinations of patched data and assumptions.

        Parameters:
        -----------
        get_bet_history (AsyncMock): A patched version of the 'get_bet_history' method.
        get_bet_status (AsyncMock): A patched version of the 'get_bet_status' method.
        instrument_id: Mock value for 'instrument_id'.
        venue_order_id: Mock value for 'venue_order_id'.
        start: Mock value for 'start' time.
        end: Mock value for 'end' time.
        get_bet_history_response: Mock result for 'get_bet_history'.
        get_bet_status_response: Mock result for 'get_bet_status'.
        is_exception (bool): If the test should expect an exception to be raised.
        expected_reports_count: The expected number of trade reports.
        account_id: The account_id fixture.
        instrument: The instrument fixture.
        exec_client: The execution client fixture.

        Test Cases Explained:
        ---------------------
        Test 1/Param set 1:
            - Tests when 'instrument_id' and a valid time range are provided.
            - Assertions: Expect 2 TradeReports.

        Test 2/Param set 2:
            - Tests when only 'venue_order_id' is provided.
            - Assertions: Expect 1 TradeReport.

        Test 3/Param set 3:
            - Tests when only a time range is provided.
            - Assertions: Expect 2 TradeReports.

        Test 4/Param set 4:
            - Tests when only 'instrument_id' is provided without a time range.
            - Order is cached
            - Assertions: Expect an exception to be raised.

        Test 5/Param set 5:
            - Tests when both 'instrument_id' and 'venue_order_id' are provided.
            - Assertions: Expect an exception to be raised.

        Returns:
            None
        """
        if is_exception:
            get_bet_status.side_effect = Exception("Failed to fetch bet status")
            get_bet_history.side_effect = Exception("Failed to fetch bet history")
        else:
            get_bet_status.return_value = get_bet_status_response
            get_bet_history.return_value = get_bet_history_response
        # start = pd.Timestamp(start) if start else None
        # end = pd.Timestamp(end) if end else None
        start_ts = pd.Timestamp(start) if start else None
        end_ts = pd.Timestamp(end) if end else None

        instrument_id = instrument.id if instrument_id is not None else None

        if cached_order is not None:
            await self.cache_valid_order(exec_client, instruments, instrument_id, cached_order, CloudbetResponses.get_bet_history_success())
        typed_venue_order_id = VenueOrderId(venue_order_id) if venue_order_id is not None else None
        try:
            # Invoke the generate_trade_reports function
            trade_reports = await exec_client.generate_trade_reports(
                instrument_id=instrument_id,
                venue_order_id=typed_venue_order_id,
                start=start_ts,
                end=end_ts
            )
            assert len(
                trade_reports) == expected_reports_count  # Replace with more specific assertions based on your needs

        except Exception as e:
            if is_exception:
                assert isinstance(e, Exception("Failed to fetch bet status"))
            else:
                print(f"Unexpected exception: {e}")
                assert False, f"Unexpected exception: {e}"

    #------------------------------------------ TRADE REPORT----------------------------------------------------

# class TestCloudbetExecutionClientConnect:
#     @pytest.mark.asyncio()
#     # we patch the import and not the class itself, because the class is used in the import
#     @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.connect")
#     async def test_connect_without_stream(self, exec_client):
#         """Test connect without a stream"""
#         # TODO: test with a live stream
#         # Arrange, Act
#         assert exec_client.is_connected is False, f"Expected client to be disconnected, got {exec_client.is_connected}"
#         # exec_client.connect() # this inherited method calls _connect in it's implementation
#         await exec_client._connect()
#         # stream_connect.return_value
#         await asyncio.sleep(0)
#         await asyncio.sleep(
#             0)  # _connect uses multiple awaits, => will take time to resolve so multiple sleeps required.
#         # Assert that underlying _client component is connected
#         assert exec_client._client.connected, f"Expected client to be connected, got {exec_client._client.connected}"
#         # exec_client._connect().assert_called_once()
#
#     # # TODO: test this last as other tasks , side effects may need to firts be resolved assert exec_client.is_connected, f"Expected client to be connected, got {exec_client.is_connected}"
#     # async def test_component_connected_state(self):
#     #     # TODO: patch the _connect method so it resolves all tasks and only then check is_connected
#     #     # assert exec_client._client.is_connected, f"Expected client to be connected, got {exec_client._client.connected}"
#     #     pass  # pragma: no cover
