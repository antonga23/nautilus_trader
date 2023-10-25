import re
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


from functools import lru_cache
from typing import Optional, Union, List
from datetime import datetime, timezone
import pandas

from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.rust.model import ContingencyType, OrderStatus, OrderSide, OrderType, LiquiditySide, \
    PositionSide, TimeInForce
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport, TradeReport, PositionStatusReport
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId, VenueOrderId, AccountId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.objects import Price, Quantity

from nautilus_trader.adapters.cloudbet.client.schema import GetBetResponse, BetStatus, SelectionSide
from nautilus_trader.model.orders import Order

from nautilus_trader.adapters.cloudbet.common import VENUE, CLOUDBET_VENUE


@lru_cache(maxsize=1024)
def make_symbol(event_id: int, submarket_name: str, outcome: str, params: Optional[str] = "") -> Symbol:
    """
    Make symbol with arbitrary number of arguments.
    Each argument will be converted to a string, cleaned, and joined with "|" to form the symbol.

    Arguments are provided as keyword arguments, and order does not matter.

    Example:
    >>> make_symbol(event_id=2135245, submarket_name="1st_half_1x2_period=1h", outcome="home", params="")
    Symbol('2135245|1st_half_1x2_period=1h|home')
    Note: The generated symbol must be no longer than 32 characters.
    """

    PyCondition.type(event_id, int, "event_id")
    PyCondition.not_none(submarket_name, "submarket_name")
    PyCondition.not_none(outcome, "outcome")
    PyCondition.type_or_none(params, str, "params")

    def _clean(input_string):
        # TODO: Consider using a more efficient regular expression pattern in the _clean function. The current
        #  pattern "[ :]" matches any single space or colon character, which might not be the intended behavior. If
        #  you want to match both space and colon characters, you should use the pattern "\s|:".
        # return re.sub(r"\s|:", "", str(s))
        return re.sub(r"[ :]", "", str(input_string))

    value: str = "|".join(
        [_clean(k) for k in (event_id, submarket_name, outcome, params)],
    )
    return Symbol(value)


@lru_cache(maxsize=512)
def extract_cloudbet_symbol(symbol: Symbol) -> tuple[int, str, str, Optional[str]]:
    """
    Extract the event_id, market_name, outcome and params from a symbol
    """
    # TODO: test this handles when params = "" or None
    PyCondition.type(symbol, Symbol, "symbol")
    event_id, market_name, outcome, params = symbol.value.split("|")
    return int(event_id), market_name, outcome, params


@lru_cache(maxsize=1024)
def cloudbet_instrument_id(
    event_id: int,
    market_name: str,
    outcome: str,
    params: Optional[str] = "",
) -> InstrumentId:
    """
    Create an instrument ID from CLOUDBET fields
    """
    PyCondition.type(event_id, int, "event_id")
    PyCondition.not_none(market_name, "market_name")
    PyCondition.not_none(outcome, "outcome")
    PyCondition.type_or_none(params, str, "params")
    symbol = make_symbol(event_id=event_id, submarket_name=market_name, outcome=outcome, params=params)
    return InstrumentId(symbol=symbol, venue=CLOUDBET_VENUE)


