#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Example: Running the betting arbitrage strategy with multi-venue setup.
# -------------------------------------------------------------------------------------------------
"""
Example script demonstrating how to set up and run the arbitrage strategy.

NOTE: This is an example for understanding the architecture. Actual execution
requires:
1. NautilusTrader built (Cython extensions)
2. API keys configured
3. Playwright installed for web scraping
4. Network access to all venues

For testing without real execution, use paper trading mode.

"""

import asyncio
import importlib
import os
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.sxbet.config import SXBetDataClientConfig
from nautilus_trader.adapters.sxbet.factories import SXBetLiveDataClientFactory
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import MessageBus
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio


def _load_cloudbet_support() -> tuple[Any, Any] | None:
    try:
        cloudbet_config = importlib.import_module("nautilus_trader.adapters.cloudbet.config")
        cloudbet_factories = importlib.import_module("nautilus_trader.adapters.cloudbet.factories")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("nautilus_trader.adapters.cloudbet"):
            return None
        raise

    return (
        cloudbet_config.CloudbetDataClientConfig,
        cloudbet_factories.CloudbetLiveDataClientFactory,
    )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value

    raise RuntimeError(
        f"Set the {name} environment variable before running this example.",
    )


async def main():  # pylint: disable=too-many-statements
    """
    Run arbitrage strategy with Cloudbet and SX.bet.

    This example demonstrates:
    1. Setting up data clients for multiple venues
    2. Loading instruments from all venues
    3. Creating and configuring the arbitrage strategy
    4. Subscribing to quote ticks
    5. Running the strategy

    """
    print("=" * 60)
    print("Multi-Venue Betting Arbitrage Strategy - Example")
    print("=" * 60)

    # ============ Configuration ============

    cloudbet_support = _load_cloudbet_support()
    SXBET_API_KEY = _require_env("SXBET_API_KEY")
    enabled_venues = ["SXBET"]
    if cloudbet_support is not None:
        enabled_venues.insert(0, "CLOUDBET")

    # Strategy config
    strategy_config = BettingArbitrageConfig(
        min_profit_margin=Decimal("0.015"),  # 1.5% minimum profit
        max_total_stake=Decimal(1000),  # Max $1000 across both sides
        enabled_venues=frozenset(enabled_venues),
        rollover_aware=True,
        auto_execute=False,  # Manual approval for safety
    )

    # ============ Initialize Components ============

    # Event loop
    loop = asyncio.get_event_loop()

    # Core components
    clock = LiveClock()
    logger = Logger(name="ArbitrageExample")
    trader_id = TraderId("ARBITRAGE-001")

    # Message bus and cache
    msgbus = MessageBus(trader_id=trader_id, clock=clock, logger=logger)
    cache = Cache(logger=logger)

    # Portfolio
    Portfolio(
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        logger=logger,
    )

    print("\n[1] Initialized core components")

    # ============ Setup Cloudbet Client ============
    cloudbet_client = None
    cloudbet_instruments = []
    if cloudbet_support is None:
        print("[2] Cloudbet adapter unavailable in this checkout, continuing with SX.bet only")
    else:
        CloudbetDataClientConfig, CloudbetLiveDataClientFactory = cloudbet_support

        # API keys must come from the environment or a secure local config source.
        CLOUDBET_API_KEY = _require_env("CLOUDBET_API_KEY")
        CLOUDBET_API_SECRET = _require_env("CLOUDBET_API_SECRET")

        cloudbet_config = CloudbetDataClientConfig(
            api_key=CLOUDBET_API_KEY,
            api_secret=CLOUDBET_API_SECRET,
            base_url="https://www.cloudbet.com/api",
        )

        cloudbet_client = CloudbetLiveDataClientFactory.create(
            loop=loop,
            client_id="CLOUDBET",
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=cloudbet_config,
        )

        print("[2] Created Cloudbet data client")

    # ============ Setup SX.bet Client ============

    sxbet_config = SXBetDataClientConfig(
        api_key=SXBET_API_KEY,
        api_url="https://api.sx.bet",
    )

    sxbet_client = SXBetLiveDataClientFactory.create(
        loop=loop,
        client_id="SXBET",
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        logger=logger,
        config=sxbet_config,
    )

    print("[3] Created SX.bet data client")

    # ============ Connect and Load Instruments ============

    print("\n[4] Connecting to venues...")

    if cloudbet_client is not None:
        await cloudbet_client.connect()
    await sxbet_client.connect()

    print("[5] Loading instruments...")

    # Load instruments from both venues
    if cloudbet_client is not None:
        await cloudbet_client._instrument_provider.load_all_async()
    await sxbet_client._instrument_provider.load_all_async()

    if cloudbet_client is not None:
        cloudbet_instruments = list(cloudbet_client._instrument_provider.list_all())
    sxbet_instruments = list(sxbet_client._instrument_provider.list_all())

    if cloudbet_client is not None:
        print(f"    - Cloudbet: {len(cloudbet_instruments)} instruments")
    print(f"    - SX.bet: {len(sxbet_instruments)} instruments")

    # ============ Create and Configure Strategy ============

    print("\n[6] Creating arbitrage strategy...")

    strategy = BettingArbitrageStrategy(config=strategy_config)

    # Register with message bus (simplified)
    # In real setup, this would be handled by TradingNode

    # Subscribe to all instruments
    all_instruments = cloudbet_instruments + sxbet_instruments
    strategy.subscribe_instruments(all_instruments)

    print(f"[7] Subscribed to {len(all_instruments)} total instruments")

    # ============ Run Strategy ============

    print("\n[8] Starting strategy...")
    print("    - Monitoring quotes from all venues")
    print("    - Finding arbitrage opportunities")
    print(f"    - Min profit margin: {strategy_config.min_profit_margin:.1%}")
    print(f"    - Auto-execute: {strategy_config.auto_execute}")

    strategy.on_start()

    # In a real scenario, the strategy would run continuously,
    # receiving quote ticks from the message bus and looking for arbitrage

    print("\n[9] Strategy running...")
    print("    (In production, this would run indefinitely)")
    print("    (Quote ticks → MarketMatcher → Arbitrage detection → Order submission)")

    # Simulate running for a period
    await asyncio.sleep(5)

    # ============ Show Statistics ============

    stats = strategy.get_stats()
    print(f"\n{'=' * 60}")
    print("Strategy Statistics:")
    print(f"    - Subscribed instruments: {stats['subscribed_instruments']}")
    print(f"    - Opportunities found: {stats['opportunities_found']}")
    print(f"    - Opportunities executed: {stats['opportunities_executed']}")
    print(f"    - Success rate: {stats['success_rate']:.1%}")

    # ============ Cleanup ============

    strategy.on_stop()
    if cloudbet_client is not None:
        await cloudbet_client.disconnect()
    await sxbet_client.disconnect()

    print(f"\n{'=' * 60}")
    print("Example completed successfully!")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    """
    To run this example:

    1. Install dependencies:
       pip install nautilus_trader playwright
       playwright install chromium

    2. Build NautilusTrader (for Cython extensions):
       cd nautilus_trader
       poetry install
       poetry run build

    3. Export credentials in the environment:
       export CLOUDBET_API_KEY=...
       export CLOUDBET_API_SECRET=...
       export SXBET_API_KEY=...
       export ETHEREUM_PRIVATE_KEY=...

    4. Run:
       python examples/strategies/run_arbitrage_example.py

    NOTE: This example requires live API access and will not run
    without valid credentials.
    """
    asyncio.run(main())
