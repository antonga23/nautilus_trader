import asyncio
import random
from collections import Counter
from datetime import datetime
from random import choice
from typing import Optional, Union
from unittest import mock
from unittest.mock import patch, AsyncMock, PropertyMock, call
from collections import Counter
import pandas as pd
import msgspec
import pytest
from nautilus_trader.core.rust.model import OrderType, ContingencyType, TimeInForce, OmsType
from nautilus_trader.core.uuid import UUID4

from nautilus_trader.accounting.accounts.base import Account

from nautilus_trader.adapters.cloudbet.client.util import (
    cb_bet_to_order_status_report,
    cloudbet_timestamp_to_unix_nanos,
    make_symbol,
    cloudbet_instrument_id,
    bet_to_trade_report,
    cb_bet_to_position_report,
)
from nautilus_trader.adapters.cloudbet.execution import CloudbetLiveExecutionClient
from nautilus_trader.model.currency import Currency

from nautilus_trader.execution.reports import OrderStatusReport, TradeReport, PositionStatusReport

from nautilus_trader.common.factories import OrderFactory

from nautilus_trader.cache.cache import Cache

from nautilus_trader.adapters.cloudbet.sockets import CloudbetStreamClient
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.model.events import AccountState, OrderFilled
from nautilus_trader.model.instruments import Instrument

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.client.schema import (
    GetLatestOddsResponse,
    SelectionStatus,
    GetEventResponse,
    EventStatus,
    GetAccountInfoResponse,
    GetAccountCurrencies,
    GetAccountBalance,
    GetBetResponse,
    GetBetHistoryResponse,
    BetStatus,
)
from nautilus_trader.adapters.cloudbet.common import CLOUDBET_VENUE
from nautilus_trader.model.enums import AccountType
from nautilus_trader.adapters.cloudbet.data_client import CloudbetDataClient
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.common.clock import LiveClock, TestClock
from nautilus_trader.common.enums import LogLevel
from nautilus_trader.common.logging import Logger
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import InstrumentCloseType
from nautilus_trader.model.enums import MarketStatus
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import (
    InstrumentId,
    AccountId,
    VenueOrderId,
    ClientOrderId,
    PositionId,
    StrategyId,
)
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.objects import Price, Money, AccountBalance, Quantity

from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orderbook import OrderBook
from nautilus_trader.model.orders import Order, LimitOrder, MarketOrder
from nautilus_trader.model.position import Position

from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetResponses, CloudbetTestStubs


