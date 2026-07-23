# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
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

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

pytest.importorskip("py_clob_client")

from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket.common.enums import PolymarketOrderSide
from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProvider
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketBookLevel
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketBookSnapshot
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketQuote
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketQuotes
from nautilus_trader.adapters.polymarket.schemas.book import PolymarketTickSizeChange
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import SubscribeInstruments
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


class _RecordingPolymarketDataClient(PolymarketDataClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.emitted: list[Any] = []

    def _handle_data(self, data: Any) -> None:
        self.emitted.append(data)


def _make_binary_option(price_inc: str) -> BinaryOption:
    instrument_id = InstrumentId.from_str(
        "0xABCDEF.POLYMARKET",
    )
    price_increment = Price.from_str(price_inc)
    size_increment = Quantity.from_str("0.01")
    return BinaryOption(
        instrument_id=instrument_id,
        raw_symbol=instrument_id.symbol,
        outcome="YES",
        description="Test Polymarket Instrument",
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC,
        price_precision=price_increment.precision,
        price_increment=price_increment,
        size_precision=size_increment.precision,
        size_increment=size_increment,
        activation_ns=0,
        expiration_ns=0,
        max_quantity=None,
        min_quantity=Quantity.from_int(1),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )


def _make_binary_option_with_id(instrument_id: str, price_inc: str = "0.01") -> BinaryOption:
    instrument_id_obj = InstrumentId.from_str(instrument_id)
    price_increment = Price.from_str(price_inc)
    size_increment = Quantity.from_str("0.01")
    return BinaryOption(
        instrument_id=instrument_id_obj,
        raw_symbol=instrument_id_obj.symbol,
        outcome="YES",
        description="Test Polymarket Instrument",
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC,
        price_precision=price_increment.precision,
        price_increment=price_increment,
        size_precision=size_increment.precision,
        size_increment=size_increment,
        activation_ns=0,
        expiration_ns=0,
        max_quantity=None,
        min_quantity=Quantity.from_int(1),
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        ts_event=0,
        ts_init=0,
    )


def _make_data_client(event_loop, trader_id: str) -> _RecordingPolymarketDataClient:
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId(trader_id), clock=clock)
    return _RecordingPolymarketDataClient(
        loop=event_loop,
        http_client=MagicMock(),
        msgbus=msgbus,
        cache=Cache(),
        clock=clock,
        instrument_provider=MagicMock(spec=PolymarketInstrumentProvider),
        config=PolymarketDataClientConfig(),
        name="TEST-POLYMARKET",
    )


def _quote(asset_id: str, side: PolymarketOrderSide, price: str, size: str) -> PolymarketQuote:
    return PolymarketQuote(
        asset_id=asset_id,
        price=price,
        side=side,
        size=size,
        hash="",
        best_bid=price,
        best_ask=price,
    )


def _build_snapshot(prices: tuple[str, str, str, str]) -> PolymarketBookSnapshot:
    bid_low, bid_high, ask_low, ask_high = prices
    return PolymarketBookSnapshot(
        market="0xMARKET",
        asset_id="0xASSET",
        bids=[
            PolymarketBookLevel(price=bid_low, size="15"),
            PolymarketBookLevel(price=bid_high, size="10"),
        ],
        asks=[
            PolymarketBookLevel(price=ask_high, size="12"),
            PolymarketBookLevel(price=ask_low, size="8"),
        ],
        timestamp="1700000000000",
    )


def test_tick_size_change_rebuilds_local_book_precision(event_loop) -> None:
    # Arrange
    loop = event_loop
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
    cache = Cache()
    provider = MagicMock(spec=PolymarketInstrumentProvider)
    http_client = MagicMock()

    config = PolymarketDataClientConfig()
    client = _RecordingPolymarketDataClient(
        loop=loop,
        http_client=http_client,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        config=config,
        name="TEST-POLYMARKET",
    )

    instrument_old = _make_binary_option("0.01")
    client._cache.add_instrument(instrument_old)
    client._add_subscription_quote_ticks(instrument_old.id)

    snapshot_old = _build_snapshot(("0.90", "0.94", "0.96", "0.99"))
    deltas_old = snapshot_old.parse_to_snapshot(instrument=instrument_old, ts_init=0)
    book_old = OrderBook(instrument_old.id, book_type=BookType.L2_MBP)
    book_old.apply_deltas(deltas_old)
    client._local_books[instrument_old.id] = book_old

    quote_old = snapshot_old.parse_to_quote(
        instrument=instrument_old,
        ts_init=0,
        drop_quotes_missing_side=False,
    )
    assert quote_old is not None
    client._last_quotes[instrument_old.id] = quote_old

    change = PolymarketTickSizeChange(
        market="0xMARKET",
        asset_id="0xASSET",
        new_tick_size="0.001",
        old_tick_size="0.01",
        timestamp="1700000001000",
    )

    # Act
    client._handle_instrument_update(instrument=instrument_old, ws_message=change)

    # Assert
    instrument_id = instrument_old.id
    provider.add.assert_called_once()

    cached_instrument = client._cache.instrument(instrument_id)
    assert cached_instrument is not None
    assert cached_instrument.price_precision == 3

    rebuilt_book = client._local_books[instrument_id]
    bid_price = rebuilt_book.best_bid_price()
    ask_price = rebuilt_book.best_ask_price()
    assert bid_price is not None
    assert ask_price is not None
    assert bid_price.precision == ask_price.precision == 3

    assert any(
        isinstance(item, QuoteTick)
        and item.instrument_id == instrument_id
        and item.bid_price.precision == item.ask_price.precision == 3
        for item in client.emitted
    )


