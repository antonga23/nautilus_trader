from __future__ import annotations


import msgspec

from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.common.config import PositiveFloat
from nautilus_trader.common.config import PositiveInt
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig

SUPPORTED_BETTING_NODE_VENUES = frozenset({"CLOUDBET", "SXBET", "POLYMARKET"})
BLOCKED_SPORTSBOOK_VENUES = frozenset({"10BET", "BLACKBET", "WSB", "EASYBET"})
SUPPORTED_VENUE_ENVIRONMENTS = {
    "CLOUDBET": frozenset({"prod", "paper"}),
    "SXBET": frozenset({"prod", "testnet"}),
    "POLYMARKET": frozenset({"prod"}),
}
VENUE_ENVIRONMENT_ALIASES = {
    "mainnet": "prod",
    "production": "prod",
    "toronto": "testnet",
    "play": "paper",
}


class BettingVenueManifest(NautilusConfig, frozen=True):
    """
    Deployable venue manifest for a betting arbitrage node.
    """

    venue: str
    client_key: str | None = None
    enabled: bool = True
    data_enabled: bool = True
    execution_enabled: bool = False
    credential_prefix: str | None = None
    load_all_instruments: bool = True
    instrument_ids: frozenset[str] | None = None
    sport_keys: frozenset[str] | None = None
    sport_ids: frozenset[int] | None = None
    league_ids: frozenset[int] | None = None
    live_only: bool = False
    instrument_load_limit: PositiveInt | None = None
    market_discovery_limit: PositiveInt | None = None
    prefer_liquid_markets: bool = False
    liquidity_probe_limit: PositiveInt = 100
    min_two_sided_markets: PositiveInt = 1
    auto_subscribe_quote_ticks: bool = False
    quote_subscription_limit: PositiveInt | None = None
    order_book_poll_interval_secs: PositiveFloat = 3.0
    order_book_poll_summary_interval_secs: PositiveFloat = 30.0
    order_book_concurrency: PositiveInt = 4
    order_book_poll_mode: str = "order_book"
    order_book_best_odds_batch_size: PositiveInt = 30
    order_book_min_concurrency: PositiveInt = 1
    order_book_max_concurrency: PositiveInt | None = None
    order_book_target_cycle_secs: PositiveFloat = 5.0
    order_book_adaptive_concurrency: bool = True
    order_book_event_batching: bool = True
    order_book_missing_prune_threshold: PositiveInt = 3
    api_url: str | None = None
    ws_url: str | None = None
    environment: str | None = None
    base_currency: str | None = None
    execution_dry_run: bool = False
    signature_type: int = 0
    use_data_api: bool = False
    metadata: dict[str, str] | None = None

    def __post_init__(self) -> None:
        normalized_venue = self.venue.strip().upper()
        if normalized_venue in BLOCKED_SPORTSBOOK_VENUES:
            raise ValueError(
                f"Venue {normalized_venue} is not deployment-ready. "
                "Only CLOUDBET, SXBET, and POLYMARKET are currently supported "
                "in the live node builder.",
            )
        if normalized_venue not in SUPPORTED_BETTING_NODE_VENUES:
            raise ValueError(
                f"Unsupported venue {normalized_venue}. Supported venues: "
                f"{sorted(SUPPORTED_BETTING_NODE_VENUES)}",
            )
        if self.execution_enabled and not self.data_enabled:
            raise ValueError("execution_enabled requires data_enabled for live betting nodes")
        if self.instrument_ids and self.load_all_instruments:
            raise ValueError("instrument_ids and load_all_instruments cannot both be set")

        msgspec.structs.force_setattr(self, "venue", normalized_venue)
        if self.client_key is not None:
            msgspec.structs.force_setattr(self, "client_key", self.client_key.strip())
        if self.credential_prefix is not None:
            msgspec.structs.force_setattr(
                self,
                "credential_prefix",
                self.credential_prefix.strip().upper(),
            )
        if self.environment is not None:
            normalized_environment = VENUE_ENVIRONMENT_ALIASES.get(
                self.environment.strip().lower(),
                self.environment.strip().lower(),
            )
            supported_environments = SUPPORTED_VENUE_ENVIRONMENTS.get(normalized_venue, frozenset())
            if normalized_environment not in supported_environments:
                raise ValueError(
                    f"Unsupported environment {normalized_environment!r} for venue "
                    f"{normalized_venue}. Supported environments: "
                    f"{sorted(supported_environments)}",
                )
            msgspec.structs.force_setattr(self, "environment", normalized_environment)
        if self.base_currency is not None:
            msgspec.structs.force_setattr(
                self,
                "base_currency",
                self.base_currency.strip().upper(),
            )


SUPPORTED_SEMANTIC_CACHE_MODES = frozenset({"fresh", "reuse", "default"})


