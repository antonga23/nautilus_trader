from decimal import Decimal
from typing import Optional, Union, List

from nautilus_trader.core.message import Event
from nautilus_trader.core.rust.common import LogColor
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
    'both_teams_to_score': ['both_teams_to_score'],
    'total_goals': ['total_goals'],  # over/under markets
    'team_total_goals': ['team_total_goals'],  # over/under markets
    'draw_no_bet': ['draw_no_bet'],  # NB: Can result in PUSH in event of draw
    'match_odds': ['double_chance'],
    'double_chance': ['asian_handicap', 'match_odds'],
    'match_odds_period_first_half': ['asian_handicap_period_first_half'],
    'match_odds_period_second_half': ['asian_handicap_period_second_half'],
    'asian_handicap': ['asian_handicap', 'draw_no_bet'],
    'asian_handicap_period_first_half': ['match_odds_period_first_half', 'asian_handicap_period_first_half'],
    'team_total_goals_period_first_half': ['team_total_goals_period_first_half'],  # over/under markets
    'team_total_goals_period_second_half': ['team_total_goals_period_second_half'],  # over/under markets
}  # TODO: store and add to cache in case it has not been added.

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
    macthing_selections : list[InstrumentId]
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
        instrument_provider: Optional[InstrumentProvider] = None,
        config: Optional[StrategyConfig] = None
    ) -> None:
        super().__init__(config=config)

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
        self._trigger_min_profit = trigger_min_profit
        self._trigger_min_size = trigger_min_size

    def check_trigger(self) -> None:
        """Check for trigger conditions."""
        self.log.info(f"Checking trigger conditions for {self.instrument_id} @ StrategyID:{self.id}")
        if not self._book:
            self.log.info(f"No book being maintained for {self.instrument_id} @ StrategyID:{self.id}")
            return

        if not self.instrument:
            self.log.info(f"No instrument loaded for StrategyID: {self.id}.")
            return
        bid_price: Price | None = self._book.best_bid_price()
        ask_price: Price | None = self._book.best_ask_price()
        if not (bid_price and ask_price):
            self.log.info(f"No bid or ask price for {self.instrument_id} bid:{bid_price} ask:{ask_price}")
            return
        bid_size: Quantity | None = self._book.best_bid_size()
        ask_size: Quantity | None = self._book.best_ask_size()
        if not (bid_size and ask_size):
            self.log.info(f"No bid or ask size for {self.instrument_id} bid:{bid_size} ask:{ask_size}")
            return
        if (bid_size < 0 and ask_size < 0) :
            self.log.info(f"Negative bid or ask size for {self.instrument_id}  bid:{bid_size} ask:{ask_size}")
            return
        # Calculate "true" implied probabilities
        bid_probability: Decimal = 1 / bid_price
        ask_probability: Decimal = 1 / ask_price
        # Check for arbitrage opportunity
        if (bid_probability + ask_probability) > 1:
            self.log.info(f"No arbitrage opportunity detected for {self.instrument_id} Profit %:  {1 -(bid_probability + ask_probability)}", color=LogColor.RED)
            return
        else:
            if 1 - (bid_probability + ask_probability) < self._trigger_min_profit: # check the profit percentage is above the trigger
                self.log.info(f"Profit percentage is below trigger percentage {self._trigger_min_profit}", color=LogColor.GREEN)
                return
            if self._trigger_min_size > bid_size + ask_size: # check the combined stake is above the trigger stake size. TODO: use a max/min notional type (Money)
                self.log.info(f"Combined stake for bid and ask:{bid_size + ask_size} is below trigger size {self._trigger_min_size}")
                return
            buy_instrument = self.instrument
            # query the _instrument_to_book mapping to find instrument with correct book
            sell_instrument_id: InstrumentId = sorted(self._instrument_to_book.items(), key=lambda item: item[1].price.as_decimal(), reverse=False)[0][0]
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

            # # Check if stake on ask is within bounds
            # if not (buy_instrument.min_size <= stake_on_ask <= buy_instrument.max_size): TODO: force max and min size intialisation
            #     print(f"Stake on ask is out of bounds: {stake_on_ask}")
            #     return
            # # Check if stake on bid is within bounds
            # if not (sell_instrument.min_size <= stake_on_bid <= sell_instrument.max_size):
            #     print(f"Stake on bid is out of bounds: {stake_on_bid}")
            #     return
            # check if strategy has any open orders
            if len(self.cache.orders_inflight(strategy_id=self.id)) > 0:
                return
            # Calculate potential profit from each outcome
            profit_if_bid_wins = stake_on_bid * bid_price - max_stake
            profit_if_ask_wins = stake_on_ask * ask_price - max_stake
            # all checks passed we need to create the orders and send it to the exchange
            self.log.info(f"Potential arbitrage opportunity detected! %: {1 - (bid_probability + ask_probability)} Profit: {profit_if_bid_wins + profit_if_ask_wins}")
            self.buy(instrument=buy_instrument, size=stake_on_ask)
            self.buy(instrument=sell_instrument, size=stake_on_bid)

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
        self.log.info(f"Successfully created OrderBook for {self.instrument_id}")
        if self.instrument.max_quantity is not None and self.instrument.min_price is not None:
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
            self.log.info(f"Added {book_order} to the OrderBook")
        # check the cache/provider and find matching selections
        matching_instruments: List[CryptoBettingInstrument] = self.selection_matcher()
        if len(matching_instruments) == 0:
            self.log.debug(f"Found {len(matching_instruments)} matching selections for {self.instrument_id}",
                          color=LogColor.BLUE)
        else:
            self.log.debug(f"Found {len(matching_instruments)} matching selections for {self.instrument_id}",
                          color=LogColor.GREEN)

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

    def on_instrument(self, instrument: CryptoBettingInstrument) -> None:
        """Actions to be performed when an Instrument update is received."""
        # check what instrument we have
        if instrument.id == self.instrument_id:
            if instrument.max_quantity is not None and instrument.min_price is not None:
                book_order = BookOrder(
                    side=OrderSide.SELL,  # By definition this is a sell
                    price=instrument.min_price,
                    size=instrument.max_quantity,
                    order_id=generate_64bit_uuid()
                )
                self._instrument_to_book[instrument.id] = book_order # update book for Strats instrument
                # update book ,replace with single call to self._book.update()
                self._book.clear_asks()
                book_event_time = instrument.ts_event or self.clock.timestamp_ns()
                self._book.add(book_order, book_event_time)
            else: # can't set size or price to zero when None, but we can set size to a negative number so will not be evaluated
                book_order = BookOrder(
                    side=OrderSide.SELL,
                    price=instrument.min_price or Price(-1, 2),
                    size=instrument.max_quantity or Quantity(-1, 2),
                    order_id=generate_64bit_uuid()
                )
                self._instrument_to_book[instrument.id] = book_order  # update book for Strats instrument
                self._book.clear_asks()
                book_event_time = instrument.ts_event or self.clock.timestamp_ns()
                self._book.add(book_order, book_event_time)
            # self.instrument = instrument
        if instrument.id in self._instrument_to_book.keys() and instrument.id != self.instrument_id:
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
            else:# can't set size or price to zero when None, but we can set size to a negative number so will not be evaluated
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
        # self.cancel_all_orders(self.instrument_id)
        # self.close_all_positions(self.instrument_id)

    def selection_matcher(self) -> List[CryptoBettingInstrument]:
        # We need to find the matching selections for the current instrumentID
        # and use the dataEngine to get the orderbook for matching selections
        matching_markets: Optional[List[str]] = MARKET_MAPPER.get(self.instrument.market_name.split(".")[-1])
        if not matching_markets:
            return []
        search_results: Optional[list[CryptoBettingInstrument]] = []
        for market in matching_markets:
            market_name = f"{self.instrument.sport_name.lower()}.{market}"  # TODO: set market type on CryptoBettingInstrument to market_name without sport name eg. market_name.split(".")[1]
            instrument_filter = {
                "event_name": self.instrument.event_name,  # TODO: use event_id instead
                "market_name": market_name,
                "sport_name": self.instrument.sport_name
            }
            search_provider_result: Optional[
                list[CryptoBettingInstrument]] = self.instrument_provider.search_instruments(
                instrument_filter)
            if search_provider_result:
                search_results.extend(search_provider_result)
        # we need to filter matching instruments by the outcome
        # in the case it is the same market, we just need to check the outcome is opposite
        matching_instruments: Optional[list[CryptoBettingInstrument]] = []
        for instrument in search_results:
            if instrument == self.instrument:
                # self.instrument will be a match for itself and we don't want that
                continue
            # same market matcher
            if instrument.market_name == self.instrument.market_name:
                matching_instrument = self.match_same_market(instrument)
                if matching_instrument:
                    matching_instruments.append(matching_instrument)
            else:
                # cross market matcher
                matching_instrument = self.match_cross_market(instrument)
                if matching_instrument:
                    matching_instruments.append(matching_instrument)

        return matching_instruments

    def match_same_market(self, instrument: CryptoBettingInstrument) -> Optional[CryptoBettingInstrument]:
        matching_instruments: Optional[CryptoBettingInstrument] = None
        # handicap instruments
        # TODO: add a better check using the handicap type/ENUM on CryptoBettingInstrument.
        if 'handicap' in self.instrument.market_name:
            if instrument.outcome != self.instrument.outcome:  # i.e. opposite outcomes away != home
                # we need to extract the handicap value from params
                matching_instrument_handicap_value = instrument.params.split("=")[-1]
                instrument_handicap_value = self.instrument.params.split("=")[-1]
                # we need to exclude split-handicaps for now (essentially two bets between two outcomes)
                # i.e. handicap_value must be a multiple of 0.5
                if float(instrument_handicap_value) % 0.5 != 0:
                    return matching_instruments
                if matching_instrument_handicap_value == instrument_handicap_value:
                    # same market, opposite outcome, same handicap values => match
                    matching_instruments = instrument
                    self.log.debug(
                        f"Found matching Instrument: {instrument.id}  for {self.instrument.id}"
                    )
                    return matching_instruments
                else:
                    # same market, opposite outcome, but different handicap values => no match
                    self.log.debug(
                        f"No match. Instrument Handicap value: {instrument_handicap_value}  Matching Instrument Handicap value: {matching_instrument_handicap_value}"
                    )
                    return matching_instruments
            else:
                # same market, same outcome => no match
                return matching_instruments
        if 'both_teams_to_score' in self.instrument.market_name:
            if instrument.outcome != self.instrument.outcome:  # i.e. opposite outcomes yes != no
                # same market, opposite outcome => match
                matching_instruments = instrument
                self.log.debug(
                    f"Found matching Instrument: {instrument.id}  for {self.instrument.id}"
                )
                return matching_instruments
        # total_goals instruments
        if 'total_goals' in self.instrument.market_name:
            # TODO: exclude these total markets {exact_total_goals_period_first_half,....}
            if instrument.outcome != self.instrument.outcome:  # i.e. opposite outcomes over != under
                # same market, opposite outcome => match
                matching_instrument_total_value = instrument.params.split("=")[-1]
                instrument_total_value = self.instrument.params.split("=")[-1]
                if matching_instrument_total_value == instrument_total_value:
                    matching_instruments = instrument
                    self.log.debug(
                        f"Found matching Instrument: {instrument.id}  for {self.instrument.id}"
                    )
                    return matching_instruments
        # draw_no_bet instruments
        if 'draw_no_bet' in self.instrument.market_name:
            if instrument.outcome != self.instrument.outcome:  # i.e. opposite outcomes home != away
                # same market, opposite outcome => match
                matching_instruments = instrument
            self.log.debug(
                f"Found matching Instrument: {instrument.id}  for {self.instrument.id}"
            )
            return matching_instruments

        return matching_instruments

    def match_cross_market(self, instrument: CryptoBettingInstrument) -> Optional[
        CryptoBettingInstrument]:
        matching_instruments: Optional[CryptoBettingInstrument] = None
        # path for double_chance and match_odds
        if (('match_odds' in self.instrument.market_name and 'double_chance' in instrument.market_name) or
            ('match_odds' in instrument.market_name and 'double_chance' in self.instrument.market_name)):  # TODO: cater for all "match_odds type" markets eg. 1x2; 3-way; etc

            # determine the match_odds instrument
            match_odds_instrument = self.instrument if 'match_odds' in self.instrument.market_name else instrument
            # determine the double_chance instrument based on the result of the match_odds_instrument
            double_chance_instrument = self.instrument if 'double_chance' in self.instrument.market_name else instrument
            # check if the match_odds_instrument_outcome is not a possible outcome of the double_chance_instrument
            if match_odds_instrument.outcome not in double_chance_instrument.outcome.split('_'):
                matching_instruments = instrument
                self.log.debug(
                    f"Found matching Instrument: {instrument.id}  for {self.instrument.id}"
                )
                return matching_instruments
        # path for double chance and handicap
        if ('double_chance' in self.instrument.market_name and 'handicap' in instrument.market_name) or \
                ('handicap' in self.instrument.market_name and 'double_chance' in instrument.market_name):
            # determine the double_chance instrument
            double_chance_instrument = self.instrument if 'double_chance' in self.instrument.market_name else instrument
            # determine the handicap instrument
            handicap_instrument = self.instrument if 'handicap' in self.instrument.market_name else instrument
            # we need to extract the handicap value from params
            instrument_handicap_value = handicap_instrument.params.split("=")[-1]
            # we need to exclude split-handicaps for now (essentially two bets between two outcomes)
            # i.e. handicap_value must be a multiple of 0.5
            if float(instrument_handicap_value) % 0.5 != 0:
                return matching_instruments
            # check if the handicap_instrument_outcome is not a possible outcome of the double_chance_instrument
            if handicap_instrument.outcome not in double_chance_instrument.outcome.split('_'):
                matching_instruments = instrument
                self.log.debug(
                    f"Found matching Instrument: {instrument.id}  for {self.instrument.id}"
                )
                return matching_instruments
            # path for double chance and draw_no_bet
            if ('double_chance' in self.instrument.market_name and 'draw_no_bet' in instrument.market_name) or \
                        ('draw_no_bet' in self.instrument.market_name and 'double_chance' in instrument.market_name):
                    # determine the double_chance instrument
                    double_chance_instrument = self.instrument if 'double_chance' in self.instrument.market_name else instrument
                    # determine the draw_no_bet instrument
                    draw_no_bet_instrument = self.instrument if 'draw_no_bet' in self.instrument.market_name else instrument
                    # check if the draw_no_bet_instrument_outcome is not a possible outcome of the double_chance_instrument
                    if draw_no_bet_instrument.outcome not in double_chance_instrument.outcome.split('_'):
                        matching_instruments = instrument
                        self.log.debug(
                            f"Found matching Instrument: {instrument.id}  for {self.instrument.id}"
                        )
                    return matching_instruments
            # path for handicap and draw_no_bet
        return matching_instruments
