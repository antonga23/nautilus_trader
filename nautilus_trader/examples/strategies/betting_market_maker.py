from decimal import Decimal

from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.cloudbet.client.util import generate_64bit_uuid
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.rust.common import LogColor
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orderbook import OrderBook
from nautilus_trader.trading.strategy import Strategy


class BettingMarketMaker(Strategy):
    """
    Provides a market making strategy for Sports/Betting orderbook arbitrage.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    max_size : Decimal
        The maximum inventory size allowed.
    trigger_min_size : Quantity
        The minimum size (stake) to trigger an order. Quantity ~= Money
    trigger_min_profit : Decimal
        The minimum profit as percentage to trigger an arbitrage.
    max_stake: int
        The maximum stake allowed for a leg (buy and sell orders).

    Attributes
    ----------
    matching_selections : list[InstrumentId]
        The list of matching selections for the strategy InstrumentID.
    markets : list[Market]
        The list of markets for the strategy.

    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        max_size: Decimal,
        trigger_min_size: Quantity,
        trigger_min_profit: Decimal,
        instrument: CryptoBettingInstrument = None,
        instrument_provider: InstrumentProvider | None = None,
        config: StrategyConfig | None = None,
    ) -> None:
        super().__init__(config=config)

        # Configuration
        self.instrument_id: InstrumentId = instrument_id
        self.max_size = max_size

        self.instrument: CryptoBettingInstrument | None = (
            instrument  # Initialized in on_start if none passed
        )
        self._book: OrderBook | None = None
        self._mid: Decimal | None = None
        self._adj = Decimal(0)
        self.instrument_provider: CloudbetInstrumentProvider = instrument_provider
        self.matching_selections: dict[InstrumentId, list[InstrumentId]] | None = None
        self._instrument_to_book: dict[InstrumentId, BookOrder] = {}
        self._trigger_min_profit = trigger_min_profit
        self._trigger_min_size = trigger_min_size
        self._market_matcher = MarketMatcher()

    def check_trigger(self) -> None:
        """
        Check for trigger conditions.
        """
        self.log.info(
            f"Checking trigger conditions for {self.instrument_id} @ StrategyID:{self.id}",
        )
        if not self._book:
            self.log.info(
                f"No book being maintained for {self.instrument_id} @ StrategyID:{self.id}",
            )
            return

        if not self.instrument:
            self.log.info(f"No instrument loaded for StrategyID: {self.id}.")
            return
        bid_price: Price | None = self._book.best_bid_price()
        ask_price: Price | None = self._book.best_ask_price()
        if not (bid_price and ask_price):
            self.log.info(
                f"No bid or ask price for {self.instrument_id} bid:{bid_price} ask:{ask_price}",
            )
            return
        bid_size: Quantity | None = self._book.best_bid_size()
        ask_size: Quantity | None = self._book.best_ask_size()
        if not (bid_size and ask_size):
            self.log.info(
                f"No bid or ask size for {self.instrument_id} bid:{bid_size} ask:{ask_size}",
            )
            return
        if bid_size < 0 and ask_size < 0:
            self.log.info(
                f"Negative bid or ask size for {self.instrument_id}  bid:{bid_size} ask:{ask_size}",
            )
            return
        # Calculate "true" implied probabilities
        bid_probability: Decimal = 1 / bid_price
        ask_probability: Decimal = 1 / ask_price
        # Check for arbitrage opportunity
        if (bid_probability + ask_probability) > 1:
            self.log.info(
                f"No arbitrage opportunity detected for {self.instrument_id} Profit %:  {1 - (bid_probability + ask_probability)}",
                color=LogColor.RED,
            )
            return
        else:
            if (
                1 - (bid_probability + ask_probability) < self._trigger_min_profit
            ):  # check the profit percentage is above the trigger
                self.log.info(
                    f"Profit percentage is below trigger percentage {self._trigger_min_profit}",
                    color=LogColor.GREEN,
                )
                return
            if (
                self._trigger_min_size > bid_size + ask_size
            ):  # check the combined stake is above the trigger stake size. TODO: use a max/min notional type (Money)
                self.log.info(
                    f"Combined stake for bid and ask:{bid_size + ask_size} is below trigger size {self._trigger_min_size}",
                )
                return
            buy_instrument = self.instrument
            # query the _instrument_to_book mapping to find instrument with correct book
            sell_instrument_id: InstrumentId = sorted(
                self._instrument_to_book.items(),
                key=lambda item: item[1].price.as_decimal(),
                reverse=False,
            )[0][0]
            if sell_instrument_id is None:
                self.log.info(f"Could not find instrument for {sell_instrument_id}")
                return
            sell_instrument: CryptoBettingInstrument = self.cache.instrument(sell_instrument_id)
            if sell_instrument is None:
                self.log.info(f"Could not find instrument for {sell_instrument_id}")
                return
            # Calculate stakes and potential profit
            max_stake = min([self.max_size, bid_size, ask_size])
            stake_on_bid = max_stake * (ask_probability / (bid_probability + ask_probability))
            stake_on_ask = max_stake * (bid_probability / (bid_probability + ask_probability))

            # TODO: enforce venue-specific stake bounds once min and max size
            # constraints are initialized consistently on the instruments.
            # check if strategy has any open orders
            if len(self.cache.orders_inflight(strategy_id=self.id)) > 0:
                return
            # Calculate potential profit from each outcome
            profit_if_bid_wins = stake_on_bid * bid_price - max_stake
            profit_if_ask_wins = stake_on_ask * ask_price - max_stake
            # all checks passed we need to create the orders and send it to the exchange
            self.log.info(
                f"Potential arbitrage opportunity detected! %: {1 - (bid_probability + ask_probability)} Profit: {profit_if_bid_wins + profit_if_ask_wins}",
            )
            self.buy(instrument=buy_instrument, size=stake_on_ask)
            self.buy(instrument=sell_instrument, size=stake_on_bid)

    def on_start(self) -> None:
        """
        Actions to be performed on strategy start.
        """
        if self.instrument is None:
            self.instrument: CryptoBettingInstrument = self.cache.instrument(self.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {self.instrument_id}")
            self.stop()
            return

        # add cached instrument price to the Order book
        self._book = OrderBook(
            instrument_id=self.instrument.id,
            book_type=BookType.L2_MBP,
        )
        self.log.info(f"Successfully created OrderBook for {self.instrument_id}")
        if self.instrument.max_quantity is not None and self.instrument.min_price is not None:
            book_order = BookOrder(
                side=OrderSide.SELL,  # By definition this is a sell
                price=self.instrument.min_price,
                size=self.instrument.max_quantity,
                order_id=generate_64bit_uuid(),
            )
            book_event_time = self.instrument.ts_event or self.clock.timestamp_ns()

            self._book.add(order=book_order, ts_event=book_event_time)

            # update _instrument_to_book mapping
            self._instrument_to_book[self.instrument.id] = book_order
            self.log.info(f"Added {book_order} to the OrderBook")
        # check the cache/provider and find matching selections
        matching_instruments: list[CryptoBettingInstrument] = self.selection_matcher()
        if len(matching_instruments) == 0:
            self.log.debug(
                f"Found {len(matching_instruments)} matching selections for {self.instrument_id}",
                color=LogColor.BLUE,
            )
        else:
            self.log.debug(
                f"Found {len(matching_instruments)} matching selections for {self.instrument_id}",
                color=LogColor.GREEN,
            )

        # for the matched instruments add their prices to the order book
        for instrument in matching_instruments:
            if instrument.max_quantity is None or instrument.min_price is None:
                continue
            synthetic_book_order = BookOrder(
                side=OrderSide.BUY,  # By definition this is a buy
                price=instrument.min_price,
                size=instrument.max_quantity,
                order_id=generate_64bit_uuid(),
            )
            book_event_time = self.instrument.ts_event or self.clock.timestamp_ns()
            self._book.add(synthetic_book_order, book_event_time)
            self._instrument_to_book[instrument.id] = synthetic_book_order
            self.log.info(f"Added Order ID: {synthetic_book_order.order_id} to the OrderBook")
        self.check_trigger()
        self.subscribe_instrument(self.instrument.id)
        for ins in matching_instruments:
            self.subscribe_instrument(ins.id)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """
        Actions to be performed when an order book delta update is received.
        """
        if not self._book:
            self.log.error("No book being maintained.")
            return

        self._book.apply_deltas(deltas)
        self.check_trigger()

    def on_order_book(self, order_book: OrderBook) -> None:
        """
        Actions to be performed when an order book update is received.
        """
        self._book = order_book
        self.check_trigger()

    def on_instrument(self, instrument: CryptoBettingInstrument) -> None:
        """
        Actions to be performed when an Instrument update is received.
        """
        # check what instrument we have
        if instrument.id == self.instrument_id:
            if instrument.max_quantity is not None and instrument.min_price is not None:
                book_order = BookOrder(
                    side=OrderSide.SELL,  # By definition this is a sell
                    price=instrument.min_price,
                    size=instrument.max_quantity,
                    order_id=generate_64bit_uuid(),
                )
                self._instrument_to_book[instrument.id] = (
                    book_order  # update book for Strats instrument
                )
                # update book ,replace with single call to self._book.update()
                self._book.clear_asks()
                book_event_time = instrument.ts_event or self.clock.timestamp_ns()
                self._book.add(book_order, book_event_time)
            else:  # can't set size or price to zero when None, but we can set size to a negative number so will not be evaluated
                book_order = BookOrder(
                    side=OrderSide.SELL,
                    price=instrument.min_price or Price(-1, 2),
                    size=instrument.max_quantity or Quantity(-1, 2),
                    order_id=generate_64bit_uuid(),
                )
                self._instrument_to_book[instrument.id] = (
                    book_order  # update book for Strats instrument
                )
                self._book.clear_asks()
                book_event_time = instrument.ts_event or self.clock.timestamp_ns()
                self._book.add(book_order, book_event_time)
        if instrument.id in self._instrument_to_book and instrument.id != self.instrument_id:
            if instrument.max_quantity is not None and instrument.min_price is not None:
                synthetic_book_order = BookOrder(
                    side=OrderSide.BUY,  # By definition this is a buy
                    price=instrument.min_price,
                    size=instrument.max_quantity,
                    order_id=generate_64bit_uuid(),
                )
                book_event_time = instrument.ts_event or self.clock.timestamp_ns()
                self._instrument_to_book[instrument.id] = synthetic_book_order
                self._book.update(synthetic_book_order, book_event_time)
            else:  # can't set size or price to zero when None, but we can set size to a negative number so will not be evaluated
                synthetic_book_order = BookOrder(
                    side=OrderSide.BUY,
                    price=instrument.min_price or Price(-1, 2),
                    size=instrument.max_quantity or Quantity(-1, 2),
                    order_id=generate_64bit_uuid(),
                )
                book_event_time = instrument.ts_event or self.clock.timestamp_ns()
                self._instrument_to_book[instrument.id] = synthetic_book_order
                self._book.update(synthetic_book_order, book_event_time)
        self.check_trigger()

    def buy(self, instrument: CryptoBettingInstrument, size: Decimal) -> None:
        """
        Users simple buy method.
        """
        if not instrument:
            self.log.error("No instrument passed.")
            return
        ins_quantity = Quantity(size, 2)
        ins_price = Price(instrument.price, 2)
        order = self.order_factory.limit(
            instrument_id=instrument.id,
            order_side=OrderSide.BUY,
            price=ins_price,
            quantity=ins_quantity,
        )

        self.submit_order(order)

    def sell(self, instrument: CryptoBettingInstrument, size: Decimal) -> None:
        """
        Users simple sell method .
        """
        if not instrument:
            self.log.error("No instrument passed.")
            return
        ins_quantity = Quantity(size, 2)
        ins_price = Price(instrument.price, 2)
        order = self.order_factory.limit(
            instrument_id=instrument.id,
            order_side=OrderSide.SELL,
            price=ins_price,
            quantity=ins_quantity,
        )

        self.submit_order(order)

    def on_stop(self) -> None:
        """
        Actions to be performed when the strategy is stopped.
        """
        return

    def selection_matcher(self) -> list[CryptoBettingInstrument]:
        """
        Find hedge selections using the shared semantic matcher.
        """
        if not self.instrument or not self.instrument_provider:
            return []
        instrument_filter = {"sport_name": self.instrument.sport_name}
        search_results = self.instrument_provider.search_instruments(instrument_filter) or []
        hedges = self._market_matcher.find_hedges(
            self.instrument,
            search_results,
            include_cross_venue=False,
        )
        return [hedge.instrument for hedge in hedges]
