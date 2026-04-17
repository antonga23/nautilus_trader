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
10bet adapter configuration.
"""

from decimal import Decimal

from nautilus_trader.adapters.tenbet.constants import DEFAULT_MAX_REQUESTS_PER_MINUTE
from nautilus_trader.adapters.tenbet.constants import DEFAULT_REQUEST_DELAY_MAX
from nautilus_trader.adapters.tenbet.constants import DEFAULT_REQUEST_DELAY_MIN
from nautilus_trader.adapters.tenbet.constants import DEFAULT_SCRAPE_INTERVAL_SECONDS
from nautilus_trader.adapters.tenbet.constants import DEFAULT_SESSION_TIMEOUT_MINUTES
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.live.config import LiveDataClientConfig
from nautilus_trader.live.config import LiveExecClientConfig


class TenBetInstrumentProviderConfig(InstrumentProviderConfig, frozen=True):
    """
    Configuration for `TenBetInstrumentProvider`.

    Parameters
    ----------
    base_url : str, optional
        Base URL for 10bet website.
    sports : frozenset[str], optional
        Sports to load instruments for (e.g., {"soccer", "basketball"}).
    headless : bool, default True
        Run browser in headless mode.
    scrape_interval : int, default 10
        Seconds between full market scrapes.

    """

    base_url: str | None = None
    sports: frozenset[str] | None = None
    headless: bool = True
    scrape_interval: int = DEFAULT_SCRAPE_INTERVAL_SECONDS


class TenBetDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for `TenBetDataClient`.

    Parameters
    ----------
    instrument_provider : TenBetInstrumentProviderConfig, optional
        Instrument provider configuration.
    base_url : str, optional
        Base URL for 10bet website.
    headless : bool, default True
        Run browser in headless mode.
    poll_interval : int, default 10
        Seconds between market data polls.
    request_delay_min : float, default 1.0
        Minimum delay between requests (anti-bot).
    request_delay_max : float, default 3.0
        Maximum delay between requests (anti-bot).
    max_requests_per_minute : int, default 20
        Maximum requests per minute (rate limiting).
    use_stealth : bool, default True
        Use stealth plugins to avoid detection.

    """

    instrument_provider: TenBetInstrumentProviderConfig | None = None
    base_url: str | None = None
    headless: bool = True
    poll_interval: int = DEFAULT_SCRAPE_INTERVAL_SECONDS
    request_delay_min: float = DEFAULT_REQUEST_DELAY_MIN
    request_delay_max: float = DEFAULT_REQUEST_DELAY_MAX
    max_requests_per_minute: int = DEFAULT_MAX_REQUESTS_PER_MINUTE
    use_stealth: bool = True


class TenBetExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for `TenBetExecutionClient`.

    Parameters
    ----------
    instrument_provider : TenBetInstrumentProviderConfig, optional
        Instrument provider configuration.
    base_url : str, optional
        Base URL for 10bet website.
    email : str, optional
        Login email (placeholder for future auth).
    password : str, optional
        Login password (placeholder for future auth).
    headless : bool, default True
        Run browser in headless mode.
    request_delay_min : float, default 1.0
        Minimum delay between requests.
    request_delay_max : float, default 3.0
        Maximum delay between requests.
    max_requests_per_minute : int, default 20
        Maximum requests per minute.
    use_stealth : bool, default True
        Use stealth plugins.
    session_timeout_minutes : int, default 30
        Session timeout in minutes.
    max_stake_zar : Decimal, default 1000
        Maximum stake per bet (ZAR).

    """

    instrument_provider: TenBetInstrumentProviderConfig | None = None
    base_url: str | None = None
    login_url: str | None = None
    email: str | None = None
    password: str | None = None
    otp_code: str | None = None
    headless: bool = True
    request_delay_min: float = DEFAULT_REQUEST_DELAY_MIN
    request_delay_max: float = DEFAULT_REQUEST_DELAY_MAX
    max_requests_per_minute: int = DEFAULT_MAX_REQUESTS_PER_MINUTE
    use_stealth: bool = True
    session_timeout_minutes: int = DEFAULT_SESSION_TIMEOUT_MINUTES
    session_state_path: str | None = None
    allow_synthetic_auth: bool = False
    allow_synthetic_execution: bool = False
    max_stake_zar: Decimal = Decimal("1000")
