from __future__ import annotations


import msgspec

from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.common.config import PositiveFloat
from nautilus_trader.examples.strategies.betting_arbitrage import BettingArbitrageConfig

SUPPORTED_BETTING_NODE_VENUES = frozenset({"SXBET", "POLYMARKET"})
BLOCKED_SPORTSBOOK_VENUES = frozenset({"10BET", "BLACKBET", "WSB", "EASYBET"})


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
    sport_ids: frozenset[int] | None = None
    league_ids: frozenset[int] | None = None
    live_only: bool = False
    api_url: str | None = None
    ws_url: str | None = None
    signature_type: int = 0
    use_data_api: bool = False
    metadata: dict[str, str] | None = None

    def __post_init__(self) -> None:
        normalized_venue = self.venue.strip().upper()
        if normalized_venue in BLOCKED_SPORTSBOOK_VENUES:
            raise ValueError(
                f"Venue {normalized_venue} is not deployment-ready. "
                "Only SXBET and POLYMARKET are currently supported in the live node builder.",
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


class BettingArbitrageNodeManifest(NautilusConfig, frozen=True):
    """
    Manifest for a deployable betting arbitrage trading node.
    """

    node_id: str
    trader_id: str = "BET-ARB-001"
    strategy: BettingArbitrageConfig = BettingArbitrageConfig(auto_execute=False)
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


__all__ = [
    "BLOCKED_SPORTSBOOK_VENUES",
    "SUPPORTED_BETTING_NODE_VENUES",
    "BettingArbitrageNodeManifest",
    "BettingVenueManifest",
]
