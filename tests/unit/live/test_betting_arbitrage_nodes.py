import json
from decimal import Decimal
from pathlib import Path
import subprocess

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


class TestSemanticCacheBootstrap:
    def test_reuses_existing_semantic_cache_without_bootstrap(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "semantic-cache"
        _seed_promoted_template(cache_dir)
        manifest = _manifest(tmp_path, cache_dir=cache_dir)

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.semantic_cache._run_bootstrap",
            lambda **_: (_ for _ in ()).throw(AssertionError("bootstrap should not run")),
        )

        status = ensure_semantic_cache_ready(manifest)

        assert status.source == "existing"
        assert status.ready is True
        assert status.promoted_template_count >= 1

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


class TestBettingArbitrageNodeRunner:
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
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.runner.ensure_semantic_cache_ready",
            lambda _: expected_status,
        )

        result = runner_main(["validate-manifest", "--manifest", str(manifest_path)])

        assert result == 0
        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "validated"
        assert payload["semanticCache"]["source"] == "bootstrapped"
        assert payload["semanticCache"]["promotedTemplateCount"] == 2
        assert payload["semanticCache"]["sameVenueExecutionEligibleTemplateCount"] == 1

    def test_run_no_start_records_semantic_cache_status(self, tmp_path, monkeypatch):
        manifest = _manifest(tmp_path, cache_dir=tmp_path / "semantic-cache")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest.json())

        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.runner.ensure_semantic_cache_ready",
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
        monkeypatch.setattr(
            "nautilus_trader.live.strategy_nodes.betting_arbitrage.runner._probe_runtime",
            lambda **_: {
                "connectedNodes": 2,
                "semanticMatchInstruments": 2,
                "positiveMarginCandidates": {"executionSafe": 0, "sameVenueExecutionEligible": 1, "total": 1},
            },
        )

        class FakeTradingNode:
            def __init__(self, config):
                self.config = config

            def build(self):
                return None

            def dispose(self):
                return None

        monkeypatch.setattr("nautilus_trader.live.node.TradingNode", FakeTradingNode)

        result = runner_main(["probe-runtime", "--manifest", str(manifest_path)])

        assert result == 0
        payload = json.loads((tmp_path / "status.json").read_text())
        assert payload["status"] == "probed"
        assert payload["runtimeProbe"]["connectedNodes"] == 2
        assert payload["runtimeProbe"]["semanticMatchInstruments"] == 2
        assert payload["runtimeProbe"]["positiveMarginCandidates"]["total"] == 1

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
        assert "probe-runtime" in workflow
        assert "--min-positive-margin-candidates 1" in workflow
        assert "Wait for deployed node status and semantic cache" in workflow
        assert 'export PYTHONPATH="$GITHUB_WORKSPACE${PYTHONPATH:+:$PYTHONPATH}"' in workflow
        assert "SXBET_API_KEY: ${{ secrets.SXBET_API_KEY }}" in workflow
        assert "CLOUDBET_API_KEY: ${{ secrets.CLOUDBET_API_KEY }}" in workflow

    def test_wait_for_strategy_node_status_can_require_ready_semantic_cache(self, tmp_path):
        status_path = tmp_path / "status.json"
        script_path = Path("scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh").resolve()
        status_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "semanticCache": {
                        "ready": True,
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
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