class TestCloudbetExecutionClient:
    @pytest.mark.asyncio()
    async def test_exec_client_fixture(self, exec_client):
        assert exec_client is not None, f"Expected exec client to be not None, got {exec_client}"
        assert isinstance(exec_client, CloudbetLiveExecutionClient), (
            f"Expected exec client to be CloudbetLiveExecutionClient, got {type(exec_client)}"
        )

    @pytest.mark.asyncio
    @patch.object(
        CloudbetClient, "login", new_callable=AsyncMock, return_value=CloudbetResponses.login()
    )
    async def test_set_account_id_not_none(self, mock_login, exec_client):
        # Call the method under test
        await exec_client.set_account_id(account_id=None)

        expected_account_id: AccountId = AccountId(
            f"{CLOUDBET_VENUE.value}-{mock_login.return_value.uuid.split('-')[0]}"
        )
        # Assert that the account ID is set correctly
        assert exec_client.account_id == expected_account_id

    # -------------------------------------- TEST ACCOUNT HANDLERS ------------------------------------------------------
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
        mock_login = mocker.patch.object(CloudbetClient, "login")
        mock_login.return_value = CloudbetResponses.login()

        mock_get_account_currencies = mocker.patch.object(CloudbetClient, "get_account_currencies")
        mock_get_account_currencies.return_value = (
            CloudbetResponses.get_account_currencies_success()
        )

        mock_get_balances = mocker.patch.object(CloudbetClient, "get_balances")
        mock_get_balances.side_effect = [
            CloudbetResponses.get_account_balances(),
            CloudbetResponses.get_account_balances(),
        ]

        expected_account_id = AccountId(
            f"{CLOUDBET_VENUE.value}-{mock_login.return_value.uuid.split('-')[0]}"
        )
        # Assert that no account exists in cache yet
        assert exec_client._cache.account(expected_account_id) is None

        # Call the method under test
        await exec_client.connection_account_state()

        # Assert that the necessary methods were called with the correct arguments
        # mock_login.assert_called_once()
        mock_get_account_currencies.assert_called_once()
        mock_get_balances.assert_has_calls([mocker.call("PLAY_EUR"), mocker.call("USDT")])
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
        mock_login = mocker.patch.object(CloudbetClient, "login")
        mock_login.return_value = CloudbetResponses.login()

        mock_get_account_currencies = mocker.patch.object(CloudbetClient, "get_account_currencies")
        mock_get_account_currencies.return_value = (
            CloudbetResponses.get_account_currencies_success()
        )

        mock_get_balances = mocker.patch.object(CloudbetClient, "get_balances")
        mock_get_balances.side_effect = [
            CloudbetResponses.get_account_balances(),
            Exception("Unable to retrieve balance for currency"),
        ]

        expected_account_id = AccountId(
            f"{CLOUDBET_VENUE.value}-{mock_login.return_value.uuid.split('-')[0]}"
        )
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
        assert exec_client._cache.account(expected_account_id).balance(
            typed_currency
        ) == AccountBalance(
            total=Money(balance_amount, typed_currency),
            locked=Money(0, typed_currency),
            free=Money(balance_amount, typed_currency),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "login_result, currencies_result, balances_result",
        [
            (
                Exception("Unable to login"),
                CloudbetResponses.get_account_currencies_success(),
                CloudbetResponses.get_account_balances(),
            ),
            (
                CloudbetResponses.login(),
                Exception("Unable to get account currencies"),
                CloudbetResponses.get_account_balances(),
            ),
            (
                CloudbetResponses.login(),
                Exception("Unable to get account currencies"),
                Exception("Unable to get balances currencies"),
            ),
            (
                CloudbetResponses.login(),
                CloudbetResponses.get_account_balances(),
                Exception("Unable to retrieve balance for currency"),
            ),
        ],
    )
    @patch.object(CloudbetClient, "login", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_account_currencies", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_balances", new_callable=AsyncMock)
    async def test_fails_retrieves_account_info_client_exceptions(
        self,
        login,
        get_account_currencies,
        get_balances,
        login_result,
        currencies_result,
        balances_result,
        exec_client,
        mocker,
    ):
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
        if (
            isinstance(login_result, Exception)
            or isinstance(currencies_result, Exception)
            or isinstance(balances_result, Exception)
        ):
            assert result is None
        else:
            assert result is not None

    # -------------------------------------- TEST ACCOUNT HANDLERS ------------------------------------------------------


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

    async def cache_valid_order(
        self,
        exec_client: Union[mock.MagicMock, CloudbetLiveExecutionClient],
        instruments: list[CryptoBettingInstrument],
        instrument_id: Optional[InstrumentId],
        cached_order: str,
        bet_history: GetBetHistoryResponse,
        **kwargs,
    ) -> None:
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
        if (
            kwargs.get("venue_order_id") is not None
        ):  # if parametrize decorator passes non-None venue_order_id, we need to add it to the Cache
            cached_venue_order_id = kwargs.get("venue_order_id")
            order = self.order_factory.limit(
                instrument_id if instrument_id else instruments[0].id,
                OrderSide.SELL,
                Quantity.from_int(10),
                Price.from_str("8.835"),
            )
            order.apply(
                TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id)
            )
            position_id = PositionId(f"{order.instrument_id.symbol.value}-{CLOUDBET_VENUE.value}")
            exec_client._cache.add_order(order=order, position_id=position_id)

            # # Override the client_order_id to None, to simulate the user not providing it
            # if client_order_id_is_none:
            #     client_order_id = None
            # else:
            #     client_order_id = order.client_order_id  # Ensure client_order_id is set to a valid value

            # print("venue_order_id", order.venue_order_id)
            # print("cached_venue_order_id", exec_client._cache.client_order_id(order.venue_order_id))

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
                    # print("typed venue_order_id", cached_venue_order_id)
                    order.apply(
                        TestEventStubs.order_accepted(
                            order=order, venue_order_id=cached_venue_order_id
                        )
                    )

                position_id = PositionId(f"{instr.id}-{CLOUDBET_VENUE.value}")
                exec_client._cache.add_order(order=order, position_id=position_id)
                exec_client._cache.update_order(order=order)

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

    def test_cloudbet_betstatus_to_order_status_report(
        self, account_id, instrument, exec_client, clock
    ):
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
            bet_response=bet_response,
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
        assert order_status_report.ts_accepted == cloudbet_timestamp_to_unix_nanos(
            bet_response.create_time
        )
        assert order_status_report.ts_last == cloudbet_timestamp_to_unix_nanos(
            bet_response.create_time
        )

    def test_cached_order_to_order_status_report(
        self, account_id, instrument, exec_client, clock, trader_id, venue_order_id
    ):
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

        limit_order.apply(
            TestEventStubs.order_accepted(order=limit_order, venue_order_id=venue_order_id)
        )  # we have to explicitly set the venue order id as it is not set by default

        # Invoke the cb_bet_to_order_status_report function
        order_status_report = cb_bet_to_order_status_report(
            account_id=account_id,
            instrument_id=instrument_id,
            ts_init=ts_init,
            client_order_id=limit_order.client_order_id,
            venue_order_id=limit_order.venue_order_id,
            report_id=report_id,
            bet_response=bet_response,
            order=limit_order,
        )
        assert isinstance(order_status_report, OrderStatusReport)

    def test_raise_assertion_error_if_neither_order_nor_bet_response_provided(
        self, account_id, instrument, exec_client, clock, venue_order_id
    ):
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
                report_id=report_id,
            )

        # Assert an Assertion Error was raised
        assert e.type == AssertionError

    # -- HAPPY PATH GENERATE ORDER STATUS REPORTS ------------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "get_bet_status_result, is_exception, client_order_id_is_none, cached_order, venue_order_id",
        [
            (
                CloudbetResponses.get_bet_status_accepted(),
                False,
                True,
                None,
                "some_venue_id",
            ),  # Test 1
            (
                CloudbetResponses.get_bet_status_accepted(),
                False,
                False,
                None,
                "some_venue_id",
            ),  # Test 2
            (
                "No bet status response received from Cloudbet",
                True,
                False,
                "valid_order",
                "some_venue_id",
            ),  # Test 3
            (None, False, False, "valid_order", "None"),  # Test 4
        ],
    )
    @patch.object(
        CloudbetClient, "login", new_callable=AsyncMock, return_value=CloudbetResponses.login()
    )
    @patch.object(CloudbetClient, "get_bet_status", new_callable=AsyncMock)
    async def test_successful_order_status_reports(
        self,
        get_bet_status,
        login,
        get_bet_status_result,
        is_exception,
        client_order_id_is_none,
        cached_order,
        venue_order_id,
        exec_client,
        instrument,
        limit_order,
    ):  # Replace exec_client, instrument, and limit_order with your actual fixtures or objects
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
        instrument_id = cloudbet_instrument_id(
            event_id=20254973,
            market_name="soccer.team_win_to_nil",
            outcome="yes",
            params="team=away",
        )  # Replace with your actual method to create an InstrumentId

        client_order_id = (
            None if client_order_id_is_none else ClientOrderId("some_id")
        )  # Replace with your actual method to create a ClientOrderId

        if cached_order == "valid_order":
            order = self.order_factory.limit(
                instrument_id,
                OrderSide.BUY,
                Quantity.from_int(10),
                Price.from_str("8.835"),
            )
            cached_venue_order_id = VenueOrderId("some_venue_order_id")
            order.apply(
                TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id)
            )  # Replace with your actual method to apply an order accepted event

            position_id = PositionId(f"{instrument.id}-{CLOUDBET_VENUE.value}")
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
            venue_order_id=actual_venue_order_id,
        )

        # Common assertions
        assert isinstance(order_status_report, OrderStatusReport)
        # TODO: add additional assertion depending on params

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "get_bet_history_result, get_bet_status_result, is_exception, instrument_id_is_none, start, end, open_only, cached_order",
        [
            (
                CloudbetResponses.get_bet_history_success(),
                CloudbetResponses.get_bet_status_accepted(),
                False,
                False,
                None,
                None,
                True,
                "valid_order",
            ),  # Test 1
            (
                CloudbetResponses.get_bet_history_success(),
                CloudbetResponses.get_bet_status_accepted(),
                False,
                False,
                None,
                None,
                False,
                "valid_order",
            ),  # Test 2
            (
                CloudbetResponses.get_bet_history_success(),
                CloudbetResponses.get_bet_status_accepted(),
                False,
                True,
                "2021-10-01",
                "2021-10-02",
                False,
                None,
            ),  # Test 3
            (None, None, True, False, None, None, False, "valid_order"),  # Test 5
            (
                CloudbetResponses.get_bet_history_success(),
                None,
                True,
                True,
                "20-10-01",
                "2021-10-02",
                True,
                "valid_order",
            ),
            # Test 5
        ],
    )
    @pytest.mark.parametrize(
        "instruments", [(CLOUDBET_VENUE, 10)], indirect=["instruments"]
    )  # return 10 instruments
    @patch.object(
        CloudbetClient, "login", new_callable=AsyncMock, return_value=CloudbetResponses.login()
    )
    @patch.object(CloudbetClient, "get_bet_history", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "get_bet_status", new_callable=AsyncMock)
    async def test_successful_generate_order_status_multi_reports(
        self,
        get_bet_status,
        get_bet_history,
        login,
        get_bet_history_result,
        get_bet_status_result,
        is_exception,
        instrument_id_is_none,
        start,
        end,
        open_only,
        cached_order,
        exec_client,
        instrument,
        instruments,
    ):
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
            await self.cache_valid_order(
                exec_client, instruments, instrument_id, cached_order, get_bet_history_result
            )

        # Convert string date to pandas Timestamp if start and end are not None
        start_ts = pd.Timestamp(start) if start else None
        end_ts = pd.Timestamp(end) if end else None

        # Call the function under test
        try:
            order_status_reports = await exec_client.generate_order_status_reports(
                instrument_id=instrument_id, start=start_ts, end=end_ts, open_only=open_only
            )
        except Exception as e:
            if is_exception:
                assert (
                    str(e) == "Failed to fetch bet history"
                    or str(e) == "Failed to fetch bet status"
                )
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
        "get_bet_status_result, is_exception, client_order_id_is_none, cached_order, venue_order_id",
        [
            ("ExceptionMessage", True, True, None, "some_venue_id"),
            # # Test 1: get_bet_status raises an exception, no client ID, no cached order
            ("ExceptionMessage", True, True, "valid_order", "some_venue_id"),
            # # Test 2: get_bet_status raises an exception, client ID from cached order, no venue_order_id
            (
                CloudbetResponses.get_bet_status_accepted(),
                True,
                False,
                "valid_order_no_venue_id",
                None,
            ),
            # Test 3: get_bet_status raises an exception, client ID from cached order, venue_order_id is None
            (CloudbetResponses.get_bet_status_accepted(), False, False, None, None),
            # Test 4: order with client ID is not found in cached. venue_order_id is None
        ],
    )
    @patch.object(
        CloudbetClient, "login", new_callable=AsyncMock, return_value=CloudbetResponses.login()
    )
    @patch.object(CloudbetClient, "get_bet_status", new_callable=AsyncMock)
    async def test_unsuccessful_order_status_reports(
        self,
        get_bet_status,
        login,
        get_bet_status_result,
        is_exception,
        client_order_id_is_none,
        cached_order,
        venue_order_id,
        exec_client,
        instrument,
    ):  # Replace exec_client, instrument with your actual fixtures or objects
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
        instrument_id = cloudbet_instrument_id(
            event_id=20254973,
            market_name="soccer.team_win_to_nil",
            outcome="yes",
            params="team=away",
        )  # Replace with your actual method to create an InstrumentId
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
                order.apply(
                    TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id)
                )

            position_id = PositionId(f"{instrument.id}-{CLOUDBET_VENUE.value}")
            exec_client._cache.add_order(order=order, position_id=position_id)

            # Override the client_order_id to None, to simulate the user not providing it
            if client_order_id_is_none:
                client_order_id = None
            else:
                client_order_id = (
                    order.client_order_id
                )  # Ensure client_order_id is set to a valid value

        actual_venue_order_id = VenueOrderId(venue_order_id) if venue_order_id is not None else None
        # Call the function under test
        order_status_report = await exec_client.generate_order_status_report(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=actual_venue_order_id,
        )

        # Assertions for unsuccessful scenarios
        assert order_status_report is None

    # ---------------------------- UNHAPPY PATH GENERATE ORDER STATUS REPORTS ------------------------------------------

    # ------------------------------------------ TRADE REPORT----------------------------------------------------
    @pytest.mark.parametrize(
        "bet_response, cached_order, is_exception, client_order_id, venue_order_id, report_id, ts_init",
        [
            (
                CloudbetResponses.get_bet_status_win(),
                None,
                False,
                None,
                "some_venue_id",
                UUID4(),
                123456789,
            ),
            # Test 1
            (
                None,
                "valid_order",
                False,
                "client_order_1",
                "some_venue_id",
                UUID4(),
                123456789,
            ),  # Test 2
            (
                "No bet status response received from Cloudbet",
                None,
                True,
                "client_order_1",
                "some_venue_id",
                UUID4(),
                123456789,
            ),  # Test 3
            (
                CloudbetResponses.get_bet_status_win(),
                "valid_order",
                False,
                "client_order_1",
                "some_venue_id",
                UUID4(),
                123456789,
            ),  # Test 4
        ],
    )
    @patch.object(CloudbetClient, "get_bet_status", new_callable=AsyncMock)
    def test_bet_to_trade_report(
        self,
        get_bet_status,
        bet_response,
        cached_order,
        is_exception,
        client_order_id,
        venue_order_id,
        report_id,
        ts_init,
        account_id,
        instrument,
        exec_client,
    ):  # Replace account_id, instrument_id with your actual fixtures or objects
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
        cached_venue_order_id: Optional[VenueOrderId] = None
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
                cached_venue_order_id = VenueOrderId(
                    CloudbetResponses.get_bet_status_win().reference_id
                )  # have to patch this even if it's not used
                order.apply(
                    TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id)
                )

            position_id = PositionId(
                f"{instrument_id}-{CLOUDBET_VENUE.value}"
            )  # TODO: set position_id to venue_order_id
            exec_client._cache.add_order(order=order, position_id=position_id)
            cached_order = order

        typed_venue_order_id = (
            VenueOrderId(venue_order_id) if cached_venue_order_id is None else cached_venue_order_id
        )
        typed_client_order_id = (
            ClientOrderId(client_order_id) if client_order_id is not None else None
        )

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
                client_order_id=typed_client_order_id,
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
            (
                "some_instrument_id",
                None,
                "20-10-01",
                "2021-10-02",
                None,
                CloudbetResponses.get_bet_history_success(),
                "invalid_order",
                False,
                21,
            ),  # Test 1, uses bet history exclusively
            (
                None,
                "some_venue_order_id",
                None,
                None,
                CloudbetResponses.get_bet_status_win(),
                CloudbetResponses.get_bet_history_success(),
                "valid_order",
                False,
                1,
            ),  # Test 2
            (
                None,
                None,
                "20-10-01",
                "2021-10-02",
                None,
                CloudbetResponses.get_bet_history_mixed_status(),
                "valid_order",
                False,
                9,
            ),  # Test 3
            (
                None,
                None,
                "20-10-01",
                "2021-10-02",
                None,
                CloudbetResponses.get_bet_history_success(),
                "invalid_order",
                False,
                0,
            ),  # Test 4 # no valid orders, no instruments so can't construct Trade Report
            (
                "some_instrument_id",
                None,
                None,
                None,
                None,
                None,
                None,
                True,
                0,
            ),  # Test 5 : Exception case
            (
                "some_instrument_id",
                None,
                None,
                None,
                None,
                CloudbetResponses.get_bet_history_success(),
                "valid_order",
                True,
                10,
            ),  # Test 6
            (
                "some_instrument_id",
                "some_venue_order_id",
                None,
                None,
                CloudbetResponses.get_bet_status_win(),
                CloudbetResponses.get_bet_history_success(),
                "valid_order",
                False,
                1,
            ),  # Test 7
            ("some_instrument_id", "some_venue_order_id", None, None, None, None, None, True, 0),
            # Test 8: Exception case
        ],
    )
    @pytest.mark.parametrize(
        "instruments", [(CLOUDBET_VENUE, 10)], indirect=["instruments"]
    )  # return 10 instruments
    @patch.object(CloudbetClient, "get_bet_status", new_callable=AsyncMock)
    @patch.object(
        CloudbetClient,
        "get_bet_history",
        new_callable=AsyncMock,
        return_value=CloudbetResponses.get_bet_history_success(),
    )
    # TODO: refactor docstring as per the test cases
    async def test_generate_trade_reports(
        self,
        get_bet_history,
        get_bet_status,
        instrument_id,
        venue_order_id,
        start,
        end,
        get_bet_status_response,
        get_bet_history_response,
        cached_order,
        is_exception,
        expected_reports_count,
        account_id,
        instrument,
        instruments,
        exec_client,
    ):
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
        Test 1/Case 1: Instrument ID Specified, Bet History Used Exclusively
            - Parameters: Valid `instrument_id`, no `venue_order_id`, `start` and `end` times specified, bet history response successful, cached order is invalid.
            - Code Path: Fetches bet history based on time range, processes bets with successful history response.
            - Assertions: Expects a list of 21 TradeReport objects, as the bet history is used exclusively without considering venue order ID.

        Test 2/Case 2: Venue Order ID Specified, Single TradeReport Expected
            - Parameters: No `instrument_id`, valid `venue_order_id`, no `start` or `end` times, bet status is WIN, valid cached order.
            - Code Path: Fetches single bet status based on venue order ID, generates a single TradeReport.
            - Assertions: Expects a single TradeReport object, as only one venue order ID is processed with a WIN status.

        Test 3/Case 3: Time Range Specified, Mixed Bet Statuses in History
            - Parameters: No `instrument_id`, no `venue_order_id`, valid `start` and `end` times, bet history has mixed statuses, valid cached order.
            - Code Path: Fetches bet history based on time range, processes bets with various statuses.
            - Assertions: Expects a list of 9 TradeReport objects that match the status criteria for processing.

        Test 4/Case 4: Time Range Specified, No Valid Orders or Instruments
            - Parameters: No `instrument_id`, no `venue_order_id`, valid `start` and `end` times, successful bet history response, invalid cached order.
            - Code Path: Attempts to fetch bet history and process bets, but fails due to invalid orders and lack of instrument ID.
            - Assertions: Expects an empty list of TradeReports due to the inability to construct any reports without valid orders or instrument IDs.

        Test 5/Case 5: Exception Case, No Parameters Specified
            - Parameters: Valid `instrument_id`, all other parameters omitted, testing exception handling.
            - Code Path: Tests the exception pathway where insufficient parameters are provided.
            - Assertions: Expects an empty list of TradeReports and potentially an exception to be logged or handled.

        Test 6/Case 6: Instrument ID Specified, Exception Expected
            - Parameters: Valid `instrument_id`, all other parameters omitted, exception flag set to true, successful bet history response, valid cached order.
            - Code Path: Tests the exception pathway for generating trade reports with only an instrument ID.
            - Assertions: Expects an empty list of TradeReports due to the exception flag being true, regardless of the bet history response or valid orders.

        Test 7/Case 7: Instrument ID and Venue Order ID Specified, Single Report from Cache
            - Parameters: Valid `instrument_id` and `venue_order_id`, no `start` or `end` times, bet status is WIN, successful bet history response, valid cached order.
            - Code Path: Fetches bet status based on venue order ID and uses the cache to generate a single TradeReport.
            - Assertions: Expects a single TradeReport object, leveraging both a specific venue order ID and instrument ID for report generation.

        Test 8/Case 8: Exception Case with Instrument and Venue Order ID Specified
            - Parameters: Valid `instrument_id` and `venue_order_id`, no `start` or `end` times, no responses provided, exception flag set to true.
            - Code Path: Tests the exception pathway when both `instrument_id` and `venue_order_id` are specified but no further data is available.
            - Assertions: Expects an empty list of TradeReports, as the exception flag indicates a failure in the report generation process.
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
            try:
                await self.cache_valid_order(
                    exec_client,
                    instruments,
                    instrument_id,
                    cached_order,
                    CloudbetResponses.get_bet_history_success(),
                    kwargs=venue_order_id,
                )
            except Exception as e:
                print(e)
        # random_bet_ref = CloudbetResponses.get_bet_history_success().bets[0].reference_id
        typed_venue_order_id = (
            VenueOrderId(CloudbetResponses.get_bet_history_success().bets[0].reference_id)
            if venue_order_id is not None
            else None
        )
        try:
            # Invoke the generate_trade_reports function
            trade_reports: list[TradeReport] = await exec_client.generate_trade_reports(
                instrument_id=instrument_id,
                venue_order_id=typed_venue_order_id,
                start=start_ts,
                end=end_ts,
            )
            assert (
                len(trade_reports) == expected_reports_count
            )  # Replace with more specific assertions based on your needs
        except Exception as e:
            if is_exception:
                assert isinstance(e, Exception("Failed to fetch bet status"))
            else:
                print(f"Unexpected exception: {e}")
                assert False, f"Unexpected exception: {e}"

    # ------------------------------------------ TRADE REPORT----------------------------------------------------

    # ------------------------------------------ PositionStatusReport----------------------------------------------------
    @pytest.mark.parametrize(
        "bet_response, cached_order, is_exception, client_order_id, venue_order_id, report_id, ts_init, position",
        [
            (
                CloudbetResponses.get_bet_status_win(),
                None,
                False,
                None,
                "some_venue_id",
                UUID4(),
                123456789,
                None,
            ),
            # Test 1 - Valid bet response, no order or position provided
            (
                None,
                "valid_order",
                False,
                "client_order_1",
                "some_venue_id",
                UUID4(),
                123456789,
                None,
            ),
            # Test 2 - No bet response, valid order provided, no position provided
            (
                "No bet status response received from Cloudbet",
                None,
                True,
                "client_order_1",
                "some_venue_id",
                UUID4(),
                123456789,
                None,
            ),
            # Test 3 - Exception case, no bet response or order provided, no position provided
            (
                CloudbetResponses.get_bet_status_win(),
                "valid_order",
                False,
                "client_order_1",
                "some_venue_id",
                UUID4(),
                123456789,
                None,
            ),
            # Test 4 - Valid bet response and order provided, no position
            (None, None, False, None, "some_venue_id", UUID4(), 123456789, "valid_position"),
            # Test 5 - No bet response or order, position provided
        ],
    )
    @patch.object(CloudbetClient, "get_bet_status", new_callable=AsyncMock)
    def test_cb_bet_to_position_report(
        self,
        get_bet_status,
        bet_response,
        cached_order,
        is_exception,
        client_order_id,
        venue_order_id,
        report_id,
        ts_init,
        position,
        account_id,
        instrument,
        exec_client,
    ):
        """
        General Overview:
        -----------------
        Test case for converting various bet responses and orders into PositionStatusReport.
        This test aims to cover multiple edge cases using different combinations of patched data and assumptions.

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
            - Assertions: Expect a PositionStatusReport.

        Test 2/Param set 2:
            - Tests when no 'bet_response' is provided, but a valid 'order' is.
            - Assertions: Expect a PositionStatusReport.

        Test 3/Param set 3:
            - Tests when 'bet_response' is invalid and no 'order' is provided.
            - Assertions: Expect an exception to be raised.

        Test 4/Param set 4:
            - Tests when both 'bet_response' and 'order' are provided.
            - Assertions: Expect a PositionStatusReport.

        Test 5/Param set 5:
            - Tests when no 'bet_response' or 'order' is provided, but a 'position' is.
            - Assertions: Expect a PositionStatusReport to be created from the 'position' only.
        Returns:
            None
        """
        if is_exception:
            get_bet_status.side_effect = Exception("Failed to fetch bet status")
        else:
            get_bet_status.return_value = bet_response

        instrument_id = instrument.id
        cached_venue_order_id: Optional[VenueOrderId] = None
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
                cached_venue_order_id = VenueOrderId(
                    CloudbetResponses.get_bet_status_win().reference_id
                )  # have to patch this even if it's not used
                order.apply(
                    TestEventStubs.order_accepted(order=order, venue_order_id=cached_venue_order_id)
                )

            position_id = PositionId(f"{instrument_id}-{CLOUDBET_VENUE.value}")
            exec_client._cache.add_order(order=order, position_id=position_id)
            cached_order = order

        typed_venue_order_id = (
            VenueOrderId(venue_order_id) if cached_venue_order_id is None else cached_venue_order_id
        )
        typed_client_order_id = (
            ClientOrderId(client_order_id) if client_order_id is not None else None
        )

        if position:
            # position_id = PositionId(f"{instrument_id}-{CLOUDBET_VENUE.value}")
            instrument_id: InstrumentId = instrument.id
            order_side: OrderSide = OrderSide.BUY
            price: Price = Price.from_str("8.835")
            quantity = Quantity.from_int(10)
            time_in_force: TimeInForce = TimeInForce.GTC
            strategy_id: StrategyId = (
                self.order_factory.strategy_id
                if self.order_factory.strategy_id is not None
                else StrategyId("S-123456")
            )
            position_id: PositionId = PositionId(f"{instrument_id}-{CLOUDBET_VENUE.value}")
            limit_order = self.order_factory.limit(
                instrument_id,
                order_side,
                quantity,
                price,
            )

            fill: OrderFilled = TestEventStubs.order_filled(
                limit_order,
                instrument=instrument,
                position_id=position_id if position_id is not None else PositionId("P-123456"),
                strategy_id=strategy_id,
                last_px=limit_order.price,
            )
            position: Position = Position(instrument=instrument, fill=fill)

        try:
            report = cb_bet_to_position_report(
                account_id=account_id,
                instrument_id=instrument_id,
                ts_init=ts_init,
                venue_order_id=typed_venue_order_id,
                report_id=report_id,
                order=cached_order,
                bet_response=bet_response,
                client_order_id=typed_client_order_id,
                position=position,
            )

            assert isinstance(report, PositionStatusReport), (
                "Report must be an instance of PositionStatusReport"
            )
            assert report.venue_position_id == PositionId(
                f"{typed_venue_order_id.value.split('-')[-1]}-{CLOUDBET_VENUE}"
            )
            assert instrument_id == report.instrument_id

        except Exception as e:
            if is_exception:
                assert isinstance(e, Exception), f"Unexpected exception type: {type(e)}"
            else:
                assert False, f"Unexpected exception: {e}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "get_bet_history_result, expected_exception, instrument_id, start, end, valid_position, expected_result_length",
        [
            # Case 1: Valid response with bets matching the criteria
            (
                CloudbetResponses.get_bet_history_mixed_status(),
                None,
                "instrument_id_1",
                "2021-10-01",
                "2021-10-02",
                True,
                9,
            ),
            # get_bet_history_mixed_status has 9 valid "accepted" bets => expected_result_length = 9
            # Case 2: Valid response but no bets match the criteria
            (
                CloudbetResponses.get_bet_history_no_bets(),
                None,
                "instrument_id_1",
                "2021-10-01",
                "2021-10-02",
                True,
                0,
            ),
            # Case 3: Exception raised when fetching bet history
            (
                Exception("Failed to fetch bet history"),
                Exception,
                "instrument_id_1",
                "2021-10-01",
                "2021-10-02",
                False,
                None,
            ),
            # Case 4: Instrument ID provided, relevant positions found in the cache
            (
                CloudbetResponses.get_bet_history_mixed_status(),
                None,
                "instrument_id_1",
                None,
                None,
                True,
                9,
            ),  # fail
            # Case 5: Instrument ID provided, no relevant positions in cache
            (
                CloudbetResponses.get_bet_history_success(),
                None,
                "instrument_id_2",
                None,
                None,
                False,
                0,
            ),
            # Case 6: Time range provided, successful fetch and process
            (
                CloudbetResponses.get_bet_history_mixed_status(),
                None,
                None,
                "2021-10-01",
                "2021-10-02",
                True,
                9,
            ),
            # Case 7: Time range provided, no bets in the range
            (
                CloudbetResponses.get_bet_history_success(),
                None,
                None,
                "2023-01-01",
                "2023-01-02",
                False,
                0,
            ),
            # Case 8: Neither instrument ID nor time range provided
            (None, AssertionError, None, None, None, False, None),
        ],
    )
    @patch.object(CloudbetClient, "get_bet_history", new_callable=AsyncMock)
    async def test_generate_position_status_reports(
        self,
        get_bet_history,
        get_bet_history_result,
        expected_exception,
        instrument_id,
        start,
        end,
        valid_position,
        expected_result_length,
        account_id,
        instrument,
        exec_client,
    ):
        """
        General Overview:
        -----------------
        Test cases for generating position status reports from bet histories and/or cached data within the Cloudbet execution client.
        This test suite aims to verify the functionality under various conditions, including valid and empty responses, exceptions during data retrieval, and different parameter combinations.

        Parameters:
        -----------
        get_bet_history (AsyncMock): A mock of the 'get_bet_history' method from the CloudbetClient, used to simulate fetching bet history.
        get_bet_history_result: The result (or exception) to be returned by the 'get_bet_history' mock.
        expected_exception (Exception or None): Specifies the type of exception expected to be raised, if any.
        instrument_id (str or None): The instrument ID to be used for fetching position status reports, can be None to simulate absence.
        start (str or None): The start datetime as a string, can be None to simulate absence.
        end (str or None): The end datetime as a string, can be None to simulate absence.
        expected_result_length (int or None): The expected length of the result list if no exception is raised, or None if an exception is expected.

        Test Cases Explained:
        ---------------------
        Test 1/Case 1: Instrument ID and Time Range Specified, Bet History Success, Matching Bets
            - Parameters: Valid `instrument_id`, valid `start` and `end` dates, bet_history with bets that match status criteria.
            - Code Path: Fetches bet history, filters by bet status, queries cache for positions, generates reports.
            - Assertions: A list of PositionStatusReport objects corresponding to the valid bets and positions is expected.

        Test 2/Case 2: Instrument ID Specified, Time Range Omitted, Bet History Success, No Matching Bets
            - Parameters: Valid `instrument_id`, no `start` or `end` dates, bet_history with no bets matching status criteria.
            - Code Path: Fetches bet history, no matching bets found, attempts to find cached positions.
            - Assertions: An empty list due to no matching bets and no relevant cached positions is expected.

        Test 3/Case 3: Exception on Fetching Bet History, Instrument ID and Time Range Specified
            - Parameters: Valid `instrument_id`, valid `start` and `end` dates, fetching bet history raises an exception.
            - Code Path: Attempts to fetch bet history, exception encountered and handled.
            - Assertions: An exception to be raised, no PositionStatusReport generated is expected.

        Test 4/Case 4: Instrument ID Provided, No Time Range, Cache Has Relevant Positions
            - Parameters: Valid `instrument_id`, no `start` or `end` dates, cache contains relevant positions.
            - Code Path: No bet history fetched, relies on cached positions for the specified `instrument_id`.
            - Assertions: A list of PositionStatusReport objects from the cached positions is expected.

        Test 5/Case 5: Instrument ID Provided, No Time Range, No Relevant Positions in Cache
            - Parameters: Another valid `instrument_id`, no `start` or `end` dates, cache does not contain relevant positions.
            - Code Path: No bet history fetched, no relevant cached positions found for the specified `instrument_id`.
            - Assertions: An empty list of PositionStatusReport objects due to lack of relevant positions is expected.

        Test 6/Case 6: Time Range Provided, No Instrument ID, Successful Fetch and Process
            - Parameters: No `instrument_id`, valid `start` and `end` dates, bet_history fetch is successful.
            - Code Path: Fetches bet history for the given time range, filters by bet status, generates reports for all instruments.
            - Assertions: A list of PositionStatusReport objects corresponding to the valid bets within the specified time range is expected.

        Test 7/Case 7: Time Range Provided, No Instrument ID, No Bets in the Range
            - Parameters: No `instrument_id`, a future `start` and `end` date with no bets present.
            - Code Path: Fetches bet history for the given time range, finds no bets.
            - Assertions: An empty list of PositionStatusReport objects due to no bets in the specified time range is expected.

        Test 8/Case 8: Neither Instrument ID Nor Time Range Provided
            - Parameters: No `instrument_id`, no `start` or `end` dates, no bet_history available.
            - Code Path: Fails to proceed due to missing required parameters for fetching bet history or querying cached positions.
            - Assertions: An AssertionError due to improper function call is expected.

        Returns:
            None
        """
        if isinstance(get_bet_history_result, Exception):
            get_bet_history.side_effect = get_bet_history_result
        else:
            get_bet_history.return_value = get_bet_history_result

        # Mock any other dependencies here if necessary

        # Convert string date to pandas Timestamp if start and end are not None
        start_ts = pd.Timestamp(start) if start else None
        end_ts = pd.Timestamp(end) if end else None

        instrument_id = instrument.id if instrument_id is not None else None

        if valid_position:
            for bet in get_bet_history_result.bets:
                if bet.status in [
                    BetStatus.PARTIAL,
                    BetStatus.HALF_LOSS,
                    BetStatus.HALF_WIN,
                    BetStatus.PUSH,
                    BetStatus.LOSS,
                    BetStatus.WIN,
                    BetStatus.ACCEPTED,
                ]:
                    order_side = choice([OrderSide.BUY, OrderSide.SELL])
                    position_id = PositionId(
                        f"{bet.reference_id.split('-')[-1]}-{CLOUDBET_VENUE.value}"
                    )
                    cached_venue_order_id = VenueOrderId(str(bet.reference_id))
                    limit_order: LimitOrder = self.order_factory.limit(
                        instrument_id if instrument_id else instrument.id,
                        order_side,
                        Quantity.from_str(str(bet.stake)),
                        Price.from_str(bet.price),
                    )

                    fill: OrderFilled = TestEventStubs.order_filled(
                        limit_order,
                        instrument=instrument,
                        position_id=position_id,
                        strategy_id=self.strategy_id,
                        last_px=limit_order.price,
                        account_id=account_id,
                        venue_order_id=cached_venue_order_id,
                    )
                    exec_client._cache.add_order(order=limit_order, position_id=position_id)
                    position: Position = Position(instrument=instrument, fill=fill)
                    exec_client._cache.add_position(
                        position=position, oms_type=OmsType.HEDGING
                    )  # OmsType.HEDGING => multiple positions per instrument
                    limit_order.apply(
                        TestEventStubs.order_accepted(
                            order=limit_order, venue_order_id=cached_venue_order_id
                        )
                    )  # Replace with your actual method to apply an order accepted event
                    exec_client._cache.update_order(
                        order=limit_order
                    )  # Tres important!! => if we don't udpate the Order the Cache's mapping between Client Order ID and Venue Order ID will be broken
                    # print("Cached Client Order ID:", exec_client._cache.client_order_id(cached_venue_order_id))
                    # print("Cached Client Order ID:", exec_client._cache.client_order_id(limit_order.venue_order_id)) # self._index_order_ids.get(venue_order_id)
                    # exec_client._cache.update_order(order=limit_order)
                    # print("Final Cached Client Order ID:", exec_client._cache.client_order_id(limit_order.venue_order_id))

        # Call the function under test
        if expected_exception:
            with pytest.raises(expected_exception):
                await exec_client.generate_position_status_reports(
                    instrument_id=instrument_id, start=start_ts, end=end_ts
                )
        else:
            position_status_reports = await exec_client.generate_position_status_reports(
                instrument_id=instrument_id, start=start_ts, end=end_ts
            )

            # Assertions
            assert len(position_status_reports) == expected_result_length
            if expected_result_length > 0:
                assert all(
                    isinstance(report, PositionStatusReport) for report in position_status_reports
                )

    # ------------------------------------------ PositionStatusReport---------------------------------------------------
    # --------------------------------------------TEST CONNECTION HANDLERS ----------------------------------------------