def cb_bet_to_order_status_report(
    account_id: AccountId,
    instrument_id: InstrumentId,
    ts_init: int,
    report_id: Union[UUID4, str],
    venue_order_id: VenueOrderId,
    client_order_id: Optional[ClientOrderId] = None,
    order: Optional[Order] = None,
    bet_response: Optional[GetBetResponse] = None
) -> OrderStatusReport:
    """
    Generates the order status report based on the provided bet response or order.

    Args:
        account_id (AccountId): The ID of the account.
        instrument_id (InstrumentId): The ID of the instrument.
        ts_init (int): The initialization timestamp.
        venue_order_id (VenueOrderId): The venue order ID.
        report_id (Union[UUID4, str]): The ID of the report.
        client_order_id (Optional[ClientOrderId], optional): The client order ID. Defaults to None.
        order (Optional[Order], optional): The order object. Defaults to None.
        bet_response (Optional[GetBetResponse], optional): The bet response object. Defaults to None.

    Returns:
        OrderStatusReport: The generated order status report.
    """
    # PyCondition.not_none(order, "order") or PyCondition.not_none(bet_response, "bet_response")
    PyCondition.type(venue_order_id, VenueOrderId, "venue_order_id") # cannot generate report without venue_order_id
    assert order is not None or bet_response is not None, "Either order or bet_response must be provided"
    report: OrderStatusReport = None
    if bet_response is not None:
        bet_status: BetStatus = bet_response.status
        order_status: OrderStatus = bet_status.get_order_status()

        bet_price: str = str(bet_response.price)  # cast from float to str
        order_price: Price = Price.from_str(bet_price)

        bet_quantity: str = str(bet_response.stake)  # cast from float to str
        filled_qty: Quantity = Quantity.from_str(bet_quantity)
        order_quantity: Quantity = filled_qty

        bet_accepted: str = bet_response.create_time  # optimistically assume order accepted at same time as bet placed
        order_accepted: int = cloudbet_timestamp_to_unix_nanos(bet_accepted)

        bet_side: SelectionSide = bet_response.side
        order_side: OrderSide = bet_side.get_order_side()

        report: OrderStatusReport = OrderStatusReport(
            account_id=account_id,
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            client_order_id=client_order_id,
            order_side=order_side,
            order_type=OrderType.LIMIT,  # cloudbet only supports limit orders / although this could be a market order
            contingency_type=ContingencyType.NO_CONTINGENCY,
            time_in_force=TimeInForce.GTC if order is None else order.time_in_force,
            # cloudbet orders are GTC by default
            order_status=order_status,
            price=order_price,
            quantity=order_quantity,
            filled_qty=filled_qty,
            report_id=report_id,
            ts_init=ts_init,
            ts_accepted=order_accepted,  # cast to unix_nanos
            ts_last=order_accepted if order is None else order.ts_last,
        )
    else:  # we only have the order in the cache and were unable to get the bet response
        PyCondition.type(order, Order, "order")  # we should never get here without an order
        venue_order_id: Optional[VenueOrderId] = order.venue_order_id if order.venue_order_id is not None else None
        if venue_order_id is None:
            return
        if order.has_price:
            order_price = order.price
        else:
            order_price = Price(0)
        filled_qty = order.filled_qty if order.filled_qty is not None else Quantity(0) # optional Order fields so we need to check for None
        order_accepted = order.ts_last  # if 0, then order has not been accepted yet
        report: OrderStatusReport = OrderStatusReport(
            account_id=account_id,
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            client_order_id=client_order_id,
            order_side=order.side,
            order_type=order.order_type if order.order_type is not None else OrderType.LIMIT,  # cloudbet only supports limit orders
            contingency_type= order.contingency_type,
            time_in_force=order.time_in_force,  # created on Order init
            order_status=order.status,
            price=order_price,
            quantity=order.quantity,
            filled_qty=filled_qty,
            report_id=report_id,
            ts_init=ts_init,
            ts_accepted=order_accepted,  # cast to unix_nanos
            ts_last=order.ts_last
        )
    return report


