from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from nautilus_trader.config import ImportableConfig
from nautilus_trader.config import ImportableFactoryConfig
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import (
    BettingArbitrageNodeManifest,
)
from nautilus_trader.live.strategy_nodes.betting_arbitrage.config import BettingVenueManifest

SXBET_DATA_CONFIG_PATH = "nautilus_trader.adapters.sxbet.config:SXBetDataClientConfig"
SXBET_EXEC_CONFIG_PATH = "nautilus_trader.adapters.sxbet.config:SXBetExecClientConfig"
SXBET_DATA_FACTORY_PATH = "nautilus_trader.adapters.sxbet.factories:SXBetLiveDataClientFactory"
SXBET_EXEC_FACTORY_PATH = "nautilus_trader.adapters.sxbet.factories:SXBetLiveExecClientFactory"
CLOUDBET_DATA_CONFIG_PATH = "nautilus_trader.adapters.cloudbet.config:CloudbetDataClientConfig"
CLOUDBET_EXEC_CONFIG_PATH = "nautilus_trader.adapters.cloudbet.config:CloudbetExecClientConfig"
CLOUDBET_DATA_FACTORY_PATH = (
    "nautilus_trader.adapters.cloudbet.factories:CloudbetLiveDataClientFactory"
)
CLOUDBET_EXEC_FACTORY_PATH = (
    "nautilus_trader.adapters.cloudbet.factories:CloudbetLiveExecClientFactory"
)
POLYMARKET_DATA_CONFIG_PATH = (
    "nautilus_trader.adapters.polymarket.config:PolymarketDataClientConfig"
)
POLYMARKET_EXEC_CONFIG_PATH = (
    "nautilus_trader.adapters.polymarket.config:PolymarketExecClientConfig"
)
POLYMARKET_DATA_FACTORY_PATH = (
    "nautilus_trader.adapters.polymarket.factories:PolymarketLiveDataClientFactory"
)
POLYMARKET_EXEC_FACTORY_PATH = (
    "nautilus_trader.adapters.polymarket.factories:PolymarketLiveExecClientFactory"
)
STRATEGY_PATH = "nautilus_trader.examples.strategies.betting_arbitrage:BettingArbitrageStrategy"
STRATEGY_CONFIG_PATH = (
    "nautilus_trader.examples.strategies.betting_arbitrage:BettingArbitrageConfig"
)
DEFAULT_RENDER_ROOT = Path("artifacts/strategy-nodes")
SXBET_TESTNET_API_URL = "https://api.toronto.sx.bet"
SXBET_TESTNET_WS_URL = "wss://api.toronto.sx.bet"
SXBET_DEFAULT_BASE_CURRENCY = "USDC"
CLOUDBET_PAPER_BASE_CURRENCY = "PLAY_EUR"

DUMMY_SECRETS = {
    "SXBET_API_KEY": "dummy-sxbet-api-key",
    "SXBET_API_KEYS": "dummy-sxbet-api-key",
    "SXBET_PRIVATE_KEY": "0x" + "1" * 64,
    "SXBET_WALLET_ADDRESS": "0x" + "2" * 40,
    "CLOUDBET_API_KEY": "dummy-cloudbet-api-key",
    "POLYMARKET_API_KEY": "dummy-polymarket-api-key",
    "POLYMARKET_API_SECRET": "dummy-polymarket-api-secret",
    "POLYMARKET_PASSPHRASE": "dummy-polymarket-passphrase",
    "POLYMARKET_PRIVATE_KEY": "0x" + "3" * 64,
    "POLYMARKET_FUNDER": "0x" + "4" * 40,
}


class MissingCredentialError(RuntimeError):
    """
    Raised when a required venue credential is unavailable.
    """


def load_manifest(path: str | os.PathLike[str]) -> BettingArbitrageNodeManifest:
    raw = Path(path).read_bytes()
    return BettingArbitrageNodeManifest.parse(raw)


def manifest_to_json(manifest: BettingArbitrageNodeManifest) -> bytes:
    return manifest.json()


def render_trading_node_config_json(config: TradingNodeConfig) -> bytes:
    return config.json()


def write_rendered_node_config(
    config: TradingNodeConfig,
    output_path: str | os.PathLike[str],
) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(config.json())
    return destination


