import os
import time
from decimal import Decimal
from itertools import count
from typing import List, Any, Optional
from nautilus_trader.common.factories import OrderFactory
import pandas as pd
import random
from unittest.mock import AsyncMock, patch
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.engine import BacktestEngineConfig
from nautilus_trader.backtest.engine import ExecEngineConfig
from nautilus_trader.backtest.engine import RiskEngineConfig
from nautilus_trader.backtest.modules import FXRolloverInterestConfig
from nautilus_trader.backtest.modules import FXRolloverInterestModule
from nautilus_trader.model.currency import Currency
import os
import json

import pathlib

from nautilus_trader.adapters.cloudbet.client.util import generate_64bit_uuid
from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.config import LoggingConfig
from nautilus_trader.examples.algorithms.twap import TWAPExecAlgorithm
from nautilus_trader.examples.strategies.betting_market_maker import BettingMarketMaker
from nautilus_trader.examples.strategies.ema_cross import EMACross
from nautilus_trader.examples.strategies.ema_cross import EMACrossConfig
from nautilus_trader.examples.strategies.ema_cross_stop_entry import EMACrossStopEntry
from nautilus_trader.examples.strategies.ema_cross_stop_entry import EMACrossStopEntryConfig
from nautilus_trader.examples.strategies.ema_cross_trailing_stop import EMACrossTrailingStop
from nautilus_trader.examples.strategies.ema_cross_trailing_stop import EMACrossTrailingStopConfig
from nautilus_trader.examples.strategies.ema_cross_twap import EMACrossTWAP
from nautilus_trader.examples.strategies.ema_cross_twap import EMACrossTWAPConfig
from nautilus_trader.examples.strategies.market_maker import MarketMaker
from nautilus_trader.examples.strategies.orderbook_imbalance import OrderBookImbalance
from nautilus_trader.examples.strategies.orderbook_imbalance import OrderBookImbalanceConfig
from nautilus_trader.model.currencies import AUD
from nautilus_trader.model.currencies import BTC
from nautilus_trader.model.currencies import GBP
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import Venue, InstrumentId
from nautilus_trader.model.instruments.betting import BettingInstrument
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.persistence.wranglers import QuoteTickDataWrangler
from nautilus_trader.persistence.wranglers import TradeTickDataWrangler

from nautilus_trader.model.data.book import BookOrder
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument
from nautilus_trader.model.orderbook.book import OrderBook
from nautilus_trader.test_kit.mocks.data import data_catalog_setup
from nautilus_trader.test_kit.providers import TestDataProvider
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from tests import TEST_DATA_DIR
from tests.integration_tests.adapters.betfair.test_kit import BetfairDataProvider

from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest
import pytz

from nautilus_trader.backtest.data_client import BacktestMarketDataClient
from nautilus_trader.backtest.exchange import SimulatedExchange
from nautilus_trader.backtest.execution_client import BacktestExecClient
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.common.clock import TestClock
from nautilus_trader.common.enums import ComponentState
from nautilus_trader.common.enums import LogLevel
from nautilus_trader.common.logging import Logger
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.indicators.average.ema import ExponentialMovingAverage
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.enums import TriggerType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.model.orders import OrderList
from nautilus_trader.msgbus.bus import MessageBus
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.risk.engine import RiskEngine
from nautilus_trader.test_kit.mocks.strategies import KaboomStrategy
from nautilus_trader.test_kit.mocks.strategies import MockStrategy
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from nautilus_trader.test_kit.stubs.data import UNIX_EPOCH
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs
from nautilus_trader.trading.strategy import Strategy

CURRENT_PATH = os.path.dirname(os.path.abspath(__file__))
TEST_DATA_PATH = os.path.join(CURRENT_PATH, "resources/StrategyCryptoBettingInstruments.json")
TEST_MARKETS_PATH = os.path.join(CURRENT_PATH, "resources/cb_markets.json")


