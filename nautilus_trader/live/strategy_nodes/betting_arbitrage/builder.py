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

DUMMY_SECRETS = {
    "SXBET_API_KEY": "dummy-sxbet-api-key",
    "SXBET_PRIVATE_KEY": "0x" + "1" * 64,
    "SXBET_WALLET_ADDRESS": "0x" + "2" * 40,
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
        if venue.venue == "SXBET":
            if venue.data_enabled:
                data_clients[client_key] = _build_sxbet_data_importable(venue, manifest)
            if venue.execution_enabled and not manifest.validation_mode:
                exec_clients[client_key] = _build_sxbet_exec_importable(venue, manifest)
        elif venue.venue == "POLYMARKET":
            if venue.data_enabled:
                data_clients[client_key] = _build_polymarket_data_importable(venue, manifest)
            if venue.execution_enabled and not manifest.validation_mode:
                exec_clients[client_key] = _build_polymarket_exec_importable(venue, manifest)
        else:
            raise ValueError(f"Unsupported venue {venue.venue}")

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


def _build_strategy_importable_config(
    manifest: BettingArbitrageNodeManifest,
    enabled_venues: list[BettingVenueManifest],
) -> ImportableStrategyConfig:
    strategy_config = manifest.strategy.json_primitives()
    strategy_config["enabled_venues"] = sorted({venue.venue for venue in enabled_venues})
    if manifest.validation_mode:
        strategy_config["auto_execute"] = False
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
    provider_config = {
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "api_url": venue.api_url,
        "load_all": venue.load_all_instruments,
        "sport_ids": sorted(venue.sport_ids) if venue.sport_ids else None,
        "league_ids": sorted(venue.league_ids) if venue.league_ids else None,
        "live_only": venue.live_only,
    }
    config = {
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "api_url": venue.api_url,
        "ws_url": venue.ws_url,
        "instrument_provider": provider_config,
        "sport_ids": sorted(venue.sport_ids) if venue.sport_ids else None,
        "reconnect_on_disconnect": True,
        "max_reconnect_attempts": 5,
        "auto_subscribe_quote_ticks": venue.auto_subscribe_quote_ticks,
        "quote_subscription_limit": venue.quote_subscription_limit,
        "order_book_poll_interval_secs": venue.order_book_poll_interval_secs,
        "order_book_poll_summary_interval_secs": venue.order_book_poll_summary_interval_secs,
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
    provider_config = {
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "api_url": venue.api_url,
        "load_all": venue.load_all_instruments,
        "sport_ids": sorted(venue.sport_ids) if venue.sport_ids else None,
        "league_ids": sorted(venue.league_ids) if venue.league_ids else None,
        "live_only": venue.live_only,
    }
    config = {
        "api_key": _resolve_secret(prefix, "API_KEY", manifest.allow_dummy_credentials),
        "private_key": _resolve_secret(prefix, "PRIVATE_KEY", manifest.allow_dummy_credentials),
        "wallet_address": _resolve_secret(
            prefix,
            "WALLET_ADDRESS",
            manifest.allow_dummy_credentials,
        ),
        "api_url": venue.api_url,
        "ws_url": venue.ws_url,
        "instrument_provider": provider_config,
        "max_retry_attempts": 3,
        "base_currency": "USDC",
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=SXBET_EXEC_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=SXBET_EXEC_FACTORY_PATH),
    )


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
        "instrument_provider": _polymarket_instrument_provider_dict(venue),
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
        "instrument_provider": _polymarket_instrument_provider_dict(venue),
        "generate_order_history_from_trades": False,
        "use_data_api": venue.use_data_api,
        "routing": {"venues": [venue.venue]},
    }
    return ImportableConfig(
        path=POLYMARKET_EXEC_CONFIG_PATH,
        config=_drop_none(config),
        factory=ImportableFactoryConfig(path=POLYMARKET_EXEC_FACTORY_PATH),
    )


def _polymarket_instrument_provider_dict(venue: BettingVenueManifest) -> dict[str, Any]:
    if venue.load_all_instruments:
        return {"load_all": True}
    return {
        "load_all": False,
        "load_ids": sorted(venue.instrument_ids or []),
    }


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
    "manifest_to_json",
    "render_trading_node_config_json",
    "write_manifest_snapshot",
    "write_rendered_node_config",
]