def build_trading_node_config(manifest: BettingArbitrageNodeManifest) -> TradingNodeConfig:
    enabled_venues = [venue for venue in manifest.venues if venue.enabled]
    strategy_config = _build_strategy_importable_config(manifest, enabled_venues)
    data_clients: dict[str, Any] = {}
    exec_clients: dict[str, Any] = {}

    for index, venue in enumerate(enabled_venues, start=1):
        client_key = _client_key(venue, index)
        _add_venue_clients(
            venue=venue,
            manifest=manifest,
            client_key=client_key,
            data_clients=data_clients,
            exec_clients=exec_clients,
        )

    logging = LoggingConfig(
        log_level=manifest.log_level,
        log_level_file=manifest.log_level,
        log_directory=manifest.log_directory,
        log_file_name=manifest.log_file_name,
        clear_log_file=False,
        use_pyo3=False,
    )

    exec_engine = LiveExecEngineConfig(
        reconciliation=manifest.reconciliation and bool(exec_clients),
        open_check_interval_secs=(manifest.open_check_interval_secs if exec_clients else None),
        open_check_open_only=True,
        graceful_shutdown_on_exception=True,
    )

    return TradingNodeConfig(
        trader_id=manifest.trader_id,
        logging=logging,
        exec_engine=exec_engine,
        strategies=[strategy_config],
        data_clients=data_clients,
        exec_clients=exec_clients,
        timeout_connection=manifest.timeout_connection,
        timeout_reconciliation=manifest.timeout_reconciliation,
        timeout_portfolio=manifest.timeout_portfolio,
        timeout_disconnection=manifest.timeout_disconnection,
        timeout_post_stop=manifest.timeout_post_stop,
        timeout_shutdown=manifest.timeout_shutdown,
    )


def manifest_execution_readiness(
    manifest: BettingArbitrageNodeManifest,
) -> dict[str, object]:
    enabled_venues = [venue for venue in manifest.venues if venue.enabled]
    venue_payloads: list[dict[str, object]] = []
    for index, venue in enumerate(enabled_venues, start=1):
        client_key = _client_key(venue, index)
        api_url, ws_url = _resolve_venue_endpoints(venue)
        venue_payloads.append(
            {
                "venue": venue.venue,
                "clientKey": client_key,
                "dataEnabled": venue.data_enabled,
                "executionEnabled": venue.execution_enabled,
                "executionDryRun": venue.execution_dry_run,
                "environment": venue.environment or "prod",
                "baseCurrency": _resolve_base_currency(venue),
                "apiUrl": api_url,
                "wsUrl": ws_url,
                "sportKeys": sorted(venue.sport_keys) if venue.sport_keys else [],
                "sportIds": sorted(venue.sport_ids) if venue.sport_ids else [],
                "liveOnly": venue.live_only,
                "loadAllInstruments": venue.load_all_instruments,
                "instrumentLoadLimit": venue.instrument_load_limit,
                "marketDiscoveryLimit": venue.market_discovery_limit,
            },
        )

    return {
        "validationMode": manifest.validation_mode,
        "autoExecute": manifest.strategy.auto_execute,
        "liveExecutionArmed": getattr(manifest.strategy, "live_execution_armed", False),
        "liveExecutionEnvArmed": os.getenv("BETTING_LIVE_EXECUTION_ARMED", "").strip().lower()
        in {"1", "true", "yes", "armed"},
        "allowSameVenueLiveExecution": getattr(
            manifest.strategy,
            "allow_same_venue_live_execution",
            False,
        ),
        "allowCrossCurrencyLiveExecution": getattr(
            manifest.strategy,
            "allow_cross_currency_live_execution",
            False,
        ),
        "executionVenueMode": getattr(manifest.strategy, "execution_venue_mode", "all"),
        "maxResolutionHorizonHours": getattr(
            manifest.strategy,
            "max_resolution_horizon_hours",
            None,
        ),
        "portfolioBaseCurrency": getattr(manifest.strategy, "portfolio_base_currency", "USD"),
        "stablecoinCurrencies": sorted(
            getattr(manifest.strategy, "stablecoin_currencies", frozenset({"USD", "USDC", "USDT"})),
        ),
        "stablecoinHaircutBps": getattr(manifest.strategy, "stablecoin_haircut_bps", 0),
        "fxQuoteMaxAgeSecs": getattr(manifest.strategy, "fx_quote_max_age_secs", 0),
        "riskCaps": {
            "maxLegStake": str(getattr(manifest.strategy, "max_leg_stake", "0")),
            "maxDailyNotional": str(getattr(manifest.strategy, "max_daily_notional", "0")),
            "maxDailyLoss": str(getattr(manifest.strategy, "max_daily_loss", "0")),
        },
        "semanticCacheConfigured": bool(manifest.semantic_rule_cache_dir),
        "venues": venue_payloads,
    }