class TestBettingMarketMaking:
    def setup(self):
        # Fixture Setup
        self.clock = TestClock()
        self.logger = Logger(
            clock=self.clock,
            level_stdout=LogLevel.DEBUG,
            bypass=True,
        )

        self.trader_id = TestIdStubs.trader_id()

        self.msgbus = MessageBus(
            trader_id=self.trader_id,
            clock=self.clock,
            logger=self.logger,
        )

        self.cache = TestComponentStubs.cache()

        self.portfolio = Portfolio(
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )

        self.data_engine = DataEngine(
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )

        self.exec_engine = ExecutionEngine(
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )

        self.risk_engine = RiskEngine(
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )

        self.instruments: List[CryptoBettingInstrument] = TestInstrumentProvider.crypto_betting_instruments(count=500,
                                                                                                            sports='Soccer')
        self.exchange = SimulatedExchange(
            venue=Venue("CLOUDBET"),
            oms_type=OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(1_000_000, USD)],
            default_leverage=Decimal(1),
            leverages={},
            msgbus=self.msgbus,
            cache=self.cache,
            instruments=self.instruments,
            modules=[],
            fill_model=FillModel(),
            clock=self.clock,
            logger=self.logger,
            latency_model=LatencyModel(0),
        )

        self.data_client = BacktestMarketDataClient(
            client_id=ClientId("SIM"),
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )

        self.exec_client = BacktestExecClient(
            exchange=self.exchange,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )

        # Wire up components
        self.exchange.register_client(self.exec_client)
        self.data_engine.register_client(self.data_client)
        self.exec_engine.register_client(self.exec_client)
        self.exchange.reset()

        for instrument in self.instruments:
            # Add instruments
            self.data_engine.process(instrument)
            self.data_engine.process(instrument)
            self.data_engine.process(instrument)
            self.cache.add_instrument(instrument)
            self.cache.add_instrument(instrument)
            self.cache.add_instrument(instrument)

            # Prepare market
            self.exchange.process_quote_tick(
                TestDataStubs.quote_tick(
                    instrument=instrument,
                    bid=90.001,
                    ask=90.002,
                ),
            )

        self.data_engine.start()
        self.exec_engine.start()

    def test_initialization(self, instrument):
        # Arrange
        strategy = BettingMarketMaker(instrument_id=instrument, max_size=Decimal(1), trigger_min_size=Decimal(1),
                                      trigger_min_profit=float(1), config=StrategyConfig(order_id_tag="001"))
        strategy.register(
            trader_id=self.trader_id,
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )

        # Act, Assert
        assert strategy.state == ComponentState.READY

    @patch.object(BettingMarketMaker, 'selection_matcher')
    # NB: Greyhound and Ice-Hockey are not supported; test will fail when passed an Instrument of that sport
    # TODO: fix "instrument" fixture
    def test_on_start(self, patch_selection_matcher, instrument):
        # Arrange
        # add instrument to cache in case it has not been added
        self.cache.add_instrument(instrument)
        matched_instruments = random.sample(self.instruments, 100)
        patch_selection_matcher.return_value = matched_instruments
        strategy: BettingMarketMaker = BettingMarketMaker(instrument_id=instrument.id, max_size=Decimal(1),
                                                          trigger_min_size=Decimal(1),
                                                          trigger_min_profit=float(1),
                                                          config=StrategyConfig(order_id_tag="001"))
        strategy.register(
            trader_id=self.trader_id,
            portfolio=self.portfolio,
            msgbus=self.msgbus,
            cache=self.cache,
            clock=self.clock,
            logger=self.logger,
        )
        # Act
        strategy._start()
        # Assert
        # check book for instrument has been created
        assert strategy._book.instrument_id == instrument.id
        # check book order has been added to instruments orderbook
        assert strategy._book.best_ask_price() == instrument.min_price
        # sort and filter matched_instruments by price, return first instrument
        matched_instruments = filter(lambda x: x.max_quantity is not None, matched_instruments)
        matched_instruments = list(matched_instruments)
        matched_instruments.sort(key=lambda x: x.min_price, reverse=True)
        assert strategy._book.best_bid_price() == matched_instruments[0].min_price
        for instrument in matched_instruments:
            if instrument.max_quantity is None:
                continue
            assert instrument.id in strategy._instrument_to_book

    # @pytest.mark.parametrize("param_market_filter", [
    #     ("match_odds"),
    #     # ("total_goals"),
    #     # ("asian_handicap")
    #     # ("asian_handicap_period_first_half")
    # ]) # TODO: add a param_sport_filter instead of filtering self.instruments in setUp()
    # # @pytest.mark.skipif(reason=" selection_matcher() WIP ")
    def test_strategy_selection_matcher(self, instrument_provider):
        """
        Test the strategy selection matcher for intra market matches

        Args:
            instrument_provider (InstrumentProvider): The instrument provider.

        Returns:
            None
        """
        # Arrange
        markets_list: List[str] = ["both_teams_to_score", "asian_handicap", "asian_handicap_period_first_half",
                                   "total_goals", "draw_no_bet", "asian_handicap_period_second_half",
                                   "asian_handicap_period_extratime", "match_odds", 'double_chance']
        try:
            with open(TEST_DATA_PATH) as json_file:
                instrument_data = json.load(json_file)
        except FileNotFoundError:
            raise Exception("Test data not found")
        instruments = [CryptoBettingInstrument.from_dict(instrument) for instrument in instrument_data if
                       instrument['sport_name'] not in ['Greyhounds', 'Ice Hockey']]
        instruments = instruments[:1000]  # do the first 1000 instruments
        instrument_provider.add_bulk(instruments)
        matched_count = 0
        final_json = []
        for instrument in instruments:
            if instrument.market_name.split(".")[-1] not in markets_list:
                continue
            strategy = BettingMarketMaker(instrument_id=instrument.id, instrument=instrument, max_size=Decimal(100),
                                          trigger_min_size=Decimal(1), trigger_min_profit=float(1),
                                          config=StrategyConfig(oms_type='Hedging'),
                                          instrument_provider=instrument_provider)
            strategy.register(
                trader_id=self.trader_id,
                portfolio=self.portfolio,
                msgbus=self.msgbus,
                cache=self.cache,
                clock=self.clock,
                logger=self.logger,
            )
            # Act
            matching_instruments = strategy.selection_matcher()

            json_matching_instruments: dict[str, Optional[List[CryptoBettingInstrument]]] = {}
            if len(matching_instruments) > 0:
                matched_count += 1
                try:
                    if instrument.market_name.split(".")[-1] in markets_list:
                        # In general, these instruments come in pairs
                        assert len(
                            matching_instruments) >= 1, f"Expected at least 1 matching instrument for market {instrument.market_name} Instrument ID: " + instrument.id.value + f" got {len(matching_instruments)}"
                        print(f"Success! InstrumentID:{instrument.id} ")

                        instrument_dict = CryptoBettingInstrument.to_dict(instrument)
                        instrument_dict.update({"side": instrument.side.value})
                        instrument_dict.update({"currency": instrument.currency.code})
                        instrument_dict_list = []
                        for ins in matching_instruments:
                            matching_instrument_dict = CryptoBettingInstrument.to_dict(ins)
                            matching_instrument_dict.update({"side": ins.side.value})
                            matching_instrument_dict.update({"currency": ins.currency.code})
                            instrument_dict_list.append(matching_instrument_dict)
                        json_matching_instruments[instrument.id.value] = [instrument_dict]
                        # add_instruments = [ins.to_dict(ins) for ins in matching_instruments]
                        # Extend the list at the existing key with the new instruments
                        json_matching_instruments[instrument.id.value].extend(instrument_dict_list)
                        final_json.append(json_matching_instruments)

                except AssertionError:
                    instrument_filter = {
                        "event_name": instrument.event_name,  # TODO: use event_id instead
                        "market_name": instrument.market_name,
                        "sport_name": instrument.sport_name
                    }
                    search_provider_results = instrument_provider.search_instruments(instrument_filter=instrument_filter)

                    for search_result in search_provider_results:
                        if search_result == instrument:
                            search_provider_results.remove(instrument)
                            continue
                    if len(search_provider_results) > 0:
                        assert False, f"Expected {len(search_provider_results)} matching instrument for asian_handicap: Instrument ID: " + instrument.id.value + f" got {len(matching_instruments)}"
                    else:
                        assert True, f"Found 0 instruments in provider that match search criteria.  Instrument ID: " + instrument.id.value
            else:
                print(f"no matching instruments found")
        print(f"Total matches: {matched_count}")
        # # save the json_matching_instruments to json
        # with open('matching_instruments.json', 'a') as outfile:
        #     json.dump(final_json, outfile)

    # @pytest.mark.parametrize("param_market_filter", [
    #     ("match_odds"),
    #     # ("total_goals"),
    #     # ("asian_handicap")
    #     # ("asian_handicap_period_first_half")
    # ])
    @patch.object(BettingMarketMaker, 'on_start')
    def test_check_trigger(self, patch_on_start, instrument_provider):
        def on_start_side_effect(strategy: BettingMarketMaker, instrument: CryptoBettingInstrument, matching_instruments: Optional[List[CryptoBettingInstrument]] = None):
            def add_book_order(strategy, instrument: CryptoBettingInstrument, side: OrderSide = OrderSide.SELL):
                if instrument.max_quantity is not None and instrument.min_price is not None:
                    book_order = BookOrder(
                        side=side,  # By definition, this is a sell
                        price=instrument.min_price,
                        size=instrument.max_quantity,
                        order_id=generate_64bit_uuid()
                    )
                    book_event_time = instrument.ts_event or self.clock.timestamp_ns()
                    strategy._book.add(book_order, book_event_time)
                    strategy._instrument_to_book[instrument.id] = book_order
            book = OrderBook(
                instrument_id=instrument.id,
                book_type=BookType.L2_MBP,
            )
            strategy._book = book

            # Add book order for the primary instrument
            add_book_order(strategy, instrument)

            # Add book orders for each matching instrument
            if matching_instruments:
                for ins in matching_instruments:
                    add_book_order(strategy, ins, OrderSide.BUY)
        matched_crypto_betting_instruments: List[List[CryptoBettingInstrument]] = TestInstrumentProvider.matched_crypto_betting_instruments()
        for pair in matched_crypto_betting_instruments:
            for ins in pair:
                self.cache.add_instrument(ins)
        for instrument_pair in matched_crypto_betting_instruments:
            instrument = instrument_pair.pop(0)
            matched_instrument_list = instrument_pair
            strategy = BettingMarketMaker(instrument_id=instrument.id, instrument=instrument, max_size=Decimal(1000),
                                      trigger_min_size=Quantity(10, 2), trigger_min_profit=Decimal(0.30),
                                      config=StrategyConfig(oms_type='Hedging'),
                                      instrument_provider=instrument_provider)
            strategy.register(
                trader_id=self.trader_id,
                portfolio=self.portfolio,
                msgbus=self.msgbus,
                cache=self.cache,
                clock=self.clock,
                logger=self.logger,
            )
            patch_on_start.side_effect = on_start_side_effect(strategy, instrument, matched_instrument_list)
            # Act
            strategy.check_trigger()
