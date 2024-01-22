from decimal import Decimal
from typing import Optional, Union, List

from nautilus_trader.core.message import Event
from nautilus_trader.core.rust.model import TimeInForce
# from nautilus_trader.core.uuid import UUID4
import uuid

from nautilus_trader.adapters.cloudbet.client.util import generate_64bit_uuid
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.config import StrategyConfig

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.data import OrderBookDeltas, BookOrder
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.events import PositionChanged
from nautilus_trader.model.events import PositionClosed
from nautilus_trader.model.events import PositionOpened
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity

from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orderbook import OrderBook
from nautilus_trader.trading.strategy import Strategy

from nautilus_trader.model.orders.limit import LimitOrder

# TODO: complete mapping between all markets for all sports

# 'soccer.team_win_to_nil': [('winner', 'team_clean_sheet'), (), ()], # example of a special case 1 market maps to 2 markets simultaneously
# 'match_odds': [ ('asian_handicap', 'double_chance') ] # example of a special case 1 market maps to 2 markets simultaneously

MARKET_MAPPER: dict[str, List[str]] = {
    'soccer.match_odds': ['double_chance'],
    'soccer.double_chance': ['asian_handicap', 'match_odds'],
    'soccer.both_teams_to_score': ['both_teams_to_score'],
    'soccer.total_goals': ['total_goals'],  # over/under markets
    'soccer.draw_no_bet': ['draw_no_bet'],  # NB: Can result in PUSH in event of draw
    # 'soccer.match_odds_period_first_half': ['asian_handicap_period_first_half', 'match_odds_period_first_half'],
    # 'soccer.match_odds_period_second_half': ['asian_handicap_period_second_half', 'match_odds_period_second_half'],
    # 'soccer.asian_handicap': ['asian_handicap', 'draw_no_bet', 'double_chance', 'match_odds'],
    'soccer.asian_handicap_period_first_half': ['match_odds_period_first_half', 'asian_handicap_period_first_half'],
    # 'soccer.asian_handicap_period_first_half': ['asian_handicap_period_first_half', 'match_odds_period_first_half'],
}  # TODO: store and add to cache in case it has not been added