def _add_venue_clients(
    *,
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
    client_key: str,
    data_clients: dict[str, Any],
    exec_clients: dict[str, Any],
) -> None:
    if venue.venue == "SXBET":
        data_builder = _build_sxbet_data_importable
        exec_builder = _build_sxbet_exec_importable
    elif venue.venue == "CLOUDBET":
        data_builder = _build_cloudbet_data_importable
        exec_builder = _build_cloudbet_exec_importable
    elif venue.venue == "POLYMARKET":
        data_builder = _build_polymarket_data_importable
        exec_builder = _build_polymarket_exec_importable
    else:
        raise ValueError(f"Unsupported venue {venue.venue}")

    if venue.data_enabled:
        data_clients[client_key] = data_builder(venue, manifest)
    if venue.execution_enabled and not manifest.validation_mode:
        exec_clients[client_key] = exec_builder(venue, manifest)


def _build_strategy_importable_config(
    manifest: BettingArbitrageNodeManifest,
    enabled_venues: list[BettingVenueManifest],
) -> ImportableStrategyConfig:
    strategy_config: dict[str, Any] = dict(manifest.strategy.json_primitives() or {})
    if not bool(strategy_config.get("opportunity_graph_enabled", True)):
        raise ValueError(
            "Strategy-node manifests must keep opportunity_graph_enabled=true",
        )
    requested_engine = str(strategy_config.get("opportunity_graph_engine") or "auto").lower()
    if requested_engine == "python":
        raise ValueError(
            "Strategy-node manifests must use semantic_rust opportunity topology",
        )
    if requested_engine in {"auto", "rust"}:
        strategy_config["opportunity_graph_engine"] = "semantic_rust"
    strategy_config["enabled_venues"] = sorted({venue.venue for venue in enabled_venues})
    semantic_quote_limits = {
        venue.venue: int(venue.quote_subscription_limit)
        for venue in enabled_venues
        if venue.quote_subscription_limit is not None
    }
    if semantic_quote_limits:
        strategy_config["semantic_quote_subscription_limit_by_venue"] = semantic_quote_limits
    if manifest.semantic_rule_cache_dir:
        strategy_config["semantic_rule_cache_dir"] = manifest.semantic_rule_cache_dir
    if manifest.status_path and not strategy_config.get("execution_approval_command_dir"):
        # Colocate the operator approve/reject command drop-box with the status file
        # so both ride the same node-dir bind mount that nodeops already reads.
        strategy_config["execution_approval_command_dir"] = str(
            Path(manifest.status_path).parent / "commands",
        )
    if manifest.validation_mode:
        strategy_config["auto_execute"] = False
        strategy_config["value_execution_enabled"] = False
    return ImportableStrategyConfig(
        strategy_path=STRATEGY_PATH,
        config_path=STRATEGY_CONFIG_PATH,
        config=strategy_config,
    )


def _build_sxbet_data_importable(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> ImportableConfig:
    prefix = _credential_prefix(venue)
    api_url, ws_url = _resolve_venue_endpoints(venue)
    api_key = _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials)
    api_key_pool = _resolve_secret_pool(prefix, "API_KEYS", manifest.allow_dummy_credentials)
    provider_config = {
        "api_key": api_key,
        "api_key_pool": api_key_pool,
        "api_url": api_url,
        "load_all": venue.load_all_instruments,
        "sport_ids": sorted(venue.sport_ids) if venue.sport_ids else None,
        "league_ids": sorted(venue.league_ids) if venue.league_ids else None,
        "live_only": venue.live_only,
        "instrument_load_limit": venue.instrument_load_limit,
        "market_discovery_limit": venue.market_discovery_limit,
        "prefer_liquid_markets": venue.prefer_liquid_markets,
        "liquidity_probe_limit": venue.liquidity_probe_limit,
        "min_two_sided_markets": venue.min_two_sided_markets,
        "max_resolution_horizon_hours": manifest.strategy.max_resolution_horizon_hours,
    }
    config = {
        "api_key": api_key,
        "api_key_pool": api_key_pool,
        "api_url": api_url,
        "ws_url": ws_url,
        "instrument_provider": provider_config,
        "sport_ids": sorted(venue.sport_ids) if venue.sport_ids else None,
        "reconnect_on_disconnect": True,
        "max_reconnect_attempts": 5,
        "auto_subscribe_quote_ticks": _provider_auto_subscribe_quote_ticks(
            venue,
            manifest,
        ),
        "quote_subscription_limit": venue.quote_subscription_limit,
        "order_book_poll_interval_secs": venue.order_book_poll_interval_secs,
        "order_book_poll_summary_interval_secs": venue.order_book_poll_summary_interval_secs,
        "order_book_concurrency": venue.order_book_concurrency,
        "order_book_poll_mode": venue.order_book_poll_mode,
        "order_book_best_odds_batch_size": venue.order_book_best_odds_batch_size,
        "order_book_min_concurrency": venue.order_book_min_concurrency,
        "order_book_max_concurrency": venue.order_book_max_concurrency,
        "order_book_target_cycle_secs": venue.order_book_target_cycle_secs,
        "order_book_adaptive_concurrency": venue.order_book_adaptive_concurrency,
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=SXBET_DATA_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=SXBET_DATA_FACTORY_PATH),
    )