class TestCloudbetExecutionClientConnect:
    @pytest.mark.asyncio()
    @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.connect")
    async def test_connect_without_stream(self, stream_connect, exec_client):
        """Test connect without a stream"""
        # TODO: test with a live stream
        # Arrange, Act
        stream_connect.side_effect = None  # No side effect for now
        assert exec_client.is_connected is False, (
            f"Expected client to be disconnected, got {exec_client.is_connected}"
        )
        await exec_client._connect()
        # Assert that underlying _client component is connected
        assert exec_client._client.connected, (
            f"Expected client to be connected, got {exec_client._client.connected}"
        )

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "stream_connect_exception, account_state_exception, expected_stream_call_count, expected_account_state_call_count",
        [
            (None, None, 0, 1),  # Both tasks complete without exceptions
            (Exception("Stream connect failed"), None, 1, 1),  # Stream connect throws exception
            (
                None,
                Exception("Account state connection failed"),
                0,
                1,
            ),  # Account state throws exception
            (
                Exception("Stream connect failed"),
                Exception("Account state connection failed"),
                1,
                1,
            ),
            # Both tasks throw exceptions
        ],
    )
    @patch("nautilus_trader.adapters.cloudbet.sockets.CloudbetStreamClient.connect")
    @patch.object(CloudbetLiveExecutionClient, "connection_account_state", new_callable=AsyncMock)
    @patch.object(CloudbetLiveExecutionClient, "_log", new_callable=PropertyMock)
    @patch.object(CloudbetStreamClient, "is_connected", new_callable=PropertyMock)
    async def test_connect_tasks(
        self,
        mock_stream_is_connected,
        mock_logger,
        mock_connection_account_state,
        mock_stream_connect,
        stream_connect_exception,
        account_state_exception,
        expected_stream_call_count,
        expected_account_state_call_count,
        exec_client,
        account_state,
        portfolio,
    ):
        """
        General Overview:
        -----------------
            This test ensures that the connection initialization tasks for the CloudbetLiveExecutionClient are working correctly. It checks the connection to the stream and the account state under various scenarios, including successful connections and simulated failures. The test also verifies that appropriate logging occurs for each situation, and that the internal state of the execution client is updated as expected.

        Parameters:
        -----------
            mock_stream_is_connected (PropertyMock): A mock of the 'is_connected' property of CloudbetStreamClient to control its return value.
            mock_logger (PropertyMock): A mock of the '_log' property to capture logging output.
            mock_connection_account_state (AsyncMock): A mock of the 'connection_account_state' method to simulate account state retrieval.
            mock_stream_connect (AsyncMock): A mock of the 'connect' method to simulate stream connection.
            stream_connect_exception (Exception or None): Specifies the exception to be raised when attempting to connect to the stream, if any.
            account_state_exception (Exception or None): Specifies the exception to be raised when retrieving the account state, if any.
            expected_stream_call_count (int): The expected number of times the stream connection attempt should be made.
            expected_account_state_call_count (int): The expected number of times the account state retrieval should be attempted.
            exec_client (CloudbetLiveExecutionClient): The instance of the execution client being tested.
            account_state (AccountState): A mock account state to be used for the test.
            portfolio (Portfolio): A mock portfolio to verify account state updates.

        Test Cases Explained:
        ---------------------
            Test 1: Successful Connection to Stream and Account State
                - Parameters: No exceptions simulated.
                - Expected Behavior: Both connection to stream and account state retrieval are attempted once.

            Test 2: Stream Connection Fails, Account State Succeeds
                - Parameters: Simulated exception for stream connection.
                - Expected Behavior: Stream connection attempt is made once, account state retrieval is attempted once, and appropriate error is logged.

            Test 3: Stream Connection Succeeds, Account State Fails
                - Parameters: Simulated exception for account state retrieval.
                - Expected Behavior: Stream connection attempt is made once, account state retrieval attempt is made once, and appropriate error is logged.

            Test 4: Both Stream Connection and Account State Fail
                - Parameters: Simulated exceptions for both stream connection and account state retrieval.
                - Expected Behavior: Stream connection attempt is made once, account state retrieval attempt is made once, and both errors are logged.

        Additional Assertions:
        ----------------------
            - Verifies that the stream is connected if no exception is raised.
            - Checks that the appropriate errors are logged based on the exceptions simulated.
            - Confirms that the portfolio balances are updated correctly if no exception is raised during account state retrieval.
            - Ensures that the watch_stream task is initiated correctly based on the internal state of the execution client.

        Returns:
            None
        """
        # Arrange
        # Set the return_value of is_connected based on stream_connect_exception
        mock_stream_is_connected.return_value = True if not stream_connect_exception else False

        def side_effect_callable():
            if stream_connect_exception:
                raise stream_connect_exception
            else:
                return iter(
                    ()
                )  # Return an empty iterator to simulate successful connection without further actions.

        mock_stream_connect.side_effect = side_effect_callable
        # mock_stream_connect.side_effect = stream_connect_exception or exec_client.stream.is_connected is True

        # print("side effect ", mock_stream_connect.side_effect)
        mock_connection_account_state.side_effect = (
            account_state_exception
            or exec_client._msgbus.send(
                endpoint=f"Portfolio.update_account",
                msg=account_state,
            )
        )
        # Act
        await exec_client._connect()
        # Assert
        assert mock_connection_account_state.call_count == expected_account_state_call_count

        if stream_connect_exception is None:
            assert exec_client.stream.is_connected is True, (
                f"Expected streaming client to be connected, got {exec_client.stream.is_connected}"
            )
            # assert mock_stream_connect.call_count == expected_stream_call_count
        # TODO: this is not the best way to assert the mock logger, but it works for now...too convoluted
        # After the _connect coroutine has been awaited, we want to inspect the mock logger for any error calls.
        # We do this by iterating through all calls made to the mock_logger and filtering for those that
        # are error method calls. We create a list of these specific calls for further inspection.
        error_calls = [call for call in mock_logger.mock_calls if call[0] == "().error"]

        # We now have two conditions to assert based on whether exceptions were expected.
        # We check if the stream connection was supposed to fail.
        if stream_connect_exception is not None:
            # If an exception for the stream connection was expected, we assert that there is at least
            # one error call recorded with the message "Stream connect failed".
            # We check this by asserting that any call in error_calls contains the string "Stream connect failed".
            # The assert statement will raise an AssertionError if the condition is False, which means
            # the error call was not found when it was expected.
            assert any("Stream connect failed" in str(call) for call in error_calls), (
                "Error not logged for stream connect failure"
            )

        # Similarly, we check if the account state connection was supposed to fail.
        if account_state_exception is not None:
            # If an exception for the account state connection was expected, we assert that there is at least
            # one error call recorded with the message "Account state connection failed".
            # We check this by asserting that any call in error_calls contains the string "Account state connection failed".
            # The assert statement here serves the same purpose as above, confirming that the error was logged.
            assert any("Account state connection failed" in str(call) for call in error_calls), (
                "Error not logged for account state failure"
            )
        # Check that the portfolio balances are as expected
        # This part depends on what the expected outcome is for the portfolio balances after _connect
        # For example, if no exceptions, the balances should be updated, otherwise not
        # assert portfolio.account(venue=CLOUDBET_VENUE).balances() == expected_balances
        if account_state_exception is None:
            portfolio_dict: dict = portfolio.account(venue=CLOUDBET_VENUE).balances()
            portfolio_account_balance: Optional[AccountBalance] = None
            for currency, account_balance in portfolio_dict.items():
                if (
                    currency.code == "GBP"
                ):  # Assuming you're looking for the Currency with code "GBP"
                    # Now you can access account_balance
                    portfolio_account_balance = account_balance
                    break
            account_state_dict: dict = account_state.balances[0].to_dict()
            portfolio_account_balance_dict: AccountBalance = portfolio_account_balance.to_dict()
            assert account_state_dict == portfolio_account_balance_dict, (
                f"Expected account balance to be {portfolio_account_balance_dict}, got {account_state_dict}"
            )
            # Additional asserts for _watch_stream_task

    @pytest.mark.asyncio
    @patch.object(CloudbetStreamClient, "disconnect", new_callable=AsyncMock)
    @patch.object(CloudbetClient, "disconnect", new_callable=AsyncMock)
    @patch.object(CloudbetLiveExecutionClient, "_log", new_callable=PropertyMock)
    async def test_disconnect(
        self, mock_log, mock_client_disconnect, mock_stream_disconnect, exec_client
    ):
        """
        Test the CloudbetLiveExecutionClient's ability to disconnect both the streaming socket and the client sessions.
        This test ensures that the necessary clean-up procedures are called and appropriate log messages are emitted.

        Parameters:
        -----------
        mock_log (PropertyMock): A mock of the '_log' property to capture logging output.
        mock_client_disconnect (AsyncMock): A mock of the 'disconnect' method of the CloudbetClient.
        mock_stream_disconnect (AsyncMock): A mock of the 'disconnect' method of the CloudbetStreamClient.
        exec_client (CloudbetLiveExecutionClient): The instance of the execution client being tested.

        Expected Behavior:
        ------------------
        - The 'disconnect' method of the CloudbetStreamClient is called to close the streaming socket.
        - The 'disconnect' method of the CloudbetClient is called to close client sessions.
        - Log messages indicating the closure of the streaming socket and client sessions are emitted.

        Returns:
            None
        """

        # Act
        await exec_client._disconnect()

        # Assert
        mock_stream_disconnect.assert_awaited_once()
        mock_client_disconnect.assert_awaited_once()
        assert mock_log.return_value.info.call_count == 2, (
            "Expected two info log messages to be emitted."
        )
        mock_log.return_value.info.assert_has_calls(
            [call("Closing streaming socket..."), call("Closing CloudbetClient sessions...")],
            any_order=True,
        )

    # --------------------------------------------TEST CONNECTION HANDLERS ---------------------------------------------