class BettingMarketMaker(Strategy):
    """
    Provides a market making strategy for Sports/Betting orderbook arbitrage.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    max_size : Decimal
        The maximum inventory size allowed.
    trigger_min_size : Union[Decimal, float]
        The minimum size to trigger an order.
    trigger_min_profit : Union[Decimal, float, str]
        The minimum profit as percentage to trigger an arbitrage.

    Attributes
    ----------
    macthing_selections : list[InstrumentId]
        The list of matching selections for the strategy InstrumentID.
    markets : list[Market]
        The list of markets for the strategy.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        max_size: Decimal,
        trigger_min_size: Union[Decimal, float],
        trigger_min_profit: Union[Decimal, float, str],
        instrument: CryptoBettingInstrument = None,
        instrument_provider: Optional[InstrumentProvider] = None,
        config: Optional[StrategyConfig] = None
    ) -> None:
        super().__init__()

        # Configuration
        self.instrument_id: InstrumentId = instrument_id
        self.max_size = max_size

        self.instrument: Optional[CryptoBettingInstrument] = instrument  # Initialized in on_start if none passed
        self._book: Optional[OrderBook] = None
        self._mid: Optional[Decimal] = None
        self._adj = Decimal(0)
        self.instrument_provider: CloudbetInstrumentProvider = instrument_provider
        self.macthing_selections: Optional[dict[InstrumentId, List[InstrumentId]]] = None
        self._instrument_to_book: dict[InstrumentId, BookOrder] = {}
        # self.order_factory = config.order_factory

    def check_trigger(self) -> None:
        """Check for trigger conditions."""
        if not self._book:
            self.log.error("No book being maintained.")
            return

        if not self.instrument:
            self.log.error("No instrument loaded.")
            return
        bid_price = self._book.best_bid_price()
        ask_price = self._book.best_ask_price()
        if not (bid_price and ask_price):
            return
        if bid_price <= ask_price:
            return
        profit = (ask_price - bid_price) / bid_price
        if profit < self.trigger_min_profit:
            return
        bid_size = self._book.best_bid_size()
        ask_size = self._book.best_ask_size()
        if not (bid_size and ask_size):
            return
        if self.trigger_min_size > min(bid_size, ask_size):
            return
        self.log.info(
            f"Book: {self._book.best_bid_price()} @ {self._book.best_ask_price()}",
        )
        if len(self.cache.orders_inflight(strategy_id=self.id)) > 0:
            return
        # all checks passed we need to create the orders and send it to the exchange
        self.buy(instrument=self.instrument, size=self.bid_size)
        # query the _instrument_to_book mapping to find instrument with correct book
        sorted_instrument_to_book = sorted(self._instrument_to_book.items(),
                                           key=lambda item: item[1].price.as_decimal(), reverse=True)
        # extract the first instrument
        sell_instrument = sorted_instrument_to_book[0]
        self.sell(sell_instrument[0], size=sell_instrument[0].size)

    def on_start(self) -> None:
        """Actions to be performed on strategy start."""
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
        book_order = BookOrder(
            side=OrderSide.SELL,  # By definition this is a sell
            price=self.instrument.min_price,
            size=self.instrument.max_quantity,
            order_id=generate_64bit_uuid()
        )
        book_event_time = self.instrument.ts_event or self.clock.timestamp_ns()

        self._book.add(order=book_order, ts_event=book_event_time)

        # update _instrument_to_book mapping
        self._instrument_to_book[self.instrument.id] = book_order
        #
        # check the cache/provider and find matching selections
        matching_instruments: List[CryptoBettingInstrument] = self.selection_matcher()

        # for the matched instruments add their prices to the order book
        for instrument in matching_instruments:
            # TODO: check instruments with no max quantity eg. market has been disabled
            if instrument.max_quantity is None:
                continue
            synthetic_book_order = BookOrder(
                side=OrderSide.BUY,  # By definition this is a buy
                price=instrument.min_price,
                size=instrument.max_quantity or Quantity(0),
                order_id=generate_64bit_uuid(),
            )
            book_event_time = self.instrument.ts_event or self.clock.timestamp_ns()
            self._book.add(synthetic_book_order, book_event_time)
            self._instrument_to_book[instrument.id] = synthetic_book_order
        # pass

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """Actions to be performed when an order book delta update is received."""
        if not self._book:
            self.log.error("No book being maintained.")
            return

        self._book.apply_deltas(deltas)
        self.check_trigger()

    def on_order_book(self, order_book: OrderBook) -> None:
        """Actions to be performed when an order book update is received."""
        self._book = order_book
        self.check_trigger()

    def buy(self, instrument: CryptoBettingInstrument, quantity: Quantity) -> None:
        """
        Users simple buy method.
        """
        if not instrument:
            self.log.error("No instrument passed.")
            return
        order = self.order_factory.limit(
            instrument_id=instrument.id,
            order_side=OrderSide.BUY,
            price=instrument.price,
            quantity=quantity,
        )

        self.submit_order(order)

    def sell(self, instrument: CryptoBettingInstrument, quantity: Quantity) -> None:
        """
        Users simple sell method .
        """
        if not instrument:
            self.log.error("No instrument passed.")
            return
        order = self.order_factory.limit(
            instrument_id=instrument.id,
            order_side=OrderSide.SELL,
            price=instrument.price,
            quantity=quantity,
        )

        self.submit_order(order)

    def on_stop(self) -> None:
        """
        Actions to be performed when the strategy is stopped.
        """
        # self.cancel_all_orders(self.instrument_id)
        # self.close_all_positions(self.instrument_id)

    def selection_matcher(self) -> List[CryptoBettingInstrument]:
        # We need to find the matching selections for the current instrumentID
        # and use the dataEngine to get the orderbook for matching selections
        matching_markets: Optional[List[str]] = MARKET_MAPPER.get(self.instrument.market_name)
        if not matching_markets:
            return []
        matching_instruments: Optional[list[CryptoBettingInstrument]] = []
        for market in matching_markets:
            market_name = f"{self.instrument.sport_name.lower()}.{market}"  # TODO: set market type on CryptoBettingInstrument to market_name without sport name eg. market_name.split(".")[1]
            instrument_filter = {
                "event_name": self.instrument.event_name,
                "market_name": market_name,
                "sport_name": self.instrument.sport_name
            }
            search_result: Optional[list[CryptoBettingInstrument]] = self.instrument_provider.search_instruments(
                instrument_filter)
            if search_result:
                matching_instruments.extend(search_result)
        # we need to filter matching instruments by the outcome
        # in the case it is the same market, we just need to check the outcome is opposite
        for instrument in matching_instruments:
            if instrument.market_name == self.instrument.market_name:
                # handicap instruments
                # TODO: add a better check using the handicap type/ENUM on CryptoBettingInstrument.
                if 'handicap' in self.instrument.market_name:
                    if instrument.outcome != self.instrument.outcome:  # i.e. opposite outcomes away != home
                        # we need to extract the handicap value from params
                        matching_instrument_handicap_value = instrument.params.split("=")[-1]
                        instrument_handicap_value = self.instrument.params.split("=")[-1]
                        if matching_instrument_handicap_value == instrument_handicap_value:
                            # same market, opposite outcome, same handicap values => match
                            matching_instruments.append(instrument)
                            # TODO: add a LoggerAdapter to log this
                        else:
                            # same market, opposite outcome, but different handicap values => no match
                            continue
                        matching_instruments.append(instrument)
                    else:
                        # same market, same outcome => no match
                        continue
                # 'soccer.both_teams_to_score': ['both_teams_to_score'],
                # 'soccer.total_goals': ['total_goals'], #over/under markets
                # 'soccer.draw_no_bet': ['draw_no_bet'], #over/under markets
                # both_teams_to_score instruments
                if 'both_teams_to_score' in self.instrument.market_name:
                    if instrument.outcome != self.instrument.outcome:  # i.e. opposite outcomes yes != no
                        # same market, opposite outcome => match
                        matching_instruments.append(instrument)
                        # TODO: add a LoggerAdapter to log this
                # total_goals instruments
                if 'total_goals' in self.instrument.market_name:
                    pass
                    if instrument.outcome != self.instrument.outcome:  # i.e. opposite outcomes over != under
                        # same market, opposite outcome => match
                        matching_instrument_total_value = instrument.params.split("=")[-1]
                        instrument_total_value = self.instrument.params.split("=")[-1]
                        if matching_instrument_total_value == instrument_total_value:
                            matching_instruments.append(instrument)
                        # TODO: add a LoggerAdapter to log this
                # draw_no_bet instruments
                if 'draw_no_bet' in self.instrument.market_name:
                    pass
                    # if instrument.outcome != self.instrument.outcome: # i.e. opposite outcomes yes != no
                    #     # same market, opposite outcome => match
                    #     matching_instruments.append(instrument)
                    #     # TODO: add a LoggerAdapter to log this

            else:
                if instrument.outcome != self.instrument.outcome:
                    pass

        return matching_instruments