def _build_sxbet_exec_importable(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> ImportableConfig:
    prefix = _credential_prefix(venue)
    api_url, ws_url = _resolve_venue_endpoints(venue)
    api_key = _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials)
    api_key_pool = _resolve_secret_pool(prefix, "API_KEYS", manifest.allow_dummy_credentials)
    provider_config = {
        "api_key": api_key,
        "api_key_pool": api_key_pool,
        "api_url": api_url,
        "load_all": venue.load_all_instruments,
        "sport_ids": sorted(venue.sport_ids) if venue.sport_ids else None,
        "league_ids": sorted(venue.league_ids) if venue.league_ids else None,
        "live_only": venue.live_only,
        "instrument_load_limit": venue.instrument_load_limit,
        "market_discovery_limit": venue.market_discovery_limit,
        "prefer_liquid_markets": venue.prefer_liquid_markets,
        "liquidity_probe_limit": venue.liquidity_probe_limit,
        "min_two_sided_markets": venue.min_two_sided_markets,
        "max_resolution_horizon_hours": manifest.strategy.max_resolution_horizon_hours,
    }
    config = {
        "api_key": api_key,
        "api_key_pool": api_key_pool,
        "private_key": _normalize_prefixed_hex_secret(
            _resolve_secret(prefix, "PRIVATE_KEY", manifest.allow_dummy_credentials),
            expected_nibbles=64,
        ),
        "wallet_address": _normalize_prefixed_hex_secret(
            _resolve_secret(
                prefix,
                "WALLET_ADDRESS",
                manifest.allow_dummy_credentials,
            ),
            expected_nibbles=40,
        ),
        "api_url": api_url,
        "ws_url": ws_url,
        "instrument_provider": provider_config,
        "max_retry_attempts": 3,
        "base_currency": _resolve_base_currency(venue),
        "dry_run": venue.execution_dry_run,
        "execution_mode": str((venue.metadata or {}).get("execution_mode") or "taker_fill"),
        "odds_slippage": int((venue.metadata or {}).get("odds_slippage") or 5),
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=SXBET_EXEC_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=SXBET_EXEC_FACTORY_PATH),
    )


def _build_cloudbet_data_importable(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> ImportableConfig:
    prefix = _credential_prefix(venue)
    api_url, _ = _resolve_venue_endpoints(venue)
    filters = _cloudbet_selection_filters(venue)
    provider_config = {
        "load_all": venue.load_all_instruments,
        "load_ids": sorted(venue.instrument_ids or []) if not venue.load_all_instruments else None,
        "filters": filters,
    }
    config = {
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "api_url": api_url,
        "instrument_provider": provider_config,
        "market_filter": tuple(sorted(filters.items())) if filters else None,
        "auto_subscribe_quote_ticks": _provider_auto_subscribe_quote_ticks(
            venue,
            manifest,
        ),
        "quote_subscription_limit": venue.quote_subscription_limit,
        "quote_poll_interval_secs": venue.order_book_poll_interval_secs,
        "quote_poll_summary_interval_secs": venue.order_book_poll_summary_interval_secs,
        "quote_poll_concurrency": venue.order_book_concurrency,
        "quote_poll_min_concurrency": venue.order_book_min_concurrency,
        "quote_poll_max_concurrency": venue.order_book_max_concurrency,
        "quote_poll_target_cycle_secs": venue.order_book_target_cycle_secs,
        "quote_poll_adaptive_concurrency": venue.order_book_adaptive_concurrency,
        "quote_poll_event_batching": venue.order_book_event_batching,
        "quote_poll_missing_prune_threshold": venue.order_book_missing_prune_threshold,
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=CLOUDBET_DATA_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=CLOUDBET_DATA_FACTORY_PATH),
    )


