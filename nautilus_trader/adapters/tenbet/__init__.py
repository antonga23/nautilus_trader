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
10bet adapter package.
"""

from nautilus_trader.adapters.tenbet.config import TenBetDataClientConfig
from nautilus_trader.adapters.tenbet.config import TenBetExecClientConfig
from nautilus_trader.adapters.tenbet.config import TenBetInstrumentProviderConfig
from nautilus_trader.adapters.tenbet.constants import TENBET_VENUE


__all__ = [
    "TENBET_VENUE",
    "TenBetDataClientConfig",
    "TenBetExecClientConfig",
    "TenBetInstrumentProviderConfig",
]