@pytest.mark.asyncio
async def test_subscribe_instruments_republishes_loaded_instruments(event_loop) -> None:
    loop = event_loop
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TEST-002"), clock=clock)
    cache = Cache()
    provider = MagicMock(spec=PolymarketInstrumentProvider)
    provider.initialize = AsyncMock()
    instrument = _make_binary_option("0.01")
    provider.get_all.side_effect = [{instrument.id: instrument}, {instrument.id: instrument}]
    http_client = MagicMock()

    client = _RecordingPolymarketDataClient(
        loop=loop,
        http_client=http_client,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        config=PolymarketDataClientConfig(),
        name="TEST-POLYMARKET",
    )
    command = SubscribeInstruments(
        client_id=ClientId(POLYMARKET_VENUE.value),
        venue=POLYMARKET_VENUE,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
        params=None,
    )

    await client._subscribe_instruments(command)

    provider.initialize.assert_not_called()
    assert any(item.id == instrument.id for item in client.emitted)
    assert instrument.id in client.subscribed_instruments()


def test_quote_for_unsubscribed_sibling_is_dropped_while_subscribed_is_delivered(
    event_loop,
) -> None:
    # Arrange: the market stream carries both tokens of a market, but this shard only
    # subscribes to one of them.
    client = _make_data_client(event_loop, "TEST-003")

    subscribed = _make_binary_option_with_id("0xMARKET-0xASSETYES.POLYMARKET")
    sibling = _make_binary_option_with_id("0xMARKET-0xASSETNO.POLYMARKET")
    client._cache.add_instrument(subscribed)
    client._cache.add_instrument(sibling)
    client._subscribed_quote_instruments.add(subscribed.id)

    ws_message = PolymarketQuotes(
        market="0xMARKET",
        price_changes=[
            _quote("0xASSETYES", PolymarketOrderSide.BUY, "0.55", "100"),
            _quote("0xASSETYES", PolymarketOrderSide.SELL, "0.60", "80"),
            _quote("0xASSETNO", PolymarketOrderSide.BUY, "0.45", "100"),
        ],
        timestamp="1700000000000",
    )

    # Act
    client._handle_quotes(ws_message=ws_message)

    # Assert: the unsubscribed sibling is filtered silently at ingest - no local book is
    # created and nothing is emitted for it.
    assert sibling.id not in client._local_books
    assert all(getattr(item, "instrument_id", None) != sibling.id for item in client.emitted)

    # Assert: the subscribed instrument is still processed and its quote delivered.
    assert subscribed.id in client._local_books
    assert any(
        isinstance(item, QuoteTick) and item.instrument_id == subscribed.id
        for item in client.emitted
    )


def test_book_snapshot_for_unsubscribed_instrument_is_dropped_silently(event_loop) -> None:
    # Arrange
    client = _make_data_client(event_loop, "TEST-004")

    subscribed = _make_binary_option_with_id("0xMARKET1-0xASSET1.POLYMARKET")
    unsubscribed = _make_binary_option_with_id("0xMARKET2-0xASSET2.POLYMARKET")
    client._cache.add_instrument(subscribed)
    client._cache.add_instrument(unsubscribed)
    client._subscribed_quote_instruments.add(subscribed.id)

    # A one-sided snapshot would hit the `drop_quotes_missing_side` WARN if it were
    # processed; the ingest filter must drop it before any processing or logging.
    snapshot_unsubscribed = PolymarketBookSnapshot(
        market="0xMARKET2",
        asset_id="0xASSET2",
        bids=[PolymarketBookLevel(price="0.40", size="50")],
        asks=[],
        timestamp="1700000000000",
    )

    # Act / Assert: unsubscribed snapshot is dropped silently.
    client._handle_book_snapshot(instrument=unsubscribed, ws_message=snapshot_unsubscribed)
    assert unsubscribed.id not in client._local_books
    assert client.emitted == []

    # Act / Assert: subscribed snapshot is processed and yields a quote.
    snapshot_subscribed = PolymarketBookSnapshot(
        market="0xMARKET1",
        asset_id="0xASSET1",
        bids=[PolymarketBookLevel(price="0.55", size="10")],
        asks=[PolymarketBookLevel(price="0.60", size="12")],
        timestamp="1700000001000",
    )
    client._handle_book_snapshot(instrument=subscribed, ws_message=snapshot_subscribed)
    assert subscribed.id in client._local_books
    assert any(
        isinstance(item, QuoteTick) and item.instrument_id == subscribed.id
        for item in client.emitted
    )


def test_subscribed_quote_ticks_includes_locally_tracked_instruments(event_loop) -> None:
    # Arrange
    client = _make_data_client(event_loop, "TEST-005")
    instrument = _make_binary_option_with_id("0xMARKET-0xASSET.POLYMARKET")

    # Act
    client._subscribed_quote_instruments.add(instrument.id)

    # Assert: the override surfaces locally tracked subscriptions and gating agrees.
    assert instrument.id in client.subscribed_quote_ticks()
    assert client._is_instrument_subscribed(instrument.id)
