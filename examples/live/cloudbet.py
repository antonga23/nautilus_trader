#!/usr/bin/env python3
import asyncio
import time
import traceback
import uuid
from decimal import Decimal
from typing import List, Optional

from frozendict import frozendict
from nautilus_trader.core.rust.common import LogLevel
from nautilus_trader.model.currency import Currency

from nautilus_trader.adapters.betfair.config import BetfairDataClientConfig
from nautilus_trader.adapters.betfair.factories import BetfairLiveDataClientFactory
from nautilus_trader.adapters.betfair.factories import BetfairLiveExecClientFactory
from nautilus_trader.adapters.betfair.factories import get_cached_betfair_client
from nautilus_trader.adapters.betfair.factories import get_cached_betfair_instrument_provider
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.config import CloudbetDataClientConfig, CloudbetExecClientConfig
from nautilus_trader.adapters.cloudbet.factories import get_cached_cloudbet_client, \
    get_cached_cloudbet_instrument_provider, CloudbetLiveDataClientFactory, CloudbetLiveExecClientFactory
from nautilus_trader.config import CacheDatabaseConfig, CacheConfig, LiveDataEngineConfig, LiveExecEngineConfig, \
    LiveRiskEngineConfig, LiveExecClientConfig, StrategyConfig, InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.config.validation import NonNegativeInt
from nautilus_trader.examples.strategies.betting_market_maker import BettingMarketMaker
from nautilus_trader.examples.strategies.orderbook_imbalance import OrderBookImbalance
from nautilus_trader.examples.strategies.orderbook_imbalance import OrderBookImbalanceConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import PLAY_EUR
from nautilus_trader.model.enums import OmsType