class BettingArbitrageNodeManifest(NautilusConfig, frozen=True):
    """
    Manifest for a deployable betting arbitrage trading node.

    Parameters
    ----------
    semantic_rule_cache_mode : str, default 'fresh'
        How the semantic rule cache is provisioned. ``'fresh'`` always re-mines
        from the live venue corpus and never reuses an existing cache;
        ``'reuse'`` reuses an existing compatible cache (seeding then mining as a
        fallback); ``'default'`` reuses a config-signature-keyed default mine from
        ``semantic_rule_cache_default_root`` when one exists and is fresh enough,
        otherwise mines fresh and registers the result.
    semantic_rule_cache_default_root : str, optional
        Root directory for the config-signature-keyed default-mine registry used
        by ``'default'`` mode (and populated by ``'fresh'`` mode when set).
    semantic_rule_cache_max_age_hours : float, optional
        Maximum age of a registered default mine that ``'default'`` mode will
        reuse. ``None`` disables the age check.
    semantic_diagnostics_interval_secs : PositiveFloat, default 90.0
        Minimum seconds between recomputes of the expensive semantic/coverage
        probe diagnostics. Trading-relevant probe fields still refresh every
        ``heartbeat_interval_secs``; the O(graph) diagnostic sections refresh at
        most this often so the status writer releases the GIL between passes
        instead of starving the venue quote-poll loops on a large graph.

    """

    node_id: str
    trader_id: str = "BET-ARB-001"
    strategy: BettingArbitrageConfig = BettingArbitrageConfig(auto_execute=False)
    semantic_rule_cache_dir: str | None = None
    semantic_rule_cache_seed_dir: str | None = None
    semantic_rule_cache_mode: str = "fresh"
    semantic_rule_cache_default_root: str | None = None
    semantic_rule_cache_max_age_hours: float | None = None
    venues: list[BettingVenueManifest] = []
    validation_mode: bool = True
    allow_dummy_credentials: bool = True
    log_level: str = "INFO"
    log_directory: str | None = None
    log_file_name: str | None = None
    rendered_config_path: str | None = None
    status_path: str | None = None
    heartbeat_path: str | None = None
    heartbeat_interval_secs: PositiveFloat = 5.0
    semantic_diagnostics_interval_secs: PositiveFloat = 90.0
    timeout_connection: PositiveFloat = 30.0
    timeout_reconciliation: PositiveFloat = 10.0
    timeout_portfolio: PositiveFloat = 10.0
    timeout_disconnection: PositiveFloat = 10.0
    timeout_post_stop: PositiveFloat = 5.0
    timeout_shutdown: PositiveFloat = 5.0
    open_check_interval_secs: PositiveFloat | None = 10.0
    reconciliation: bool = True
    metadata: dict[str, str] | None = None

    def __post_init__(self) -> None:
        node_id = self.node_id.strip()
        trader_id = self.trader_id.strip()
        if not node_id:
            raise ValueError("node_id is required")
        if not trader_id:
            raise ValueError("trader_id is required")
        if not self.venues:
            raise ValueError("At least one venue is required")
        enabled_venues = [venue for venue in self.venues if venue.enabled]
        if not enabled_venues:
            raise ValueError("At least one enabled venue is required")

        msgspec.structs.force_setattr(self, "node_id", node_id)
        msgspec.structs.force_setattr(self, "trader_id", trader_id)
        msgspec.structs.force_setattr(self, "log_level", self.log_level.strip().upper())
        if self.semantic_rule_cache_dir is not None:
            msgspec.structs.force_setattr(
                self,
                "semantic_rule_cache_dir",
                self.semantic_rule_cache_dir.strip(),
            )
        if self.semantic_rule_cache_seed_dir is not None:
            msgspec.structs.force_setattr(
                self,
                "semantic_rule_cache_seed_dir",
                self.semantic_rule_cache_seed_dir.strip(),
            )
        normalized_cache_mode = self.semantic_rule_cache_mode.strip().lower()
        if normalized_cache_mode not in SUPPORTED_SEMANTIC_CACHE_MODES:
            raise ValueError(
                f"Unsupported semantic_rule_cache_mode {normalized_cache_mode!r}. "
                f"Supported modes: {sorted(SUPPORTED_SEMANTIC_CACHE_MODES)}",
            )
        msgspec.structs.force_setattr(self, "semantic_rule_cache_mode", normalized_cache_mode)
        if self.semantic_rule_cache_default_root is not None:
            msgspec.structs.force_setattr(
                self,
                "semantic_rule_cache_default_root",
                self.semantic_rule_cache_default_root.strip(),
            )
        if (
            self.semantic_rule_cache_max_age_hours is not None
            and self.semantic_rule_cache_max_age_hours <= 0
        ):
            raise ValueError("semantic_rule_cache_max_age_hours must be positive when set")


__all__ = [
    "BLOCKED_SPORTSBOOK_VENUES",
    "SUPPORTED_BETTING_NODE_VENUES",
    "SUPPORTED_SEMANTIC_CACHE_MODES",
    "SUPPORTED_VENUE_ENVIRONMENTS",
    "VENUE_ENVIRONMENT_ALIASES",
    "BettingArbitrageNodeManifest",
    "BettingVenueManifest",
]
