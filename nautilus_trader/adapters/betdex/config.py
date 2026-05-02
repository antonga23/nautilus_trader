# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
BetDex/Monaco adapter configuration classes.
"""

from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.config import PositiveFloat
from nautilus_trader.config import PositiveInt


class BetDexInstrumentProviderConfig(InstrumentProviderConfig, frozen=True):
    """
    Configuration for ``BetDexInstrumentProvider`` instances.
    """

    app_id: str | None = None
    api_key: str | None = None
    api_url: str | None = None
    load_all: bool = False
    event_ids: frozenset[str] | None = None
    category_ids: frozenset[str] | None = None
    subcategory_ids: frozenset[str] | None = None
    event_group_ids: frozenset[str] | None = None
    sport_keys: frozenset[str] | None = None
    live_only: bool = False
    instrument_load_limit: PositiveInt | None = None
    market_discovery_limit: PositiveInt | None = None
    event_discovery_limit: PositiveInt = 100
    page_size: PositiveInt = 100
    log_warnings: bool = True


class BetDexDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for ``BetDexDataClient`` instances.
    """

    app_id: str | None = None
    api_key: str | None = None
    api_url: str | None = None
    stream_url: str | None = None
    instrument_provider: BetDexInstrumentProviderConfig | None = None  # type: ignore[assignment]
    auto_subscribe_quote_ticks: bool = False
    quote_subscription_limit: PositiveInt | None = None
    quote_poll_interval_secs: PositiveFloat = 10.0
    quote_poll_summary_interval_secs: PositiveFloat = 30.0
    quote_poll_concurrency: PositiveInt = 4


class BetDexExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for ``BetDexExecutionClient`` instances.
    """

    app_id: str = ""
    api_key: str = ""
    wallet_id: str = ""
    api_url: str | None = None
    instrument_provider: BetDexInstrumentProviderConfig | None = None  # type: ignore[assignment]
    base_currency: str = "USDC"
    match_behavior: str = "CancelUnmatched"
    keep_when_in_play: bool = False
    allow_production_execution: bool = False
