# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212, PYL-W0613
# skipcq: PYL-W0108, PYL-W1514, PYL-R0903, PYL-R0913, PYL-C0301
# skipcq: PYL-C0302, PYL-E0401, PYL-C0411
# pylint: disable=missing-module-docstring,missing-class-docstring
# pylint: disable=missing-function-docstring,protected-access,unused-argument
# pylint: disable=unnecessary-lambda,unspecified-encoding,too-few-public-methods
# pylint: disable=too-many-arguments,line-too-long,too-many-lines
# pylint: disable=import-error,wrong-import-order

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import hashlib
import json
import logging
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import msgspec

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.fees import fee_adjusted_coverage_basket
from nautilus_trader.adapters.betting.common.odds import devig_probabilities
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import CorpusSnapshot
from nautilus_trader.adapters.betting.semantics import MarketNormalizer
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.adapters.polymarket import providers as polymarket_providers
from nautilus_trader.config import ImportableConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageStrategy
from nautilus_trader.live.strategy_nodes.betting_arbitrage import builder as node_builder
from nautilus_trader.live.strategy_nodes.betting_arbitrage import runner as node_runner
from nautilus_trader.live.strategy_nodes.betting_arbitrage import semantic_cache as node_cache
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import build_trading_node_config
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import (
    manifest_execution_readiness,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import (
    BettingArbitrageNodeManifest,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import BettingVenueManifest
from nautilus_trader.live.strategy_nodes.betting_arbitrage.runner import main as runner_main
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    SemanticCacheStatus,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache import (
    ensure_semantic_cache_ready,
)
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency


def _instrument(
    *,
    venue: str,
    market_type: str,
    outcome: str,
    market_name: str | None = None,
    params: str = "",
    handicap: float | None = None,
    event_id: str = "event-1",
    event_name: str = "Team A vs Team B",
    home_name: str = "Team A",
    away_name: str = "Team B",
    sport_name: str = "soccer",
    start_time: str = "2026-03-13T18:00:00Z",
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id=event_id,
        event_name=event_name,
        home_name=home_name,
        away_name=away_name,
        sport_name=sport_name,
        competition_name="Test League",
        market_name=market_name or market_type,
        market_type=market_type,
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.1,
        currency=Currency.from_str("USDC"),
        params=params,
        handicap=handicap,
        start_time=start_time,
    )


def _polymarket_winner_instrument(*, outcome: str, resolution_policy: str = "lose"):
    return CryptoBettingInstrument(
        venue=Venue("POLYMARKET"),
        event_id="pm-event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="basketball",
        competition_name="NBA",
        market_name="basketball.winner",
        market_type="basketball.winner",
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.1,
        currency=Currency.from_str("USDC"),
        start_time="2026-03-13T18:00:00Z",
        info={"resolution_policy": {"tie_or_unknown": resolution_policy}},
    )


def _polymarket_spread_instrument(*, outcome: str, line: str):
    return CryptoBettingInstrument(
        venue=Venue("POLYMARKET"),
        event_id="pm-event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="basketball",
        competition_name="NBA",
        market_name="basketball.spread",
        market_type="basketball.spread",
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.1,
        currency=Currency.from_str("USDC"),
        params=f"line={line}",
        handicap=float(line),
        start_time="2026-03-13T18:00:00Z",
        info={"resolution_policy": {"tie_or_unknown": "lose"}},
    )


def _seed_promoted_template(
    cache_dir: Path,
    *,
    same_venue_only: bool = False,
    manifest: BettingArbitrageNodeManifest | None = None,
) -> None:
    instrument_a = (
        _instrument(venue="SXBET", market_type="draw_no_bet", outcome="home")
        if same_venue_only
        else _instrument(venue="SXBET", market_type="match_odds", outcome="home")
    )
    instrument_b = (
        _instrument(
            venue="SXBET",
            market_type="asian_handicap",
            market_name="asian_handicap",
            outcome="home",
            params="line=0",
            handicap=0.0,
        )
        if same_venue_only
        else _instrument(venue="SXBET", market_type="double_chance", outcome="away_draw")
    )
    support = TemplateSupportStats(
        template_id="template-support",
        observed_count=3 if same_venue_only else 10,
        event_count=3 if same_venue_only else 10,
        provider_count=1,
        providers=("SXBET",),
        sports=("soccer",),
        confidence=0.99 if same_venue_only else 1.0,
    )
    store = RuleStore(FileRuleCache(cache_dir))
    store.save_manifest(
        RuleCorpusManifest(
            manifest_id="manifest-sxbet",
            provider="SXBET",
            fetched_at="2026-04-27T00:00:00Z",
            endpoint_version="test",
            sport_count=1,
            event_count=support.event_count,
            selection_count=support.observed_count * 2,
            market_taxonomy_hash="test",
            source_refs=(),
        ),
    )
    rule = RuleClassifier().classify(instrument_a, instrument_b)
    assert rule is not None
    template = SemanticRuleTemplate.from_rule(rule, support=support)
    promoted = RulePromotionPolicy().promote_template(store, template)
    assert promoted is not None
    node_cache._write_semantic_cache_compatibility(cache_dir, manifest=manifest)


def _manifest(
    tmp_path: Path,
    *,
    cache_dir: Path | None = None,
    seed_dir: Path | None = None,
    cache_mode: str = "fresh",
    cache_default_root: Path | None = None,
    cache_max_age_hours: float | None = None,
) -> BettingArbitrageNodeManifest:
    return BettingArbitrageNodeManifest(
        node_id="sxbet-node",
        trader_id="BETARB-TEST-SEM",
        validation_mode=True,
        allow_dummy_credentials=True,
        semantic_rule_cache_dir=str(cache_dir) if cache_dir is not None else None,
        semantic_rule_cache_seed_dir=str(seed_dir) if seed_dir is not None else None,
        semantic_rule_cache_mode=cache_mode,
        semantic_rule_cache_default_root=(
            str(cache_default_root) if cache_default_root is not None else None
        ),
        semantic_rule_cache_max_age_hours=cache_max_age_hours,
        rendered_config_path=str(tmp_path / "trading-node-config.json"),
        status_path=str(tmp_path / "status.json"),
        heartbeat_path=str(tmp_path / "heartbeat.json"),
        venues=[
            BettingVenueManifest(
                venue="SXBET",
                client_key="SXBET_PRIMARY",
                execution_enabled=False,
                instrument_load_limit=10,
                market_discovery_limit=10,
            ),
        ],
    )


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
    def test_manifest_json_helpers_round_trip(self, tmp_path):
        manifest = _manifest(tmp_path)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(node_builder.manifest_to_json(manifest))

        loaded = node_builder.load_manifest(manifest_path)
        config = build_trading_node_config(manifest)

        assert loaded.node_id == manifest.node_id
        assert node_builder.render_trading_node_config_json(config) == config.json()

    def test_validation_mode_forces_safe_strategy_and_no_exec_clients(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            semantic_rule_cache_dir="artifacts/semantic-rule-cache/sxbet-validation",
            allow_dummy_credentials=True,
            strategy=BettingArbitrageConfig(
                min_profit_margin=Decimal("0.02"),
                max_total_stake=Decimal(100),
                auto_execute=True,
                value_execution_enabled=True,
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
        assert config.strategies[0].config["value_execution_enabled"] is False
        assert config.strategies[0].config["enabled_venues"] == ["SXBET"]
        assert config.strategies[0].config["opportunity_graph_engine"] == "semantic_rust"
        assert (
            config.strategies[0].config["semantic_rule_cache_dir"]
            == "artifacts/semantic-rule-cache/sxbet-validation"
        )

    def test_builder_defaults_approval_command_dir_beside_status_file(self, tmp_path):
        manifest = _manifest(tmp_path)

        config = build_trading_node_config(manifest)

        strategy_config = config.strategies[0].config
        assert strategy_config["execution_approval_command_dir"] == str(tmp_path / "commands")
        assert strategy_config["execution_approval_mode"] == "manual"

    def test_builder_keeps_explicit_approval_command_dir(self, tmp_path):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            allow_dummy_credentials=True,
            status_path=str(tmp_path / "status.json"),
            strategy=BettingArbitrageConfig(
                execution_approval_command_dir="/custom/commands",
            ),
            venues=[BettingVenueManifest(venue="SXBET")],
        )

        config = build_trading_node_config(manifest)

        assert config.strategies[0].config["execution_approval_command_dir"] == "/custom/commands"

    def test_builder_leaves_approval_command_dir_unset_without_status_path(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[BettingVenueManifest(venue="SXBET")],
        )

        config = build_trading_node_config(manifest)

        assert config.strategies[0].config["execution_approval_command_dir"] is None

    def test_semantic_cache_manifest_rejects_python_opportunity_graph(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            semantic_rule_cache_dir="artifacts/semantic-rule-cache/sxbet-validation",
            allow_dummy_credentials=True,
            strategy=BettingArbitrageConfig(
                opportunity_graph_engine="python",
            ),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                ),
            ],
        )

        with pytest.raises(ValueError, match="semantic_rust opportunity topology"):
            build_trading_node_config(manifest)

    def test_manifest_rejects_python_opportunity_graph_without_semantic_cache(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            allow_dummy_credentials=True,
            strategy=BettingArbitrageConfig(
                opportunity_graph_engine="python",
            ),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                ),
            ],
        )

        with pytest.raises(ValueError, match="semantic_rust opportunity topology"):
            build_trading_node_config(manifest)

    def test_manifest_rejects_disabled_opportunity_graph(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            allow_dummy_credentials=True,
            strategy=BettingArbitrageConfig(
                opportunity_graph_enabled=False,
            ),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                ),
            ],
        )

        with pytest.raises(ValueError, match="opportunity_graph_enabled=true"):
            build_trading_node_config(manifest)

    def test_semantic_cache_manifest_upgrades_legacy_rust_engine(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            semantic_rule_cache_dir="artifacts/semantic-rule-cache/sxbet-validation",
            allow_dummy_credentials=True,
            strategy=BettingArbitrageConfig(
                opportunity_graph_engine="rust",
            ),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                ),
            ],
        )

        config = build_trading_node_config(manifest)

        assert config.strategies[0].config["opportunity_graph_engine"] == "semantic_rust"

    def test_manifest_upgrades_auto_engine_without_semantic_cache(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-validation",
            trader_id="BETARB-TEST-001",
            validation_mode=True,
            allow_dummy_credentials=True,
            strategy=BettingArbitrageConfig(
                opportunity_graph_engine="auto",
            ),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                ),
            ],
        )

        config = build_trading_node_config(manifest)

        assert config.strategies[0].config["opportunity_graph_engine"] == "semantic_rust"

    def test_sxbet_exec_client_uses_dummy_credentials(self, monkeypatch):
        monkeypatch.delenv("SXBET_API_KEY", raising=False)
        monkeypatch.delenv("SXBET_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("SXBET_WALLET_ADDRESS", raising=False)
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

    def test_sxbet_testnet_execution_readiness_manifest_builds_exec_client(self, monkeypatch):
        monkeypatch.setenv("SXBET_API_KEY", "testnet-sxbet-api-key")
        monkeypatch.setenv("SXBET_PRIVATE_KEY", "0x" + "1" * 64)
        monkeypatch.setenv("SXBET_WALLET_ADDRESS", "0x" + "2" * 40)
        manifest = node_builder.load_manifest(
            Path("deploy/strategy_nodes/betting_arbitrage/sxbet-testnet-execution-readiness.json"),
        )

        config = build_trading_node_config(manifest)
        exec_client = config.exec_clients["SXBET_PRIMARY"]
        data_client = config.data_clients["SXBET_PRIMARY"]

        assert manifest.validation_mode is False
        assert config.strategies[0].config["auto_execute"] is False
        assert data_client.config["api_url"] == "https://api.toronto.sx.bet"
        assert data_client.config["ws_url"] == "wss://api.toronto.sx.bet"
        assert exec_client.config["api_url"] == "https://api.toronto.sx.bet"
        assert exec_client.config["ws_url"] == "wss://api.toronto.sx.bet"
        assert exec_client.config["base_currency"] == "USDC"
        assert exec_client.config["dry_run"] is True

    def test_sxbet_data_client_receives_order_book_runtime_settings(self, monkeypatch):
        monkeypatch.delenv("SXBET_API_KEY", raising=False)
        monkeypatch.delenv("SXBET_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("SXBET_WALLET_ADDRESS", raising=False)
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
                    market_discovery_limit=None,
                    prefer_liquid_markets=True,
                    liquidity_probe_limit=250,
                    min_two_sided_markets=2,
                    auto_subscribe_quote_ticks=True,
                    quote_subscription_limit=40,
                    order_book_poll_interval_secs=5.0,
                    order_book_poll_summary_interval_secs=30.0,
                    order_book_concurrency=8,
                    order_book_poll_mode="best_odds_batch",
                    order_book_best_odds_batch_size=30,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        data_client = config.data_clients["SXBET_PRIMARY"]

        assert config.timeout_connection == 240.0
        assert data_client.config["instrument_provider"]["instrument_load_limit"] == 50
        assert data_client.config["instrument_provider"]["market_discovery_limit"] is None
        assert data_client.config["instrument_provider"]["prefer_liquid_markets"] is True
        assert data_client.config["instrument_provider"]["liquidity_probe_limit"] == 250
        assert data_client.config["instrument_provider"]["min_two_sided_markets"] == 2
        assert data_client.config["instrument_provider"]["api_key_pool"] == ("dummy-sxbet-api-key",)
        assert data_client.config["auto_subscribe_quote_ticks"] is True
        assert data_client.config["quote_subscription_limit"] == 40
        assert data_client.config["order_book_poll_interval_secs"] == 5.0
        assert data_client.config["order_book_poll_summary_interval_secs"] == 30.0
        assert data_client.config["order_book_concurrency"] == 8
        assert data_client.config["order_book_poll_mode"] == "best_odds_batch"
        assert data_client.config["order_book_best_odds_batch_size"] == 30
        assert data_client.config["api_key_pool"] == ("dummy-sxbet-api-key",)

    def test_liquidity_depth_manifest_fields_thread_through_builder(self, monkeypatch):
        monkeypatch.delenv("SXBET_API_KEY", raising=False)
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-depth",
            trader_id="BETARB-TEST-DEPTH",
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                    prefer_liquid_markets=True,
                    min_market_depth=25.0,
                    top_markets_by_depth=8,
                    min_quote_depth=25.0,
                ),
                BettingVenueManifest(
                    venue="CLOUDBET",
                    client_key="CLOUDBET_PRIMARY",
                    min_quote_depth=50.0,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        provider_config = config.data_clients["SXBET_PRIMARY"].config["instrument_provider"]
        strategy_config = config.strategies[0].config

        assert provider_config["min_market_depth"] == 25.0
        assert provider_config["top_markets_by_depth"] == 8
        assert strategy_config["min_quote_depth_by_venue"] == {"SXBET": 25.0, "CLOUDBET": 50.0}
        assert strategy_config["cross_venue_liquidity_priority_enabled"] is True

    def test_liquidity_depth_manifest_fields_default_off(self, monkeypatch):
        monkeypatch.delenv("SXBET_API_KEY", raising=False)
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-nodepth",
            trader_id="BETARB-TEST-NODEPTH",
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(venue="SXBET", client_key="SXBET_PRIMARY"),
            ],
        )

        config = build_trading_node_config(manifest)
        provider_config = config.data_clients["SXBET_PRIMARY"].config["instrument_provider"]
        strategy_config = config.strategies[0].config

        # Unset depth thresholds stay None on the provider config and the strategy keeps
        # the feature off with an empty gate, so existing manifests behave as before.
        assert provider_config["min_market_depth"] is None
        assert provider_config["top_markets_by_depth"] is None
        assert strategy_config["min_quote_depth_by_venue"] == {}
        assert strategy_config["cross_venue_liquidity_priority_enabled"] is False

    def test_cloudbet_data_client_receives_runtime_settings(self, monkeypatch):
        monkeypatch.delenv("CLOUDBET_API_KEY", raising=False)
        manifest = BettingArbitrageNodeManifest(
            node_id="cloudbet-validation",
            trader_id="BETARB-TEST-CB",
            validation_mode=True,
            allow_dummy_credentials=True,
            semantic_rule_cache_dir="artifacts/semantic-rule-cache/cloudbet-validation",
            venues=[
                BettingVenueManifest(
                    venue="CLOUDBET",
                    client_key="CLOUDBET_PRIMARY",
                    sport_keys=frozenset({"soccer", "basketball"}),
                    instrument_load_limit=40,
                    prefer_liquid_markets=True,
                    auto_subscribe_quote_ticks=True,
                    quote_subscription_limit=60,
                    order_book_poll_interval_secs=7.0,
                    order_book_poll_summary_interval_secs=31.0,
                    order_book_concurrency=3,
                    order_book_missing_prune_threshold=2,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        data_client = config.data_clients["CLOUDBET_PRIMARY"]

        assert config.exec_clients == {}
        assert config.strategies[0].config["enabled_venues"] == ["CLOUDBET"]
        assert data_client.path == (
            "nautilus_trader.adapters.cloudbet.config:CloudbetDataClientConfig"
        )
        assert data_client.config["api_key"] == "dummy-cloudbet-api-key"
        assert data_client.config["instrument_provider"]["load_all"] is True
        assert data_client.config["instrument_provider"]["filters"]["sport_key"] == [
            "basketball",
            "soccer",
        ]
        assert "totals" in data_client.config["instrument_provider"]["filters"]["market_name"]
        assert "draw_no_bet" in data_client.config["instrument_provider"]["filters"]["market_name"]
        assert data_client.config["instrument_provider"]["filters"]["limit"] == 40
        assert data_client.config["auto_subscribe_quote_ticks"] is False
        assert data_client.config["quote_subscription_limit"] == 60
        assert data_client.config["quote_poll_interval_secs"] == 7.0
        assert data_client.config["quote_poll_summary_interval_secs"] == 31.0
        assert data_client.config["quote_poll_concurrency"] == 3
        assert data_client.config["quote_poll_min_concurrency"] == 1
        assert data_client.config["quote_poll_target_cycle_secs"] == 5.0
        assert data_client.config["quote_poll_adaptive_concurrency"] is True
        assert data_client.config["quote_poll_event_batching"] is True
        assert data_client.config["quote_poll_missing_prune_threshold"] == 2

    def test_cloudbet_data_client_keeps_auto_subscribe_without_semantic_cache(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="cloudbet-no-semantic-cache",
            trader_id="BETARB-TEST-CB",
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(
                    venue="CLOUDBET",
                    client_key="CLOUDBET_PRIMARY",
                    instrument_load_limit=40,
                    auto_subscribe_quote_ticks=True,
                    quote_subscription_limit=60,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        data_client = config.data_clients["CLOUDBET_PRIMARY"]

        assert data_client.config["auto_subscribe_quote_ticks"] is True
        assert data_client.config["quote_subscription_limit"] == 60

    def test_cloudbet_execution_readiness_manifest_builds_play_money_exec_client(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("CLOUDBET_API_KEY", "cloudbet-live-api-key")
        manifest = node_builder.load_manifest(
            Path("deploy/strategy_nodes/betting_arbitrage/cloudbet-execution-readiness.json"),
        )

        config = build_trading_node_config(manifest)
        exec_client = config.exec_clients["CLOUDBET_PRIMARY"]

        assert manifest.validation_mode is False
        assert config.strategies[0].config["auto_execute"] is False
        assert exec_client.config["base_currency"] == "PLAY_EUR"
        assert exec_client.config["api_key"] == "cloudbet-live-api-key"
        assert exec_client.config.get("api_url") is None
        assert exec_client.config["dry_run"] is True

        readiness = manifest_execution_readiness(manifest)
        assert readiness["validationMode"] is False
        assert readiness["autoExecute"] is False
        assert readiness["semanticCacheConfigured"] is True
        assert readiness["venues"] == [
            {
                "venue": "CLOUDBET",
                "clientKey": "CLOUDBET_PRIMARY",
                "dataEnabled": True,
                "executionEnabled": True,
                "executionDryRun": True,
                "environment": "paper",
                "baseCurrency": "PLAY_EUR",
                "apiUrl": None,
                "wsUrl": None,
                "sportKeys": [
                    "american_football",
                    "baseball",
                    "basketball",
                    "ice_hockey",
                    "soccer",
                    "tennis",
                ],
                "sportIds": [],
                "liveOnly": False,
                "loadAllInstruments": True,
                "instrumentLoadLimit": 40,
                "marketDiscoveryLimit": 40,
            },
        ]

    def test_sxbet_execution_readiness_manifest_uses_testnet_endpoints(self, monkeypatch):
        monkeypatch.setenv("SXBET_API_KEY", "sxbet-live-api-key")
        monkeypatch.setenv("SXBET_PRIVATE_KEY", "0x" + "a" * 64)
        monkeypatch.setenv("SXBET_WALLET_ADDRESS", "0x" + "b" * 40)

        manifest = node_builder.load_manifest(
            Path("deploy/strategy_nodes/betting_arbitrage/sxbet-testnet-execution-readiness.json"),
        )

        config = build_trading_node_config(manifest)
        exec_client = config.exec_clients["SXBET_PRIMARY"]

        assert manifest.validation_mode is False
        assert config.strategies[0].config["auto_execute"] is False
        assert exec_client.config["api_url"] == node_builder.SXBET_TESTNET_API_URL
        assert exec_client.config["ws_url"] == node_builder.SXBET_TESTNET_WS_URL
        assert exec_client.config["dry_run"] is True
        assert exec_client.config["base_currency"] == "USDC"

        readiness = manifest_execution_readiness(manifest)
        assert readiness["venues"][0]["environment"] == "testnet"
        assert readiness["venues"][0]["apiUrl"] == node_builder.SXBET_TESTNET_API_URL
        assert readiness["venues"][0]["wsUrl"] == node_builder.SXBET_TESTNET_WS_URL
        assert readiness["venues"][0]["executionDryRun"] is True

    def test_cloudbet_sxbet_live_pilot_manifest_builds_live_exec_clients(self, monkeypatch):
        monkeypatch.setenv("CLOUDBET_API_KEY", "cloudbet-live-api-key")
        monkeypatch.setenv("SXBET_API_KEY", "sxbet-live-api-key")
        monkeypatch.setenv("SXBET_PRIVATE_KEY", "a" * 64)
        monkeypatch.setenv("SXBET_WALLET_ADDRESS", "0x" + "b" * 40)
        monkeypatch.delenv("BETTING_LIVE_EXECUTION_ARMED", raising=False)

        manifest = node_builder.load_manifest(
            Path(
                "deploy/strategy_nodes/betting_arbitrage/"
                "cloudbet-sxbet-cross-venue-live-pilot.json",
            ),
        )
        config = build_trading_node_config(manifest)

        assert manifest.validation_mode is False
        assert config.strategies[0].config["auto_execute"] is True
        assert config.strategies[0].config["live_execution_armed"] is True
        assert config.strategies[0].config["execution_venue_mode"] == "cross_venue"
        assert config.strategies[0].config["max_resolution_horizon_hours"] == 48.0
        assert config.strategies[0].config["portfolio_base_currency"] == "USD"
        assert config.strategies[0].config["allow_cross_currency_live_execution"] is False
        assert config.strategies[0].config["max_total_stake"] == "25"
        assert config.strategies[0].config["max_leg_stake"] == "15"
        assert set(config.exec_clients) == {"CLOUDBET_PRIMARY", "SXBET_PRIMARY"}
        cloudbet_exec = config.exec_clients["CLOUDBET_PRIMARY"]
        sxbet_exec = config.exec_clients["SXBET_PRIMARY"]
        assert cloudbet_exec.config["dry_run"] is False
        assert cloudbet_exec.config["accept_price_change"] == "BETTER"
        assert cloudbet_exec.config["pending_acceptance_poll_attempts"] == 3
        assert cloudbet_exec.config["pending_acceptance_poll_interval_secs"] == 0.5
        assert sxbet_exec.config["dry_run"] is False
        assert sxbet_exec.config["execution_mode"] == "taker_fill"
        assert sxbet_exec.config["odds_slippage"] == 5
        assert sxbet_exec.config["private_key"] == "0x" + "a" * 64

        readiness = manifest_execution_readiness(manifest)
        assert readiness["autoExecute"] is True
        assert readiness["liveExecutionArmed"] is True
        assert readiness["liveExecutionEnvArmed"] is False
        assert readiness["allowCrossCurrencyLiveExecution"] is False
        assert readiness["executionVenueMode"] == "cross_venue"
        assert readiness["maxResolutionHorizonHours"] == 48.0
        assert readiness["portfolioBaseCurrency"] == "USD"
        assert readiness["riskCaps"] == {
            "maxLegStake": "15",
            "maxDailyNotional": "100",
            "maxDailyLoss": "25",
        }

    def test_polymarket_sxbet_cross_venue_pilot_manifest_is_unarmed_by_env(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_API_KEY", "pm-key")
        monkeypatch.setenv("POLYMARKET_API_SECRET", "test-value")
        monkeypatch.setenv("POLYMARKET_PASSPHRASE", "test-value")
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
        monkeypatch.setenv("POLYMARKET_FUNDER", "0x" + "b" * 40)
        monkeypatch.setenv("SXBET_API_KEY", "sxbet-live-api-key")
        monkeypatch.setenv("SXBET_PRIVATE_KEY", "a" * 64)
        monkeypatch.setenv("SXBET_WALLET_ADDRESS", "0x" + "b" * 40)
        monkeypatch.delenv("BETTING_LIVE_EXECUTION_ARMED", raising=False)

        manifest = node_builder.load_manifest(
            Path(
                "deploy/strategy_nodes/betting_arbitrage/"
                "polymarket-sxbet-cross-venue-live-pilot.json",
            ),
        )
        config = build_trading_node_config(manifest)

        assert manifest.validation_mode is False
        assert config.strategies[0].config["execution_venue_mode"] == "cross_venue"
        assert config.strategies[0].config["allow_same_venue_live_execution"] is False
        assert config.strategies[0].config["max_resolution_horizon_hours"] == 48.0
        assert config.strategies[0].config["semantic_unmatched_quote_probe_venues"] == [
            "POLYMARKET",
        ]
        assert config.strategies[0].config["semantic_unmatched_quote_probe_limit_per_venue"] == 160
        provider_config = config.data_clients["POLYMARKET_PRIMARY"].config["instrument_provider"]
        assert provider_config["filters"]["max_resolution_horizon_hours"] == 48.0
        sxbet_data = config.data_clients["SXBET_PRIMARY"].config
        assert sxbet_data["order_book_min_concurrency"] == 4
        assert sxbet_data["order_book_max_concurrency"] == 16
        assert sxbet_data["order_book_target_cycle_secs"] == 3.0
        assert sxbet_data["order_book_adaptive_concurrency"] is True
        assert set(config.exec_clients) == {"POLYMARKET_PRIMARY", "SXBET_PRIMARY"}
        readiness = manifest_execution_readiness(manifest)
        assert readiness["liveExecutionArmed"] is True
        assert readiness["liveExecutionEnvArmed"] is False

    def test_live_exec_factory_name_handles_importable_factory_instances(self):
        from nautilus_trader.adapters.cloudbet.factories import CloudbetLiveExecClientFactory
        from nautilus_trader.live import node_builder as live_node_builder

        factory = CloudbetLiveExecClientFactory()

        assert not hasattr(factory, "__name__")
        assert live_node_builder._factory_name(factory) == "CloudbetLiveExecClientFactory"
        assert (
            live_node_builder._factory_name(CloudbetLiveExecClientFactory)
            == "CloudbetLiveExecClientFactory"
        )

    def test_cloudbet_factories_match_live_node_builder_signature(self, monkeypatch):
        from nautilus_trader.adapters.cloudbet import factories as cloudbet_factories
        from nautilus_trader.adapters.cloudbet.config import CloudbetDataClientConfig
        from nautilus_trader.adapters.cloudbet.config import CloudbetExecClientConfig

        class FakeClient:
            pass

        class FakeProvider:
            pass

        class FakeDataClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeExecClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        def fake_cached_client(*, logger, **kwargs):
            assert logger is not None
            return FakeClient()

        def fake_cached_provider(*, logger, **kwargs):
            assert logger is not None
            return FakeProvider()

        monkeypatch.setattr(
            cloudbet_factories,
            "get_cached_cloudbet_client",
            fake_cached_client,
        )
        monkeypatch.setattr(
            cloudbet_factories,
            "get_cached_cloudbet_instrument_provider",
            fake_cached_provider,
        )
        monkeypatch.setattr(cloudbet_factories, "CloudbetDataClient", FakeDataClient)
        monkeypatch.setattr(
            cloudbet_factories,
            "CloudbetLiveExecutionClient",
            FakeExecClient,
        )

        data_client = cloudbet_factories.CloudbetLiveDataClientFactory.create(
            loop=Mock(),
            name="CLOUDBET",
            config=CloudbetDataClientConfig(
                instrument_provider=InstrumentProviderConfig(
                    load_all=True,
                    filters={"sport_key": ["soccer", "basketball"]},
                ),
            ),
            msgbus=Mock(),
            cache=Mock(),
            clock=Mock(),
        )
        exec_client = cloudbet_factories.CloudbetLiveExecClientFactory.create(
            loop=Mock(),
            name="CLOUDBET",
            config=CloudbetExecClientConfig(),
            msgbus=Mock(),
            cache=Mock(),
            clock=Mock(),
        )

        assert data_client.kwargs["logger"] is not None
        assert exec_client.kwargs["logger"] is not None

    def test_cloudbet_instrument_provider_cache_accepts_list_filters(self, monkeypatch):
        from nautilus_trader.adapters.cloudbet import factories as cloudbet_factories

        class FakeProvider:
            def __init__(self, *, client, logger, config):
                self.client = client
                self.logger = logger
                self.config = config

        monkeypatch.setattr(cloudbet_factories, "INSTRUMENT_PROVIDER", None)
        monkeypatch.setattr(cloudbet_factories, "CloudbetInstrumentProvider", FakeProvider)

        config = InstrumentProviderConfig(
            load_all=True,
            filters={"sport_key": ["soccer", "basketball"]},
        )
        provider = cloudbet_factories.get_cached_cloudbet_instrument_provider(
            client=Mock(),
            logger=Mock(),
            config=config,
        )

        assert provider.config is config

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
        assert data_client.config["instrument_provider"]["use_gamma_markets"] is True
        assert "filters" not in data_client.config["instrument_provider"]
        created = data_client.create()
        assert created.instrument_provider.load_ids == frozenset(
            {InstrumentId.from_str("condition-token.POLYMARKET")},
        )

    def test_polymarket_load_all_uses_gamma_sports_filters(self):
        manifest = BettingArbitrageNodeManifest(
            node_id="polymarket-sports-discovery",
            trader_id="BETARB-TEST-003",
            validation_mode=True,
            allow_dummy_credentials=True,
            venues=[
                BettingVenueManifest(
                    venue="POLYMARKET",
                    client_key="POLYMARKET_PRIMARY",
                    load_all_instruments=True,
                    sport_keys=frozenset({"basketball", "soccer"}),
                    instrument_load_limit=25,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        provider = config.data_clients["POLYMARKET_PRIMARY"].config["instrument_provider"]

        assert provider == {
            "load_all": True,
            "filters": {
                "is_active": True,
                "limit": 25,
                "max_results": 25,
                "sports": ["basketball", "soccer"],
            },
            "use_gamma_markets": True,
        }

    def test_polymarket_sports_filter_matches_gamma_market_text(self):
        basketball_market = {
            "question": "Will the Los Angeles Lakers win their NBA game?",
            "slug": "lakers-nba-game",
            "events": [{"title": "Los Angeles Lakers vs Denver Nuggets"}],
        }
        politics_market = {
            "question": "Will the mayor win re-election?",
            "slug": "mayor-election",
        }

        assert polymarket_providers._market_matches_sports_filter(
            basketball_market,
            {"basketball"},
        )
        assert not polymarket_providers._market_matches_sports_filter(
            politics_market,
            {"basketball"},
        )

    def test_polymarket_gamma_sports_discovery_balances_requested_sports(self):
        class Clock:
            @staticmethod
            def timestamp_ns() -> int:
                return 123

        provider = polymarket_providers.PolymarketInstrumentProvider(
            client=Mock(),
            clock=Clock(),
            config=InstrumentProviderConfig(),
        )
        event_limits: list[tuple[str, str, int]] = []

        def market_event(sport: str, index: int) -> dict[str, object]:
            return {
                "id": f"{sport}-event-{index}",
                "title": f"{sport.title()} Team {index} vs Other",
                "slug": f"{sport}-event-{index}",
                "startDate": "2026-05-10T18:00:00Z",
                "markets": [
                    {
                        "id": f"{sport}-market-{index}",
                        "conditionId": f"{sport}-condition-{index}",
                        "question": f"Will {sport} team {index} win?",
                    },
                ],
            }

        async def fake_gamma_get_json(endpoint, params=None):
            if endpoint == "/sports":
                return [
                    {"sport": "soccer", "tags": "11"},
                    {"sport": "tennis", "tags": "22"},
                ]
            assert endpoint == "/events"
            assert params is not None
            tag_id = str(params["tag_id"])
            event_limits.append((tag_id, str(params["order"]), int(params["limit"])))
            sport = "soccer" if tag_id == "11" else "tennis"
            return [market_event(sport, index) for index in range(4)]

        provider._gamma_get_json = fake_gamma_get_json

        markets = asyncio.run(
            provider._load_sports_event_markets_using_gamma(
                sports_filter={"soccer", "tennis"},
                max_results=4,
            ),
        )

        assert event_limits == [
            ("11", "volume24hr", 2),
            ("11", "volume", 2),
            ("22", "volume24hr", 2),
            ("22", "volume", 2),
        ]
        assert Counter(market["sport"] for market in markets) == {"soccer": 2, "tennis": 2}

    def test_polymarket_gamma_sports_discovery_balances_canonical_sport_groups(self):
        class Clock:
            @staticmethod
            def timestamp_ns() -> int:
                return 123

        provider = polymarket_providers.PolymarketInstrumentProvider(
            client=Mock(),
            clock=Clock(),
            config=InstrumentProviderConfig(),
        )
        event_limits: list[tuple[str, str, int]] = []

        def market_event(sport: str, index: int) -> dict[str, object]:
            return {
                "id": f"{sport}-event-{index}",
                "title": f"{sport.title()} Team {index} vs Other",
                "slug": f"{sport}-event-{index}",
                "startDate": "2026-05-10T18:00:00Z",
                "markets": [
                    {
                        "id": f"{sport}-market-{index}",
                        "conditionId": f"{sport}-condition-{index}",
                        "question": f"Will {sport} team {index} win?",
                    },
                ],
            }

        async def fake_gamma_get_json(endpoint, params=None):
            if endpoint == "/sports":
                return [
                    {"sport": "soccer", "tags": "11"},
                    {"sport": "epl", "tags": "12"},
                    {"sport": "ucl", "tags": "13"},
                    {"sport": "atp", "tags": "22"},
                    {"sport": "wta", "tags": "23"},
                ]
            assert endpoint == "/events"
            assert params is not None
            tag_id = str(params["tag_id"])
            event_limits.append((tag_id, str(params["order"]), int(params["limit"])))
            sport = "soccer" if tag_id in {"11", "12", "13"} else "tennis"
            return [market_event(sport, index) for index in range(6)]

        provider._gamma_get_json = fake_gamma_get_json

        markets = asyncio.run(
            provider._load_sports_event_markets_using_gamma(
                sports_filter={"soccer", "tennis"},
                max_results=6,
            ),
        )

        assert event_limits == [
            ("11", "volume24hr", 3),
            ("11", "volume", 3),
            ("12", "volume24hr", 3),
            ("12", "volume", 3),
            ("13", "volume24hr", 3),
            ("13", "volume", 3),
            ("22", "volume24hr", 3),
            ("22", "volume", 3),
            ("23", "volume24hr", 3),
            ("23", "volume", 3),
        ]
        assert Counter(market["sport"] for market in markets) == {"soccer": 3, "tennis": 3}

    def test_polymarket_tag_market_discovery_finds_match_level_tennis(
        self,
        monkeypatch,
    ):
        class Clock:
            @staticmethod
            def timestamp_ns() -> int:
                return 123

        provider = polymarket_providers.PolymarketInstrumentProvider(
            client=Mock(),
            clock=Clock(),
            config=InstrumentProviderConfig(),
        )
        list_calls: list[dict[str, object]] = []

        async def fake_gamma_get_json(endpoint, params=None):
            assert endpoint == "/sports"
            return [
                {"sport": "atp", "tags": "1,864,100639,101232"},
                {"sport": "wta", "tags": "1,864,100639,102123"},
            ]

        async def fake_list_markets(*, http_client, filters, max_results=None, **kwargs):
            del http_client, kwargs
            list_calls.append({"filters": dict(filters), "max_results": max_results})
            if str(filters.get("tag_id")) != "864":
                return []
            return [
                {
                    "id": "market-1",
                    "conditionId": "condition-1",
                    "question": "Internazionali BNL d'Italia: Frances Tiafoe vs Ignacio Buse",
                    "slug": "atp-tiafoe-buse-2026-05-10",
                    "active": True,
                    "closed": False,
                    "archived": False,
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.38", "0.62"]',
                    "clobTokenIds": '["yes-token", "no-token"]',
                    "events": [
                        {
                            "title": "Frances Tiafoe vs Ignacio Buse",
                            "slug": "tiafoe-buse",
                        },
                    ],
                },
            ]

        provider._gamma_get_json = fake_gamma_get_json
        monkeypatch.setattr(polymarket_providers, "list_markets", fake_list_markets)

        markets = asyncio.run(
            provider._load_sport_tag_markets_using_gamma(
                filters={"is_active": True},
                sports_filter={"tennis"},
                max_results=4,
            ),
        )

        assert len(markets) == 1
        assert markets[0]["sport"] == "tennis"
        assert markets[0]["sportsTag"] == "atp"
        assert markets[0]["sportsTagIds"] == ("864", "101232", "102123")
        assert markets[0]["events"][0]["sport"] == "tennis"
        assert any(call["filters"]["tag_id"] == "864" for call in list_calls)
        assert all(call["filters"]["order"] == "volume24hr" for call in list_calls)

    def test_polymarket_tag_market_discovery_preserves_event_start_date_iso(self, monkeypatch):
        class Clock:
            @staticmethod
            def timestamp_ns() -> int:
                return 123

        provider = polymarket_providers.PolymarketInstrumentProvider(
            client=Mock(),
            clock=Clock(),
            config=InstrumentProviderConfig(),
        )

        async def fake_gamma_get_json(endpoint, params=None):
            assert endpoint == "/sports"
            return [{"sport": "atp", "tags": "1,864,100639,101232"}]

        async def fake_list_markets(*, http_client, filters, max_results=None, **kwargs):
            del http_client, filters, max_results, kwargs
            return [
                {
                    "id": "market-iso",
                    "conditionId": "condition-iso",
                    "question": "Internazionali BNL d'Italia: Frances Tiafoe vs Ignacio Buse",
                    "slug": "atp-tiafoe-buse-2026-05-13",
                    "active": True,
                    "closed": False,
                    "archived": False,
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.38", "0.62"]',
                    "clobTokenIds": '["yes-token", "no-token"]',
                    "events": [
                        {
                            "title": "Frances Tiafoe vs Ignacio Buse",
                            "slug": "tiafoe-buse",
                            "startDateIso": "2026-05-13T19:00:00Z",
                        },
                    ],
                },
            ]

        provider._gamma_get_json = fake_gamma_get_json
        monkeypatch.setattr(polymarket_providers, "list_markets", fake_list_markets)

        markets = asyncio.run(
            provider._load_sport_tag_markets_using_gamma(
                filters={"is_active": True},
                sports_filter={"tennis"},
                max_results=4,
            ),
        )

        assert len(markets) == 1
        assert markets[0]["events"][0]["startDateIso"] == "2026-05-13T19:00:00Z"

    def test_polymarket_gamma_discovery_prioritizes_event_markets_before_tags(self):
        class Clock:
            @staticmethod
            def timestamp_ns() -> int:
                return 123

        provider = polymarket_providers.PolymarketInstrumentProvider(
            client=Mock(),
            clock=Clock(),
            config=InstrumentProviderConfig(),
        )
        calls: list[tuple[str, int | None]] = []

        async def fake_tag_markets(**kwargs):
            calls.append(("tag", kwargs["max_results"]))
            return int(kwargs["max_results"] or 0)

        async def fake_event_markets(**kwargs):
            calls.append(("event", kwargs["max_results"]))
            return 4

        async def fake_gamma_markets(**kwargs):
            calls.append(("fallback", kwargs["max_results"]))
            return 0

        provider._load_filtered_sport_tag_gamma_markets = fake_tag_markets
        provider._load_filtered_sports_event_markets = fake_event_markets
        provider._load_filtered_gamma_markets = fake_gamma_markets

        asyncio.run(
            provider._load_markets_using_gamma(
                {
                    "is_active": True,
                    "max_results": 10,
                    "sports": ["soccer", "tennis"],
                },
            ),
        )

        assert calls == [("event", 10), ("tag", 6), ("fallback", 0)]

    def test_polymarket_gamma_discovery_prefers_near_term_tag_markets(self, monkeypatch):
        class Clock:
            @staticmethod
            def timestamp_ns() -> int:
                return int(datetime(2026, 5, 10, 12, tzinfo=UTC).timestamp() * 1_000_000_000)

        provider = polymarket_providers.PolymarketInstrumentProvider(
            client=Mock(),
            clock=Clock(),
            config=InstrumentProviderConfig(),
        )
        list_limits: list[int | None] = []

        def market(condition_id: str, title: str, start_date: str) -> dict[str, object]:
            return {
                "id": condition_id,
                "conditionId": condition_id,
                "question": f"Will {title} happen?",
                "slug": title.lower().replace(" ", "-"),
                "active": True,
                "closed": False,
                "archived": False,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.50", "0.50"]',
                "clobTokenIds": f'["{condition_id}yes", "{condition_id}no"]',
                "orderPriceMinTickSize": 0.001,
                "orderMinSize": 5,
                "events": [
                    {
                        "title": title,
                        "slug": title.lower().replace(" ", "-"),
                        "startDate": start_date,
                    },
                ],
            }

        async def fake_gamma_get_json(endpoint, params=None):
            if endpoint == "/sports":
                return [{"sport": "tennis", "tags": "864"}]
            if endpoint == "/events":
                return []
            raise AssertionError(endpoint)

        list_filters: list[dict[str, object]] = []

        async def fake_list_markets(*, http_client, filters, max_results=None, **kwargs):
            del http_client, kwargs
            list_limits.append(max_results)
            list_filters.append(dict(filters))
            return [
                market(
                    "futurecondition",
                    "Frances Tiafoe vs Ignacio Buse 2027",
                    "2027-05-10T18:00:00Z",
                ),
                market(
                    "nearcondition",
                    "Frances Tiafoe vs Ignacio Buse",
                    "2026-05-10T18:00:00Z",
                ),
            ]

        provider._gamma_get_json = fake_gamma_get_json
        monkeypatch.setattr(polymarket_providers, "list_markets", fake_list_markets)

        asyncio.run(
            provider._load_markets_using_gamma(
                {
                    "is_active": True,
                    "max_results": 1,
                    "max_resolution_horizon_hours": 48.0,
                    "sports": ["tennis"],
                },
            ),
        )

        assert list_limits == [26]
        assert "start_date_min" in list_filters[0]
        assert "start_date_max" in list_filters[0]
        instruments = provider.list_all()
        assert len(instruments) == 2
        assert {
            instrument.info["_gamma_original"]["events"][0]["title"] for instrument in instruments
        } == {"Frances Tiafoe vs Ignacio Buse"}

    def test_polymarket_runtime_horizon_ranking_diversifies_fixture_events(self):
        now = datetime(2026, 5, 10, 12, tzinfo=UTC)

        def market(condition_id: str, event_id: str, question: str) -> dict[str, object]:
            return {
                "id": condition_id,
                "conditionId": condition_id,
                "question": question,
                "events": [
                    {
                        "id": event_id,
                        "title": event_id,
                        "startDate": "2026-05-10T18:00:00Z",
                    },
                ],
            }

        ranked = polymarket_providers._rank_runtime_horizon_markets(
            [
                market("a-winner", "event-a", "Team A winner"),
                market("a-spread", "event-a", "Team A spread"),
                market("a-total", "event-a", "Team A total points"),
                market("b-winner", "event-b", "Team B winner"),
            ],
            now=now,
            horizon=timedelta(hours=48),
        )

        assert [polymarket_providers._market_event_key(market) for market in ranked[:2]] == [
            "event-a",
            "event-b",
        ]

    def test_polymarket_provider_preserves_selected_token_metadata(self):
        class Clock:
            @staticmethod
            def timestamp_ns() -> int:
                return 123

        provider = polymarket_providers.PolymarketInstrumentProvider(
            client=Mock(),
            clock=Clock(),
            config=InstrumentProviderConfig(),
        )
        market_info = {
            "condition_id": "0xabc",
            "question": "Will Team A win?",
            "minimum_tick_size": "0.001",
            "minimum_order_size": "5",
            "end_date_iso": "2026-05-10T00:00:00Z",
            "maker_base_fee": "0",
            "taker_base_fee": "0",
            "tokens": [
                {"token_id": "tokenyes", "outcome": "Yes", "price": 0.44},
                {"token_id": "tokenno", "outcome": "No", "price": 0.56},
            ],
        }

        instrument = provider._load_instrument(market_info, "tokenno", "No")

        assert instrument.info["selected_token_id"] == "tokenno"
        assert instrument.info["selected_outcome"] == "No"
        assert instrument.info["selected_token_price"] == 0.56

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

    def test_multi_venue_validation_manifest_builds_without_exec_clients(self, monkeypatch):
        monkeypatch.setenv("SXBET_API_KEY", "sxbet-api-key")
        monkeypatch.setenv("SXBET_PRIVATE_KEY", "0x" + "1" * 64)
        monkeypatch.setenv("SXBET_WALLET_ADDRESS", "0x" + "2" * 40)
        monkeypatch.setenv("CLOUDBET_API_KEY", "cloudbet-api-key")
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "3" * 64)
        monkeypatch.setenv("POLYMARKET_FUNDER", "0x" + "4" * 40)
        monkeypatch.setenv("POLYMARKET_API_KEY", "polymarket-api-key")
        monkeypatch.setenv("POLYMARKET_API_SECRET", "polymarket-api-secret")
        monkeypatch.setenv("POLYMARKET_PASSPHRASE", "polymarket-passphrase")

        manifest = node_builder.load_manifest(
            Path("deploy/strategy_nodes/betting_arbitrage/multi-venue-validation.json"),
        )
        config = build_trading_node_config(manifest)

        assert manifest.allow_dummy_credentials is True
        assert sorted(config.data_clients) == [
            "CLOUDBET_PRIMARY",
            "POLYMARKET_PRIMARY",
            "SXBET_PRIMARY",
        ]
        assert config.exec_clients == {}
        assert config.strategies[0].config["auto_execute"] is False
        assert config.strategies[0].config["enabled_venues"] == [
            "CLOUDBET",
            "POLYMARKET",
            "SXBET",
        ]
        assert config.strategies[0].config["quote_freshness_profile"] == "pre_match"
        assert config.strategies[0].config["max_resolution_horizon_hours"] == 168.0
        assert config.strategies[0].config["semantic_unmatched_quote_probe_venues"] == [
            "POLYMARKET",
        ]
        assert config.strategies[0].config["semantic_unmatched_quote_probe_limit_per_venue"] == 80
        assert config.strategies[0].config["semantic_quote_subscription_limit_by_venue"] == {
            "CLOUDBET": 120,
            "POLYMARKET": 180,
            "SXBET": 120,
        }
        assert (
            config.strategies[0].config["semantic_rule_cache_dir"]
            == "artifacts/semantic-rule-cache/multi-venue-validation"
        )
        cloudbet_config = config.data_clients["CLOUDBET_PRIMARY"].config
        assert cloudbet_config["instrument_provider"]["filters"]["limit"] == 240
        assert "market_name" not in cloudbet_config["instrument_provider"]["filters"]
        assert cloudbet_config["quote_poll_interval_secs"] == 1.0
        assert cloudbet_config["quote_poll_concurrency"] == 24
        assert cloudbet_config["quote_poll_min_concurrency"] == 4
        assert cloudbet_config["quote_poll_max_concurrency"] == 48
        assert cloudbet_config["quote_poll_target_cycle_secs"] == 4.0
        assert cloudbet_config["quote_poll_adaptive_concurrency"] is True
        assert cloudbet_config["quote_poll_event_batching"] is True
        assert cloudbet_config["quote_poll_missing_prune_threshold"] == 3
        assert (
            config.data_clients["POLYMARKET_PRIMARY"].config["instrument_provider"]["load_all"]
            is True
        )
        assert (
            config.data_clients["POLYMARKET_PRIMARY"].config["instrument_provider"][
                "use_gamma_markets"
            ]
            is True
        )
        assert config.data_clients["POLYMARKET_PRIMARY"].config["instrument_provider"][
            "filters"
        ] == {
            "is_active": True,
            "limit": 240,
            "max_results": 240,
            "max_resolution_horizon_hours": 168.0,
            "sports": [
                "american_football",
                "baseball",
                "basketball",
                "ice_hockey",
                "soccer",
                "tennis",
            ],
        }
        created = config.data_clients["POLYMARKET_PRIMARY"].create()
        assert isinstance(hash(created.instrument_provider), int)

    def test_custom_credential_prefix_and_secret_pool_are_applied(self, monkeypatch):
        monkeypatch.setenv("CUSTOMSXBET_API_KEY", "explicit-api-key")
        monkeypatch.setenv("CUSTOMSXBET_API_KEYS", "pool-a, pool-b pool-c")

        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-custom-prefix",
            trader_id="BETARB-TEST-005",
            validation_mode=True,
            allow_dummy_credentials=False,
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                    credential_prefix="customsxbet",
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        data_client = config.data_clients["SXBET_PRIMARY"]

        assert data_client.config["api_key"] == "explicit-api-key"
        assert data_client.config["api_key_pool"] == ("pool-a", "pool-b", "pool-c")

    def test_polymarket_exec_client_uses_non_validation_credentials(self, monkeypatch):
        monkeypatch.setenv("CUSTOMPOLY_PRIVATE_KEY", "0x" + "a" * 64)
        monkeypatch.setenv("CUSTOMPOLY_FUNDER", "0x" + "b" * 40)
        monkeypatch.setenv("CUSTOMPOLY_API_KEY", "poly-api-key")
        monkeypatch.setenv("CUSTOMPOLY_API_SECRET", "poly-api-secret")
        monkeypatch.setenv("CUSTOMPOLY_PASSPHRASE", "poly-passphrase")

        manifest = BettingArbitrageNodeManifest(
            node_id="polymarket-live",
            trader_id="BETARB-TEST-006",
            validation_mode=False,
            allow_dummy_credentials=False,
            venues=[
                BettingVenueManifest(
                    venue="POLYMARKET",
                    client_key="POLYMARKET_PRIMARY",
                    credential_prefix="CUSTOMPOLY",
                    execution_enabled=True,
                    use_data_api=True,
                ),
            ],
        )

        config = build_trading_node_config(manifest)
        exec_client = config.exec_clients["POLYMARKET_PRIMARY"]

        assert isinstance(exec_client, ImportableConfig)
        assert exec_client.config["private_key"] == "0x" + "a" * 64
        assert exec_client.config["funder"] == "0x" + "b" * 40
        assert exec_client.config["api_key"] == "poly-api-key"
        assert exec_client.config["api_secret"] == "poly-api-secret"
        assert exec_client.config["passphrase"] == "poly-passphrase"
        assert exec_client.config["use_data_api"] is True

    def test_builder_rejects_unsupported_venue_branch(self, tmp_path):
        manifest = _manifest(tmp_path)
        msgspec.structs.force_setattr(manifest.venues[0], "venue", "UNSUPPORTED")

        with pytest.raises(ValueError, match="Unsupported venue UNSUPPORTED"):
            build_trading_node_config(manifest)

    def test_secret_helpers_cover_missing_and_dummy_paths(self, monkeypatch):
        monkeypatch.delenv("SXBET_API_KEYS", raising=False)
        monkeypatch.setenv("SXBET_API_KEY", "single-key")
        assert node_builder._resolve_secret_pool("SXBET", "API_KEYS", False) == ("single-key",)

        monkeypatch.setenv("SXBET_API_KEYS", " key-1, key-2  key-3 ")
        assert node_builder._resolve_secret_pool("SXBET", "API_KEYS", False) == (
            "key-1",
            "key-2",
            "key-3",
        )

        monkeypatch.setenv("SXBET_API_KEYS", "   ")
        assert node_builder._resolve_secret_pool("SXBET", "API_KEYS", False) is None

        monkeypatch.delenv("SXBET_API_KEYS", raising=False)
        monkeypatch.delenv("SXBET_API_KEY", raising=False)
        assert node_builder._resolve_secret_pool("SXBET", "API_KEYS", False) is None
        assert node_builder._resolve_secret_pool("SXBET", "API_KEYS", True) == (
            "dummy-sxbet-api-key",
        )

        monkeypatch.setenv("SXBET_API_KEY", "env-secret")
        assert node_builder._resolve_secret("SXBET", "API_KEY", False) == "env-secret"
        monkeypatch.delenv("SXBET_API_KEY", raising=False)
        assert (
            node_builder._resolve_secret("CUSTOM", "API_SECRET", True) == "dummy-custom-api-secret"
        )
        with pytest.raises(node_builder.MissingCredentialError, match="SXBET_API_KEY"):
            node_builder._resolve_secret("SXBET", "API_KEY", False)


class TestSemanticCacheBootstrap:
    def test_disabled_cache_returns_disabled_status(self, tmp_path):
        manifest = _manifest(tmp_path, cache_dir=None)
        msgspec.structs.force_setattr(manifest, "semantic_rule_cache_dir", None)

        status = ensure_semantic_cache_ready(manifest)

        assert status.path is None
        assert status.source == "disabled"
        assert status.ready is False

    def test_reuses_existing_semantic_cache_without_bootstrap(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        manifest = _manifest(tmp_path, cache_dir=cache_dir, cache_mode="reuse")
        _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: (_ for _ in ()).throw(AssertionError("bootstrap should not run")),
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "existing"
        assert status.ready is True
        assert status.compatible is True
        assert status.promoted_template_count >= 1

    def test_semantic_cache_scope_mismatch_triggers_bootstrap(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        original_manifest = _manifest(tmp_path, cache_dir=cache_dir)
        _seed_promoted_template(cache_dir, manifest=original_manifest)
        scoped_manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-node",
            trader_id="BETARB-TEST-SEM",
            validation_mode=True,
            allow_dummy_credentials=True,
            semantic_rule_cache_dir=str(cache_dir),
            semantic_rule_cache_mode="reuse",
            rendered_config_path=str(tmp_path / "trading-node-config.json"),
            status_path=str(tmp_path / "status.json"),
            heartbeat_path=str(tmp_path / "heartbeat.json"),
            venues=[
                BettingVenueManifest(
                    venue="SXBET",
                    client_key="SXBET_PRIMARY",
                    sport_ids=frozenset({5}),
                    execution_enabled=False,
                    instrument_load_limit=10,
                    market_discovery_limit=10,
                ),
            ],
        )
        bootstrapped: list[Path] = []

        def fake_bootstrap(*, cache_dir, **_):
            bootstrapped.append(cache_dir)
            _seed_promoted_template(cache_dir, manifest=scoped_manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(scoped_manifest)

        assert bootstrapped == [cache_dir]
        assert status.source == "bootstrapped"
        assert status.ready is True
        assert status.compatible is True

    def test_rebuilds_ready_cache_when_compatibility_marker_is_stale(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        manifest = _manifest(tmp_path, cache_dir=cache_dir, cache_mode="reuse")
        _seed_promoted_template(cache_dir, manifest=manifest)
        (cache_dir / node_cache.SEMANTIC_CACHE_COMPATIBILITY_FILE).write_text("stale\n")
        stale_file = cache_dir / "stale.bin"
        stale_file.write_text("old")

        def fake_bootstrap(*, cache_dir, **_):
            assert not stale_file.exists()
            _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "bootstrapped"
        assert status.ready is True
        assert status.compatible is True

    def test_bootstraps_missing_semantic_cache(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        manifest = _manifest(tmp_path, cache_dir=cache_dir, cache_mode="reuse")

        def fake_bootstrap(*, cache_dir, **_):
            _seed_promoted_template(cache_dir)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "bootstrapped"
        assert status.ready is True
        assert status.manifest_count >= 1
        assert status.promoted_template_count >= 1

    def test_seeds_missing_semantic_cache_before_bootstrap(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        seed_dir = tmp_path / "seed-cache"
        manifest = _manifest(tmp_path, cache_dir=cache_dir, cache_mode="reuse")
        seed_dir.mkdir()
        (seed_dir / "marker").write_text("seeded", encoding="utf-8")
        monkeypatch.setenv(node_cache.SEMANTIC_CACHE_SEED_DIR_ENV, str(seed_dir))
        statuses = {
            (str(cache_dir), "existing"): SemanticCacheStatus(
                path=str(cache_dir),
                source="existing",
                manifest_count=0,
                promoted_template_count=0,
                execution_safe_template_count=0,
                same_venue_execution_eligible_template_count=0,
            ),
            (str(seed_dir), "existing"): SemanticCacheStatus(
                path=str(seed_dir),
                source="existing",
                manifest_count=1,
                promoted_template_count=2,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
                compatibility_version=node_cache.SEMANTIC_CACHE_COMPATIBILITY_VERSION,
                compatible=True,
            ),
            (str(cache_dir), "seeded"): SemanticCacheStatus(
                path=str(cache_dir),
                source="seeded",
                manifest_count=1,
                promoted_template_count=2,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
                compatibility_version=node_cache.SEMANTIC_CACHE_COMPATIBILITY_VERSION,
                compatible=True,
            ),
        }

        def fake_status(path, *, source="existing", manifest=None):
            return statuses[(str(path), source)]

        monkeypatch.setattr(node_cache, "semantic_cache_status", fake_status)
        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: (_ for _ in ()).throw(AssertionError("bootstrap should not run")),
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "seeded"
        assert (cache_dir / "marker").read_text(encoding="utf-8") == "seeded"

    def test_manifest_seed_dir_overrides_semantic_cache_seed_env(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        manifest_seed_dir = tmp_path / "manifest-seed-cache"
        env_seed_dir = tmp_path / "env-seed-cache"
        manifest = _manifest(
            tmp_path,
            cache_dir=cache_dir,
            seed_dir=manifest_seed_dir,
            cache_mode="reuse",
        )
        manifest_seed_dir.mkdir()
        env_seed_dir.mkdir()
        (manifest_seed_dir / "marker").write_text("manifest-seed", encoding="utf-8")
        (env_seed_dir / "marker").write_text("env-seed", encoding="utf-8")
        monkeypatch.setenv(node_cache.SEMANTIC_CACHE_SEED_DIR_ENV, str(env_seed_dir))
        statuses = {
            (str(cache_dir), "existing"): SemanticCacheStatus(
                path=str(cache_dir),
                source="existing",
                manifest_count=0,
                promoted_template_count=0,
                execution_safe_template_count=0,
                same_venue_execution_eligible_template_count=0,
            ),
            (str(manifest_seed_dir), "existing"): SemanticCacheStatus(
                path=str(manifest_seed_dir),
                source="existing",
                manifest_count=1,
                promoted_template_count=2,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
                compatibility_version=node_cache.SEMANTIC_CACHE_COMPATIBILITY_VERSION,
                compatible=True,
            ),
            (str(cache_dir), "seeded"): SemanticCacheStatus(
                path=str(cache_dir),
                source="seeded",
                manifest_count=1,
                promoted_template_count=2,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
                compatibility_version=node_cache.SEMANTIC_CACHE_COMPATIBILITY_VERSION,
                compatible=True,
            ),
        }

        def fake_status(path, *, source="existing", manifest=None):
            return statuses[(str(path), source)]

        monkeypatch.setattr(node_cache, "semantic_cache_status", fake_status)
        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: (_ for _ in ()).throw(AssertionError("bootstrap should not run")),
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "seeded"
        assert (cache_dir / "marker").read_text(encoding="utf-8") == "manifest-seed"

    def test_unusable_semantic_cache_fails_validation(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: None,
        )

        with pytest.raises(RuntimeError, match="usable cache"):
            ensure_semantic_cache_ready(manifest)

    def test_fresh_mode_remines_even_when_compatible_cache_exists(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        manifest = _manifest(tmp_path, cache_dir=cache_dir, cache_mode="fresh")
        _seed_promoted_template(cache_dir, manifest=manifest)
        assert node_cache.semantic_cache_status(cache_dir, manifest=manifest).ready is True

        bootstrapped: list[Path] = []

        def fake_bootstrap(*, cache_dir, **_):
            bootstrapped.append(cache_dir)
            _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(manifest)

        assert bootstrapped == [cache_dir]
        assert status.source == "bootstrapped"
        assert status.ready is True
        assert status.compatible is True

    def test_reuse_mode_returns_existing_cache_without_bootstrap(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        manifest = _manifest(tmp_path, cache_dir=cache_dir, cache_mode="reuse")
        _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: (_ for _ in ()).throw(AssertionError("bootstrap should not run")),
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "existing"
        assert status.ready is True
        assert status.compatible is True

    def test_fresh_mode_registers_default_mine(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        default_root = tmp_path / "default-mines"
        manifest = _manifest(
            tmp_path,
            cache_dir=cache_dir,
            cache_mode="fresh",
            cache_default_root=default_root,
        )

        def fake_bootstrap(*, cache_dir, **_):
            _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "bootstrapped"
        registry_dir = node_cache._default_mine_dir(str(default_root), manifest)
        assert registry_dir.parent == default_root
        assert node_cache.semantic_cache_status(registry_dir, manifest=manifest).ready is True

    def test_default_mode_reuses_registered_mine_without_bootstrap(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        default_root = tmp_path / "default-mines"
        manifest = _manifest(
            tmp_path,
            cache_dir=cache_dir,
            cache_mode="default",
            cache_default_root=default_root,
        )
        registry_dir = node_cache._default_mine_dir(str(default_root), manifest)
        _seed_promoted_template(registry_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: (_ for _ in ()).throw(AssertionError("bootstrap should not run")),
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "default-mine"
        assert status.ready is True
        assert status.compatible is True

    def test_default_mode_mines_and_registers_when_registry_empty(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        default_root = tmp_path / "default-mines"
        manifest = _manifest(
            tmp_path,
            cache_dir=cache_dir,
            cache_mode="default",
            cache_default_root=default_root,
        )
        bootstrapped: list[Path] = []

        def fake_bootstrap(*, cache_dir, **_):
            bootstrapped.append(cache_dir)
            _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(manifest)

        assert bootstrapped == [cache_dir]
        assert status.source == "bootstrapped"
        registry_dir = node_cache._default_mine_dir(str(default_root), manifest)
        assert node_cache.semantic_cache_status(registry_dir, manifest=manifest).ready is True

    def test_default_mode_ignores_stale_registry_entry(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        default_root = tmp_path / "default-mines"
        manifest = _manifest(
            tmp_path,
            cache_dir=cache_dir,
            cache_mode="default",
            cache_default_root=default_root,
            cache_max_age_hours=1.0,
        )
        registry_dir = node_cache._default_mine_dir(str(default_root), manifest)
        _seed_promoted_template(registry_dir, manifest=manifest)
        # Materialize the summary (carries generated_at_unix_secs) then backdate it.
        assert node_cache.semantic_cache_status(registry_dir, manifest=manifest).ready is True
        stale = time.time() - 7200.0
        summary_path = registry_dir / node_cache.SEMANTIC_CACHE_SUMMARY_FILE
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["generated_at_unix_secs"] = stale
        summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
        os.utime(registry_dir / node_cache.SEMANTIC_CACHE_COMPATIBILITY_FILE, (stale, stale))

        bootstrapped: list[Path] = []

        def fake_bootstrap(*, cache_dir, **_):
            bootstrapped.append(cache_dir)
            _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(manifest)

        assert bootstrapped == [cache_dir]
        assert status.source == "bootstrapped"

    def test_default_mode_without_default_root_mines_fresh(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        manifest = _manifest(tmp_path, cache_dir=cache_dir, cache_mode="default")
        bootstrapped: list[Path] = []

        def fake_bootstrap(*, cache_dir, **_):
            bootstrapped.append(cache_dir)
            _seed_promoted_template(cache_dir, manifest=manifest)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            fake_bootstrap,
        )

        status = ensure_semantic_cache_ready(manifest)

        assert bootstrapped == [cache_dir]
        assert status.source == "bootstrapped"

    def test_invalid_semantic_cache_mode_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Unsupported semantic_rule_cache_mode"):
            _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache", cache_mode="stale")

    def test_non_positive_semantic_cache_max_age_hours_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="max_age_hours must be positive"):
            _manifest(
                tmp_path,
                cache_dir=tmp_path / "semantic-cache",
                cache_mode="default",
                cache_max_age_hours=0.0,
            )

    def test_semantic_cache_status_counts_missing_and_same_venue_templates(
        self,
        tmp_path,
        monkeypatch,
    ):
        class FakeStore:
            def __init__(self, _cache):
                pass

            def list_manifest_ids(self):
                return ["manifest-a", "manifest-b"]

            def list_promoted_template_ids(self):
                return ["missing-template", "exec-safe", "same-venue"]

            def list_coverage_proof_ids(self):
                return ["proof-a", "proof-b"]

            def list_coverage_hyperedge_ids(self):
                return ["hyperedge-a"]

            def list_snapshot_ids(self):
                return ["coverage-sxbet-old", "coverage-sxbet-new"]

            def load_snapshot(self, snapshot_id):
                payloads = {
                    "coverage-sxbet-old": {
                        "provider": "SXBET",
                        "sports": {
                            "soccer": {
                                "selection_count": 0,
                                "event_count": 0,
                                "blocker": "old",
                            },
                        },
                    },
                    "coverage-sxbet-new": {
                        "provider": "SXBET",
                        "coverage_mode": "active_live",
                        "live_only": True,
                        "prefer_liquid_markets": True,
                        "requested_sports": ["basketball", "baseball", "american_football"],
                        "resolved_sports": ["basketball", "baseball"],
                        "unresolved_requested_sports": ["american_football"],
                        "sports": {
                            "basketball": {
                                "selection_count": 12,
                                "event_count": 4,
                                "attempts": [{"source": "active"}],
                            },
                            "baseball": {
                                "selection_count": 0,
                                "event_count": 0,
                                "blocker": "no_active_markets_or_provider_data",
                            },
                        },
                    },
                }
                return CorpusSnapshot(
                    snapshot_id=snapshot_id,
                    provider="SXBET",
                    endpoint="/semantic/coverage/sxbet",
                    fetched_at="2026-05-07T00:00:00Z"
                    if snapshot_id.endswith("new")
                    else "2026-05-06T00:00:00Z",
                    payload=json.dumps(payloads[snapshot_id]).encode("utf-8"),
                )

            def load_promoted_template(self, template_id):
                mapping = {
                    "exec-safe": SimpleNamespace(
                        safety_tier=node_cache.SafetyTier.EXECUTION_SAFE.value,
                        execution_safe=True,
                        same_venue_execution_eligible=False,
                        relationship_type="COMPLEMENTARY_COVERAGE",
                        has_void=False,
                        has_partial=False,
                        has_unknown=False,
                        support=SimpleNamespace(catalog_promotable=True),
                        pattern_a=SimpleNamespace(market_family="TOTALS"),
                        pattern_b=SimpleNamespace(market_family="TOTALS"),
                        caveats=(),
                        eligibility_reasons=("execution_safe_complementary_coverage",),
                    ),
                    "same-venue": SimpleNamespace(
                        safety_tier=(
                            node_cache.SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value
                        ),
                        execution_safe=False,
                        same_venue_execution_eligible=True,
                        relationship_type="COMPLEMENTARY_COVERAGE",
                        has_void=False,
                        has_partial=False,
                        has_unknown=False,
                        support=SimpleNamespace(catalog_promotable=True),
                        pattern_a=SimpleNamespace(market_family="MATCH_ODDS"),
                        pattern_b=SimpleNamespace(market_family="MATCH_ODDS"),
                        caveats=(),
                        eligibility_reasons=("same_venue_risk_engine_elevation_required",),
                    ),
                }
                return mapping.get(template_id)

        monkeypatch.setattr(node_cache, "FileRuleCache", lambda path: path)
        monkeypatch.setattr(node_cache, "RuleStore", FakeStore)

        status = node_cache.semantic_cache_status(tmp_path)

        assert status.manifest_count == 2
        assert status.promoted_template_count == 3
        assert status.execution_safe_template_count == 1
        assert status.same_venue_execution_eligible_template_count == 1
        assert status.promoted_safety_tier_counts == {
            "EXECUTION_SAFE": 1,
            "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE": 1,
        }
        assert status.strict_execution_blocker_counts == {
            "same_venue_risk_engine_elevation_required": 1,
        }
        assert status.promoted_market_family_counts == {
            "MATCH_ODDS + MATCH_ODDS": 1,
            "TOTALS + TOTALS": 1,
        }
        assert status.execution_safe_market_family_counts == {"TOTALS + TOTALS": 1}
        assert status.same_venue_eligible_market_family_counts == {
            "MATCH_ODDS + MATCH_ODDS": 1,
        }
        assert status.coverage_proof_count == 2
        assert status.coverage_hyperedge_count == 1
        assert status.summary_reused is False
        assert status.bootstrap_phase_timings_secs == {}
        assert status.provider_corpus_coverage["SXBET"]["sports_with_selections"] == 1
        assert status.provider_corpus_coverage["SXBET"]["total_selection_count"] == 12
        assert status.provider_corpus_coverage["SXBET"]["zero_selection_sports"] == ["baseball"]
        assert status.provider_corpus_coverage["SXBET"]["coverage_mode"] == "active_live"
        assert status.provider_corpus_coverage["SXBET"]["live_only"] is True
        assert status.provider_corpus_coverage["SXBET"]["prefer_liquid_markets"] is True
        assert status.provider_corpus_coverage["SXBET"]["requested_sports"] == [
            "american_football",
            "baseball",
            "basketball",
        ]
        assert status.provider_corpus_coverage["SXBET"]["resolved_sports"] == [
            "baseball",
            "basketball",
        ]
        assert status.provider_corpus_coverage["SXBET"]["unresolved_requested_sports"] == [
            "american_football",
        ]
        assert status.provider_corpus_coverage["SXBET"]["blocker_counts"] == {
            "no_active_markets_or_provider_data": 1,
        }

    def test_semantic_cache_status_reuses_summary_without_template_scan(
        self,
        tmp_path,
        monkeypatch,
    ):
        summary_path = tmp_path / node_cache.SEMANTIC_CACHE_SUMMARY_FILE
        summary_path.write_text(
            json.dumps(
                {
                    "compatibility_version": None,
                    "compatibility_scope": None,
                    "manifest_count": 2,
                    "promoted_template_count": 3,
                    "execution_safe_template_count": 1,
                    "same_venue_execution_eligible_template_count": 1,
                    "coverage_proof_count": 2,
                    "coverage_hyperedge_count": 1,
                    "manifest_index_signature": node_cache._semantic_cache_index_signature(
                        ["manifest-a", "manifest-b"],
                    ),
                    "promoted_template_index_signature": node_cache._semantic_cache_index_signature(
                        ["missing-template", "exec-safe", "same-venue"],
                    ),
                    "coverage_proof_index_signature": node_cache._semantic_cache_index_signature(
                        ["proof-a", "proof-b"],
                    ),
                    "coverage_hyperedge_index_signature": node_cache._semantic_cache_index_signature(
                        ["hyperedge-a"],
                    ),
                    "promoted_safety_tier_counts": {
                        "EXECUTION_SAFE": 1,
                        "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE": 1,
                    },
                    "promoted_market_family_counts": {
                        "MATCH_ODDS + MATCH_ODDS": 1,
                        "TOTALS + TOTALS": 1,
                    },
                    "execution_safe_market_family_counts": {"TOTALS + TOTALS": 1},
                    "same_venue_eligible_market_family_counts": {
                        "MATCH_ODDS + MATCH_ODDS": 1,
                    },
                    "strict_execution_blocker_counts": {
                        "same_venue_risk_engine_elevation_required": 1,
                    },
                },
            ),
            encoding="utf-8",
        )
        timings_path = tmp_path / node_cache.SEMANTIC_CACHE_BOOTSTRAP_TIMINGS_FILE
        timings_path.write_text(
            json.dumps(
                {
                    "phase_timings_secs": {
                        "refresh_sxbet_corpus": 1.25,
                        "mine_event_candidates": 0.5,
                    },
                },
            ),
            encoding="utf-8",
        )

        class SummaryOnlyStore:
            def __init__(self, _cache):
                pass

            def list_manifest_ids(self):
                return ["manifest-a", "manifest-b"]

            def list_promoted_template_ids(self):
                return ["missing-template", "exec-safe", "same-venue"]

            def list_coverage_proof_ids(self):
                return ["proof-a", "proof-b"]

            def list_coverage_hyperedge_ids(self):
                return ["hyperedge-a"]

            def load_promoted_template(self, template_id):
                raise AssertionError(f"summary cache should avoid loading {template_id}")

        monkeypatch.setattr(node_cache, "RuleStore", SummaryOnlyStore)
        monkeypatch.setattr(node_cache, "FileRuleCache", lambda path: path)

        summary_status = node_cache.semantic_cache_status(tmp_path)

        assert summary_status.promoted_template_count == 3
        assert summary_status.execution_safe_template_count == 1
        assert summary_status.same_venue_execution_eligible_template_count == 1
        assert summary_status.strict_execution_blocker_counts == {
            "same_venue_risk_engine_elevation_required": 1,
        }
        assert summary_status.promoted_market_family_counts == {
            "MATCH_ODDS + MATCH_ODDS": 1,
            "TOTALS + TOTALS": 1,
        }
        assert summary_status.execution_safe_market_family_counts == {"TOTALS + TOTALS": 1}
        assert summary_status.same_venue_eligible_market_family_counts == {
            "MATCH_ODDS + MATCH_ODDS": 1,
        }
        assert summary_status.summary_reused is True
        assert summary_status.bootstrap_phase_timings_secs == {
            "mine_event_candidates": 0.5,
            "refresh_sxbet_corpus": 1.25,
        }

    def test_run_bootstrap_without_running_loop_executes_async_path(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        calls: list[tuple[Path, object]] = []

        async def fake_bootstrap(*, manifest, cache_dir, logger):
            calls.append((cache_dir, logger))

        monkeypatch.setattr(node_cache, "_bootstrap_semantic_cache", fake_bootstrap)

        node_cache._run_bootstrap(
            manifest=manifest,
            cache_dir=tmp_path / "semantic-cache",
            logger=None,
        )

        assert calls == [(tmp_path / "semantic-cache", None)]

    def test_run_bootstrap_with_running_loop_uses_thread_and_propagates_errors(
        self,
        tmp_path,
        monkeypatch,
    ):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        thread_targets: list[object] = []

        class InlineThread:
            def __init__(self, *, target, daemon):
                self._target = target
                self._daemon = daemon

            def start(self):
                thread_targets.append(self._daemon)
                self._target()

            def join(self):
                return None

        async def fake_bootstrap(*, manifest, cache_dir, logger):
            return None

        monkeypatch.setattr(node_cache.asyncio, "get_running_loop", lambda: object())
        monkeypatch.setattr(node_cache.threading, "Thread", InlineThread)
        monkeypatch.setattr(node_cache, "_bootstrap_semantic_cache", fake_bootstrap)

        node_cache._run_bootstrap(
            manifest=manifest,
            cache_dir=tmp_path / "semantic-cache",
            logger=None,
        )

        assert thread_targets == [True]

        async def failing_bootstrap(*, manifest, cache_dir, logger):
            raise RuntimeError("bootstrap-failed")

        monkeypatch.setattr(node_cache, "_bootstrap_semantic_cache", failing_bootstrap)
        with pytest.raises(RuntimeError, match="bootstrap-failed"):
            node_cache._run_bootstrap(
                manifest=manifest,
                cache_dir=tmp_path / "semantic-cache",
                logger=None,
            )

    def test_bootstrap_semantic_cache_runs_refresh_mine_and_promotion(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        store_marker = object()
        promoted: list[tuple[object, str]] = []
        refresh_calls: list[str] = []
        mine_store_calls: list[bool] = []
        mine_templates_calls: list[tuple[bool, bool]] = []
        mine_coverage_calls: list[bool] = []

        class FakeStore:
            def __init__(self, _cache):
                pass

        class FakeMiner:
            def __init__(self, store):
                assert isinstance(store, FakeStore)

            def mine_store(self, *, persist):
                mine_store_calls.append(persist)

            def mine_templates_from_store(self, *, persist, persist_event_candidates):
                mine_templates_calls.append((persist, persist_event_candidates))
                return ["template-a", "template-b"]

            def mine_coverage_from_store(self, *, persist):
                mine_coverage_calls.append(persist)
                return []

        class FakePromotionPolicy:
            def promote_template(self, store, template, *, allowlisted=False, venue_agnostic=False):
                promoted.append((store, template, allowlisted, venue_agnostic))
                return template

        monkeypatch.setattr(node_cache, "FileRuleCache", lambda path: path)
        monkeypatch.setattr(node_cache, "RuleStore", FakeStore)
        monkeypatch.setattr(node_cache, "SnapshotIngestor", lambda store: store_marker)
        monkeypatch.setattr(node_cache, "RuleMiner", FakeMiner)
        monkeypatch.setattr(node_cache, "RulePromotionPolicy", FakePromotionPolicy)

        async def fake_required(*, venues, ingestor, logger):
            assert ingestor is store_marker
            refresh_calls.append("sxbet")

        async def fake_cloudbet(*, manifest, venues, ingestor, logger):
            assert ingestor is store_marker
            assert [venue.venue for venue in venues] == ["SXBET"]
            refresh_calls.append("cloudbet")

        monkeypatch.setattr(node_cache, "_refresh_required_sxbet_corpus", fake_required)
        monkeypatch.setattr(node_cache, "_refresh_cloudbet_corpus", fake_cloudbet)

        asyncio.run(
            node_cache._bootstrap_semantic_cache(
                manifest=manifest,
                cache_dir=tmp_path / "semantic-cache",
                logger=None,
            ),
        )

        assert refresh_calls == ["sxbet", "cloudbet"]
        assert mine_store_calls == [True]
        assert mine_templates_calls == [(True, False)]
        assert mine_coverage_calls == [True]
        assert [template for _, template, _, _ in promoted] == ["template-a", "template-b"]

    def test_portable_polymarket_templates_are_promoted_venue_agnostic(self):
        home = _polymarket_winner_instrument(outcome="home")
        away = _polymarket_winner_instrument(outcome="away")
        rule = RuleClassifier().classify(home, away)
        assert rule is not None
        template = SemanticRuleTemplate.from_rule(
            rule,
            support=TemplateSupportStats(
                template_id="pm-portable",
                observed_count=10,
                event_count=10,
                provider_count=1,
                providers=("POLYMARKET",),
                sports=("basketball",),
                confidence=1.0,
            ),
        )

        assert node_cache._is_portable_polymarket_template(template)

        ambiguous = _polymarket_winner_instrument(outcome="home", resolution_policy="50_50")
        assert RuleClassifier().classify(ambiguous, away) is None
        ambiguous_template = replace(
            template,
            pattern_a=replace(
                template.pattern_a,
                resolution_policy=(("tie_or_unknown", "50_50"),),
            ),
        )
        assert not node_cache._is_portable_polymarket_template(ambiguous_template)

    def test_portable_polymarket_templates_include_deterministic_spreads(self):
        home = _polymarket_spread_instrument(outcome="home", line="1.5")
        away = _polymarket_spread_instrument(outcome="away", line="-1.5")
        rule = RuleClassifier().classify(home, away)
        assert rule is not None
        template = SemanticRuleTemplate.from_rule(
            rule,
            support=TemplateSupportStats(
                template_id="pm-portable-spread",
                observed_count=10,
                event_count=10,
                provider_count=1,
                providers=("POLYMARKET",),
                sports=("basketball",),
                confidence=1.0,
            ),
        )

        assert node_cache._is_portable_polymarket_template(template)

        whole_line = _polymarket_spread_instrument(outcome="home", line="1")
        whole_opposite = _polymarket_spread_instrument(outcome="away", line="-1")
        whole_rule = RuleClassifier().classify(whole_line, whole_opposite)
        assert whole_rule is not None
        whole_template = SemanticRuleTemplate.from_rule(
            whole_rule,
            support=TemplateSupportStats(
                template_id="pm-portable-spread-void",
                observed_count=10,
                event_count=10,
                provider_count=1,
                providers=("POLYMARKET",),
                sports=("basketball",),
                confidence=1.0,
            ),
        )
        assert not node_cache._is_portable_polymarket_template(whole_template)

    def test_refresh_required_sxbet_corpus_skips_when_no_sxbet_venues(self):
        ingestor = Mock()

        asyncio.run(
            node_cache._refresh_required_sxbet_corpus(
                venues=[BettingVenueManifest(venue="POLYMARKET")],
                ingestor=ingestor,
                logger=None,
            ),
        )

        assert not ingestor.mock_calls

    def test_refresh_required_sxbet_corpus_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("SXBET_API_KEY", raising=False)
        monkeypatch.setattr(node_cache, "_DEFAULT_LOCAL_ENV_FILES", ())

        with pytest.raises(RuntimeError, match="SXBET_API_KEY"):
            asyncio.run(
                node_cache._refresh_required_sxbet_corpus(
                    venues=[BettingVenueManifest(venue="SXBET")],
                    ingestor=Mock(),
                    logger=None,
                ),
            )

    def test_refresh_required_sxbet_corpus_derives_window_and_disconnects(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("SXBET_API_KEY", "sxbet-live-key")
        monkeypatch.setattr(node_cache.time, "time", lambda: 1_000_000)
        refresh_calls: list[dict[str, object]] = []

        class FakeClient:
            def __init__(self, *, api_key, logger):
                self.api_key = api_key
                self.logger = logger
                self.connected = False
                self.disconnected = False

            async def connect(self):
                self.connected = True

            async def disconnect(self):
                self.disconnected = True

        class FakeIngestor:
            async def refresh_sxbet(
                self,
                client,
                *,
                sports,
                sport_ids,
                from_time,
                to_time,
                instrument_limit,
                market_discovery_limit,
                prefer_liquid_markets,
                liquidity_probe_limit,
                min_two_sided_markets,
                live_only,
            ):
                refresh_calls.append(
                    {
                        "client": client,
                        "sports": sports,
                        "sport_ids": sport_ids,
                        "from_time": from_time,
                        "to_time": to_time,
                        "instrument_limit": instrument_limit,
                        "market_discovery_limit": market_discovery_limit,
                        "prefer_liquid_markets": prefer_liquid_markets,
                        "liquidity_probe_limit": liquidity_probe_limit,
                        "min_two_sided_markets": min_two_sided_markets,
                        "live_only": live_only,
                    },
                )

        monkeypatch.setattr(node_cache, "SXBetHttpClient", FakeClient)
        venues = [
            BettingVenueManifest(
                venue="SXBET",
                sport_ids=frozenset({77}),
                instrument_load_limit=300,
                market_discovery_limit=275,
            ),
            BettingVenueManifest(
                venue="SXBET",
                sport_ids=frozenset({3}),
                live_only=True,
                instrument_load_limit=280,
                market_discovery_limit=400,
                prefer_liquid_markets=True,
                liquidity_probe_limit=350,
                min_two_sided_markets=2,
            ),
        ]

        asyncio.run(
            node_cache._refresh_required_sxbet_corpus(
                venues=venues,
                ingestor=FakeIngestor(),
                logger=None,
            ),
        )

        assert len(refresh_calls) == 1
        call = refresh_calls[0]
        assert call["sports"] is None
        assert call["sport_ids"] == [3, 77]
        assert call["from_time"] == 1_000_000 - 6 * 60 * 60
        assert call["to_time"] == 1_000_000 + 6 * 60 * 60
        assert call["instrument_limit"] == 300
        assert call["market_discovery_limit"] == 400
        assert call["prefer_liquid_markets"] is True
        assert call["liquidity_probe_limit"] == 350
        assert call["min_two_sided_markets"] == 2
        assert call["live_only"] is True
        assert call["client"].connected is True
        assert call["client"].disconnected is True

    def test_sxbet_corpus_scope_uses_sport_keys_and_scales_defaults(self):
        scope = node_cache._sxbet_corpus_scope(
            [
                BettingVenueManifest(
                    venue="SXBET",
                    sport_keys=frozenset({"soccer", "basketball", "tennis"}),
                ),
            ],
        )

        assert scope.sport_keys == ["basketball", "soccer", "tennis"]
        assert scope.sport_ids is None
        assert scope.instrument_limit == 250
        assert scope.market_discovery_limit == 360

    def test_sxbet_corpus_scope_defaults_to_six_target_sports(self):
        scope = node_cache._sxbet_corpus_scope([BettingVenueManifest(venue="SXBET")])

        assert scope.sport_keys == list(node_cache.DEFAULT_SXBET_SPORTS)
        assert scope.sport_ids is None
        assert scope.instrument_limit == 480
        assert scope.market_discovery_limit == 720

    def test_semantic_cache_scope_records_default_target_sports(self, tmp_path):
        manifest = BettingArbitrageNodeManifest(
            node_id="sxbet-node",
            trader_id="BETARB-TEST-SEM",
            validation_mode=True,
            semantic_rule_cache_dir=str(tmp_path / "semantic-cache"),
            venues=[BettingVenueManifest(venue="SXBET")],
        )

        payload = {
            "providers": [
                {
                    "venue": "SXBET",
                    "sport_keys": list(node_cache.DEFAULT_SXBET_SPORTS),
                    "sport_ids": "all",
                    "league_ids": "all",
                    "live_only": False,
                    "instrument_load_limit": None,
                    "market_discovery_limit": None,
                    "prefer_liquid_markets": False,
                    "liquidity_probe_limit": 100,
                    "min_two_sided_markets": 1,
                },
            ],
        }
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()[:24]

        assert node_cache._semantic_cache_scope_key(manifest) == expected

    def test_refresh_required_sxbet_corpus_disconnects_after_failure(self, monkeypatch):
        monkeypatch.setenv("SXBET_API_KEY", "sxbet-live-key")

        class FakeClient:
            disconnected = False

            def __init__(self, *, api_key, logger):
                pass

            async def connect(self):
                return None

            async def disconnect(self):
                type(self).disconnected = True

        class FailingIngestor:
            async def refresh_sxbet(self, *args, **kwargs):
                raise RuntimeError("refresh-failed")

        monkeypatch.setattr(node_cache, "SXBetHttpClient", FakeClient)

        with pytest.raises(RuntimeError, match="refresh-failed"):
            asyncio.run(
                node_cache._refresh_required_sxbet_corpus(
                    venues=[BettingVenueManifest(venue="SXBET")],
                    ingestor=FailingIngestor(),
                    logger=None,
                ),
            )

        assert FakeClient.disconnected is True

    def test_refresh_optional_cloudbet_skips_without_api_key(self, monkeypatch):
        monkeypatch.delenv("CLOUDBET_API_KEY", raising=False)
        ingestor = Mock()

        asyncio.run(
            node_cache._refresh_optional_cloudbet_corpus(
                manifest=None,
                ingestor=ingestor,
                logger=None,
            ),
        )

        assert not ingestor.mock_calls

    def test_refresh_optional_cloudbet_success_and_warning_paths(self, monkeypatch):
        monkeypatch.setenv("CLOUDBET_API_KEY", "cloudbet-key")
        monkeypatch.setattr(node_cache.time, "time", lambda: 2_000_000)
        refresh_calls: list[dict[str, object]] = []
        disconnects: list[str] = []

        class FakeClient:
            def __init__(self, *, loop, logger, api_key):
                self.loop = loop
                self.logger = logger
                self.api_key = api_key

            async def connect(self):
                return None

            async def disconnect(self):
                disconnects.append("disconnect")

        class SuccessIngestor:
            async def refresh_cloudbet(
                self,
                client,
                *,
                sports,
                from_timestamp,
                to_timestamp,
                limit,
                adaptive_window,
                max_window_seconds,
                min_events_per_sport,
                include_recent_past_on_sparse,
                include_bets,
            ):
                refresh_calls.append(
                    {
                        "client": client,
                        "sports": sports,
                        "from_timestamp": from_timestamp,
                        "to_timestamp": to_timestamp,
                        "limit": limit,
                        "adaptive_window": adaptive_window,
                        "max_window_seconds": max_window_seconds,
                        "min_events_per_sport": min_events_per_sport,
                        "include_recent_past_on_sparse": include_recent_past_on_sparse,
                        "include_bets": include_bets,
                    },
                )

        class FailingIngestor:
            async def refresh_cloudbet(self, *args, **kwargs):
                raise RuntimeError("cloudbet-failed")

        monkeypatch.setattr(node_cache, "CloudbetClient", FakeClient)

        asyncio.run(
            node_cache._refresh_optional_cloudbet_corpus(
                manifest=None,
                ingestor=SuccessIngestor(),
                logger=None,
            ),
        )

        assert len(refresh_calls) == 1
        call = refresh_calls[0]
        assert call["client"].api_key == "cloudbet-key"
        assert call["sports"] == list(node_cache.DEFAULT_CLOUDBET_SPORTS)
        assert call["from_timestamp"] == 2_000_000
        assert call["to_timestamp"] == 2_000_000 + 24 * 60 * 60
        assert call["limit"] == 20
        assert call["adaptive_window"] is True
        assert call["max_window_seconds"] == 7 * 24 * 60 * 60
        assert call["min_events_per_sport"] == 1
        assert call["include_recent_past_on_sparse"] is True
        assert call["include_bets"] is False

        logger = Mock()
        asyncio.run(
            node_cache._refresh_optional_cloudbet_corpus(
                manifest=None,
                ingestor=FailingIngestor(),
                logger=logger,
            ),
        )

        logger.warning.assert_called_once()
        assert disconnects == ["disconnect", "disconnect"]

    def test_refresh_cloudbet_corpus_required_without_api_key_fails(self, monkeypatch):
        monkeypatch.delenv("CLOUDBET_API_KEY", raising=False)
        monkeypatch.setattr(node_cache, "_DEFAULT_LOCAL_ENV_FILES", ())

        with pytest.raises(RuntimeError, match="CLOUDBET_API_KEY"):
            asyncio.run(
                node_cache._refresh_cloudbet_corpus(
                    manifest=None,
                    venues=[BettingVenueManifest(venue="CLOUDBET")],
                    ingestor=Mock(),
                    logger=None,
                ),
            )

    def test_refresh_cloudbet_corpus_derives_required_scope(self, monkeypatch):
        monkeypatch.setenv("CLOUDBET_API_KEY", "cloudbet-key")
        monkeypatch.setattr(node_cache.time, "time", lambda: 2_000_000)
        refresh_calls: list[dict[str, object]] = []

        class FakeClient:
            def __init__(self, *, loop, logger, api_key):
                self.loop = loop
                self.logger = logger
                self.api_key = api_key
                self.disconnected = False

            async def connect(self):
                return None

            async def disconnect(self):
                self.disconnected = True

        class SuccessIngestor:
            async def refresh_cloudbet(
                self,
                client,
                *,
                sports,
                from_timestamp,
                to_timestamp,
                limit,
                adaptive_window,
                max_window_seconds,
                min_events_per_sport,
                include_recent_past_on_sparse,
                include_bets,
            ):
                refresh_calls.append(
                    {
                        "client": client,
                        "sports": sports,
                        "from_timestamp": from_timestamp,
                        "to_timestamp": to_timestamp,
                        "limit": limit,
                        "adaptive_window": adaptive_window,
                        "max_window_seconds": max_window_seconds,
                        "min_events_per_sport": min_events_per_sport,
                        "include_recent_past_on_sparse": include_recent_past_on_sparse,
                        "include_bets": include_bets,
                    },
                )

        monkeypatch.setattr(node_cache, "CloudbetClient", FakeClient)

        asyncio.run(
            node_cache._refresh_cloudbet_corpus(
                manifest=None,
                venues=[
                    BettingVenueManifest(
                        venue="CLOUDBET",
                        sport_keys=frozenset({"soccer", "basketball"}),
                        instrument_load_limit=35,
                    ),
                ],
                ingestor=SuccessIngestor(),
                logger=None,
            ),
        )

        assert len(refresh_calls) == 1
        call = refresh_calls[0]
        assert call["client"].api_key == "cloudbet-key"
        assert call["client"].disconnected is True
        assert call["sports"] == ["basketball", "soccer"]
        assert call["from_timestamp"] == 2_000_000
        assert call["to_timestamp"] == 2_000_000 + 24 * 60 * 60
        assert call["limit"] == 35
        assert call["adaptive_window"] is True
        assert call["max_window_seconds"] == 7 * 24 * 60 * 60
        assert call["include_recent_past_on_sparse"] is True
        assert call["include_bets"] is False

    def test_semantic_cache_local_env_loader_sources_repo_local_workspace_env(
        self,
        tmp_path,
        monkeypatch,
    ):
        env_file = tmp_path / ".env.cloud-workspace.local"
        env_file.write_text("SXBET_API_KEY=file-sxbet-key\n", encoding="utf-8")
        monkeypatch.setattr(node_cache, "_DEFAULT_LOCAL_ENV_FILES", (env_file,))
        original = os.environ.pop("SXBET_API_KEY", None)
        try:
            loaded = node_cache._load_local_workspace_env()
            assert loaded == env_file
            assert os.environ["SXBET_API_KEY"] == "file-sxbet-key"
        finally:
            os.environ.pop("SXBET_API_KEY", None)
            if original is not None:
                os.environ["SXBET_API_KEY"] = original

    def test_refresh_polymarket_corpus_derives_manifest_scope(self):
        refresh_calls: list[dict[str, object]] = []

        class FakeIngestor:
            async def refresh_polymarket(self, *, sports, limit, http_client=None):
                refresh_calls.append(
                    {
                        "sports": sports,
                        "limit": limit,
                        "http_client": http_client,
                    },
                )

        venues = [
            BettingVenueManifest(
                venue="POLYMARKET",
                sport_keys=frozenset({"basketball", "soccer"}),
                instrument_load_limit=45,
                market_discovery_limit=120,
            ),
            BettingVenueManifest(venue="SXBET"),
        ]

        asyncio.run(
            node_cache._refresh_polymarket_corpus(
                venues=venues,
                ingestor=FakeIngestor(),
                logger=None,
            ),
        )

        assert refresh_calls == [
            {
                "sports": ["basketball", "soccer"],
                "limit": 120,
                "http_client": None,
            },
        ]

    def test_refresh_polymarket_corpus_skips_when_no_polymarket_venues(self):
        ingestor = Mock()

        asyncio.run(
            node_cache._refresh_polymarket_corpus(
                venues=[BettingVenueManifest(venue="SXBET")],
                ingestor=ingestor,
                logger=None,
            ),
        )

        assert not ingestor.mock_calls


class TestBettingArbitrageNodeRunner:
    def test_runtime_probe_payload_exposes_execution_approvals(self):
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )

        payload = node_runner._collect_runtime_probe_payload(
            strategy,
            min_profit_margin=Decimal("0.02"),
            elapsed_seconds=1.0,
        )

        approvals = payload["executionApprovals"]
        assert approvals["mode"] == "manual"
        assert approvals["pending"] == []
        assert approvals["staged"] == 0
        assert payload["strategyStats"]["execution_approvals"] == approvals

    def test_heartbeat_writer_emits_alive_payload(self, tmp_path, monkeypatch):
        heartbeat_path = tmp_path / "heartbeat.json"

        class FakeStopEvent:
            def __init__(self):
                self.calls = 0

            def wait(self, _interval_secs):
                self.calls += 1
                return self.calls > 1

        monkeypatch.setattr(node_runner, "_utc_now", lambda: "2026-04-29T10:00:00Z")
        writer = node_runner.HeartbeatWriter(
            heartbeat_path=heartbeat_path,
            node_id="sxbet-node",
            interval_secs=0.0,
            stop_event=FakeStopEvent(),
        )

        writer.run()

        payload = json.loads(heartbeat_path.read_text())
        assert payload == {
            "nodeId": "sxbet-node",
            "status": "alive",
            "at": "2026-04-29T10:00:00Z",
        }

    def test_render_node_config_supports_stdout_and_explicit_output(
        self,
        tmp_path,
        monkeypatch,
        capsys,
    ):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())
        monkeypatch.setattr(
            node_runner,
            "ensure_semantic_cache_ready",
            lambda _: None,
        )

        result = runner_main(["render-node-config", "--manifest", str(manifest_path)])

        assert result == 0
        rendered_stdout = capsys.readouterr().out
        assert json.loads(rendered_stdout)["trader_id"] == manifest.trader_id

        explicit_output = tmp_path / "rendered-explicit.json"
        result = runner_main(
            [
                "render-node-config",
                "--manifest",
                str(manifest_path),
                "--output",
                str(explicit_output),
            ],
        )

        assert result == 0
        assert json.loads(explicit_output.read_text())["trader_id"] == manifest.trader_id

    def test_validate_manifest_writes_semantic_cache_status(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())
        expected_status = SemanticCacheStatus(
            path=str(tmp_path / "semantic-cache"),
            source="bootstrapped",
            manifest_count=1,
            promoted_template_count=2,
            execution_safe_template_count=1,
            same_venue_execution_eligible_template_count=1,
            summary_reused=True,
            bootstrap_phase_timings_secs={"total": 12.5, "mine_event_candidates": 1.25},
        )
        monkeypatch.setattr(
            (
                "nautilus_trader.live.strategy_nodes.betting_arbitrage.runner."
                "ensure_semantic_cache_ready"
            ),
            lambda _: expected_status,
        )

        result = runner_main(["validate-manifest", "--manifest", str(manifest_path)])

        assert result == 0
        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "validated"
        assert payload["semanticCache"]["source"] == "bootstrapped"
        assert payload["semanticCache"]["promotedTemplateCount"] == 2
        assert payload["semanticCache"]["sameVenueExecutionEligibleTemplateCount"] == 1
        assert payload["semanticCache"]["summaryReused"] is True
        assert payload["semanticCache"]["bootstrapPhaseTimingsSeconds"]["total"] == 12.5
        assert payload["executionReadiness"]["validationMode"] is True
        assert payload["executionReadiness"]["autoExecute"] is False
        assert payload["executionReadiness"]["venues"][0]["venue"] == "SXBET"

    def test_validate_manifest_failure_writes_failed_status(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())
        monkeypatch.setattr(
            node_runner,
            "ensure_semantic_cache_ready",
            lambda _: SemanticCacheStatus(
                path=str(tmp_path / "semantic-cache"),
                source="existing",
                manifest_count=1,
                promoted_template_count=1,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
            ),
        )
        monkeypatch.setattr(
            node_runner,
            "build_trading_node_config",
            lambda manifest: (_ for _ in ()).throw(ValueError("bad-config")),
        )

        with pytest.raises(ValueError, match="bad-config"):
            runner_main(["validate-manifest", "--manifest", str(manifest_path)])

        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "failed"
        assert payload["error"] == "ValueError('bad-config')"
        assert payload["semanticCache"]["ready"] is True
        assert payload["executionReadiness"]["venues"][0]["executionEnabled"] is False

    def test_run_no_start_records_semantic_cache_status(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())

        monkeypatch.setattr(
            (
                "nautilus_trader.live.strategy_nodes.betting_arbitrage.runner."
                "ensure_semantic_cache_ready"
            ),
            lambda _: SemanticCacheStatus(
                path=str(tmp_path / "semantic-cache"),
                source="existing",
                manifest_count=1,
                promoted_template_count=1,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
            ),
        )

        class FakeTradingNode:
            def __init__(self, config):
                self.config = config

            def build(self):
                return None

            def dispose(self):
                return None

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", FakeTradingNode)

        result = runner_main(["run", "--manifest", str(manifest_path), "--no-start"])

        assert result == 0
        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "built"
        assert payload["semanticCache"]["source"] == "existing"
        assert payload["semanticCache"]["executionSafeTemplateCount"] == 1
        assert payload["executionReadiness"]["semanticCacheConfigured"] is True

    def test_probe_runtime_records_runtime_probe_status(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.runner.ensure_semantic_cache_ready",
            lambda _: SemanticCacheStatus(
                path=str(tmp_path / "semantic-cache"),
                source="existing",
                manifest_count=1,
                promoted_template_count=2,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=1,
            ),
        )
        observed_probe_kwargs = {}

        def fake_probe_runtime(**kwargs):
            observed_probe_kwargs.update(kwargs)
            return {
                "graphEngine": "rust",
                "topologySource": "rust_semantic",
                "semanticTemplateCount": 2,
                "connectedNodes": 2,
                "semanticMatchInstruments": 2,
                "quotedSemanticMatchInstruments": 2,
                "quotedEdges": 1,
                "positiveMarginCandidates": {
                    "executionSafe": 0,
                    "sameVenueExecutionEligible": 1,
                    "total": 1,
                },
                "venueCoverage": {
                    "crossVenueCandidateCount": 1,
                    "quotedNodeCounts": {
                        "CLOUDBET": 2,
                        "SXBET": 2,
                    },
                },
            }

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.runner._probe_runtime",
            fake_probe_runtime,
        )

        class FakeTradingNode:
            def __init__(self, config):
                self.config = config

            def build(self):
                return None

            def dispose(self):
                return None

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", FakeTradingNode)

        result = runner_main(
            [
                "probe-runtime",
                "--manifest",
                str(manifest_path),
                "--require-rust-semantic-topology",
                "--min-cross-venue-candidates",
                "1",
                "--require-cross-venue-candidates-or-blockers",
                "--min-quoted-node-count",
                "CLOUDBET:2",
                "--min-quoted-node-count",
                "SXBET:2",
                "--allow-subscription-fallback",
            ],
        )

        assert result == 0
        assert observed_probe_kwargs["require_rust_semantic_topology"] is True
        assert observed_probe_kwargs["min_cross_venue_candidates"] == 1
        assert observed_probe_kwargs["require_cross_venue_candidates_or_blockers"] is True
        assert observed_probe_kwargs["min_quoted_node_counts"] == {
            "CLOUDBET": 2,
            "SXBET": 2,
        }
        assert observed_probe_kwargs["allow_subscription_fallback"] is True
        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "probed"
        assert payload["runtimeProbe"]["graphEngine"] == "rust"
        assert payload["runtimeProbe"]["topologySource"] == "rust_semantic"
        assert payload["runtimeProbe"]["connectedNodes"] == 2
        assert payload["runtimeProbe"]["semanticMatchInstruments"] == 2
        assert payload["runtimeProbe"]["quotedSemanticMatchInstruments"] == 2
        assert payload["runtimeProbe"]["positiveMarginCandidates"]["total"] == 1

    def test_probe_runtime_stops_before_dispose_on_coverage_error(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())

        monkeypatch.setattr(
            node_runner,
            "ensure_semantic_cache_ready",
            lambda _: SemanticCacheStatus(
                path=str(tmp_path / "semantic-cache"),
                source="existing",
                manifest_count=1,
                promoted_template_count=1,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
            ),
        )

        def _raise_coverage(**_kwargs):
            raise node_runner.RuntimeProbeCoverageError("no coverage", {"connectedNodes": 0})

        monkeypatch.setattr(node_runner, "_probe_runtime", _raise_coverage)

        events: list[str] = []

        class RunningTradingNode:
            instances: list["RunningTradingNode"] = []

            def __init__(self, config):
                self.config = config
                self._running = True
                type(self).instances.append(self)

            def build(self):
                return None

            def is_running(self):
                return self._running

            def stop(self):
                events.append("stop")
                self._running = False

            def dispose(self):
                events.append("dispose")
                if self._running:
                    raise RuntimeError("Cannot dispose a connected data client")

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", RunningTradingNode)

        with (
            caplog.at_level(logging.ERROR, logger=node_runner.__name__),
            pytest.raises(node_runner.RuntimeProbeCoverageError),
        ):
            runner_main(["probe-runtime", "--manifest", str(manifest_path)])

        # stop() must be called once (RUNNING -> STOPPED) before the first dispose().
        # dispose() may be called more than once (main() also disposes on the re-raise
        # after _handle_probe_runtime_command's own finally has cleaned up), but the
        # node is no longer RUNNING so the pyo3 abort is impossible.
        assert events[:2] == ["stop", "dispose"], events
        assert events.count("stop") == 1
        assert not any(
            "Cannot dispose a connected data client" in rec.getMessage() for rec in caplog.records
        )
        assert not any("node.dispose() failed" in rec.getMessage() for rec in caplog.records)
        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "failed"

    def test_probe_runtime_skips_stop_when_node_already_stopped(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())

        monkeypatch.setattr(
            node_runner,
            "ensure_semantic_cache_ready",
            lambda _: SemanticCacheStatus(
                path=str(tmp_path / "semantic-cache"),
                source="existing",
                manifest_count=1,
                promoted_template_count=1,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
            ),
        )

        def _raise_coverage(**_kwargs):
            raise node_runner.RuntimeProbeCoverageError("no coverage", {"connectedNodes": 0})

        monkeypatch.setattr(node_runner, "_probe_runtime", _raise_coverage)

        events: list[str] = []

        class StoppedTradingNode:
            def __init__(self, config):
                self.config = config

            def build(self):
                return None

            def is_running(self):
                return False

            def stop(self):
                events.append("stop")

            def dispose(self):
                events.append("dispose")

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", StoppedTradingNode)

        with pytest.raises(node_runner.RuntimeProbeCoverageError):
            runner_main(["probe-runtime", "--manifest", str(manifest_path)])

        # No stop() call — the node was never RUNNING from is_running()'s perspective.
        assert "stop" not in events
        assert events
        assert all(e == "dispose" for e in events)

    def test_semantic_probe_diagnostics_report_live_pattern_template_overlap(self):
        instrument = _instrument(
            venue="SXBET",
            market_type="total_goals",
            market_name="Total Goals",
            outcome="over",
            params="line=2.5",
            handicap=2.5,
        )

        class FakeGraph:
            nodes_by_id = {"node-1": SimpleNamespace(instrument=instrument)}

            @staticmethod
            def _semantic_template_payloads():
                params_key = json.dumps([["line", "2.5"]], separators=(",", ":"))
                return [
                    {
                        "template_id": "template-total-25",
                        "relationship_type": "COMPLEMENTARY_COVERAGE",
                        "provider_scope": ["SXBET"],
                        "venue_agnostic": False,
                        "confidence": 0.99,
                        "caveats": [],
                        "safety_tier": "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE",
                        "execution_safe": False,
                        "same_venue_execution_eligible": True,
                        "pattern_a": {
                            "sport": "soccer",
                            "scope": "full_time",
                            "market_type": "TOTALS",
                            "market_family": "TOTALS",
                            "selection": "OVER",
                            "params_key": params_key,
                        },
                        "pattern_b": {
                            "sport": "soccer",
                            "scope": "full_time",
                            "market_type": "TOTALS",
                            "market_family": "TOTALS",
                            "selection": "UNDER",
                            "params_key": params_key,
                        },
                    },
                ]

        diagnostics = node_runner._semantic_probe_diagnostics(FakeGraph())

        assert diagnostics["normalizedNodeCount"] == 1
        assert diagnostics["normalizationErrorCount"] == 0
        assert diagnostics["supportedProviderNodeCount"] == 1
        assert diagnostics["unsupportedProviderNodeCount"] == 0
        assert diagnostics["supportedProviderCoverageRatio"] == 1.0
        assert diagnostics["commonPatternKeyCount"] == 1
        assert diagnostics["unsupportedProviderPatternCount"] == 0
        assert diagnostics["unsupportedProviderPatterns"] == []
        assert diagnostics["unsupportedProviderPatternSamples"] == []
        assert diagnostics["nodeSports"] == [{"key": "soccer", "count": 1}]
        assert diagnostics["templateTierRelationships"] == [
            {
                "key": [
                    "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE",
                    "COMPLEMENTARY_COVERAGE",
                ],
                "count": 1,
            },
        ]
        assert diagnostics["sameVenueEligibleTemplates"][0]["templateId"] == "template-total-25"
        assert diagnostics["sameVenueEligibleTemplates"][0]["patternA"]["selection"] == "OVER"

    def test_semantic_probe_diagnostics_caps_template_sample_lists(self):
        instrument = _instrument(
            venue="SXBET",
            market_type="total_goals",
            market_name="Total Goals",
            outcome="over",
            params="line=2.5",
            handicap=2.5,
        )
        params_key = json.dumps([["line", "2.5"]], separators=(",", ":"))
        pattern = {
            "sport": "soccer",
            "scope": "full_time",
            "market_type": "TOTALS",
            "market_family": "TOTALS",
            "selection": "OVER",
            "params_key": params_key,
        }
        templates = [
            {
                "template_id": f"template-total-{index:02d}",
                "relationship_type": "COMPLEMENTARY_COVERAGE",
                "provider_scope": ["SXBET"],
                "venue_agnostic": False,
                "confidence": 0.99,
                "caveats": [],
                "safety_tier": "EXECUTION_SAFE",
                "execution_safe": True,
                "same_venue_execution_eligible": True,
                "pattern_a": pattern,
                "pattern_b": {**pattern, "selection": "UNDER"},
            }
            for index in reversed(range(node_runner._TEMPLATE_SAMPLE_LIMIT + 5))
        ]

        class FakeGraph:
            nodes_by_id = {"node-1": SimpleNamespace(instrument=instrument)}

            @staticmethod
            def _semantic_template_payloads():
                return templates

        diagnostics = node_runner._semantic_probe_diagnostics(FakeGraph())

        assert diagnostics["templateCount"] == len(templates)
        for key in ("executionSafeTemplates", "sameVenueEligibleTemplates"):
            sampled = diagnostics[key]
            assert len(sampled) == node_runner._TEMPLATE_SAMPLE_LIMIT
            assert sampled[0]["templateId"] == "template-total-00"
            assert [item["templateId"] for item in sampled] == sorted(
                item["templateId"] for item in sampled
            )

    def test_semantic_probe_diagnostics_reports_unsupported_provider_patterns(self):
        unsupported_instrument = _instrument(
            venue="POLYMARKET",
            market_type="totals",
            market_name="TOTALS",
            outcome="over",
            params="line=3.5",
            sport_name="soccer",
        )

        class FakeGraph:
            nodes_by_id = {
                "node-1": SimpleNamespace(
                    instrument=unsupported_instrument,
                    canonical_event_key=unsupported_instrument.event_key(include_start_time=True),
                ),
            }

            @staticmethod
            def _semantic_template_payloads():
                return []

        diagnostics = node_runner._semantic_probe_diagnostics(FakeGraph())

        assert diagnostics["supportedProviderNodeCount"] == 0
        assert diagnostics["unsupportedProviderNodeCount"] == 1
        assert diagnostics["supportedProviderCoverageRatio"] == 0.0
        assert diagnostics["unsupportedProviderPatternCount"] == 1
        assert diagnostics["unsupportedProviderPatterns"] == [
            {
                "key": [
                    "POLYMARKET",
                    "soccer",
                    "full_time",
                    "TOTALS",
                    "TOTALS",
                    "OVER",
                    '[["line","3.5"]]',
                ],
                "count": 1,
            },
        ]
        assert diagnostics["unsupportedProviderPatternSamples"][0]["provider"] == "POLYMARKET"
        assert diagnostics["unsupportedProviderPatternSamples"][0]["selection"] == "OVER"
        assert diagnostics["unsupportedProviderPatternSamples"][0]["samples"][0][
            "instrumentId"
        ] == str(unsupported_instrument.id)

    def test_probe_graph_snapshot_retries_transient_mutation_error(self, monkeypatch):  # skipcq
        # A large graph being rebuilt races the probe into "dictionary changed size during
        # iteration"; the snapshot must retry rather than collapse to an empty payload.
        class _FlakyDict(dict):
            def __init__(self, *args, fail_times=0, **kwargs):
                super().__init__(*args, **kwargs)
                self._fail_remaining = fail_times

            def values(self):
                if self._fail_remaining > 0:
                    self._fail_remaining -= 1
                    raise RuntimeError("dictionary changed size during iteration")
                return super().values()

        graph = SimpleNamespace(
            edges_by_id=_FlakyDict({"e1": object()}, fail_times=2),
            nodes_by_id={"n1": object()},
            quotes_by_node_id={},
            edge_ids_by_node_id={"n1": {"e1"}},
        )
        monkeypatch.setattr(node_runner.time, "sleep", lambda *_: None)

        snapshot = node_runner._snapshot_probe_graph_state(graph, attempts=5)

        assert snapshot is not None
        assert len(list(snapshot["edges"])) == 1

    def test_probe_graph_snapshot_gives_up_after_attempts(self, monkeypatch, caplog):  # skipcq
        class _AlwaysFlakyDict(dict):
            def values(self):
                raise RuntimeError("dictionary changed size during iteration")

        graph = SimpleNamespace(
            edges_by_id=_AlwaysFlakyDict({"e1": object()}),
            nodes_by_id={"n1": object()},
            quotes_by_node_id={},
            edge_ids_by_node_id={},
        )
        monkeypatch.setattr(node_runner.time, "sleep", lambda *_: None)

        with caplog.at_level(logging.WARNING, logger=node_runner.__name__):
            snapshot = node_runner._snapshot_probe_graph_state(graph, attempts=3)

        assert snapshot is None
        assert "failed after 3 attempts" in caplog.text

    def test_runtime_probe_writer_survives_collection_error(self, tmp_path, monkeypatch):  # skipcq
        # A daemon-thread exception would freeze status.json; the writer must swallow a
        # collection error, log it, and stay alive for the next cycle.
        class _WaitOnceEvent:
            def __init__(self):
                self._calls = 0

            def wait(self, _timeout):
                self._calls += 1
                return self._calls > 1  # run the body once, then stop

        def _raise_collect(*_args, **_kwargs):
            raise RuntimeError("collection boom")

        monkeypatch.setattr(node_runner, "_collect_runtime_probe_payload", _raise_collect)
        writer = node_runner.RuntimeProbeStatusWriter(
            status_path=tmp_path / "status.json",
            manifest=SimpleNamespace(strategy=SimpleNamespace(min_profit_margin="0.02")),
            strategy=SimpleNamespace(),
            semantic_cache=None,
            manifest_snapshot=tmp_path / "manifest.json",
            rendered_config_path=tmp_path / "rendered.json",
            heartbeat_path=tmp_path / "heartbeat.json",
            interval_secs=0.0,
            stop_event=_WaitOnceEvent(),
        )

        writer.run()  # must return without propagating the collection error

    def test_runtime_probe_throttles_expensive_diagnostics_within_interval(self, monkeypatch):
        # The status writer runs every heartbeat, but the semantic/coverage diagnostics do
        # O(graph) work. They must recompute at most once per interval and be reused between
        # so the writer releases the GIL instead of starving the venue quote-poll loops.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )

        semantic_calls = {"count": 0}
        coverage_calls = {"count": 0}

        def _spy_semantic(_graph):
            semantic_calls["count"] += 1
            return {"available": True, "call": semantic_calls["count"]}

        def _spy_coverage(*_args, **_kwargs):
            coverage_calls["count"] += 1
            return {"call": coverage_calls["count"]}

        monkeypatch.setattr(node_runner, "_semantic_probe_diagnostics", _spy_semantic)
        monkeypatch.setattr(
            node_runner,
            "_probe_coverage_book_devig_diagnostics",
            _spy_coverage,
        )

        now = [1_000.0]
        throttle = node_runner._RuntimeProbeDiagnosticsThrottle(90.0, clock=lambda: now[0])

        def _collect():
            return node_runner._collect_runtime_probe_payload(
                strategy,
                min_profit_margin=Decimal("0.02"),
                elapsed_seconds=now[0] - 1_000.0,
                diagnostics=throttle,
            )

        first = _collect()
        assert semantic_calls["count"] == 1
        assert coverage_calls["count"] == 1

        now[0] += 30.0  # within the 90s interval
        second = _collect()
        assert semantic_calls["count"] == 1  # reused, not recomputed
        assert coverage_calls["count"] == 1
        assert second["semanticDiagnostics"] == first["semanticDiagnostics"]

        now[0] += 65.0  # 95s since the last recompute -> interval elapsed
        third = _collect()
        assert semantic_calls["count"] == 2
        assert coverage_calls["count"] == 2
        assert third["semanticDiagnostics"]["call"] == 2

    def test_runtime_probe_refreshes_trading_fields_every_cycle(self, monkeypatch):
        # Trading-relevant fields stay fresh every heartbeat even while the expensive
        # semantic section is throttled and reused.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )

        base_stats = strategy.get_stats()
        cycle = {"n": 0}

        def _fake_get_stats():
            stats = dict(base_stats)
            stats["provider_quote_poll_stats"] = {"SXBET": {"cycles": cycle["n"]}}
            return stats

        monkeypatch.setattr(strategy, "get_stats", _fake_get_stats)

        semantic_calls = {"count": 0}

        def _spy_semantic(_graph):
            semantic_calls["count"] += 1
            return {"available": True, "call": semantic_calls["count"]}

        monkeypatch.setattr(node_runner, "_semantic_probe_diagnostics", _spy_semantic)

        now = [0.0]
        throttle = node_runner._RuntimeProbeDiagnosticsThrottle(3_600.0, clock=lambda: now[0])

        poll_stats_seen = []
        semantic_seen = []
        for _ in range(3):
            cycle["n"] += 1
            now[0] += 5.0  # a heartbeat apart, well inside the interval
            payload = node_runner._collect_runtime_probe_payload(
                strategy,
                min_profit_margin=Decimal("0.02"),
                elapsed_seconds=now[0],
                diagnostics=throttle,
            )
            poll_stats_seen.append(payload["providerQuotePollStats"])
            semantic_seen.append(payload["semanticDiagnostics"])

        assert poll_stats_seen == [
            {"SXBET": {"cycles": 1}},
            {"SXBET": {"cycles": 2}},
            {"SXBET": {"cycles": 3}},
        ]
        # Expensive section computed once and reused across the three heartbeats.
        assert semantic_calls["count"] == 1
        assert all(seen == semantic_seen[0] for seen in semantic_seen)

    def test_runtime_probe_large_graph_skips_heavy_sections_per_heartbeat(self, monkeypatch):
        # Perf sanity: on a large graph the O(graph) semantic/coverage functions must not run
        # on every heartbeat. Over 20 heartbeats (100s) with a 90s interval they run twice,
        # not twenty times -- so the quote path cost is independent of graph size.
        strategy = BettingArbitrageStrategy(
            config=BettingArbitrageConfig(enabled_venues=frozenset(["SXBET"])),
        )
        base_stats = strategy.get_stats()

        def _fake_get_stats():
            stats = dict(base_stats)
            stats["opportunity_graph_nodes"] = 39_010
            return stats

        monkeypatch.setattr(strategy, "get_stats", _fake_get_stats)

        heavy_calls = {"semantic": 0, "coverage": 0}

        def _spy_semantic(_graph):
            heavy_calls["semantic"] += 1
            return {"available": True}

        def _spy_coverage(*_args, **_kwargs):
            heavy_calls["coverage"] += 1
            return {}

        monkeypatch.setattr(node_runner, "_semantic_probe_diagnostics", _spy_semantic)
        monkeypatch.setattr(
            node_runner,
            "_probe_coverage_book_devig_diagnostics",
            _spy_coverage,
        )

        now = [0.0]
        throttle = node_runner._RuntimeProbeDiagnosticsThrottle(90.0, clock=lambda: now[0])
        heartbeats = 20
        last_payload = None
        for _ in range(heartbeats):
            now[0] += 5.0
            last_payload = node_runner._collect_runtime_probe_payload(
                strategy,
                min_profit_margin=Decimal("0.02"),
                elapsed_seconds=now[0],
                diagnostics=throttle,
            )

        assert last_payload["graphNodes"] == 39_010
        assert heavy_calls["semantic"] == 2
        assert heavy_calls["coverage"] == 2

    def test_runtime_probe_venue_coverage_explains_zero_cross_venue_pairs(self):
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
        )
        cloudbet_instrument = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(
                enabled_venues=frozenset({"CLOUDBET", "POLYMARKET", "SXBET"}),
                semantic_quote_subscription_limit_by_venue={"CLOUDBET": 80, "SXBET": 120},
            ),
            _quote_subscribed_instrument_ids={
                sxbet_instrument.id,
                cloudbet_instrument.id,
            },
        )
        nodes = {
            "sxbet-node": SimpleNamespace(
                instrument=sxbet_instrument,
            ),
            "cloudbet-node": SimpleNamespace(
                instrument=cloudbet_instrument,
            ),
        }
        edges = [
            SimpleNamespace(
                source_node_id="sxbet-node",
                target_node_id="cloudbet-node",
            ),
        ]

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=edges,
            nodes=nodes,
            quotes={"sxbet-node": object()},
            matched_node_ids={"sxbet-node", "cloudbet-node"},
            candidate_venue_pairs={"SXBET->SXBET": {"positive": 1}},
        )

        assert coverage["enabledVenues"] == ["CLOUDBET", "POLYMARKET", "SXBET"]
        assert coverage["nodeCounts"] == {"CLOUDBET": 1, "POLYMARKET": 0, "SXBET": 1}
        assert coverage["eventKeyCounts"] == {"CLOUDBET": 1, "POLYMARKET": 0, "SXBET": 1}
        assert coverage["eventSportCounts"] == {
            "CLOUDBET": {"soccer": 1},
            "POLYMARKET": {},
            "SXBET": {"soccer": 1},
        }
        assert coverage["quoteSubscriptionCounts"] == {
            "CLOUDBET": 1,
            "POLYMARKET": 0,
            "SXBET": 1,
        }
        assert coverage["quoteSubscriptionLimits"] == {"CLOUDBET": 80, "SXBET": 120}
        assert coverage["quoteSubscriptionLimitExceededCounts"] == {}
        assert coverage["quoteSubscriptionGapCounts"] == {
            "CLOUDBET": 1,
            "POLYMARKET": 0,
            "SXBET": 0,
        }
        assert coverage["venuesWithSubscriptionQuoteGap"] == ["CLOUDBET"]
        assert coverage["quotedNodeCounts"] == {
            "CLOUDBET": 0,
            "POLYMARKET": 0,
            "SXBET": 1,
        }
        assert coverage["unquotedSemanticMatchedNodeCounts"] == {
            "CLOUDBET": 1,
            "POLYMARKET": 0,
            "SXBET": 0,
        }
        assert coverage["unquotedSemanticMatchedNodeSamples"]["CLOUDBET"] == [
            {
                "instrumentId": str(cloudbet_instrument.id),
                "eventKey": cloudbet_instrument.event_key(include_start_time=False),
                "pattern": {
                    "marketFamily": "MATCH_ODDS",
                    "marketType": "MATCH_ODDS",
                    "paramsKey": "[]",
                    "scope": "full_time",
                    "selection": "AWAY",
                    "sport": "soccer",
                },
            },
        ]
        assert coverage["edgeCounts"]["SXBET->CLOUDBET"] == 1
        assert coverage["quotedEdgeCounts"]["SXBET->CLOUDBET"] == 0
        assert coverage["candidateCounts"]["SXBET->SXBET"] == 1
        assert coverage["crossVenueCandidateCount"] == 0
        readiness = {item["venuePair"]: item for item in coverage["crossVenueQuoteReadiness"]}
        assert readiness["SXBET->CLOUDBET"]["status"] == "common_fixture_unquoted"
        assert readiness["SXBET->CLOUDBET"]["commonEventKeyCount"] >= 1
        assert readiness["SXBET->CLOUDBET"]["fullyQuotedCommonEventKeyCount"] == 0
        assert readiness["SXBET->CLOUDBET"]["quotedEdgeCount"] == 0
        assert readiness["SXBET->CLOUDBET"]["candidateCount"] == 0
        assert readiness["CLOUDBET->POLYMARKET"]["status"] == "missing_instruments"
        zero_reasons = {
            item["venuePair"]: item["reason"] for item in coverage["zeroCandidateVenuePairs"]
        }
        assert zero_reasons["SXBET->CLOUDBET"] == "no_quoted_semantic_edge"
        assert zero_reasons["CLOUDBET->POLYMARKET"] == "missing_instruments"
        zero_reports = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}
        assert zero_reports["SXBET->CLOUDBET"]["blockerReason"] == (
            "quotes_missing_for_semantic_edges"
        )
        assert zero_reports["SXBET->CLOUDBET"]["sourceNodeCount"] == 1
        assert zero_reports["SXBET->CLOUDBET"]["targetNodeCount"] == 1
        assert zero_reports["SXBET->CLOUDBET"]["edgeCount"] == 1
        assert zero_reports["SXBET->CLOUDBET"]["quotedEdgeCount"] == 0
        assert zero_reports["SXBET->CLOUDBET"]["candidateCount"] == 0
        assert zero_reports["SXBET->CLOUDBET"]["commonEventKeyCount"] >= 1
        assert zero_reports["SXBET->CLOUDBET"]["fullyQuotedCommonEventKeyCount"] == 0
        assert zero_reports["SXBET->CLOUDBET"]["sourceQuotedCommonEventKeyCount"] >= 1
        assert zero_reports["SXBET->CLOUDBET"]["targetQuotedCommonEventKeyCount"] == 0
        assert (
            sxbet_instrument.event_key(include_start_time=False)
            in zero_reports["SXBET->CLOUDBET"]["commonEventKeySamples"]
        )
        assert zero_reports["SXBET->CLOUDBET"]["unquotedCommonEventKeySamples"][0]["sourceQuoted"]
        assert not zero_reports["SXBET->CLOUDBET"]["unquotedCommonEventKeySamples"][0][
            "targetQuoted"
        ]
        assert zero_reports["SXBET->CLOUDBET"]["sampleBlockerCounts"] == {}
        assert zero_reports["SXBET->CLOUDBET"]["samples"][0]["marketFamily"] == (
            "MATCH_ODDS + MATCH_ODDS"
        )

    def test_runtime_probe_quote_observation_state_flags_subscribed_without_quotes(self):
        venue_coverage = {
            "quoteSubscriptionCounts": {
                "CLOUDBET": 80,
                "POLYMARKET": 92,
                "SXBET": 120,
            },
            "quotedNodeCounts": {
                "CLOUDBET": 0,
                "POLYMARKET": 0,
                "SXBET": 0,
            },
            "quoteSubscriptionGapCounts": {
                "CLOUDBET": 80,
                "POLYMARKET": 92,
                "SXBET": 120,
            },
            "quotedSemanticMatchedNodeCounts": {
                "CLOUDBET": 0,
                "POLYMARKET": 0,
                "SXBET": 0,
            },
            "unquotedSemanticMatchedNodeCounts": {
                "CLOUDBET": 1815,
                "POLYMARKET": 92,
                "SXBET": 328,
            },
            "venuesWithSubscriptionQuoteGap": ["CLOUDBET", "POLYMARKET", "SXBET"],
            "unquotedSemanticMatchedNodeSamples": {
                "POLYMARKET": [{"instrumentId": "poly-1"}],
            },
        }
        stats = {
            "provider_quote_poll_stats": {
                "CLOUDBET": {"polls": 0},
                "SXBET": {"polls": 0},
            },
            "opportunity_graph_quote_states": 0,
            "subscribed_instruments": 292,
            "instrument_cache_miss": 7,
            "quote_odds_rejected": 3,
            "instrument_cache_miss_by_venue": {"CLOUDBET": 7},
            "quote_odds_rejected_by_venue": {"SXBET": 3},
        }

        state = node_runner._probe_quote_observation_state(stats, venue_coverage)

        assert state["status"] == "subscribed_but_no_quotes"
        assert state["health"] == "fail"
        assert state["totalQuoteSubscriptions"] == 292
        assert state["totalQuotedNodes"] == 0
        assert state["totalQuoteSubscriptionGaps"] == 292
        assert state["venuesWithSubscriptionQuoteGap"] == ["CLOUDBET", "POLYMARKET", "SXBET"]
        assert state["providerQuotePollStats"]["CLOUDBET"] == {"polls": 0}
        assert state["unquotedSemanticMatchedNodeSamples"]["POLYMARKET"][0]["instrumentId"] == (
            "poly-1"
        )
        assert state["instrumentCacheMiss"] == 7
        assert state["quoteOddsRejected"] == 3
        assert state["instrumentCacheMissCounts"] == {"CLOUDBET": 7}
        assert state["quoteOddsRejectedCounts"] == {"SXBET": 3}

    def test_venue_pair_coverage_reports_no_common_fixture_without_false_pair_samples(self):
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
            event_id="event-sxbet",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
        )
        cloudbet_instrument = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
            event_id="event-cloudbet",
            event_name="Team C vs Team D",
            home_name="Team C",
            away_name="Team D",
            sport_name="basketball",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"CLOUDBET", "SXBET"})),
            _quote_subscribed_instrument_ids={sxbet_instrument.id, cloudbet_instrument.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "sxbet-node": SimpleNamespace(instrument=sxbet_instrument),
                "cloudbet-node": SimpleNamespace(instrument=cloudbet_instrument),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        reports = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}
        report = reports["SXBET->CLOUDBET"]
        assert report["reason"] == "no_semantic_edge"
        assert report["blockerReason"] == "no_common_fixture"
        assert report["sourceNodeCount"] == 1
        assert report["targetNodeCount"] == 1
        assert report["edgeCount"] == 0
        assert report["quotedEdgeCount"] == 0
        assert report["candidateCount"] == 0
        assert report["commonEventKeyCount"] == 0
        assert report["fullyQuotedCommonEventKeyCount"] == 0
        assert report["sourceQuotedCommonEventKeyCount"] == 0
        assert report["targetQuotedCommonEventKeyCount"] == 0
        assert report["unquotedCommonEventKeySamples"] == []
        assert report["discoveryGapReason"] == "no_common_fixture_loaded"
        assert report["sourceEventSportCounts"] == {"soccer": 1}
        assert report["targetEventSportCounts"] == {"basketball": 1}
        assert report["sampleBlockerCounts"] == {}
        assert report["samples"] == []
        assert report["fixtureDiscoveryBlockerCounts"]
        assert report["fixtureDiscoverySamples"][0]["fixtureIdentityProof"]["sameFixture"] is False
        assert report["fixtureDiscoverySamples"][0]["eventKeyA"] == "soccer:a:b"
        assert report["fixtureDiscoverySamples"][0]["eventKeyB"] == "basketball:c:d"

    def test_venue_pair_coverage_canonicalizes_provider_fixture_aliases(self):
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
            event_id="sxbet-cle-min",
            event_name="CLE Cavaliers vs MIN Timberwolves",
            home_name="CLE Cavaliers",
            away_name="MIN Timberwolves",
            sport_name="basketball",
        )
        cloudbet_instrument = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
            event_id="cloudbet-cle-min",
            event_name="Cleveland Cavaliers vs Minnesota Timberwolves",
            home_name="Cleveland Cavaliers",
            away_name="Minnesota Timberwolves",
            sport_name="basketball",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"CLOUDBET", "SXBET"})),
            _quote_subscribed_instrument_ids={sxbet_instrument.id, cloudbet_instrument.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "sxbet-node": SimpleNamespace(instrument=sxbet_instrument),
                "cloudbet-node": SimpleNamespace(instrument=cloudbet_instrument),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        report = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}[
            "SXBET->CLOUDBET"
        ]
        assert report["reason"] == "no_semantic_edge"
        assert report["blockerReason"] == "no_semantic_edge"
        assert report["commonEventKeyCount"] >= 1
        assert (
            "basketball:cleveland cavaliers:minnesota timberwolves"
            in report["commonEventKeySamples"]
        )
        assert report["samples"][0]["canonicalEventKeyA"] == (
            "basketball:cleveland cavaliers:minnesota timberwolves"
        )
        assert report["samples"][0]["canonicalEventKeyB"] == (
            "basketball:cleveland cavaliers:minnesota timberwolves"
        )

    def test_venue_pair_coverage_indexes_common_fixtures_beyond_sample_window(self):
        common_sxbet = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
            event_id="sxbet-common",
            event_name="Shared Team A vs Shared Team B",
            home_name="Shared Team A",
            away_name="Shared Team B",
        )
        common_cloudbet = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
            event_id="cloudbet-common",
            event_name="Shared Team A vs Shared Team B",
            home_name="Shared Team A",
            away_name="Shared Team B",
        )
        nodes: dict[str, SimpleNamespace] = {}
        for index in range(45):
            nodes[f"sxbet-filler-{index}"] = SimpleNamespace(
                instrument=_instrument(
                    venue="SXBET",
                    market_type="match_odds",
                    outcome="home",
                    event_id=f"sxbet-filler-{index}",
                    event_name=f"SXBET Home {index} vs SXBET Away {index}",
                    home_name=f"SXBET Home {index}",
                    away_name=f"SXBET Away {index}",
                ),
            )
            nodes[f"cloudbet-filler-{index}"] = SimpleNamespace(
                instrument=_instrument(
                    venue="CLOUDBET",
                    market_type="match_odds",
                    outcome="away",
                    event_id=f"cloudbet-filler-{index}",
                    event_name=f"Cloudbet Home {index} vs Cloudbet Away {index}",
                    home_name=f"Cloudbet Home {index}",
                    away_name=f"Cloudbet Away {index}",
                ),
            )
        nodes["sxbet-common"] = SimpleNamespace(instrument=common_sxbet)
        nodes["cloudbet-common"] = SimpleNamespace(instrument=common_cloudbet)
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"CLOUDBET", "SXBET"})),
            _quote_subscribed_instrument_ids={common_sxbet.id, common_cloudbet.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes=nodes,
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        reports = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}
        report = reports["SXBET->CLOUDBET"]
        assert report["reason"] == "no_semantic_edge"
        assert report["blockerReason"] == "no_semantic_edge"
        assert report["commonEventKeyCount"] >= 1
        assert report["samples"][0]["eventKeyA"] == common_sxbet.event_key(
            include_start_time=False,
        )
        assert report["samples"][0]["eventKeyB"] == common_cloudbet.event_key(
            include_start_time=False,
        )

    def test_venue_pair_coverage_does_not_treat_same_name_different_start_as_common_fixture(self):
        polymarket_instrument = _instrument(
            venue="POLYMARKET",
            market_type="totals",
            outcome="under",
            event_id="poly-leeds-spurs",
            event_name="Leeds United vs Tottenham Hotspur",
            home_name="Leeds United",
            away_name="Tottenham Hotspur",
            sport_name="soccer",
            start_time="2026-03-13T18:00:00Z",
        )
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="totals",
            outcome="over",
            event_id="sxbet-leeds-spurs",
            event_name="Leeds United vs Tottenham Hotspur",
            home_name="Leeds United",
            away_name="Tottenham Hotspur",
            sport_name="soccer",
            start_time="2026-03-26T08:00:00Z",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"POLYMARKET", "SXBET"})),
            _quote_subscribed_instrument_ids={polymarket_instrument.id, sxbet_instrument.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "poly-node": SimpleNamespace(instrument=polymarket_instrument),
                "sxbet-node": SimpleNamespace(instrument=sxbet_instrument),
            },
            quotes={"poly-node": object(), "sxbet-node": object()},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        report = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}[
            "POLYMARKET->SXBET"
        ]
        assert report["commonEventKeyCount"] >= 1
        assert report["fullyQuotedCommonEventKeyCount"] >= 1
        assert report["verifiedCommonFixtureSampleCount"] == 0
        assert report["fixtureProofBlockerCounts"] == {"start_time_mismatch": 1}
        assert coverage["zeroCandidateFixtureProofBlockerCounts"] == {
            "start_time_mismatch": 2,
        }
        assert report["blockerReason"] == "no_common_fixture"
        assert report["discoveryGapReason"] == "common_event_aliases_failed_fixture_proof"
        assert report["samples"][0]["fixtureIdentityProof"]["sameFixture"] is False
        assert report["samples"][0]["fixtureIdentityProof"]["reason"] == "start_time_mismatch"
        assert report["samples"][0]["fixtureStartTimeA"] == "2026-03-13T18:00:00Z"
        assert report["samples"][0]["fixtureStartTimeB"] == "2026-03-26T08:00:00Z"
        assert "soccer:leeds united:tottenham hotspur" in report["samples"][0]["eventAliasKeysA"]
        assert "soccer:leeds united:tottenham hotspur" in report["samples"][0]["eventAliasKeysB"]

    def test_venue_pair_coverage_allows_cross_venue_short_start_time_drift(self):
        polymarket_instrument = _instrument(
            venue="POLYMARKET",
            market_type="moneyline_2way",
            outcome="no",
            event_id="poly-felix-navone",
            event_name="Felix Auger-Aliassime vs Mariano Navone",
            home_name="Felix Auger-Aliassime",
            away_name="Mariano Navone",
            sport_name="tennis",
            start_time="2026-03-13T18:00:00Z",
        )
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
            event_id="sxbet-felix-navone",
            event_name="Felix Auger Aliassime vs Mariano Navone",
            home_name="Felix Auger Aliassime",
            away_name="Mariano Navone",
            sport_name="tennis",
            start_time="2026-03-13T22:00:00Z",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"POLYMARKET", "SXBET"})),
            _quote_subscribed_instrument_ids={polymarket_instrument.id, sxbet_instrument.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "poly-node": SimpleNamespace(instrument=polymarket_instrument),
                "sxbet-node": SimpleNamespace(instrument=sxbet_instrument),
            },
            quotes={"poly-node": object(), "sxbet-node": object()},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        report = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}[
            "POLYMARKET->SXBET"
        ]
        assert report["commonEventKeyCount"] >= 1
        assert report["fullyQuotedCommonEventKeyCount"] >= 1
        assert report["verifiedCommonFixtureSampleCount"] >= 1
        assert report["fixtureProofBlockerCounts"] == {}
        assert coverage["zeroCandidateFixtureProofBlockerCounts"] == {}
        assert report["blockerReason"] == "no_semantic_edge"
        assert report["samples"][0]["fixtureIdentityProof"]["sameFixture"] is True
        assert (
            report["samples"][0]["fixtureIdentityProof"]["reason"]
            == "canonical_fixture_match_start_time_conflict"
        )

    def test_venue_pair_coverage_uses_fixture_aliases_for_noisy_polymarket_names(self):
        polymarket_instrument = _instrument(
            venue="POLYMARKET",
            market_type="match_odds",
            outcome="home",
            event_id="poly-arsenal-west-ham",
            event_name="Arsenal Exact Score vs West Ham United",
            home_name="Arsenal Exact Score",
            away_name="West Ham United",
            sport_name="soccer",
        )
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="away",
            event_id="sxbet-arsenal-west-ham",
            event_name="Arsenal vs West Ham United",
            home_name="Arsenal",
            away_name="West Ham United",
            sport_name="soccer",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"POLYMARKET", "SXBET"})),
            _quote_subscribed_instrument_ids={polymarket_instrument.id, sxbet_instrument.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "poly-node": SimpleNamespace(instrument=polymarket_instrument),
                "sxbet-node": SimpleNamespace(instrument=sxbet_instrument),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        report = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}[
            "POLYMARKET->SXBET"
        ]
        assert report["blockerReason"] == "no_semantic_edge"
        assert "soccer:arsenal:west ham united" in report["commonEventKeySamples"]
        assert report["samples"][0]["canonicalEventKeyA"] == ("soccer:arsenal:west ham united")
        assert report["samples"][0]["canonicalEventKeyB"] == ("soccer:arsenal:west ham united")

    def test_venue_pair_coverage_respects_execution_venue_mode(self):
        sxbet_home = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
            event_id="sxbet-home",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
        )
        sxbet_away = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="away",
            event_id="sxbet-away",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
        )
        polymarket_home = _instrument(
            venue="POLYMARKET",
            market_type="match_odds",
            outcome="home",
            event_id="poly-home",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(
                enabled_venues=frozenset({"POLYMARKET", "SXBET"}),
                execution_venue_mode="cross_venue",
            ),
            _quote_subscribed_instrument_ids={sxbet_home.id, sxbet_away.id, polymarket_home.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "sxbet-home": SimpleNamespace(instrument=sxbet_home),
                "sxbet-away": SimpleNamespace(instrument=sxbet_away),
                "poly-home": SimpleNamespace(instrument=polymarket_home),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        assert set(coverage["edgeCounts"]) == {"POLYMARKET->SXBET", "SXBET->POLYMARKET"}
        assert {item["venuePair"] for item in coverage["zeroCandidateVenuePairs"]} == {
            "POLYMARKET->SXBET",
            "SXBET->POLYMARKET",
        }

        strategy._config.execution_venue_mode = "same_venue"
        same_venue_coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "sxbet-home": SimpleNamespace(instrument=sxbet_home),
                "sxbet-away": SimpleNamespace(instrument=sxbet_away),
                "poly-home": SimpleNamespace(instrument=polymarket_home),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        assert set(same_venue_coverage["edgeCounts"]) == {
            "POLYMARKET->POLYMARKET",
            "SXBET->SXBET",
        }

    def test_venue_pair_coverage_infers_same_market_params_mismatch_from_samples(self):
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="totals",
            market_name="TOTALS",
            outcome="over",
            params="line=2.5",
            event_id="event-1",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
        )
        cloudbet_instrument = _instrument(
            venue="CLOUDBET",
            market_type="totals",
            market_name="TOTALS",
            outcome="under",
            params="line=3.5",
            event_id="event-2",
            event_name="Team A vs Team B",
            home_name="Team A",
            away_name="Team B",
            sport_name="soccer",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"CLOUDBET", "SXBET"})),
            _quote_subscribed_instrument_ids={sxbet_instrument.id, cloudbet_instrument.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "sxbet-node": SimpleNamespace(instrument=sxbet_instrument),
                "cloudbet-node": SimpleNamespace(instrument=cloudbet_instrument),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        reports = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}
        report = reports["SXBET->CLOUDBET"]
        assert report["reason"] == "no_semantic_edge"
        assert report["blockerReason"] == "same_market_params_mismatch"
        assert report["sampleBlockerCounts"] == {"same_market_params_mismatch": 1}
        assert report["samples"][0]["blockerHint"] == "same_market_params_mismatch"
        assert report["samples"][0]["matcherSuspectReason"] == "same_market_params_mismatch"

    def test_venue_pair_coverage_prioritizes_comparable_common_fixture_samples(self):
        polymarket_spread = _instrument(
            venue="POLYMARKET",
            market_type="basketball.spread",
            market_name="basketball.spread_binary",
            outcome="home",
            params="line=-7.5",
            sport_name="basketball",
        )
        polymarket_total = _instrument(
            venue="POLYMARKET",
            market_type="totals",
            market_name="TOTALS",
            outcome="over",
            params="line=174.5",
            sport_name="basketball",
        )
        sxbet_period_winner = _instrument(
            venue="SXBET",
            market_type="match_odds",
            market_name="MATCH_ODDS",
            outcome="home",
            params="period=p1",
            sport_name="basketball",
        )
        sxbet_total = _instrument(
            venue="SXBET",
            market_type="totals",
            market_name="TOTALS",
            outcome="under",
            params="line=171",
            sport_name="basketball",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"POLYMARKET", "SXBET"})),
            _quote_subscribed_instrument_ids={
                polymarket_spread.id,
                polymarket_total.id,
                sxbet_period_winner.id,
                sxbet_total.id,
            },
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "poly-spread": SimpleNamespace(instrument=polymarket_spread),
                "sxbet-period-winner": SimpleNamespace(instrument=sxbet_period_winner),
                "poly-total": SimpleNamespace(instrument=polymarket_total),
                "sxbet-total": SimpleNamespace(instrument=sxbet_total),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        report = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}[
            "POLYMARKET->SXBET"
        ]
        assert report["blockerReason"] == "same_market_params_mismatch"
        assert report["samples"][0]["marketFamily"] == "TOTALS + TOTALS"
        assert report["samples"][0]["blockerHint"] == "same_market_params_mismatch"

    def test_venue_pair_coverage_samples_deep_common_fixture_market_families(self):
        polymarket_decoys = [
            _instrument(
                venue="POLYMARKET",
                market_type="basketball.spread",
                market_name="basketball.spread_binary",
                outcome="home",
                params=f"line=-{index + 1}.5",
                handicap=-(index + 1.5),
                sport_name="basketball",
            )
            for index in range(45)
        ]
        polymarket_total = _instrument(
            venue="POLYMARKET",
            market_type="totals",
            market_name="TOTALS",
            outcome="over",
            params="line=174.5",
            sport_name="basketball",
        )
        sxbet_total = _instrument(
            venue="SXBET",
            market_type="totals",
            market_name="TOTALS",
            outcome="under",
            params="line=171",
            sport_name="basketball",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"POLYMARKET", "SXBET"})),
            _quote_subscribed_instrument_ids={
                *(instrument.id for instrument in polymarket_decoys),
                polymarket_total.id,
                sxbet_total.id,
            },
        )
        nodes = {
            **{
                f"poly-spread-{index}": SimpleNamespace(instrument=instrument)
                for index, instrument in enumerate(polymarket_decoys)
            },
            "poly-total": SimpleNamespace(instrument=polymarket_total),
            "sxbet-total": SimpleNamespace(instrument=sxbet_total),
        }

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes=nodes,
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        report = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}[
            "POLYMARKET->SXBET"
        ]
        assert report["samples"][0]["marketFamily"] == "TOTALS + TOTALS"
        assert report["samples"][0]["blockerHint"] == "same_market_params_mismatch"

    def test_venue_pair_coverage_reports_polymarket_corners_as_provider_scope_mismatch(self):
        polymarket_instrument = _instrument(
            venue="POLYMARKET",
            market_type="soccer.totals",
            market_name="soccer.totals_binary",
            outcome="over",
            params="line=12.5&subject=corners",
            event_id="poly-arsenal-west-ham-corners",
            event_name="Arsenal Total Corners vs West Ham United",
            home_name="Arsenal Total Corners",
            away_name="West Ham United",
            sport_name="soccer",
        )
        sxbet_instrument = _instrument(
            venue="SXBET",
            market_type="totals",
            market_name="TOTALS",
            outcome="under",
            params="line=2.5",
            event_id="sxbet-arsenal-west-ham-goals",
            event_name="Arsenal vs West Ham United",
            home_name="Arsenal",
            away_name="West Ham United",
            sport_name="soccer",
        )
        strategy = SimpleNamespace(
            _config=SimpleNamespace(enabled_venues=frozenset({"POLYMARKET", "SXBET"})),
            _quote_subscribed_instrument_ids={polymarket_instrument.id, sxbet_instrument.id},
        )

        coverage = node_runner._venue_pair_coverage(
            strategy,
            edges=[],
            nodes={
                "poly-node": SimpleNamespace(instrument=polymarket_instrument),
                "sxbet-node": SimpleNamespace(instrument=sxbet_instrument),
            },
            quotes={},
            matched_node_ids=set(),
            candidate_venue_pairs={},
        )

        report = {item["venuePair"]: item for item in coverage["zeroCandidateVenuePairs"]}[
            "POLYMARKET->SXBET"
        ]
        assert report["reason"] == "no_semantic_edge"
        assert report["blockerReason"] == "provider_scope_mismatch"
        assert "soccer:arsenal:west ham united" in report["commonEventKeySamples"]
        assert report["sampleBlockerCounts"] == {"provider_scope_mismatch": 1}
        assert report["samples"][0]["blockerHint"] == "provider_scope_mismatch"
        assert report["samples"][0]["fixtureIdentityProof"]["sameFixture"] is True

    def test_resolution_horizon_payload_counts_near_term_quoted_edges(self):
        inside_start = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
        recent_past_start = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
        outside_start = (datetime.now(tz=UTC) + timedelta(days=10)).isoformat()
        stale_start = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
        inside = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
            event_id="inside-a",
            start_time=inside_start,
        )
        inside_other = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
            event_id="inside-b",
            start_time=inside_start,
        )
        recent_past = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
            event_id="recent-past",
            start_time=recent_past_start,
        )
        outside = _instrument(
            venue="POLYMARKET",
            market_type="match_odds",
            outcome="away",
            event_id="outside",
            start_time=outside_start,
        )
        stale = _instrument(
            venue="POLYMARKET",
            market_type="match_odds",
            outcome="away",
            event_id="stale",
            start_time=stale_start,
        )
        nodes = {
            "a": SimpleNamespace(instrument=inside),
            "b": SimpleNamespace(instrument=inside_other),
            "c": SimpleNamespace(instrument=recent_past),
            "d": SimpleNamespace(instrument=outside),
            "e": SimpleNamespace(instrument=stale),
        }

        payload = node_runner._resolution_horizon_payload(
            {"max_resolution_horizon_hours": 48.0},
            nodes=nodes,
            quotes={"a": object(), "b": object(), "c": object(), "d": object(), "e": object()},
            edges=[
                SimpleNamespace(source_node_id="a", target_node_id="b"),
                SimpleNamespace(source_node_id="a", target_node_id="c"),
                SimpleNamespace(source_node_id="a", target_node_id="d"),
                SimpleNamespace(source_node_id="a", target_node_id="e"),
            ],
        )

        assert payload["enabled"] is True
        assert payload["eventsInsideHorizon"] == 1
        assert payload["recentPastEvents"] == 1
        assert payload["eventsOutsideHorizon"] == 1
        assert payload["stalePastEvents"] == 1
        assert payload["quotedCandidatesInsideHorizon"] == 2
        assert payload["blockedCandidatesDueHorizon"] == 2

    def test_runtime_probe_candidate_samples_include_dry_run_provenance(self):
        instrument_a = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = _instrument(
            venue="POLYMARKET",
            market_type="match_odds",
            outcome="away",
        )
        quality = {
            "profitMargin": "0.05",
            "marginBand": "positive",
            "rejectionBucket": "positive",
            "venuePair": "CLOUDBET->POLYMARKET",
            "marketFamily": "MATCH_ODDS",
            "venueA": str(instrument_a.id.venue),
            "venueB": str(instrument_b.id.venue),
            "ruleId": "rule-1",
            "templateId": "template-1",
            "relationshipType": "COMPLEMENTARY_COVERAGE",
            "freshnessProfile": "pre_match",
            "timingFlags": ["fresh"],
            "quoteAgeASeconds": 0.25,
            "quoteAgeBSeconds": 0.5,
            "fetchLatencyASeconds": 0.05,
            "fetchLatencyBSeconds": 0.1,
            "dryRunEligible": True,
            "dryRunEligibilityReason": "strict_execution_safe",
            "sameVenueRiskPolicy": {
                "executionDisabledUntilRiskEngineApproval": True,
            },
            "wouldExecuteSameVenueDryRun": False,
        }
        counters = node_runner.ProbeProfitabilityCounters()

        node_runner._record_probe_quality(
            counters,
            quality,
        )

        payload = counters.to_payload()
        sample = payload["sample_candidates"][0]
        assert sample["venueA"] == "CLOUDBET"
        assert sample["venueB"] == "POLYMARKET"
        assert sample["ruleId"] == "rule-1"
        assert sample["templateId"] == "template-1"
        assert sample["relationshipType"] == "COMPLEMENTARY_COVERAGE"
        assert sample["dryRunEligible"] is True
        assert sample["dryRunEligibilityReason"] == "strict_execution_safe"
        assert sample["sameVenueRiskPolicy"]["executionDisabledUntilRiskEngineApproval"] is True
        assert sample["wouldExecuteSameVenueDryRun"] is False
        assert counters.semantic_blocked_reasons == Counter()
        assert payload["timing_flags"] == {"fresh": 1}
        assert payload["freshness_profiles"] == {"pre_match": 1}
        assert payload["venue_quote_health"]["CLOUDBET"]["max_quote_age_secs"] == 0.25
        assert payload["venue_quote_health"]["POLYMARKET"]["max_fetch_latency_secs"] == 0.1
        assert payload["latency_histograms"]["quote_age_secs"]["count"] == 2
        assert payload["latency_histograms"]["fetch_latency_secs"]["max"] == 0.1
        assert payload["latency_histograms"]["pair_skew_secs"]["count"] == 1
        assert payload["pair_skew_by_venue_pair"]["CLOUDBET->POLYMARKET"]["max"] == 0.0
        assert payload["live_quote_age_slo"]["observations"] == 0
        assert payload["live_timing_slo"]["fetch_latency"]["observations"] == 0
        assert payload["live_timing_slo"]["pair_skew"]["observations"] == 0
        assert payload["same_venue_dry_run"] == {
            "passes": 0,
            "failures": 0,
            "failure_reasons": {},
        }

    def test_runtime_probe_respects_execution_venue_mode(self):
        strategy = SimpleNamespace(
            _config=SimpleNamespace(execution_venue_mode="cross_venue"),
        )
        sxbet_a = SimpleNamespace(
            instrument=SimpleNamespace(id=SimpleNamespace(venue="SXBET")),
        )
        sxbet_b = SimpleNamespace(
            instrument=SimpleNamespace(id=SimpleNamespace(venue="SXBET")),
        )
        polymarket = SimpleNamespace(
            instrument=SimpleNamespace(id=SimpleNamespace(venue="POLYMARKET")),
        )

        assert not node_runner._probe_edge_matches_execution_venue_mode(
            strategy,
            sxbet_a,
            sxbet_b,
        )
        assert node_runner._probe_edge_matches_execution_venue_mode(strategy, sxbet_a, polymarket)

        strategy._config.execution_venue_mode = "same_venue"
        assert node_runner._probe_edge_matches_execution_venue_mode(strategy, sxbet_a, sxbet_b)
        assert not node_runner._probe_edge_matches_execution_venue_mode(
            strategy,
            sxbet_a,
            polymarket,
        )

    def test_runtime_probe_candidate_decision_latency_fills_strategy_gap(self):
        counters = node_runner.ProbeProfitabilityCounters()
        counters.candidate_decision_latency_ns.extend([1_000_000, 3_000_000])
        profitability = counters.to_payload()

        diagnostics = node_runner._runtime_latency_diagnostics(
            {
                "latency_diagnostics": {
                    "candidate_decision": {
                        "count": 0,
                        "p50_ms": 0.0,
                        "p95_ms": 0.0,
                        "p99_ms": 0.0,
                        "max_ms": 0.0,
                    },
                },
            },
            profitability,
        )

        assert diagnostics["candidate_decision"]["count"] == 2
        assert diagnostics["candidate_decision"]["p95_ms"] == 1.0
        assert diagnostics["candidate_decision"]["max_ms"] == 3.0
        assert diagnostics["candidate_decision_source"] == "runtime_probe"
        assert diagnostics["runtime_probe_candidate_decision"]["count"] == 2
        assert diagnostics["sloStatus"]["overall"] == "unknown"

    def test_runtime_latency_diagnostics_reports_slo_pass_warn_and_missing_stages(self):
        complete = node_runner._runtime_latency_diagnostics(
            {
                "latency_diagnostics": {
                    "quote_event_to_strategy": {"count": 3},
                    "graph_scan": {"count": 3},
                    "candidate_decision": {"count": 3},
                },
            },
            {
                "quoted_edges": 3,
                "positive_execution": 1,
                "positive_same_venue": 0,
                "threshold_execution": 1,
                "threshold_same_venue": 0,
                "live_timing_slo": {
                    "quote_age": {
                        "threshold_secs": 5.0,
                        "observations": 6,
                        "violations": 0,
                    },
                    "fetch_latency": {
                        "threshold_mode": "per_candidate",
                        "max_threshold_secs": 2.0,
                        "observations": 6,
                        "violations": 0,
                    },
                    "pair_skew": {
                        "threshold_mode": "per_candidate",
                        "max_threshold_secs": 1.0,
                        "observations": 3,
                        "violations": 0,
                    },
                },
                "latency_histograms": {"fetch_latency_secs": {"count": 6}},
                "candidate_decision_latency": {},
            },
        )
        assert complete["sloStatus"]["overall"] == "pass"
        assert complete["diagnosticWarnings"] == []

        stale = node_runner._runtime_latency_diagnostics(
            {
                "latency_diagnostics": {
                    "quote_event_to_strategy": {"count": 3},
                    "graph_scan": {"count": 3},
                    "candidate_decision": {"count": 3},
                },
            },
            {
                "quoted_edges": 3,
                "positive_execution": 0,
                "positive_same_venue": 0,
                "threshold_execution": 0,
                "threshold_same_venue": 0,
                "live_timing_slo": {
                    "quote_age": {
                        "threshold_secs": 5.0,
                        "observations": 6,
                        "violations": 2,
                    },
                    "fetch_latency": {"observations": 0, "violations": 0},
                    "pair_skew": {"observations": 0, "violations": 0},
                },
                "latency_histograms": {"fetch_latency_secs": {"count": 6}},
                "candidate_decision_latency": {},
            },
        )
        assert stale["sloStatus"]["overall"] == "warn"
        assert stale["sloStatus"]["quoteAge"]["status"] == "warn"

        missing = node_runner._runtime_latency_diagnostics(
            {"latency_diagnostics": {}},
            {
                "quoted_edges": 2,
                "positive_execution": 0,
                "positive_same_venue": 0,
                "threshold_execution": 0,
                "threshold_same_venue": 0,
                "live_timing_slo": {},
                "latency_histograms": {},
                "candidate_decision_latency": {},
            },
        )
        assert missing["sloStatus"]["overall"] == "unknown"
        assert missing["sloStatus"]["missingStages"] == [
            "quote_receive",
            "graph_scan",
            "candidate_decision",
            "provider_latency",
        ]
        assert missing["diagnosticWarnings"] == [
            "missing_quote_receive_latency",
            "missing_graph_scan_latency",
            "missing_candidate_decision_latency",
            "missing_provider_latency",
        ]

        quote_only_provider_latency = node_runner._runtime_latency_diagnostics(
            {
                "latency_diagnostics": {
                    "quote_event_to_strategy": {"count": 3},
                    "graph_scan": {"count": 3},
                    "candidate_decision": {"count": 3},
                    "quote_fetch_latency": {
                        "count": 3,
                        "p50_ms": 500.0,
                        "p95_ms": 700.0,
                        "p99_ms": 800.0,
                        "max_ms": 900.0,
                    },
                },
            },
            {
                "quoted_edges": 2,
                "positive_execution": 0,
                "positive_same_venue": 0,
                "threshold_execution": 0,
                "threshold_same_venue": 0,
                "live_timing_slo": {},
                "latency_histograms": {},
                "candidate_decision_latency": {},
            },
        )
        assert quote_only_provider_latency["sloStatus"]["fetchLatency"]["status"] == "pass"
        assert quote_only_provider_latency["sloStatus"]["strategyLatency"][
            "providerLatencyObserved"
        ]
        assert "provider_latency" not in quote_only_provider_latency["sloStatus"]["missingStages"]
        assert "missing_provider_latency" not in quote_only_provider_latency["diagnosticWarnings"]

        p95_pass_with_max_outlier = node_runner._runtime_latency_diagnostics(
            {
                "latency_diagnostics": {
                    "quote_event_to_strategy": {"count": 3},
                    "graph_scan": {"count": 3},
                    "candidate_decision": {"count": 3},
                },
            },
            {
                "quoted_edges": 2,
                "positive_execution": 0,
                "positive_same_venue": 0,
                "threshold_execution": 0,
                "threshold_same_venue": 0,
                "live_timing_slo": {},
                "latency_histograms": {
                    "pair_skew_secs": {"count": 8, "p95": 0.8, "max": 1.4},
                },
                "candidate_decision_latency": {},
            },
        )
        assert p95_pass_with_max_outlier["sloStatus"]["pairSkew"]["status"] == "pass"
        assert p95_pass_with_max_outlier["sloStatus"]["pairSkew"]["thresholdMode"] == (
            "histogram_p95"
        )
        assert p95_pass_with_max_outlier["sloStatus"]["pairSkew"]["outlierMaxExceeded"] is True

    def test_runtime_probe_aggregates_same_venue_dry_run_reasons(self):
        counters = node_runner.ProbeProfitabilityCounters()
        quality = {
            "profitMargin": "0.05",
            "marginBand": "positive",
            "rejectionBucket": "positive",
            "venuePair": "SXBET->SXBET",
            "marketFamily": "TOTALS",
            "venueA": "SXBET",
            "venueB": "SXBET",
            "freshnessProfile": "live",
            "timingFlags": ["quote_age"],
            "quoteAgeASeconds": 6.0,
            "quoteAgeBSeconds": 0.5,
            "quoteDeltaSeconds": 0.1,
            "fetchLatencyASeconds": 0.05,
            "fetchLatencyBSeconds": 0.1,
            "maxPairSkewSeconds": 0.1,
            "maxFetchLatencySeconds": 0.1,
            "executionSafe": False,
            "sameVenueExecutionEligible": True,
            "wouldExecuteSameVenueDryRun": False,
            "sameVenueRiskPolicy": {
                "sameVenue": True,
                "sameFixture": True,
                "compatibleMarketFamily": True,
                "freshQuotes": False,
                "sufficientLiquidity": True,
                "thresholdProfit": True,
            },
        }

        node_runner._record_probe_quality(counters, quality)

        payload = counters.to_payload()
        assert payload["same_venue_dry_run"]["passes"] == 0
        assert payload["same_venue_dry_run"]["failures"] == 1
        assert payload["same_venue_dry_run"]["failure_reasons"] == {"freshQuotes": 1}
        assert payload["live_quote_age_slo"]["observations"] == 2
        assert payload["live_quote_age_slo"]["violations"] == 1
        assert payload["live_timing_slo"]["quote_age"]["observations"] == 2
        assert payload["live_timing_slo"]["quote_age"]["violations"] == 1
        assert payload["live_timing_slo"]["fetch_latency"]["observations"] == 2
        assert payload["live_timing_slo"]["fetch_latency"]["violations"] == 0
        assert payload["live_timing_slo"]["fetch_latency"]["min_threshold_secs"] == 0.1
        assert payload["live_timing_slo"]["fetch_latency"]["max_threshold_secs"] == 0.1
        assert payload["live_timing_slo"]["pair_skew"]["observations"] == 1
        assert payload["live_timing_slo"]["pair_skew"]["violations"] == 0
        assert payload["live_timing_slo"]["pair_skew"]["min_threshold_secs"] == 0.1
        assert payload["live_timing_slo"]["pair_skew"]["max_threshold_secs"] == 0.1

    def test_run_success_and_failure_paths_record_status_transitions(self, tmp_path, monkeypatch):
        def semantic_status(_manifest):
            return SemanticCacheStatus(
                path=str(tmp_path / "semantic-cache"),
                source="bootstrapped",
                manifest_count=1,
                promoted_template_count=1,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
            )

        monkeypatch.setattr(node_runner, "ensure_semantic_cache_ready", semantic_status)
        monkeypatch.setattr(node_runner.HeartbeatWriter, "start", lambda self: None)

        original_write_json = node_runner._write_json
        observed_statuses: list[str] = []

        def tracking_write_json(path, payload):
            if "status" in payload:
                observed_statuses.append(payload["status"])
            return original_write_json(path, payload)

        monkeypatch.setattr(node_runner, "_write_json", tracking_write_json)

        class SuccessTradingNode:
            instances: list["SuccessTradingNode"] = []

            def __init__(self, config):
                self.config = config
                self.disposed = False
                type(self).instances.append(self)

            def build(self):
                return None

            def run(self):
                return None

            def dispose(self):
                self.disposed = True

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", SuccessTradingNode)
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest-success.json"
        manifest_path.write_bytes(manifest.json())

        assert runner_main(["run", "--manifest", str(manifest_path)]) == 0
        success_payload = json.loads((tmp_path / "status.json").read_text())
        assert observed_statuses[-4:] == ["building", "built", "running", "completed"]
        assert success_payload["status"] == "completed"
        assert success_payload["heartbeatPath"] == str(tmp_path / "heartbeat.json")
        assert SuccessTradingNode.instances[-1].disposed is True

        observed_statuses.clear()

        class FailingTradingNode(SuccessTradingNode):
            def run(self):
                raise RuntimeError("node-run-failed")

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", FailingTradingNode)
        failing_manifest_path = tmp_path / "manifest-failure.json"
        failing_manifest_path.write_bytes(manifest.json())

        with pytest.raises(RuntimeError, match="node-run-failed"):
            runner_main(["run", "--manifest", str(failing_manifest_path)])

        failure_payload = json.loads((tmp_path / "status.json").read_text())
        assert observed_statuses[-4:] == ["building", "built", "running", "failed"]
        assert failure_payload["status"] == "failed"
        assert failure_payload["error"] == "RuntimeError('node-run-failed')"
        assert FailingTradingNode.instances[-1].disposed is True

    def test_live_run_starts_runtime_probe_status_writer(self, tmp_path, monkeypatch):
        def semantic_status(_manifest):
            return SemanticCacheStatus(
                path=str(tmp_path / "semantic-cache"),
                source="existing",
                manifest_count=1,
                promoted_template_count=1,
                execution_safe_template_count=1,
                same_venue_execution_eligible_template_count=0,
            )

        monkeypatch.setattr(node_runner, "ensure_semantic_cache_ready", semantic_status)
        monkeypatch.setattr(node_runner.HeartbeatWriter, "start", lambda self: None)
        monkeypatch.setattr(
            node_runner,
            "_resolve_betting_strategy",
            lambda _node: object(),
        )

        observed_writer: dict[str, object] = {}

        class FakeRuntimeProbeStatusWriter:
            def __init__(self, **kwargs):
                observed_writer["kwargs"] = kwargs

            def start(self):
                observed_writer["started"] = True

        monkeypatch.setattr(
            node_runner,
            "RuntimeProbeStatusWriter",
            FakeRuntimeProbeStatusWriter,
        )

        class LiveTradingNode:
            def __init__(self, config):
                self.config = config
                self.trader = object()

            def build(self):
                return None

            def run(self):
                return None

            def dispose(self):
                return None

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", LiveTradingNode)
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        msgspec.structs.force_setattr(manifest, "validation_mode", False)
        manifest_path = tmp_path / "manifest-live.json"
        manifest_path.write_bytes(manifest.json())

        assert runner_main(["run", "--manifest", str(manifest_path)]) == 0

        assert observed_writer["started"] is True
        writer_kwargs = observed_writer["kwargs"]
        assert writer_kwargs["manifest"].validation_mode is False
        assert writer_kwargs["semantic_cache"]["ready"] is True

    def test_semantic_cache_payload_helpers(self, tmp_path, monkeypatch):
        expected_status = SemanticCacheStatus(
            path=str(tmp_path / "semantic-cache"),
            source="bootstrapped",
            manifest_count=2,
            promoted_template_count=3,
            execution_safe_template_count=1,
            same_venue_execution_eligible_template_count=1,
            promoted_safety_tier_counts={"EXECUTION_SAFE": 1},
            strict_execution_blocker_counts={"same_venue_risk_engine_elevation_required": 1},
        )
        monkeypatch.setattr(node_runner, "ensure_semantic_cache_ready", lambda _: expected_status)

        payload = node_runner._ensure_semantic_cache(_manifest(tmp_path, cache_dir=tmp_path / "x"))

        assert payload == {
            "path": str(tmp_path / "semantic-cache"),
            "source": "bootstrapped",
            "ready": True,
            "manifestCount": 2,
            "promotedTemplateCount": 3,
            "executionSafeTemplateCount": 1,
            "sameVenueExecutionEligibleTemplateCount": 1,
            "promotedSafetyTierCounts": {"EXECUTION_SAFE": 1},
            "strictExecutionBlockerCounts": {
                "same_venue_risk_engine_elevation_required": 1,
            },
            "promotedMarketFamilyCounts": {},
            "executionSafeMarketFamilyCounts": {},
            "sameVenueEligibleMarketFamilyCounts": {},
            "providerCorpusCoverage": {},
            "coverageProofCount": 0,
            "coverageHyperedgeCount": 0,
            "compatibilityVersion": None,
            "compatibilityScope": None,
            "compatible": True,
            "summaryReused": False,
            "bootstrapPhaseTimingsSeconds": {},
        }
        assert node_runner._semantic_cache_payload(None) is None

    def test_runtime_probe_candidate_quality_bucket_helpers(self):
        edge = SimpleNamespace(execution_safe=True)

        assert node_runner._probe_margin_band(Decimal("0.01")) == "positive"
        assert node_runner._probe_margin_band(Decimal("-0.005")) == "0% to -1%"
        assert node_runner._probe_margin_band(Decimal("-0.015")) == "-1% to -2%"
        assert node_runner._probe_margin_band(Decimal("-0.03")) == "-2% to -5%"
        assert node_runner._probe_margin_band(Decimal("-0.07")) == "< -5%"

        base_kwargs = {
            "edge": edge,
            "allow_same_venue": False,
            "profit_margin": Decimal("0.03"),
            "min_profit_margin": Decimal("0.02"),
            "quote_age_a_secs": 0.1,
            "quote_age_b_secs": 0.1,
            "quote_delta_secs": 0.1,
            "fetch_latency_a_secs": 0.1,
            "fetch_latency_b_secs": 0.1,
            "available_size_a": Decimal(100),
            "available_size_b": Decimal(100),
            "max_quote_age_secs": 30.0,
            "max_pair_skew_secs": 5.0,
            "max_fetch_latency_secs": 10.0,
        }

        assert node_runner._probe_rejection_bucket(**base_kwargs) == "positive"
        assert (
            node_runner._probe_rejection_bucket(
                **{**base_kwargs, "fetch_latency_a_secs": 11.0},
            )
            == "fetch_latency"
        )
        assert (
            node_runner._probe_rejection_bucket(
                **{**base_kwargs, "available_size_a": Decimal(0)},
            )
            == "liquidity"
        )
        assert (
            node_runner._probe_rejection_bucket(
                **{**base_kwargs, "profit_margin": Decimal("-0.01")},
            )
            == "negative_margin"
        )
        non_execution_kwargs = {**base_kwargs, "edge": SimpleNamespace(execution_safe=False)}
        assert (
            node_runner._probe_rejection_bucket(**non_execution_kwargs)
            == "unsupported_market_family"
        )
        assert (
            node_runner._probe_rejection_bucket(
                **{
                    **non_execution_kwargs,
                    "edge": SimpleNamespace(
                        execution_safe=False,
                        safety_tier="TOPOLOGY_SAFE",
                        relationship_type="COMPLEMENTARY_COVERAGE",
                    ),
                },
            )
            == "topology_only"
        )
        assert (
            node_runner._probe_rejection_bucket(
                **{
                    **non_execution_kwargs,
                    "edge": SimpleNamespace(
                        execution_safe=False,
                        relationship_type="VOID_COMPATIBLE_HEDGE",
                    ),
                },
            )
            == "void_settlement"
        )
        timing_kwargs = {
            key: base_kwargs[key]
            for key in (
                "quote_age_a_secs",
                "quote_age_b_secs",
                "quote_delta_secs",
                "fetch_latency_a_secs",
                "fetch_latency_b_secs",
                "max_quote_age_secs",
                "max_pair_skew_secs",
                "max_fetch_latency_secs",
            )
        }
        assert node_runner._probe_timing_flags(**timing_kwargs) == ["fresh"]
        assert node_runner._probe_timing_flags(
            **{
                **timing_kwargs,
                "quote_age_a_secs": 31.0,
                "quote_delta_secs": 6.0,
            },
        ) == ["quote_age", "pair_skew"]
        assert (
            node_runner._semantic_blocked_reason(
                {"blockerReason": "void_settlement", "rejectionBucket": "topology_only"},
            )
            == "void_settlement"
        )
        assert (
            node_runner._semantic_blocked_relationship(
                {
                    "safetyTier": "TOPOLOGY_SAFE",
                    "relationshipType": "EQUIVALENT_SELECTION",
                },
            )
            == "TOPOLOGY_SAFE:EQUIVALENT_SELECTION"
        )

    def test_runtime_probe_same_venue_policy_uses_fixture_identity(self):
        instrument_a = _instrument(
            venue="SXBET",
            market_type="asian_handicap",
            market_name="asian_handicap",
            outcome="home",
            params="line=0.0",
            handicap=0.0,
        )
        instrument_b = _instrument(
            venue="SXBET",
            market_type="asian_handicap",
            market_name="asian_handicap",
            outcome="away",
            params="line=0.5",
            handicap=0.5,
        )
        strategy = SimpleNamespace(
            matcher_suspect_reason=BettingArbitrageStrategy.matcher_suspect_reason,
            semantic_fixture_suspect_reason=(
                BettingArbitrageStrategy.semantic_fixture_suspect_reason
            ),
            quote_age_secs=lambda _observed_ns, _quote: 0.1,
            _quote_pair_skew_secs=lambda _quote_a, _quote_b: 0.0,
            quote_fetch_latency_secs=lambda _quote: 0.1,
            quote_available_size=lambda _quote: Decimal(100),
            quote_freshness_thresholds=lambda _instrument_a, _instrument_b: SimpleNamespace(
                profile="pre_match",
                max_quote_age_secs=30.0,
                max_pair_skew_secs=5.0,
                max_fetch_latency_secs=10.0,
            ),
        )
        edge = SimpleNamespace(
            rule_id="rule-1",
            template_id="template-1",
            relationship_type="VOID_COMPATIBLE_HEDGE",
            safety_tier="EXECUTION_SAFE_SAME_VENUE_ELIGIBLE",
            execution_safe=False,
            same_venue_execution_eligible=True,
        )
        quote_a = SimpleNamespace(
            odds=Decimal("2.20"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000),
        )
        quote_b = SimpleNamespace(
            odds=Decimal("2.20"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000),
        )

        quality = node_runner._probe_candidate_quality(
            strategy,
            edge=edge,
            source_node=SimpleNamespace(
                instrument=instrument_a,
                market_name="asian_handicap",
                outcome="home",
            ),
            target_node=SimpleNamespace(
                instrument=instrument_b,
                market_name="asian_handicap",
                outcome="away",
            ),
            quote_a=quote_a,
            quote_b=quote_b,
            min_profit_margin=Decimal("0.02"),
            allow_same_venue=True,
        )

        policy = quality["sameVenueRiskPolicy"]
        assert policy["sameFixture"] is True
        assert policy["suspectReason"] == "same_market_params_mismatch"
        assert policy["fixtureSuspectReason"] == "none"
        assert policy["diagnosticSuspect"] is True
        assert quality["wouldExecuteSameVenueDryRun"] is True
        assert quality["rawProfitMargin"] == quality["feeAdjustedProfitMargin"]
        assert quality["feeDrag"] == "0"
        assert quality["devig"] == {
            "enabled": False,
            "bookStatus": "disabled",
            "valueClassification": "devig_disabled",
        }
        assert quality["candidateValueClassification"] == "devig_disabled"
        assert quality["takerFeeRateA"] == "0"
        assert quality["takerFeeRateB"] == "0"
        assert quality["makerRebateRateA"] == "0"
        assert quality["makerRebateRateB"] == "0"

    def test_runtime_probe_candidate_quality_uses_strategy_pair_skew(self):
        instrument_a = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="away",
        )
        strategy = SimpleNamespace(
            fee_adjusted_opportunity=lambda opportunity: opportunity,
            matcher_suspect_reason=BettingArbitrageStrategy.matcher_suspect_reason,
            semantic_fixture_suspect_reason=(
                BettingArbitrageStrategy.semantic_fixture_suspect_reason
            ),
            quote_age_secs=lambda _observed_ns, _quote: 0.1,
            _quote_pair_skew_secs=lambda _quote_a, _quote_b: 0.2,
            quote_fetch_latency_secs=lambda _quote: 0.1,
            quote_available_size=lambda _quote: Decimal(100),
            quote_freshness_thresholds=lambda _instrument_a, _instrument_b: SimpleNamespace(
                profile="pre_match",
                max_quote_age_secs=30.0,
                max_pair_skew_secs=5.0,
                max_fetch_latency_secs=10.0,
            ),
        )
        edge = SimpleNamespace(
            rule_id="rule-1",
            template_id="template-1",
            relationship_type="COMPLEMENTARY_COVERAGE",
            safety_tier="EXECUTION_SAFE",
            execution_safe=True,
            same_venue_execution_eligible=False,
        )
        quote_a = SimpleNamespace(
            odds=Decimal("2.20"),
            received_ns=20_000_000_000,
            quote=SimpleNamespace(ts_event=1_000_000_000, size=Decimal(100)),
        )
        quote_b = SimpleNamespace(
            odds=Decimal("2.20"),
            received_ns=20_000_000_000,
            quote=SimpleNamespace(ts_event=11_000_000_000, size=Decimal(100)),
        )

        quality = node_runner._probe_candidate_quality(
            strategy,
            edge=edge,
            source_node=SimpleNamespace(
                instrument=instrument_a,
                market_name="match_odds",
                outcome="home",
            ),
            target_node=SimpleNamespace(
                instrument=instrument_b,
                market_name="match_odds",
                outcome="away",
            ),
            quote_a=quote_a,
            quote_b=quote_b,
            min_profit_margin=Decimal("0.02"),
            allow_same_venue=False,
        )

        assert quality["quoteDeltaSeconds"] == 0.2
        assert quality["rejectionBucket"] == "positive"

    def test_runtime_probe_devig_diagnostics_classifies_value_without_execution(self):
        instrument_a = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = _instrument(
            venue="POLYMARKET",
            market_type="match_odds",
            outcome="away",
        )
        config = BettingArbitrageConfig(
            min_profit_margin=Decimal("0.02"),
            devig_enabled=True,
            devig_method="shin",
            devig_reference_venues=("CLOUDBET",),
            value_diagnostics_enabled=True,
            value_execution_enabled=False,
            min_value_edge=Decimal("0.005"),
        )
        strategy = SimpleNamespace(
            _config=config,
            devigged_book=lambda odds: devig_probabilities(odds, method=config.devig_method),
            fee_adjusted_opportunity=lambda opportunity: opportunity,
            matcher_suspect_reason=BettingArbitrageStrategy.matcher_suspect_reason,
            semantic_fixture_suspect_reason=(
                BettingArbitrageStrategy.semantic_fixture_suspect_reason
            ),
            quote_age_secs=lambda _observed_ns, _quote: 0.1,
            _quote_pair_skew_secs=lambda _quote_a, _quote_b: 0.0,
            quote_fetch_latency_secs=lambda _quote: 0.1,
            quote_available_size=lambda _quote: Decimal(100),
            quote_freshness_thresholds=lambda _instrument_a, _instrument_b: SimpleNamespace(
                profile="pre_match",
                max_quote_age_secs=30.0,
                max_pair_skew_secs=5.0,
                max_fetch_latency_secs=10.0,
            ),
        )
        edge = SimpleNamespace(
            rule_id="rule-1",
            template_id="template-1",
            relationship_type="COMPLEMENTARY_COVERAGE",
            safety_tier="EXECUTION_SAFE",
            execution_safe=True,
            same_venue_execution_eligible=False,
        )
        quote_a = SimpleNamespace(
            odds=Decimal("1.75"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000, size=Decimal(100)),
        )
        quote_b = SimpleNamespace(
            odds=Decimal("2.20"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000, size=Decimal(100)),
        )

        quality = node_runner._probe_candidate_quality(
            strategy,
            edge=edge,
            source_node=SimpleNamespace(
                instrument=instrument_a,
                market_name="match_odds",
                outcome="home",
            ),
            target_node=SimpleNamespace(
                instrument=instrument_b,
                market_name="match_odds",
                outcome="away",
            ),
            quote_a=quote_a,
            quote_b=quote_b,
            min_profit_margin=Decimal("0.02"),
            allow_same_venue=False,
        )

        devig = quality["devig"]
        assert devig["enabled"] is True
        assert devig["bookStatus"] == "synthetic_cross_venue_pair"
        assert devig["referenceVenue"] == "mixed:CLOUDBET+POLYMARKET"
        assert devig["devigMethod"] == "shin"
        assert devig["valueExecutionEnabled"] is False
        assert devig["valueExecutionBlockedReason"] == "value_execution_disabled"
        assert Decimal(devig["bookVig"]) > 0
        assert quality["candidateValueClassification"] in {
            "sportsbook_value_edge",
            "prediction_market_value_edge",
            "vig_only_edge",
            "locked_execution_safe_arbitrage",
        }

        counters = node_runner.ProbeProfitabilityCounters()
        node_runner._record_probe_quality(counters, quality)
        payload = counters.to_payload()
        assert payload["devig_diagnostics"]["evaluated_edges"] == 1
        assert payload["devig_diagnostics"]["complete_books"] == 1
        assert payload["devig_diagnostics"]["method_counts"] == {"shin": 1}
        assert sum(payload["devig_diagnostics"]["value_buckets"].values()) == 1
        assert quality["basketRebateRate"] == "0"
        assert quality["basketBoostRate"] == "0"

    def test_runtime_probe_candidate_quality_records_fee_adjustment_error(self):
        instrument_a = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = _instrument(
            venue="SXBET",
            market_type="match_odds",
            outcome="away",
        )

        def fee_adjusted_opportunity(_opportunity):
            msg = "Decimal odds must be greater than 1, got 1"
            raise ValueError(msg)

        strategy = SimpleNamespace(
            fee_adjusted_opportunity=fee_adjusted_opportunity,
            matcher_suspect_reason=BettingArbitrageStrategy.matcher_suspect_reason,
            semantic_fixture_suspect_reason=(
                BettingArbitrageStrategy.semantic_fixture_suspect_reason
            ),
            quote_age_secs=lambda _observed_ns, _quote: 0.1,
            _quote_pair_skew_secs=lambda _quote_a, _quote_b: 0.0,
            quote_fetch_latency_secs=lambda _quote: 0.1,
            quote_available_size=lambda _quote: Decimal(100),
            quote_freshness_thresholds=lambda _instrument_a, _instrument_b: SimpleNamespace(
                profile="pre_match",
                max_quote_age_secs=30.0,
                max_pair_skew_secs=5.0,
                max_fetch_latency_secs=10.0,
            ),
        )
        edge = SimpleNamespace(
            rule_id="rule-1",
            template_id="template-1",
            relationship_type="COMPLEMENTARY_COVERAGE",
            safety_tier="EXECUTION_SAFE",
            execution_safe=True,
            same_venue_execution_eligible=False,
        )
        quote_a = SimpleNamespace(
            odds=Decimal(1),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000, size=Decimal(100)),
        )
        quote_b = SimpleNamespace(
            odds=Decimal("2.20"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000, size=Decimal(100)),
        )

        quality = node_runner._probe_candidate_quality(
            strategy,
            edge=edge,
            source_node=SimpleNamespace(
                instrument=instrument_a,
                market_name="match_odds",
                outcome="home",
            ),
            target_node=SimpleNamespace(
                instrument=instrument_b,
                market_name="match_odds",
                outcome="away",
            ),
            quote_a=quote_a,
            quote_b=quote_b,
            min_profit_margin=Decimal("0.02"),
            allow_same_venue=False,
        )

        assert quality["rejectionBucket"] == "invalid_odds"
        assert quality["feeAdjusted"] is False
        assert quality["feeAdjustmentError"] == "Decimal odds must be greater than 1, got 1"
        assert quality["feeAdjustedOddsA"] == "1"
        assert quality["feeAdjustedOddsB"] == "2.20"

    def test_runtime_probe_devig_does_not_label_topology_edge_locked_arbitrage(self):
        instrument_a = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="home",
        )
        instrument_b = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
        )
        config = BettingArbitrageConfig(
            min_profit_margin=Decimal("0.02"),
            devig_enabled=True,
            devig_method="proportional",
            value_diagnostics_enabled=True,
            value_execution_enabled=False,
        )
        strategy = SimpleNamespace(
            _config=config,
            devigged_book=lambda odds: devig_probabilities(odds, method=config.devig_method),
            fee_adjusted_opportunity=lambda opportunity: opportunity,
            matcher_suspect_reason=BettingArbitrageStrategy.matcher_suspect_reason,
            semantic_fixture_suspect_reason=(
                BettingArbitrageStrategy.semantic_fixture_suspect_reason
            ),
            quote_age_secs=lambda _observed_ns, _quote: 0.1,
            _quote_pair_skew_secs=lambda _quote_a, _quote_b: 0.0,
            quote_fetch_latency_secs=lambda _quote: 0.1,
            quote_available_size=lambda _quote: Decimal(100),
            quote_freshness_thresholds=lambda _instrument_a, _instrument_b: SimpleNamespace(
                profile="pre_match",
                max_quote_age_secs=30.0,
                max_pair_skew_secs=5.0,
                max_fetch_latency_secs=10.0,
            ),
        )
        edge = SimpleNamespace(
            rule_id="rule-1",
            template_id="template-1",
            relationship_type="EQUIVALENT_SELECTION",
            safety_tier="TOPOLOGY_SAFE",
            execution_safe=False,
            same_venue_execution_eligible=False,
            caveats=(),
        )
        quote_a = SimpleNamespace(
            odds=Decimal("3.00"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000, size=Decimal(100)),
        )
        quote_b = SimpleNamespace(
            odds=Decimal("3.10"),
            received_ns=10_000_000_000,
            quote=SimpleNamespace(ts_event=9_900_000_000, size=Decimal(100)),
        )

        quality = node_runner._probe_candidate_quality(
            strategy,
            edge=edge,
            source_node=SimpleNamespace(
                instrument=instrument_a,
                market_name="match_odds",
                outcome="home",
            ),
            target_node=SimpleNamespace(
                instrument=instrument_b,
                market_name="match_odds",
                outcome="away",
            ),
            quote_a=quote_a,
            quote_b=quote_b,
            min_profit_margin=Decimal("0.02"),
            allow_same_venue=False,
        )

        assert quality["rejectionBucket"] == "equivalent_selection"
        # An EQUIVALENT_SELECTION is the same outcome on two books, not a
        # complementary partition, so the complementary-partition arb margin must be
        # suppressed and the pair surfaced through the independent devig value-edge
        # stream rather than as a positive semantic arbitrage edge.
        assert quality["profitMargin"] == "0"
        assert quality["rawProfitMargin"] == "0"
        assert quality["feeAdjustedProfitMargin"] == "0"
        assert quality["candidateValueClassification"] == "sportsbook_value_edge"

    def test_runtime_probe_excludes_equivalent_selection_from_positive_arb_stream(self):
        config = BettingArbitrageConfig(
            min_profit_margin=Decimal("0.02"),
            devig_enabled=True,
            devig_method="proportional",
            value_diagnostics_enabled=True,
            value_execution_enabled=False,
            min_value_edge=Decimal("0.005"),
        )
        strategy = SimpleNamespace(
            _config=config,
            devigged_book=lambda odds: devig_probabilities(odds, method=config.devig_method),
            fee_adjusted_opportunity=lambda opportunity: opportunity,
            matcher_suspect_reason=BettingArbitrageStrategy.matcher_suspect_reason,
            semantic_fixture_suspect_reason=(
                BettingArbitrageStrategy.semantic_fixture_suspect_reason
            ),
            quote_age_secs=lambda _observed_ns, _quote: 0.1,
            _quote_pair_skew_secs=lambda _quote_a, _quote_b: 0.0,
            quote_fetch_latency_secs=lambda _quote: 0.1,
            quote_available_size=lambda _quote: Decimal(100),
            quote_freshness_thresholds=lambda _instrument_a, _instrument_b: SimpleNamespace(
                profile="pre_match",
                max_quote_age_secs=30.0,
                max_pair_skew_secs=5.0,
                max_fetch_latency_secs=10.0,
            ),
        )

        def _quote(odds):
            return SimpleNamespace(
                odds=Decimal(odds),
                received_ns=10_000_000_000,
                quote=SimpleNamespace(ts_event=9_900_000_000, size=Decimal(100)),
            )

        # EQUIVALENT_SELECTION: the same "home" outcome priced on two CLOUDBET books.
        # The lopsided 10.86 / 12.84 odds imply probabilities summing well below 1, so
        # the complementary-partition formula would fabricate a ~+488% "arb" margin.
        equivalent_edge = SimpleNamespace(
            rule_id="eq-rule",
            template_id="eq-template",
            relationship_type="EQUIVALENT_SELECTION",
            safety_tier="EXECUTION_SAFE_SAME_VENUE_ELIGIBLE",
            execution_safe=False,
            same_venue_execution_eligible=True,
            caveats=(),
        )
        equivalent_quality = node_runner._probe_candidate_quality(
            strategy,
            edge=equivalent_edge,
            source_node=SimpleNamespace(
                instrument=_instrument(venue="CLOUDBET", market_type="match_odds", outcome="home"),
                market_name="match_odds",
                outcome="home",
            ),
            target_node=SimpleNamespace(
                instrument=_instrument(
                    venue="CLOUDBET",
                    market_type="draw_no_bet",
                    outcome="home",
                ),
                market_name="draw_no_bet",
                outcome="home",
            ),
            quote_a=_quote("10.86"),
            quote_b=_quote("12.84"),
            min_profit_margin=Decimal("0.02"),
            allow_same_venue=True,
        )

        # COMPLEMENTARY_COVERAGE: genuine two-sided coverage with a real positive margin.
        complementary_edge = SimpleNamespace(
            rule_id="cc-rule",
            template_id="cc-template",
            relationship_type="COMPLEMENTARY_COVERAGE",
            safety_tier="EXECUTION_SAFE",
            execution_safe=True,
            same_venue_execution_eligible=False,
            caveats=(),
        )
        complementary_quality = node_runner._probe_candidate_quality(
            strategy,
            edge=complementary_edge,
            source_node=SimpleNamespace(
                instrument=_instrument(
                    venue="CLOUDBET",
                    market_type="match_odds",
                    outcome="home",
                    event_id="event-2",
                ),
                market_name="match_odds",
                outcome="home",
            ),
            target_node=SimpleNamespace(
                instrument=_instrument(
                    venue="SXBET",
                    market_type="match_odds",
                    outcome="away",
                    event_id="event-2",
                ),
                market_name="match_odds",
                outcome="away",
            ),
            quote_a=_quote("2.10"),
            quote_b=_quote("2.05"),
            min_profit_margin=Decimal("0.02"),
            allow_same_venue=False,
        )

        # Fix point 1: no complementary-partition arb margin for the equivalent pair,
        # while the genuine complementary pair keeps its real positive margin.
        assert equivalent_quality["profitMargin"] == "0"
        assert equivalent_quality["rawProfitMargin"] == "0"
        assert Decimal(complementary_quality["profitMargin"]) > 0

        counters = node_runner.ProbeProfitabilityCounters()
        node_runner._record_probe_quality(counters, equivalent_quality)
        node_runner._record_probe_quality(counters, complementary_quality)
        payload = counters.to_payload()

        positive_relationships = {
            candidate["relationshipType"] for candidate in payload["sample_candidates"]
        }
        value_relationships = {
            candidate["relationshipType"] for candidate in payload["value_edge_candidates"]
        }

        # Fix point 2: the equivalent pair is excluded from the positive-arb stream,
        # while the genuine complementary pair is still recorded there.
        assert "EQUIVALENT_SELECTION" not in positive_relationships
        assert "COMPLEMENTARY_COVERAGE" in positive_relationships
        # Fix point 3: the equivalent pair is still surfaced in the value-edge stream.
        assert "EQUIVALENT_SELECTION" in value_relationships

    def test_record_probe_quality_keeps_positive_equivalent_out_of_positive_stream(self):
        # Even if a positive complementary-partition margin reaches the recorder, an
        # EQUIVALENT_SELECTION candidate must never be filed as a positive-arb sample.
        counters = node_runner.ProbeProfitabilityCounters()
        quality = {
            "profitMargin": "4.88",
            "marginBand": "positive",
            "rejectionBucket": "positive",
            "venuePair": "CLOUDBET->CLOUDBET",
            "marketFamily": "match_odds",
            "relationshipType": "EQUIVALENT_SELECTION",
            "devig": {"enabled": False},
        }

        node_runner._record_probe_quality(counters, quality)

        assert counters.to_payload()["sample_candidates"] == []

    def test_record_probe_opportunity_positive_counters_ignore_equivalent_selection(self):
        counters = node_runner.ProbeProfitabilityCounters()
        opportunity = SimpleNamespace(profit_margin=Decimal("0.05"))

        node_runner._record_probe_opportunity(
            counters,
            opportunity=opportunity,
            edge=SimpleNamespace(relationship_type="EQUIVALENT_SELECTION"),
            source_node=None,
            target_node=None,
            allow_same_venue=True,
            min_profit_margin=Decimal("0.02"),
        )

        assert counters.positive_same_venue == 0
        assert counters.threshold_same_venue == 0

        node_runner._record_probe_opportunity(
            counters,
            opportunity=opportunity,
            edge=SimpleNamespace(relationship_type="COMPLEMENTARY_COVERAGE"),
            source_node=None,
            target_node=None,
            allow_same_venue=False,
            min_profit_margin=Decimal("0.02"),
        )

        assert counters.positive_execution == 1
        assert counters.threshold_execution == 1

    def test_runtime_probe_coverage_book_devig_uses_quoted_hyperedges(self):
        instrument_a = _instrument(venue="CLOUDBET", market_type="match_odds", outcome="home")
        instrument_b = _instrument(venue="CLOUDBET", market_type="match_odds", outcome="away")
        nodes = {
            str(instrument_a.id): SimpleNamespace(instrument=instrument_a),
            str(instrument_b.id): SimpleNamespace(instrument=instrument_b),
        }
        quotes = {
            str(instrument_a.id): SimpleNamespace(odds=Decimal("2.20")),
            str(instrument_b.id): SimpleNamespace(odds=Decimal("2.20")),
        }
        strategy = SimpleNamespace(
            fee_adjusted_coverage_basket=lambda instruments, odds: fee_adjusted_coverage_basket(
                odds,
                devig_method="proportional",
            ),
        )
        coverage_diagnostics = {
            "sampleHyperedges": [
                {
                    "hyperedge_id": "hyperedge-1",
                    "coverage_proof_id": "proof-1",
                    "instrument_ids": [str(instrument_a.id), str(instrument_b.id)],
                    "provider_scope": ["CLOUDBET"],
                    "safety_tier": "EXECUTION_SAFE",
                    "execution_safe": True,
                },
            ],
        }

        payload = node_runner._probe_coverage_book_devig_diagnostics(
            strategy,
            coverage_diagnostics=coverage_diagnostics,
            nodes=nodes,
            quotes=quotes,
            min_profit_margin=Decimal("0.02"),
        )

        assert payload["sampledHyperedges"] == 1
        assert payload["quotedHyperedges"] == 1
        assert payload["methodCounts"] == {"proportional": 1}
        assert payload["valueBuckets"] == {"coverage_locked_execution_safe_arbitrage": 1}
        assert payload["samples"][0]["hyperedgeId"] == "hyperedge-1"

    def test_runtime_probe_coverage_book_devig_bridges_semantic_predicates_to_runtime_nodes(self):
        instrument_a = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="home",
            event_name="CLE Cavaliers vs MIN Timberwolves",
            home_name="CLE Cavaliers",
            away_name="MIN Timberwolves",
            sport_name="basketball",
        )
        instrument_b = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
            event_name="Cleveland Cavaliers vs Minnesota Timberwolves",
            home_name="Cleveland Cavaliers",
            away_name="Minnesota Timberwolves",
            sport_name="basketball",
        )
        nodes = {
            str(instrument_a.id): SimpleNamespace(instrument=instrument_a),
            str(instrument_b.id): SimpleNamespace(instrument=instrument_b),
        }
        quotes = {
            str(instrument_a.id): SimpleNamespace(odds=Decimal("2.20")),
            str(instrument_b.id): SimpleNamespace(odds=Decimal("2.20")),
        }
        strategy = SimpleNamespace(
            fee_adjusted_coverage_basket=lambda instruments, odds: fee_adjusted_coverage_basket(
                odds,
                devig_method="proportional",
            ),
        )
        coverage_diagnostics = {
            "sampleHyperedges": [
                {
                    "hyperedge_id": "hyperedge-semantic",
                    "coverage_proof_id": "proof-semantic",
                    "instrument_ids": ["semantic-home", "semantic-away"],
                    "provider_scope": ["CLOUDBET"],
                    "safety_tier": "EXECUTION_SAFE",
                    "execution_safe": True,
                    "predicates": [
                        {
                            "instrument_id": "semantic-home",
                            "provider": "CLOUDBET",
                            "event_key": "basketball|cle_cavaliers|min_timberwolves",
                            "sport": "basketball",
                            "scope": "full_time",
                            "market_family": "MATCH_ODDS",
                            "selection": "HOME",
                            "params_key": "[]",
                        },
                        {
                            "instrument_id": "semantic-away",
                            "provider": "CLOUDBET",
                            "event_key": "basketball|cle_cavaliers|min_timberwolves",
                            "sport": "basketball",
                            "scope": "full_time",
                            "market_family": "MATCH_ODDS",
                            "selection": "AWAY",
                            "params_key": "[]",
                        },
                    ],
                },
            ],
        }

        payload = node_runner._probe_coverage_book_devig_diagnostics(
            strategy,
            coverage_diagnostics=coverage_diagnostics,
            nodes=nodes,
            quotes=quotes,
            min_profit_margin=Decimal("0.02"),
        )

        assert payload["quotedHyperedges"] == 1
        assert payload["incompleteHyperedges"] == 0
        assert payload["samples"][0]["instrumentIds"] == [
            str(instrument_a.id),
            str(instrument_b.id),
        ]
        assert payload["samples"][0]["semanticInstrumentIds"] == [
            "semantic-home",
            "semantic-away",
        ]

    def test_runtime_probe_coverage_book_devig_builds_node_index_once(self, monkeypatch):
        instrument_a = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="home",
            event_name="CLE Cavaliers vs MIN Timberwolves",
            home_name="CLE Cavaliers",
            away_name="MIN Timberwolves",
            sport_name="basketball",
        )
        instrument_b = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="away",
            event_name="Cleveland Cavaliers vs Minnesota Timberwolves",
            home_name="Cleveland Cavaliers",
            away_name="Minnesota Timberwolves",
            sport_name="basketball",
        )
        nodes = {
            str(instrument_a.id): SimpleNamespace(instrument=instrument_a),
            str(instrument_b.id): SimpleNamespace(instrument=instrument_b),
        }
        quotes = {
            str(instrument_a.id): SimpleNamespace(odds=Decimal("2.20")),
            str(instrument_b.id): SimpleNamespace(odds=Decimal("2.20")),
        }
        strategy = SimpleNamespace(
            fee_adjusted_coverage_basket=lambda instruments, odds: fee_adjusted_coverage_basket(
                odds,
                devig_method="proportional",
            ),
        )
        predicates = [
            {
                "instrument_id": "semantic-home",
                "provider": "CLOUDBET",
                "event_key": "basketball|cle_cavaliers|min_timberwolves",
                "sport": "basketball",
                "scope": "full_time",
                "market_family": "MATCH_ODDS",
                "selection": "HOME",
                "params_key": "[]",
            },
            {
                "instrument_id": "semantic-away",
                "provider": "CLOUDBET",
                "event_key": "basketball|cle_cavaliers|min_timberwolves",
                "sport": "basketball",
                "scope": "full_time",
                "market_family": "MATCH_ODDS",
                "selection": "AWAY",
                "params_key": "[]",
            },
        ]
        coverage_diagnostics = {
            "sampleHyperedges": [
                {
                    "hyperedge_id": f"hyperedge-{i}",
                    "coverage_proof_id": f"proof-{i}",
                    "instrument_ids": ["semantic-home", "semantic-away"],
                    "provider_scope": ["CLOUDBET"],
                    "safety_tier": "EXECUTION_SAFE",
                    "execution_safe": True,
                    "predicates": predicates,
                }
                for i in range(3)
            ],
        }

        real_index = node_runner._coverage_runtime_node_index
        index_builds = []

        def counting_index(nodes_arg, quoted_ids):
            index_builds.append(1)
            return real_index(nodes_arg, quoted_ids)

        monkeypatch.setattr(node_runner, "_coverage_runtime_node_index", counting_index)

        payload = node_runner._probe_coverage_book_devig_diagnostics(
            strategy,
            coverage_diagnostics=coverage_diagnostics,
            nodes=nodes,
            quotes=quotes,
            min_profit_margin=Decimal("0.02"),
        )

        assert len(index_builds) == 1
        assert payload["sampledHyperedges"] == 3
        assert payload["quotedHyperedges"] == 3
        assert payload["incompleteHyperedges"] == 0

    def test_runtime_probe_coverage_book_devig_reports_missing_semantic_legs(self):
        instrument_a = _instrument(
            venue="CLOUDBET",
            market_type="match_odds",
            outcome="home",
            sport_name="basketball",
        )
        nodes = {str(instrument_a.id): SimpleNamespace(instrument=instrument_a)}
        quotes = {str(instrument_a.id): SimpleNamespace(odds=Decimal("2.20"))}
        strategy = SimpleNamespace(
            fee_adjusted_coverage_basket=lambda instruments, odds: fee_adjusted_coverage_basket(
                odds,
                devig_method="proportional",
            ),
        )

        payload = node_runner._probe_coverage_book_devig_diagnostics(
            strategy,
            coverage_diagnostics={
                "sampleHyperedges": [
                    {
                        "hyperedge_id": "hyperedge-missing",
                        "coverage_proof_id": "proof-missing",
                        "instrument_ids": ["semantic-home", "semantic-away"],
                        "predicates": [
                            {
                                "instrument_id": "semantic-away",
                                "provider": "CLOUDBET",
                                "event_key": "basketball|missing_home|missing_away",
                                "sport": "basketball",
                                "scope": "full_time",
                                "market_family": "MATCH_ODDS",
                                "selection": "AWAY",
                                "params_key": "[]",
                            },
                        ],
                    },
                ],
            },
            nodes=nodes,
            quotes=quotes,
            min_profit_margin=Decimal("0.02"),
        )

        assert payload["quotedHyperedges"] == 0
        assert payload["incompleteHyperedges"] == 1
        assert payload["valueBuckets"] == {"coverage_reference_book_incomplete": 1}
        assert payload["samples"][0]["missingInstrumentIds"] == ["semantic-away"]

    def test_instrument_refresh_payload_includes_per_venue_counts(self):
        payload = node_runner._instrument_refresh_payload(
            {
                "instrument_refresh_requests": 3,
                "instrument_refresh_failures": 1,
                "instrument_refresh_added": 4,
                "instrument_refresh_removed": 2,
                "instrument_refresh_delisted_removed": 2,
                "instrument_refresh_reconciles": 3,
                "instrument_refresh_graph_rebuilds": 2,
                "instrument_refresh_stale_triggers": 1,
                "quote_unsubscribe_requests": 2,
                "instrument_refresh_by_venue": {
                    "SXBET": {
                        "requests": 2,
                        "failures": 1,
                        "added": 4,
                        "removed": 2,
                        "delisted_removed": 2,
                        "reconciles": 3,
                        "graph_rebuilds": 2,
                        "stale_triggers": 1,
                        "quote_unsubscribe_requests": 2,
                    },
                },
                "latency_diagnostics": {
                    "instrument_refresh_reconcile": {"count": 3, "p95_ms": 1200.0},
                },
            },
        )

        assert payload["venues"]["SXBET"]["requests"] == 2
        assert payload["venues"]["SXBET"]["quote_unsubscribe_requests"] == 2
        assert payload["reconcileLatency"]["p95_ms"] == 1200.0

    def test_runtime_manifest_rewrite_includes_semantic_cache_dir(self):
        deploy_script = Path(
            "scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh",
        ).read_text()
        assert (
            'data["semantic_rule_cache_dir"] = "/var/lib/nautilus-node/semantic-rule-cache"'
            in deploy_script
        )
        assert (
            'data["semantic_rule_cache_seed_dir"] = "/var/lib/nautilus-node/semantic-rule-cache-seed"'
            in deploy_script
        )
        assert 'ensure_dir "$node_dir/semantic-rule-cache-seed"' in deploy_script
        assert 'rm -f "$node_dir/status.json" "$node_dir/heartbeat.json"' in deploy_script

    def test_release_workflow_validates_sxbet_manifest_with_semantic_env(self):
        workflow = Path(".github/workflows/strategy-node-release.yml").read_text()
        assert "Validate SX.bet manifest" in workflow
        assert "Probe SX.bet runtime semantic coverage" in workflow
        assert "Validate Cloudbet manifest" in workflow
        assert "Probe Cloudbet runtime semantic coverage" in workflow
        assert "Probe selected multi-venue runtime semantic coverage" in workflow
        assert "cloudbet-single-venue.json" in workflow
        assert "probe-runtime" in workflow
        assert "--min-quoted-match-instruments 2" in workflow
        assert "--min-positive-margin-candidates 0" in workflow
        assert "--require-cross-venue-candidates-or-blockers" in workflow
        assert "--min-quoted-node-count CLOUDBET:2" in workflow
        assert "--min-quoted-node-count POLYMARKET:2" in workflow
        assert "--min-quoted-node-count SXBET:2" in workflow
        assert "--allow-subscription-fallback" in workflow
        assert "min_positive_margin_candidates=0" in workflow
        assert (
            '[ "$manifest_path" = "deploy/strategy_nodes/betting_arbitrage/multi-venue-validation.json" ]'
            in workflow
        )
        assert "require_cross_venue_candidates_or_blockers=true" in workflow
        assert "report_args+=(--require-cross-venue-candidates-or-blockers)" in workflow
        assert "wait_timeout_seconds=1800" in workflow
        assert '--timeout-seconds "$wait_timeout_seconds"' in workflow
        assert '--min-positive-margin-candidates "$min_positive_margin_candidates"' in workflow
        assert '--min-cross-venue-candidates "$min_cross_venue_candidates"' in workflow
        assert '"${cross_venue_args[@]}"' in workflow
        assert "--require-rust-semantic-topology" in workflow
        assert "Wait for deployed node status and semantic cache" in workflow
        assert "--require-runtime-probe" in workflow
        assert "Overlay branch strategy-node sources onto installed wheel" in workflow
        assert "Build validated wheel from checked-out source" in workflow
        assert "SXBET_API_KEY: ${{ secrets.SXBET_API_KEY }}" in workflow
        assert "CLOUDBET_API_KEY: ${{ secrets.CLOUDBET_API_KEY }}" in workflow
        assert "POLYMARKET_API_SECRET: ${{ secrets.POLYMARKET_API_SECRET }}" in workflow
        assert "Validate selected dispatch manifest" in workflow
        assert "INPUT_MANIFEST_PATH: ${{ github.event.inputs.manifest_path }}" in workflow
        assert "append_env_secret CLOUDBET_API_KEY" in workflow
        assert "runs-on: [self-hosted, linux, x64, ec2, deploy, trading]" in workflow
        assert "Prepare local deploy assets" in workflow
        assert "Load strategy-node image archive" in workflow
        assert '--env-file "$LOCAL_DEPLOY_ENV_FILE"' in workflow
        assert "current-session.json" in workflow
        assert "manifest.runtime release current-session" in workflow
        assert "$artifact_dir/$name.json" in workflow
        assert "node.log" in workflow
        assert "events.jsonl" in workflow
        assert '"executionReadiness": status.get("executionReadiness")' in workflow
        assert "runtime_probe_summary = dict(runtime)" in workflow
        assert "latencyDiagnostics" in workflow
        assert "providerQuotePollStats" in workflow
        assert "zeroCandidateVenuePairSamples" in workflow
        assert "venueQuoteHealth" in workflow
        assert "runtime-report.json" in workflow
        assert "Evaluate deployed runtime report" in workflow
        assert "scripts/betting/runtime_probe_report.py" in workflow
        tmp_artifact_dir = "/" + "tmp/artifacts/strategy-nodes"
        assert tmp_artifact_dir in workflow
        assert "--require-auto-execute-false" in workflow
        assert "--require-validation-mode" in workflow
        assert "--require-rust-semantic" in workflow
        assert "--require-coverage-runtime" in workflow
        assert "--min-quoted-semantic-instruments 2" in workflow
        assert "Upload deployed node status artifacts to transient CI storage" in workflow

    def test_runtime_verify_workflow_dumps_logs_on_failure(self):
        workflow = Path(".github/workflows/strategy-node-runtime-verify.yml").read_text()
        assert "timeout-minutes: 30" in workflow
        assert "default: '900'" in workflow
        assert 'timeout_seconds="${INPUT_TIMEOUT_SECONDS:-900}"' in workflow
        assert "persist_node_runtime_artifacts" in workflow
        assert "dump_node_runtime_artifacts() {" in workflow
        assert (
            "persist_node_runtime_artifacts"
            in workflow.split("dump_node_runtime_artifacts() {", maxsplit=1)[1]
        )
        assert "dump_node_runtime_artifacts" in workflow
        assert "trap 'status=$?;" in workflow
        assert "node_log_tail" in workflow
        assert "events_tail" in workflow
        assert "min_cross_venue_candidates" in workflow
        assert "min_quoted_node_counts" in workflow
        assert "runtime_probe_summary<<EOF" in workflow
        assert "manifest.runtime.json" in workflow
        assert "release.json" in workflow
        assert "coverageProofCount" in workflow
        assert "coverageHyperedgeCount" in workflow
        assert "coverageDiagnostics" in workflow
        assert "latencyDiagnostics" in workflow
        assert "runtime_probe_summary = dict(runtime_probe)" in workflow
        assert '"executionReadiness": status.get("executionReadiness")' in workflow
        assert "zeroCandidateVenuePairSamples" in workflow
        assert "runtime-report.json" in workflow
        assert "semantic_verify_enabled" in workflow
        assert "semantic_verify_required_providers" in workflow
        assert "semantic_verify_target_sports" in workflow
        assert "scripts/betting/runtime_probe_report.py" in workflow
        assert "runtime_expectation" in workflow
        assert "--require-auto-execute-false" in workflow
        assert "--require-validation-mode" in workflow
        assert "--require-live-execution-env-unarmed" in workflow
        assert "--require-cross-currency-live-blocked" in workflow
        assert "Unsupported runtime_expectation=$runtime_expectation" in workflow
        assert "--require-rust-semantic" in workflow
        assert "--require-coverage-runtime" in workflow
        assert "--min-quoted-semantic-instruments 2" in workflow
        assert "verify_semantic_cache_completion.py" in workflow
        assert ".venv/bin/python" not in workflow
        assert "semantic-completion.json" in workflow
        assert "semantic-completion.stderr" in workflow
        assert "sudo -n python3" in workflow
        assert "semantic_completion_verifier_failed_before_json_output" in workflow

    def test_strategy_node_maintenance_workflow_archives_before_stop(self):
        workflow = Path(".github/workflows/strategy-node-maintenance.yml").read_text()
        script = Path("scripts/deploy/strategy_nodes/archive_strategy_nodes.sh").read_text()

        assert "workflow_dispatch" in workflow
        assert "archive_strategy_nodes.sh" in workflow
        assert "self-hosted" in workflow
        assert "trading" in workflow
        assert "docker container inspect" in script
        assert "docker logs" in script
        assert "docker stats" in script
        assert "docker stop" in script
        assert "remove:" in workflow
        assert "docker rm" in script
        assert "node_dir_removed=true" in script

    def test_runner_cleanup_preserves_target_cache_by_default(self):
        script = Path("scripts/ci/self_hosted_runner_cleanup.sh").read_text()

        assert 'prune_target_artifacts="${RUNNER_PRUNE_TARGET_ARTIFACTS:-false}"' in script
        assert "-path '*/target/*'" in script
        assert 'if [[ "$prune_target_artifacts" == "true" ]]; then' in script
        default_artifact_block = script.split(
            'if [[ "$prune_target_artifacts" == "true" ]]; then',
            maxsplit=1,
        )[0]
        assert "-path '*/target/*'" not in default_artifact_block

    def test_wait_for_strategy_node_status_can_require_ready_semantic_cache(self, tmp_path):
        status_path = tmp_path / "status.json"
        script_path = Path(
            "scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh",
        ).resolve()
        status_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "semanticCache": {
                        "ready": True,
                    },
                    "runtimeProbe": {
                        "graphEngine": "rust",
                        "topologySource": "rust_semantic",
                        "semanticTemplateCount": 2,
                        "connectedNodes": 2,
                        "semanticMatchInstruments": 2,
                        "quotedSemanticMatchInstruments": 2,
                        "positiveMarginCandidates": {"total": 0},
                        "venueCoverage": {
                            "crossVenueCandidateCount": 1,
                            "quotedNodeCounts": {
                                "CLOUDBET": 2,
                                "SXBET": 2,
                            },
                        },
                    },
                },
            ),
        )

        result = subprocess.run(  # noqa: S603
            [
                str(script_path),
                "--status-file",
                str(status_path),
                "--timeout-seconds",
                "5",
                "--success-status",
                "running",
                "--require-semantic-cache-ready",
                "--require-runtime-probe",
                "--require-rust-semantic-topology",
                "--min-connected-nodes",
                "2",
                "--min-match-instruments",
                "2",
                "--min-quoted-match-instruments",
                "2",
                "--min-positive-margin-candidates",
                "0",
                "--min-cross-venue-candidates",
                "1",
                "--min-quoted-node-count",
                "CLOUDBET:2",
                "--min-quoted-node-count",
                "SXBET:2",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    def test_wait_for_strategy_node_status_accepts_cross_venue_blocker_samples(self, tmp_path):
        status_path = tmp_path / "status.json"
        script_path = Path(
            "scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh",
        ).resolve()
        status_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "semanticCache": {
                        "ready": True,
                    },
                    "runtimeProbe": {
                        "graphEngine": "rust",
                        "topologySource": "rust_semantic",
                        "semanticTemplateCount": 2,
                        "connectedNodes": 2,
                        "semanticMatchInstruments": 2,
                        "quotedSemanticMatchInstruments": 2,
                        "positiveMarginCandidates": {"total": 1},
                        "venueCoverage": {
                            "crossVenueCandidateCount": 0,
                            "quotedNodeCounts": {
                                "POLYMARKET": 2,
                                "SXBET": 2,
                            },
                            "zeroCandidateVenuePairs": [
                                {
                                    "venuePair": "POLYMARKET->SXBET",
                                    "blockerReason": "fixture_identity_mismatch",
                                },
                            ],
                        },
                    },
                },
            ),
        )

        result = subprocess.run(  # noqa: S603
            [
                str(script_path),
                "--status-file",
                str(status_path),
                "--timeout-seconds",
                "5",
                "--success-status",
                "running",
                "--require-semantic-cache-ready",
                "--require-runtime-probe",
                "--require-rust-semantic-topology",
                "--min-connected-nodes",
                "2",
                "--min-match-instruments",
                "2",
                "--min-quoted-match-instruments",
                "2",
                "--min-positive-margin-candidates",
                "1",
                "--min-cross-venue-candidates",
                "1",
                "--require-cross-venue-candidates-or-blockers",
                "--min-quoted-node-count",
                "POLYMARKET:2",
                "--min-quoted-node-count",
                "SXBET:2",
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr


def _cold_start_probe_payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "graphEngine": "rust",
        "topologySource": "rust_semantic",
        "semanticTemplateCount": 2,
        "connectedNodes": 2,
        "semanticMatchInstruments": 2,
        "quotedSemanticMatchInstruments": 0,
        "quotedEdges": 0,
        "positiveMarginCandidates": {"total": 0},
        "venueCoverage": {
            "crossVenueCandidateCount": 0,
            "quotedNodeCounts": {"POLYMARKET": 0, "SXBET": 0},
            "quoteSubscriptionCounts": {"POLYMARKET": 3, "SXBET": 2},
        },
        "quoteObservationState": {"status": "subscribed_but_no_quotes"},
    }
    payload.update(overrides)
    return payload


_MULTI_VENUE_GATE_KWARGS = {
    "min_connected_nodes": 2,
    "min_match_instruments": 2,
    "min_quoted_match_instruments": 2,
    "min_positive_margin_candidates": 1,
    "require_cross_venue_candidates_or_blockers": True,
    "min_quoted_node_counts": {"POLYMARKET": 2, "SXBET": 2},
    "require_rust_semantic_topology": True,
}


def test_runtime_probe_subscription_fallback_engages_when_subscribed_but_unquoted() -> None:
    payload = _cold_start_probe_payload()

    assert node_runner._runtime_probe_satisfied(
        payload,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )
    assert payload["subscriptionFallback"] == {
        "engaged": True,
        "venues": ["POLYMARKET", "SXBET"],
        "quoteObservationStatus": "subscribed_but_no_quotes",
    }


def test_runtime_probe_subscription_fallback_accepts_partial_quote_gap() -> None:
    payload = _cold_start_probe_payload(
        venueCoverage={
            "crossVenueCandidateCount": 0,
            "quotedNodeCounts": {"POLYMARKET": 2, "SXBET": 1},
            "quoteSubscriptionCounts": {"POLYMARKET": 3, "SXBET": 2},
        },
        quoteObservationState={"status": "partial_subscription_quote_gap"},
    )

    assert node_runner._runtime_probe_satisfied(
        payload,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )
    assert payload["subscriptionFallback"]["venues"] == ["SXBET"]


def test_runtime_probe_default_stays_strict_without_fallback_flag() -> None:
    payload = _cold_start_probe_payload()

    assert not node_runner._runtime_probe_satisfied(
        payload,
        **_MULTI_VENUE_GATE_KWARGS,
    )
    assert "subscriptionFallback" not in payload


def test_runtime_probe_subscription_fallback_requires_subscription_minimums() -> None:
    payload = _cold_start_probe_payload(
        venueCoverage={
            "crossVenueCandidateCount": 0,
            "quotedNodeCounts": {"POLYMARKET": 0, "SXBET": 0},
            "quoteSubscriptionCounts": {"POLYMARKET": 3, "SXBET": 1},
        },
    )

    assert not node_runner._runtime_probe_satisfied(
        payload,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )
    assert "subscriptionFallback" not in payload


def test_runtime_probe_subscription_fallback_rejects_no_quote_subscriptions_state() -> None:
    payload = _cold_start_probe_payload(
        venueCoverage={
            "crossVenueCandidateCount": 0,
            "quotedNodeCounts": {"POLYMARKET": 0, "SXBET": 0},
            "quoteSubscriptionCounts": {"POLYMARKET": 0, "SXBET": 0},
        },
        quoteObservationState={"status": "no_quote_subscriptions"},
    )

    assert not node_runner._runtime_probe_satisfied(
        payload,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )


def test_runtime_probe_subscription_fallback_keeps_wiring_checks_strict() -> None:
    below_min_connections = _cold_start_probe_payload(connectedNodes=1)
    assert not node_runner._runtime_probe_satisfied(
        below_min_connections,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )

    no_instruments = _cold_start_probe_payload(semanticMatchInstruments=0)
    assert not node_runner._runtime_probe_satisfied(
        no_instruments,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )

    missing_topology = _cold_start_probe_payload(topologySource="python")
    assert not node_runner._runtime_probe_satisfied(
        missing_topology,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )


def test_runtime_probe_strict_pass_does_not_flag_fallback() -> None:
    payload = _cold_start_probe_payload(
        quotedSemanticMatchInstruments=2,
        quotedEdges=1,
        positiveMarginCandidates={"total": 1},
        venueCoverage={
            "crossVenueCandidateCount": 1,
            "quotedNodeCounts": {"POLYMARKET": 2, "SXBET": 2},
            "quoteSubscriptionCounts": {"POLYMARKET": 3, "SXBET": 2},
        },
        quoteObservationState={"status": "quotes_observed"},
    )

    assert node_runner._runtime_probe_satisfied(
        payload,
        **_MULTI_VENUE_GATE_KWARGS,
        allow_subscription_fallback=True,
    )
    assert "subscriptionFallback" not in payload


def test_probe_rag_band_classifies_green_amber_red() -> None:
    # profitable -> green
    assert node_runner._probe_rag_band(Decimal("0.05")) == "green"
    assert node_runner._probe_rag_band(Decimal("0.0001")) == "green"
    # slightly unprofitable (0% to -5%, inclusive) -> amber
    assert node_runner._probe_rag_band(Decimal(0)) == "amber"
    assert node_runner._probe_rag_band(Decimal("-0.03")) == "amber"
    assert node_runner._probe_rag_band(Decimal("-0.05")) == "amber"
    # unprofitable (worse than -5%) -> red
    assert node_runner._probe_rag_band(Decimal("-0.0501")) == "red"
    assert node_runner._probe_rag_band(Decimal("-0.20")) == "red"


def _provider_pattern_key(instrument: CryptoBettingInstrument) -> tuple[str, ...]:
    normalized = MarketNormalizer.normalize(instrument)
    return (
        normalized.venue,
        normalized.sport,
        normalized.scope,
        normalized.market_type,
        normalized.market_family,
        normalized.selection,
        node_runner._semantic_params_key(normalized.params),
    )


def test_synthetic_line_node_pattern_matches_line_keyed_template() -> None:
    node_key = _provider_pattern_key(
        _instrument(
            venue="CLOUDBET",
            market_type="total_sets",
            outcome="over",
            sport_name="tennis",
            params="total=2.5",
        ),
    )
    template_key = _provider_pattern_key(
        _instrument(
            venue="CLOUDBET",
            market_type="total_sets",
            outcome="over",
            sport_name="tennis",
            params="line=2.5",
        ),
    )

    assert node_key[-1] == '[["line","2.5"]]'
    assert node_key == template_key

    node_counts = Counter({node_key: 5})
    template_counts = Counter({template_key: 1})
    assert node_runner._supported_provider_node_count(node_counts, template_counts) == 5


def test_distinct_line_node_pattern_stays_unsupported() -> None:
    node_key = _provider_pattern_key(
        _instrument(
            venue="CLOUDBET",
            market_type="total_sets",
            outcome="over",
            sport_name="tennis",
            params="total=3.5",
        ),
    )
    template_key = _provider_pattern_key(
        _instrument(
            venue="CLOUDBET",
            market_type="total_sets",
            outcome="over",
            sport_name="tennis",
            params="line=2.5",
        ),
    )

    assert node_key != template_key

    node_counts = Counter({node_key: 5})
    template_counts = Counter({template_key: 1})
    assert node_runner._supported_provider_node_count(node_counts, template_counts) == 0