# TODO: test function
def bet_to_trade_report(
    order: Order,
    account_id: AccountId,
    instrument_id: InstrumentId,
    bet_response: GetBetResponse,
    ts_init: int,
    venue_order_id: VenueOrderId,
    report_id: Union[UUID4, str],
    client_order_id: Optional[ClientOrderId] = None,  # (None if external order)
) -> TradeReport:
    """
    Convert a cloudbet bet response to a trade report
    """
    # TradeId ~= VenueOrderId for cloudbet, so check if order has a trade_id generated internally else use venue_order_id
    trade_id = order.last_trade_id if order.last_trade_id else venue_order_id
    bet_side: SelectionSide = bet_response.side
    trade_side: OrderSide = bet_side.get_order_side()
    bet_price: str = str(bet_response.price)  # cast from float to str
    trade_price: Price = Price.from_str(bet_price)
    #
    bet_quantity: str = str(bet_response.stake)  # cast from float to str
    # filled_qty : Quantity = Quantity.from_str(bet_quantity)
    trade_quantity: Quantity = Quantity.from_str(bet_quantity)
    #
    bet_time: str = bet_response.create_time  # optimistically assume order accepted at same time as bet placed
    trade_accepted: int = cloudbet_timestamp_to_unix_nanos(bet_time)

    order_liquidity_side: LiquiditySide = LiquiditySide.MAKER if trade_side == OrderSide.BUY else LiquiditySide.TAKER  # TODO: handle OderSide undefined case?

    report: TradeReport = TradeReport(
        account_id=account_id,
        instrument_id=instrument_id,
        venue_order_id=venue_order_id,
        client_order_id=client_order_id,  # (None if external order)
        trade_id=trade_id,
        order_side=trade_side,
        last_qty=trade_quantity,  # cloudbet doesn't track fills, so use order_quantity
        last_px=trade_price,
        liquidity_side=order_liquidity_side,
        report_id=report_id,
        ts_init=ts_init,
        ts_event=trade_accepted,
        commission=None
    )  # TODO: commission is fixed. Check value on cloudbet and add here
    return report


def cb_bet_to_position_report(
    order: List[Order],
    account_id: AccountId,
    instrument_id: InstrumentId,
    bet_response: GetBetResponse,
    ts_init: int,
    venue_order_id: VenueOrderId,
    report_id: Union[UUID4, str],
    client_order_id: Optional[ClientOrderId] = None,  # (None if external order)
) -> PositionStatusReport:
    """
    Convert a cloudbet bet response to a Position status report
    """
    bet_side: SelectionSide = bet_response.side
    position_side: PositionSide = bet_side.get_position_side(bet_side)

    bet_quantity: str = str(bet_response.stake)  # cast from float to str
    position_quantity: Quantity = Quantity.from_str(bet_quantity)

    bet_time: str = bet_response.create_time  # optimistically assume order accepted at same time as bet placed
    position_accepted: int = cloudbet_timestamp_to_unix_nanos(bet_time)

    report: PositionStatusReport = PositionStatusReport(
        account_id=account_id,
        instrument_id=instrument_id,
        position_side=position_side,  # cast to PositionSide
        quantity=position_quantity,
        report_id=report_id,
        ts_last=position_accepted,
        ts_init=ts_init,
        venue_position_id=venue_order_id,  # cast to PostionID, optional
    )
    return report


CLOUDBET_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@lru_cache(maxsize=255)
def cloudbet_timestamp_to_unix_nanos(cloudbet_timestamp: str) -> int:
    """
    Convert a cloudbet timestamp to unix nanoseconds

    Parameters
    ----------
    cloudbet_timestamp : str
        A timestamp in the format "2023-09-19T12:51:11Z"

    Returns
    -------
    int
        A unix timestamp in nanoseconds

    Raises
    ------
    ValueError
        If the cloudbet_timestamp is not in the correct format
    TypeError
        If the provided argument is not a string
    """
    PyCondition.type(cloudbet_timestamp, str, "cloudbet_timestamp")
    try:
        # Parse the string to datetime
        dt = datetime.strptime(cloudbet_timestamp, CLOUDBET_TIMESTAMP_FORMAT)
        # dt = dt.replace(tzinfo=timezone.utc)  # make the datetime object timezone-aware
        # Convert to unix nanoseconds
        return int(dt.timestamp() * 1e9)  # we need to add 1e9 for nanosecond precision
    except ValueError:
        raise ValueError("Invalid timestamp format")


@lru_cache(maxsize=255)
def datetime_to_cloudbet_timestamp(ts: pandas.Timestamp) -> str:
    """
    Convert a pandas datetime to a cloudbet formatted  timestamp string

    Parameters
    ----------
    ts : datetime
        A datetime object
    Returns
    -------
    str
        A timestamp in the format "2023-09-19T12:51:11Z"
    Raises
    ------
    ValueError
        If the timestamp is None
    TypeError
        If `argument` is not of the expected type.
    """
    PyCondition.not_none(ts, "ts")
    PyCondition.type(ts, pandas.Timestamp, "ts")
    return ts.strftime('%Y-%m-%dT%H:%M:%SZ')
