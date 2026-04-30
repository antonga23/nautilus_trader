# skipcq: PYL-C0114, PYL-C0115, PYL-C0116, PYL-W0212, PYL-W0613
# skipcq: PYL-W0108, PYL-W1514, PYL-R0903, PYL-R0913, PYL-C0301
# skipcq: PYL-C0302, PYL-E0401, PYL-C0411
# pylint: disable=missing-module-docstring,missing-class-docstring
# pylint: disable=missing-function-docstring,protected-access,unused-argument
# pylint: disable=unnecessary-lambda,unspecified-encoding,too-few-public-methods
# pylint: disable=too-many-arguments,line-too-long,too-many-lines
# pylint: disable=import-error,wrong-import-order

import asyncio
import json
from decimal import Decimal
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import msgspec

import pytest

from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics import FileRuleCache
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics import TemplateSupportStats
from nautilus_trader.config import ImportableConfig
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig
from nautilus_trader.live.strategy_nodes.betting_arbitrage import builder as node_builder
from nautilus_trader.live.strategy_nodes.betting_arbitrage import runner as node_runner
from nautilus_trader.live.strategy_nodes.betting_arbitrage import semantic_cache as node_cache
from nautilus_trader.live.strategy_nodes.betting_arbitrage.builder import build_trading_node_config
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
) -> CryptoBettingInstrument:
    return CryptoBettingInstrument(
        venue=Venue(venue),
        event_id="event-1",
        event_name="Team A vs Team B",
        home_name="Team A",
        away_name="Team B",
        sport_name="soccer",
        competition_name="Test League",
        market_name=market_name or market_type,
        market_type=market_type,
        outcome=outcome,
        side=SelectionSide.BACK,
        price=2.1,
        currency=Currency.from_str("USDC"),
        params=params,
        handicap=handicap,
        start_time="2026-03-13T18:00:00Z",
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


def _manifest(tmp_path: Path, *, cache_dir: Path | None = None) -> BettingArbitrageNodeManifest:
    return BettingArbitrageNodeManifest(
        node_id="sxbet-node",
        trader_id="BETARB-TEST-SEM",
        validation_mode=True,
        allow_dummy_credentials=True,
        semantic_rule_cache_dir=str(cache_dir) if cache_dir is not None else None,
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
        assert config.strategies[0].config["opportunity_graph_engine"] == "auto"
        assert (
            config.strategies[0].config["semantic_rule_cache_dir"]
            == "artifacts/semantic-rule-cache/sxbet-validation"
        )

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
                    market_discovery_limit=None,
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
        assert data_client.config["api_key_pool"] == ("dummy-sxbet-api-key",)

    def test_cloudbet_data_client_receives_runtime_settings(self):
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
                    auto_subscribe_quote_ticks=True,
                    quote_subscription_limit=60,
                    order_book_poll_interval_secs=7.0,
                    order_book_poll_summary_interval_secs=31.0,
                    order_book_concurrency=3,
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
        assert data_client.config["instrument_provider"]["filters"]["limit"] == 40
        assert data_client.config["auto_subscribe_quote_ticks"] is True
        assert data_client.config["quote_subscription_limit"] == 60
        assert data_client.config["quote_poll_interval_secs"] == 7.0
        assert data_client.config["quote_poll_summary_interval_secs"] == 31.0
        assert data_client.config["quote_poll_concurrency"] == 3

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
            config=CloudbetDataClientConfig(),
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
        manifest = _manifest(tmp_path, cache_dir=cache_dir)
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
        manifest = _manifest(tmp_path, cache_dir=cache_dir)
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
        manifest = _manifest(tmp_path, cache_dir=cache_dir)

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

    def test_unusable_semantic_cache_fails_validation(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: None,
        )

        with pytest.raises(RuntimeError, match="usable cache"):
            ensure_semantic_cache_ready(manifest)

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

            def load_promoted_template(self, template_id):
                mapping = {
                    "exec-safe": SimpleNamespace(
                        safety_tier=node_cache.SafetyTier.EXECUTION_SAFE.value,
                    ),
                    "same-venue": SimpleNamespace(
                        safety_tier=(
                            node_cache.SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value
                        ),
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

        class FakePromotionPolicy:
            def promote_template(self, store, template):
                promoted.append((store, template))
                return template

        monkeypatch.setattr(node_cache, "FileRuleCache", lambda path: path)
        monkeypatch.setattr(node_cache, "RuleStore", FakeStore)
        monkeypatch.setattr(node_cache, "SnapshotIngestor", lambda store: store_marker)
        monkeypatch.setattr(node_cache, "RuleMiner", FakeMiner)
        monkeypatch.setattr(node_cache, "RulePromotionPolicy", FakePromotionPolicy)

        async def fake_required(*, manifest, venues, ingestor, logger):
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
        assert [template for _, template in promoted] == ["template-a", "template-b"]

    def test_refresh_required_sxbet_corpus_skips_when_no_sxbet_venues(self):
        ingestor = Mock()

        asyncio.run(
            node_cache._refresh_required_sxbet_corpus(
                manifest=None,
                venues=[BettingVenueManifest(venue="POLYMARKET")],
                ingestor=ingestor,
                logger=None,
            ),
        )

        assert not ingestor.mock_calls

    def test_refresh_required_sxbet_corpus_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("SXBET_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="SXBET_API_KEY"):
            asyncio.run(
                node_cache._refresh_required_sxbet_corpus(
                    manifest=None,
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
                sport_ids,
                from_time,
                to_time,
                instrument_limit,
                market_discovery_limit,
                prefer_liquid_markets,
                liquidity_probe_limit,
                min_two_sided_markets,
            ):
                refresh_calls.append(
                    {
                        "client": client,
                        "sport_ids": sport_ids,
                        "from_time": from_time,
                        "to_time": to_time,
                        "instrument_limit": instrument_limit,
                        "market_discovery_limit": market_discovery_limit,
                        "prefer_liquid_markets": prefer_liquid_markets,
                        "liquidity_probe_limit": liquidity_probe_limit,
                        "min_two_sided_markets": min_two_sided_markets,
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
                manifest=None,
                venues=venues,
                ingestor=FakeIngestor(),
                logger=None,
            ),
        )

        assert len(refresh_calls) == 1
        call = refresh_calls[0]
        assert call["sport_ids"] == [3, 77]
        assert call["from_time"] == 1_000_000 - 6 * 60 * 60
        assert call["to_time"] == 1_000_000 + 6 * 60 * 60
        assert call["instrument_limit"] == 300
        assert call["market_discovery_limit"] == 400
        assert call["prefer_liquid_markets"] is True
        assert call["liquidity_probe_limit"] == 350
        assert call["min_two_sided_markets"] == 2
        assert call["client"].connected is True
        assert call["client"].disconnected is True

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
                    manifest=None,
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


class TestBettingArbitrageNodeRunner:
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
            ],
        )

        assert result == 0
        assert observed_probe_kwargs["require_rust_semantic_topology"] is True
        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "probed"
        assert payload["runtimeProbe"]["graphEngine"] == "rust"
        assert payload["runtimeProbe"]["topologySource"] == "rust_semantic"
        assert payload["runtimeProbe"]["connectedNodes"] == 2
        assert payload["runtimeProbe"]["semanticMatchInstruments"] == 2
        assert payload["runtimeProbe"]["quotedSemanticMatchInstruments"] == 2
        assert payload["runtimeProbe"]["positiveMarginCandidates"]["total"] == 1

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
                        "provider_scope": ["SXBET"],
                        "safety_tier": "EXECUTION_SAFE_SAME_VENUE_ELIGIBLE",
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
        assert diagnostics["commonPatternKeyCount"] == 1
        assert diagnostics["nodeSports"] == [{"key": "soccer", "count": 1}]

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

    def test_semantic_cache_payload_helpers(self, tmp_path, monkeypatch):
        expected_status = SemanticCacheStatus(
            path=str(tmp_path / "semantic-cache"),
            source="bootstrapped",
            manifest_count=2,
            promoted_template_count=3,
            execution_safe_template_count=1,
            same_venue_execution_eligible_template_count=1,
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
            "compatibilityVersion": None,
            "compatibilityScope": None,
            "compatible": True,
        }
        assert node_runner._semantic_cache_payload(None) is None

    def test_runtime_manifest_rewrite_includes_semantic_cache_dir(self):
        deploy_script = Path(
            "scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh",
        ).read_text()
        assert (
            'data["semantic_rule_cache_dir"] = "/var/lib/nautilus-node/semantic-rule-cache"'
            in deploy_script
        )

    def test_release_workflow_validates_sxbet_manifest_with_semantic_env(self):
        workflow = Path(".github/workflows/strategy-node-release.yml").read_text()
        assert "Validate SX.bet manifest" in workflow
        assert "Probe SX.bet runtime semantic coverage" in workflow
        assert "Validate Cloudbet manifest" in workflow
        assert "Probe Cloudbet runtime semantic coverage" in workflow
        assert "cloudbet-single-venue.json" in workflow
        assert "probe-runtime" in workflow
        assert "--min-quoted-match-instruments 2" in workflow
        assert "--min-positive-margin-candidates 0" in workflow
        assert "--require-rust-semantic-topology" in workflow
        assert "Wait for deployed node status and semantic cache" in workflow
        assert "--require-runtime-probe" in workflow
        assert "Overlay branch strategy-node sources onto installed wheel" in workflow
        assert "Build validated wheel from checked-out source" in workflow
        assert "SXBET_API_KEY: ${{ secrets.SXBET_API_KEY }}" in workflow
        assert "CLOUDBET_API_KEY: ${{ secrets.CLOUDBET_API_KEY }}" in workflow

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
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
