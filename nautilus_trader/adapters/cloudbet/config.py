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

from nautilus_trader.model.objects import Currency

from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import PositiveFloat
from nautilus_trader.config import PositiveInt
from nautilus_trader.config import RoutingConfig


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

    instrument_provider: InstrumentProviderConfig = InstrumentProviderConfig(load_all=True)
    routing: RoutingConfig = RoutingConfig()
    api_key: str | None = None
    api_url: str | None = None
    market_filter: tuple | None = None
    handle_revised_bars: bool = False
    auto_subscribe_quote_ticks: bool = False
    quote_subscription_limit: PositiveInt | None = None
    quote_poll_interval_secs: PositiveFloat = 10.0
    quote_poll_summary_interval_secs: PositiveFloat = 30.0
    quote_poll_concurrency: PositiveInt = 4
    quote_poll_min_concurrency: PositiveInt = 1
    quote_poll_max_concurrency: PositiveInt = 16
    quote_poll_target_cycle_secs: PositiveFloat = 5.0
    quote_poll_adaptive_concurrency: bool = True
    quote_poll_event_batching: bool = True
    quote_poll_missing_prune_threshold: PositiveInt = 3


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

    api_key: str | None = None
    api_url: str | None = None
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
    dry_run : bool, default False
        If True, build Cloudbet bet requests but do not submit them to the Trading API.
    accept_price_change : str, default "BETTER"
        Cloudbet price-change policy for live pilot betting.
    pending_acceptance_poll_attempts : int, default 3
        Number of status checks for Cloudbet bets that remain pending after submit.
    pending_acceptance_poll_interval_secs : float, default 0.5
        Delay between pending-acceptance status checks.
    settlement_poll_interval_secs : float, default 30.0
        Delay between graded-bet settlement reconciliation polls.
    """

    base_currency: Currency = None
    market_filter: dict | None = None
    api_key: str | None = None
    api_url: str | None = None
    dry_run: bool = False
    accept_price_change: str = "BETTER"
    pending_acceptance_poll_attempts: int = 3
    pending_acceptance_poll_interval_secs: float = 0.5
    settlement_poll_interval_secs: float = 30.0
