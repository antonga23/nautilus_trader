# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2023 Nautech Systems Pty Ltd. All rights reserved.
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

from typing import Optional

from nautilus_trader.model.currency import Currency

from nautilus_trader.config import LiveDataClientConfig, InstrumentProviderConfig, RoutingConfig
from nautilus_trader.config import LiveExecClientConfig


class CloudbetDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for ``CloudbetDataClient`` instances.

    Parameters
    ----------
    api_key : str, optional
        The cloudbet api key.
    api_url : str, optional
        The cloudbet api url.
    market_filter : tuple, optional
    """

    instrument_provider: InstrumentProviderConfig = InstrumentProviderConfig()
    routing: RoutingConfig = RoutingConfig()
    api_key: Optional[str] = None
    api_url: Optional[str] = None
    market_filter: Optional[tuple] = None
    handle_revised_bars: bool = False

class CloudbetInstrumentProviderConfig(InstrumentProviderConfig, frozen=True):
    """
    Configuration for ``CloudbetDataClient`` instances.

    Parameters
    ----------
    api_key : str, optional
        The cloudbet api key.
    api_url : str, optional
        The cloudbet api url.
    load_all : bool, default False
        If all venue instruments should be loaded on start.
    load_ids : FrozenSet[str], optional
        The list of instrument IDs to be loaded on start (if `load_all_instruments` is False).
    filters : frozendict, optional
        The venue specific instrument loading filters to apply.
    filter_callable: str, optional
        A fully qualified path to a callable that takes a single argument, `instrument` and returns a bool, indicating
        whether the instrument should be loaded
    log_warnings : bool, default True
        If parser warnings should be logged.
    """

    api_key: Optional[str] = None
    api_url: Optional[str] = None
    load_all = False
    load_ids = None
    filters = None
    filter_callable = None
    log_warnings = True

# TODO: pass this typed config to LiveCloudbetExecClient constructor
class CloudbetExecClientConfig(LiveExecClientConfig, kw_only=True, frozen=True):
    """
    Configuration for `CloudbetExecClient` instances.

    Parameters
    ----------
    base_currency : Currency
        The base currency of the account.
    market_filter : tuple
        The market filter to use.
    api_key : str
        The cloudbet api key.
    api_url : str
        The cloudbet api url.
    """

    base_currency: Currency = None
    market_filter: Optional[dict] = None
    api_key: Optional[str] = None
    api_url: Optional[str] = None