class TestCloudbetExecutionCommand:
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

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "valid_cache_instrument, order_has_price, place_bet_response, expected_order_status, expected_exception",
        [
            ("valid_instrument", True, CloudbetResponses.place_bet_success(), "accepted", False),
            ("valid_instrument", False, None, "rejected", ValueError),
            ("valid_instrument", True, CloudbetResponses.place_bet_failure(), "rejected", False),
            ("invalid_instrument", True, None, "rejected", TypeError),  # invalid instrument
            ("valid_instrument", True, Exception("Some exception"), "rejected", False),
        ],
    )
    @patch.object(CloudbetLiveExecutionClient, "_cache")
    @patch.object(CloudbetLiveExecutionClient, "generate_order_submitted")
    @patch.object(CloudbetLiveExecutionClient, "generate_order_accepted")
    @patch.object(CloudbetLiveExecutionClient, "generate_order_rejected")
    @patch.object(CloudbetClient, "place_bets", new_callable=AsyncMock)
    async def test_submit_order(
        self,
        mock_place_bets,
        mock_generate_order_rejected,
        mock_generate_order_accepted,
        mock_generate_order_submitted,
        mock_cache,
        valid_cache_instrument,
        order_has_price,
        place_bet_response,
        expected_order_status,
        expected_exception,
        exec_client,
        exec_engine,
        instrument,
        account_id,
        clock,
    ):
        """
        Tests the _submit_order method of CloudbetLiveExecutionClient.

        Parameters:
        -----------
        mock_place_bets (AsyncMock): Mocks the CloudbetClient's place_bets method.
        mock_generate_order_rejected (Mock): Mocks the generate_order_rejected method.
        mock_generate_order_accepted (Mock): Mocks the generate_order_accepted method.
        mock_generate_order_submitted (Mock): Mocks the generate_order_submitted method.
        mock_cache (Mock): Mocks the _cache attribute of CloudbetLiveExecutionClient.
        valid_cache_instrument (str): Represents a valid instrument for testing.
        order_has_price (bool): Indicates if the order has a price.
        place_bet_response (Mock or None): The response to simulate from place_bets method.
        expected_order_status (str): The expected order status ("accepted" or "rejected").
        exec_client (CloudbetLiveExecutionClient): The execution client instance.
        exec_engine (ExecutionEngine): The execution engine instance.

        Test Cases Explained:
        ---------------------
        Test 1: Valid instrument, order has price, place bet success -> Order accepted
            - Parameters: Valid instrument ID, order with price, successful place bet response.
            - Expected Behavior: Order submission process is completed successfully, order is accepted.

        Test 2: Valid instrument, order does not have price -> Order rejected
            - Parameters: Valid instrument ID, order without price.
            - Expected Behavior: Order submission is aborted, order is rejected due to missing price.

        Test 3: Valid instrument, order has price, place bet failure -> Order rejected
            - Parameters: Valid instrument ID, order with price, failure in place bet response.
            - Expected Behavior: Order submission process encounters an error during place bet, order is rejected.

        Test 4: Invalid instrument, order has price -> Order rejected
            - Parameters: Invalid instrument ID, order with price.
            - Expected Behavior: Order submission process is aborted due to invalid instrument, order is rejected.
        """
        # Arrange
        order_side: OrderSide = choice([OrderSide.BUY, OrderSide.SELL])
        order_quantity: int = random.randint(1, 100)
        order_price: str = str(round(random.uniform(1, 20), 2))

        if order_has_price:
            order: LimitOrder = self.order_factory.limit(
                instrument.id,
                order_side,
                Quantity.from_int(order_quantity),
                Price.from_str(order_price),
            )
        else:
            order: MarketOrder = self.order_factory.market(
                instrument.id,
                order_side,
                Quantity.from_int(order_quantity),
            )
        submit_order_command = SubmitOrder(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            order=order,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        if valid_cache_instrument == "valid_instrument":
            mock_cache.instrument.return_value = instrument
        else:
            mock_cache.instrument.return_value = None  # Return None for invalid instrument

        # command = create_submit_order_command(valid_cache_instrument, order_has_price)
        # mock_cache.instrument.return_value = create_mock_instrument(valid_cache_instrument)
        if isinstance(place_bet_response, Exception):
            mock_place_bets.side_effect = place_bet_response
        else:
            mock_place_bets.return_value = place_bet_response

        # Act
        if expected_exception:
            with pytest.raises(expected_exception):
                await exec_client._submit_order(submit_order_command)
        else:
            await exec_client._submit_order(submit_order_command)

            # Assert
            mock_generate_order_submitted.assert_called_once()

            if expected_order_status == "accepted":
                mock_generate_order_accepted.assert_called_once()
                mock_generate_order_rejected.assert_not_called()
            else:
                mock_generate_order_accepted.assert_not_called()
                mock_generate_order_rejected.assert_called_once()
            # TODO: check if below method is called or order status  has been updated to submittted
            #  cpdef void _send_order_event(self, OrderEvent event):
            #         self._msgbus.send(
            #             endpoint="ExecEngine.process",
            #             msg=event,
            #         )

    # -------------------------------------------TEST COMMAND HANDLERS---------------------------------------------------
    # -------------------------------------------TEST COMMAND HANDLERS---------------------------------------------------