async def main():
    """
    A trading node for Cloudbet.
    """
    # Connect to Cloudbet client early to pre-load instruments
    loop = asyncio.get_event_loop()
    logger = Logger(clock=LiveClock(), level_stdout=LogLevel.DEBUG)
    client: CloudbetClient = get_cached_cloudbet_client(
        api_key='eyJhbGciOiJSUzI1NiIsImtpZCI6IkhKcDkyNnF3ZXBjNnF3LU9rMk4zV05pXzBrRFd6cEdwTzAxNlRJUjdRWDAiLCJ0eXAiOiJKV1QifQ.eyJhY2Nlc3NfdGllciI6InRyYWRpbmciLCJleHAiOjIwMDI2MjM5MDgsImlhdCI6MTY4NzI2MzkwOCwianRpIjoiMzlkMTgwODYtNWYxNy00Y2QxLTg5NDEtODU1YzQ4ODAyNWYyIiwic3ViIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3IiwidGVuYW50IjoiY2xvdWRiZXQiLCJ1dWlkIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3In0.Sn7cONVxnz3hmbiWYh8TB0jK_yx86rZ6S-Pd2bw1b0WTA5MK88nHbYmGtHC8Wu8tDegvE5dK_bo-Ra0pcB50Hg-oa_1IkLTh3XwG7aT6tfzg61Qj0_vfkPhw2UPjVrSGw3w8bRxFNXldB3ls1xk2C-5M-f-PA7aPSoG5ebXOGsjmno-rV7HQJ_48xjF8QgLEtt9daxHQAmQ8DNzoAwKJ2ILZHg09GAL2Lfi5m48NMYAUYgInn20QIJVlcDqljltPUG5JQPtWGlVsyMIDz1QwobpcxjdE3zbhHnES64kD3eqjuKX52vMgmeDLgJvth5LbzTgxgHhZl2t9lyr_-x7lig',
        # Pass here or will source from the `CLOUDBET_API_KEY` env var
        api_url='https://sports-api.cloudbet.com/pub',
        # Pass here or will source from the `CLOUDBET_API_SECRET` env var
        logger=logger,
        loop=loop,
    )
    # need to initialize a session to make network requests
    await client.connect()

    unix_epoch_now = int(time.time())
    unix_epoch_6h = unix_epoch_now + (6 * 60 * 60)
    limit = 100
    filters = {
        'sport_key': 'soccer',
        'from_timestamp': unix_epoch_now,
        'to_timestamp': unix_epoch_6h,
        'live': 'false',
        'limit': limit,
        # 'market_names': ['1x2', 'handicap']
    }
    filters = frozendict(filters)
    provider_config = InstrumentProviderConfig(
        filters=filters
    )

    # Find instruments for a particular sport, event and market
    provider = get_cached_cloudbet_instrument_provider(
        client=client,
        logger=logger,
        config=provider_config,
    )
    await provider.load_all_async()
    instruments = provider.list_all()
    # await client.disconnect()

    trading_config = TradingNodeConfig(
        timeout_connection=180000.0,
        timeout_reconciliation=5.0,
        timeout_portfolio=5.0,
        timeout_disconnection=5.0,
        timeout_post_stop=2.0,
        trader_id="CLOUDBET-023",
        cache_database=CacheDatabaseConfig(type="redis", flush=True),
        data_engine=LiveDataEngineConfig(
            debug=True,
            qsize=10_0000,  # TODO: set qsize based on expected number of Instruments
        ),
        exec_engine=LiveExecEngineConfig(
            reconciliation=False,
            reconciliation_lookback_mins=None,
            inflight_check_interval_ms=2_000,
            inflight_check_threshold_ms=5_000,  # check Cloudbet API every 5 seconds for inflight orders/placement
            qsize=10_000,
        ),
        risk_engine=LiveRiskEngineConfig(
            qsize=10_000,
            bypass=False,
            max_order_submit_rate="1/00:00:01",
            max_order_modify_rate="1/00:00:01",
            max_notional_per_order={},  # this is set at the Strategy level
            debug=True
        ),
        data_clients={
            "CLOUDBET": CloudbetDataClientConfig(
                api_key='eyJhbGciOiJSUzI1NiIsImtpZCI6IkhKcDkyNnF3ZXBjNnF3LU9rMk4zV05pXzBrRFd6cEdwTzAxNlRJUjdRWDAiLCJ0eXAiOiJKV1QifQ.eyJhY2Nlc3NfdGllciI6InRyYWRpbmciLCJleHAiOjIwMDI2MjM5MDgsImlhdCI6MTY4NzI2MzkwOCwianRpIjoiMzlkMTgwODYtNWYxNy00Y2QxLTg5NDEtODU1YzQ4ODAyNWYyIiwic3ViIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3IiwidGVuYW50IjoiY2xvdWRiZXQiLCJ1dWlkIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3In0.Sn7cONVxnz3hmbiWYh8TB0jK_yx86rZ6S-Pd2bw1b0WTA5MK88nHbYmGtHC8Wu8tDegvE5dK_bo-Ra0pcB50Hg-oa_1IkLTh3XwG7aT6tfzg61Qj0_vfkPhw2UPjVrSGw3w8bRxFNXldB3ls1xk2C-5M-f-PA7aPSoG5ebXOGsjmno-rV7HQJ_48xjF8QgLEtt9daxHQAmQ8DNzoAwKJ2ILZHg09GAL2Lfi5m48NMYAUYgInn20QIJVlcDqljltPUG5JQPtWGlVsyMIDz1QwobpcxjdE3zbhHnES64kD3eqjuKX52vMgmeDLgJvth5LbzTgxgHhZl2t9lyr_-x7lig',
                # Pass here or will source from the `CLOUDBET_API_KEY` env var,
                api_url='https://sports-api.cloudbet.com/pub',
                market_filter=None
            ),
            # "CLOUDBET_SECONDARY": CloudbetDataClientConfig(
            #     api_key=None,
            #     api_url=None,
            #     market_filter=None
            # )
        },
        exec_clients={
            "CLOUDBET": CloudbetExecClientConfig(
                base_currency=PLAY_EUR,
                api_key='eyJhbGciOiJSUzI1NiIsImtpZCI6IkhKcDkyNnF3ZXBjNnF3LU9rMk4zV05pXzBrRFd6cEdwTzAxNlRJUjdRWDAiLCJ0eXAiOiJKV1QifQ.eyJhY2Nlc3NfdGllciI6InRyYWRpbmciLCJleHAiOjIwMDI2MjM5MDgsImlhdCI6MTY4NzI2MzkwOCwianRpIjoiMzlkMTgwODYtNWYxNy00Y2QxLTg5NDEtODU1YzQ4ODAyNWYyIiwic3ViIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3IiwidGVuYW50IjoiY2xvdWRiZXQiLCJ1dWlkIjoiOGY1OGFiNTAtOGRlMi00N2EwLTkxZjYtMDQzMzg1YWMxOTE3In0.Sn7cONVxnz3hmbiWYh8TB0jK_yx86rZ6S-Pd2bw1b0WTA5MK88nHbYmGtHC8Wu8tDegvE5dK_bo-Ra0pcB50Hg-oa_1IkLTh3XwG7aT6tfzg61Qj0_vfkPhw2UPjVrSGw3w8bRxFNXldB3ls1xk2C-5M-f-PA7aPSoG5ebXOGsjmno-rV7HQJ_48xjF8QgLEtt9daxHQAmQ8DNzoAwKJ2ILZHg09GAL2Lfi5m48NMYAUYgInn20QIJVlcDqljltPUG5JQPtWGlVsyMIDz1QwobpcxjdE3zbhHnES64kD3eqjuKX52vMgmeDLgJvth5LbzTgxgHhZl2t9lyr_-x7lig',
                # Pass here or will source from the `CLOUDBET_API_KEY` env var,
                api_url='https://sports-api.cloudbet.com/pub',
                # market_filter=None,
                # api_key=None,  # TODO: pass different set of keys for execution
                # api_url=None,
            )
        }
    )

    strategies = [
        BettingMarketMaker(
            instrument_id=instrument.id,
            instrument=instrument,
            max_size=Decimal(100),  # TODO: pass from config or RISK ENGINE  but sensible defaults for now
            trigger_min_size=Decimal(10),  # pass from config or RISK ENGINE
            trigger_min_profit=float(0.5),  # pass from config or RISK ENGINE
            instrument_provider=provider,
            config=StrategyConfig(
                oms_type='Hedging' # allow multiple positions per instrument. see nautilus_core/model/src/enums.rs for all Enums
            )
        )
        for instrument in instruments
    ]

    # Setup TradingNode
    node = TradingNode(config=trading_config)
    node.trader.add_strategies(strategies)

    # # Register your client factories with the node (can take user defined factories)
    node.add_data_client_factory("CLOUDBET", CloudbetLiveDataClientFactory)
    node.add_exec_client_factory("CLOUDBET", CloudbetLiveExecClientFactory)
    node.build()

    try:
        node.run()
        await asyncio.gather(*asyncio.all_tasks())
    except Exception as e:
        print(e)
        print(traceback.format_exc())
    finally:
        node.dispose()


if __name__ == "__main__":
    asyncio.run(
        main()
    )