def _build_cloudbet_exec_importable(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> ImportableConfig:
    prefix = _credential_prefix(venue)
    api_url, _ = _resolve_venue_endpoints(venue)
    filters = _cloudbet_selection_filters(venue)
    config = {
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "api_url": api_url,
        "base_currency": _resolve_base_currency(venue),
        "market_filter": dict(filters) if filters else None,
        "dry_run": venue.execution_dry_run,
        "accept_price_change": str(
            (venue.metadata or {}).get("accept_price_change")
            or manifest.strategy.execution_price_change_policy
            or "better",
        ),
        "pending_acceptance_poll_attempts": int(
            (venue.metadata or {}).get("pending_acceptance_poll_attempts") or 3,
        ),
        "pending_acceptance_poll_interval_secs": float(
            (venue.metadata or {}).get("pending_acceptance_poll_interval_secs") or 0.5,
        ),
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=CLOUDBET_EXEC_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=CLOUDBET_EXEC_FACTORY_PATH),
    )


def _provider_auto_subscribe_quote_ticks(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> bool:
    if manifest.semantic_rule_cache_dir:
        return False
    return venue.auto_subscribe_quote_ticks


def _resolve_venue_endpoints(venue: BettingVenueManifest) -> tuple[str | None, str | None]:
    api_url = venue.api_url
    ws_url = venue.ws_url
    if venue.venue == "SXBET" and venue.environment == "testnet":
        api_url = api_url or SXBET_TESTNET_API_URL
        ws_url = ws_url or SXBET_TESTNET_WS_URL
    return api_url, ws_url


def _resolve_base_currency(venue: BettingVenueManifest) -> str | None:
    if venue.base_currency:
        return venue.base_currency
    if venue.venue == "SXBET":
        return SXBET_DEFAULT_BASE_CURRENCY
    if venue.venue == "CLOUDBET" and venue.environment == "paper":
        return CLOUDBET_PAPER_BASE_CURRENCY
    return None


def _cloudbet_selection_filters(venue: BettingVenueManifest) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if venue.sport_keys:
        filters["sport_key"] = sorted(venue.sport_keys)
    if venue.live_only:
        filters["live"] = "true"
    else:
        filters["live"] = "false"
    if venue.prefer_liquid_markets:
        filters["market_name"] = [
            "asian_handicap",
            "draw_no_bet",
            "handicap",
            "match_odds",
            "match_winner",
            "moneyline",
            "point_spread",
            "spread",
            "team_totals",
            "total_goals",
            "totals",
            "winner",
        ]
    if venue.instrument_load_limit is not None:
        filters["limit"] = int(venue.instrument_load_limit)
    return filters


def _build_polymarket_data_importable(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> ImportableConfig:
    prefix = _credential_prefix(venue)
    config = {
        "private_key": _resolve_secret(prefix, "PRIVATE_KEY", manifest.allow_dummy_credentials),
        "signature_type": venue.signature_type,
        "funder": _resolve_secret(prefix, "FUNDER", manifest.allow_dummy_credentials),
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "api_secret": _resolve_secret(prefix, "API_SECRET", manifest.allow_dummy_credentials),
        "passphrase": _resolve_secret(prefix, "PASSPHRASE", manifest.allow_dummy_credentials),
        "base_url_http": venue.api_url,
        "base_url_ws": venue.ws_url,
        "instrument_provider": _polymarket_instrument_provider_dict(venue, manifest),
        "compute_effective_deltas": False,
        "drop_quotes_missing_side": True,
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=POLYMARKET_DATA_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=POLYMARKET_DATA_FACTORY_PATH),
    )


def _build_polymarket_exec_importable(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> ImportableConfig:
    prefix = _credential_prefix(venue)
    config = {
        "private_key": _resolve_secret(prefix, "PRIVATE_KEY", manifest.allow_dummy_credentials),
        "signature_type": venue.signature_type,
        "funder": _resolve_secret(prefix, "FUNDER", manifest.allow_dummy_credentials),
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "api_secret": _resolve_secret(prefix, "API_SECRET", manifest.allow_dummy_credentials),
        "passphrase": _resolve_secret(prefix, "PASSPHRASE", manifest.allow_dummy_credentials),
        "base_url_http": venue.api_url,
        "base_url_ws": venue.ws_url,
        "instrument_provider": _polymarket_instrument_provider_dict(venue, manifest),
        "generate_order_history_from_trades": False,
        "use_data_api": venue.use_data_api,
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=POLYMARKET_EXEC_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=POLYMARKET_EXEC_FACTORY_PATH),
    )


def _polymarket_instrument_provider_dict(
    venue: BettingVenueManifest,
    manifest: BettingArbitrageNodeManifest,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if venue.sport_keys:
        filters["sports"] = sorted(venue.sport_keys)
    if venue.instrument_load_limit is not None:
        filters["limit"] = min(int(venue.instrument_load_limit), 500)
        filters["max_results"] = int(venue.instrument_load_limit)
    max_resolution_horizon_hours = manifest.strategy.max_resolution_horizon_hours
    if venue.load_all_instruments:
        return {
            "load_all": True,
            "filters": _drop_none(
                {
                    "is_active": True,
                    "max_resolution_horizon_hours": max_resolution_horizon_hours,
                    **filters,
                },
            ),
            "use_gamma_markets": True,
        }
    provider_config: dict[str, Any] = {
        "load_all": False,
        "load_ids": sorted(venue.instrument_ids or []),
        "use_gamma_markets": True,
    }
    if filters:
        provider_config["filters"] = filters
    return provider_config


def _credential_prefix(venue: BettingVenueManifest) -> str:
    if venue.credential_prefix:
        return venue.credential_prefix
    return venue.venue.replace(".", "_").replace("-", "_").upper()


def _resolve_secret(prefix: str, suffix: str, allow_dummy_credentials: bool) -> str:
    key = f"{prefix}_{suffix}"
    value = os.environ.get(key)
    if value:
        return value
    if allow_dummy_credentials:
        dummy_value = DUMMY_SECRETS.get(key)
        if dummy_value is not None:
            return dummy_value
        return f"dummy-{key.lower().replace('_', '-')}"
    raise MissingCredentialError(f"Missing required credential: {key}")


def _resolve_secret_pool(
    prefix: str,
    suffix: str,
    allow_dummy_credentials: bool,
) -> tuple[str, ...] | None:
    key = f"{prefix}_{suffix}"
    value = os.environ.get(key)
    if not value and suffix == "API_KEYS":
        value = os.environ.get(f"{prefix}_API_KEY")
    if value:
        keys = tuple(part.strip() for part in re.split(r"[\s,]+", value) if part.strip())
        return keys or None
    if allow_dummy_credentials:
        dummy_value = DUMMY_SECRETS.get(key) or DUMMY_SECRETS.get(f"{prefix}_API_KEY")
        if dummy_value is not None:
            return (dummy_value,)
    return None


def _normalize_prefixed_hex_secret(value: str, *, expected_nibbles: int) -> str:
    normalized = value.strip()
    if normalized.startswith("0X"):
        normalized = f"0x{normalized[2:]}"
    if not normalized.startswith("0x") and re.fullmatch(
        rf"[0-9a-fA-F]{{{expected_nibbles}}}",
        normalized,
    ):
        return f"0x{normalized}"
    return normalized


def _client_key(venue: BettingVenueManifest, index: int) -> str:
    raw = venue.client_key or f"{venue.venue}_{index}"
    cleaned = re.sub(r"[^A-Z0-9_]+", "_", raw.upper()).strip("_")
    return cleaned or f"CLIENT_{index}"


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def default_render_paths(manifest: BettingArbitrageNodeManifest) -> dict[str, Path]:
    root = DEFAULT_RENDER_ROOT / manifest.node_id
    return {
        "root": root,
        "manifest": root / "manifest.json",
        "rendered_config": root / "trading-node-config.json",
        "status": root / "status.json",
        "heartbeat": root / "heartbeat.json",
    }


def write_manifest_snapshot(
    manifest: BettingArbitrageNodeManifest,
    output_path: str | os.PathLike[str],
) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(json.dumps(manifest.json_primitives(), indent=2).encode("utf8"))
    return destination


__all__ = [
    "MissingCredentialError",
    "build_trading_node_config",
    "default_render_paths",
    "load_manifest",
    "manifest_execution_readiness",
    "manifest_to_json",
    "render_trading_node_config_json",
    "write_manifest_snapshot",
    "write_rendered_node_config",
]
