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
SX.bet adapter package.
"""

from nautilus_trader.adapters.sxbet.config import SXBetDataClientConfig
from nautilus_trader.adapters.sxbet.config import SXBetExecClientConfig
from nautilus_trader.adapters.sxbet.config import SXBetInstrumentProviderConfig
from nautilus_trader.adapters.sxbet.constants import SXBET_VENUE
from nautilus_trader.adapters.sxbet.data import SXBetDataClient
from nautilus_trader.adapters.sxbet.execution import SXBetExecutionClient
from nautilus_trader.adapters.sxbet.factories import SXBetLiveDataClientFactory
from nautilus_trader.adapters.sxbet.factories import SXBetLiveExecClientFactory
from nautilus_trader.adapters.sxbet.factories import get_sxbet_http_client
from nautilus_trader.adapters.sxbet.factories import get_sxbet_instrument_provider
from nautilus_trader.adapters.sxbet.http_client import SXBetHttpClient
from nautilus_trader.adapters.sxbet.providers import SXBetInstrumentProvider
from nautilus_trader.adapters.sxbet.realtime import SXBetRealtimeClient


__all__ = [
    # Constants
    "SXBET_VENUE",
    # Clients
    "SXBetDataClient",
    # Config
    "SXBetDataClientConfig",
    "SXBetExecClientConfig",
    "SXBetExecutionClient",
    "SXBetHttpClient",
    "SXBetInstrumentProvider",
    "SXBetInstrumentProviderConfig",
    # Factories
    "SXBetLiveDataClientFactory",
    "SXBetLiveExecClientFactory",
    "SXBetRealtimeClient",
    "get_sxbet_http_client",
    "get_sxbet_instrument_provider",
]
