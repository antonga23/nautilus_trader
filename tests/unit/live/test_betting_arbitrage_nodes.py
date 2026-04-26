from decimal import Decimal

import pytest

from nautilus_trader.config import ImportableConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import build_trading_node_config
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import (
    BettingArbitrageNodeManifest,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import BettingVenueManifest


class TestBettingArbitrageNodeManifest:
    def test_rejects_blocked_sportsbook_venues(self):
        with pytest.raises(ValueError, match="not deployment-ready"):
            BettingVenueManifest(venue="10BET")

    def test_requires_enabled_venue(self):
        with pytest.raises(ValueError, match="At least one enabled venue"):
            BettingArbitrageNodeManifest(
                node_id="test-node",
                venues=[BettingVenueManifest(venue="SXBET", enabled=False)],
            )


class TestBettingArbitrageNodeBuilder:
    def test_validation_mode_forces_safe_strategy_and_no_exec_clients(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            allow_dummy_credentials=True,
            strategy=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                max_total_stake=Decimal(100),
                auto_execute=True,
            ),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                    execution_enabled=True,
                ),
            ],
        )

        config = build_trading_node_config(manifest)

        assert len(config.exec_clients) == 0
        assert len(config.strategies) == 1
        assert config.strategies[0].config["auto_execute"] is False
        assert config.strategies[0].config["enabled_venues"] == ["SXBET"]

    def test_sxbet_exec_client_uses_dummy_credentials(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-live",
            trader_id="BETARB-TEST-002",
            validation_mode=False,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                    execution_enabled=True,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        exec_client = config.exec_clients["SXBET_PRIMARY"]

        assert isinstance(exec_client, ImportableConfig)
        assert exec_client.config["api_key"] == "dummy-sxbet-api-key"
        assert exec_client.config["private_key"].startswith("0x")
        assert exec_client.config["wallet_address"].startswith("0x")

    def test_sxbet_data_client_receives_order_book_runtime_settings(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-runtime",
            trader_id="BETARB-TEST-002",
            timeout_connection=240.0,
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                    instrument_load_limit=50,
                    market_discovery_limit=500,
                    prefer_liquid_markets=True,
                    liquidity_probe_limit=250,
                    min_two_sided_markets=2,
                    auto_subscribe_quote_ticks=True,
                    quote_subscription_limit=40,
                    order_book_poll_interval_secs=5.0,
                    order_book_poll_summary_interval_secs=30.0,
                    order_book_concurrency=8,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        data_client = config.data_clients["SXBET_PRIMARY"]

        assert config.timeout_connection == 240.0
        assert data_client.config["instrument_provider"]["instrument_load_limit"] == 50
        assert data_client.config["instrument_provider"]["market_discovery_limit"] == 500
        assert data_client.config["instrument_provider"]["prefer_liquid_markets"] is True
        assert data_client.config["instrument_provider"]["liquidity_probe_limit"] == 250
        assert data_client.config["instrument_provider"]["min_two_sided_markets"] == 2
        assert data_client.config["instrument_provider"]["api_key_pool"] == ("dummy-sxbet-api-key",)
        assert data_client.config["auto_subscribe_quote_ticks"] is True
        assert data_client.config["quote_subscription_limit"] == 40
        assert data_client.config["order_book_poll_interval_secs"] == 5.0
        assert data_client.config["order_book_poll_summary_interval_secs"] == 30.0
        assert data_client.config["order_book_concurrency"] == 8
        assert data_client.config["api_key_pool"] == ("dummy-sxbet-api-key",)

    def test_polymarket_instrument_ids_flow_into_importable_config(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="polymarket-validation",
            trader_id="BETARB-TEST-003",
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(
                    venue="POLYMARKET",
                    client_key="POLYMARKET_PRIMARY",
                    load_all_instruments=False,
                    instrument_ids=frozenset(
                        {
                            "condition-token.POLYMARKET",
                        },
                    ),
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        data_client = config.data_clients["POLYMARKET_PRIMARY"]

        assert isinstance(data_client, ImportableConfig)
        assert data_client.config["instrument_provider"]["load_all"] is False
        assert data_client.config["instrument_provider"]["load_ids"] == [
            "condition-token.POLYMARKET",
        ]

    def test_mixed_supported_topology_updates_strategy_enabled_venues(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="mixed-validation",
            trader_id="BETARB-TEST-004",
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(venue="POLYMARKET", client_key="POLYMARKET_PRIMARY"),
                BettingVenueManifest(venue="SXBET", client_key="SXBET_PRIMARY"),
            ],
        )

        config = build_trading_node_config(manifest)

        assert config.strategies[0].config["enabled_venues"] == ["POLYMARKET", "SXBET"]
