# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet configuration classes.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

from nautilus_trader.adapters.easybet.constants import DEFAULT_MAX_REQUESTS_PER_MINUTE
from nautilus_trader.adapters.easybet.constants import DEFAULT_QUOTE_POLL_INTERVAL
from nautilus_trader.adapters.easybet.constants import DEFAULT_REQUEST_DELAY_MAX
from nautilus_trader.adapters.easybet.constants import DEFAULT_REQUEST_DELAY_MIN
from nautilus_trader.adapters.easybet.constants import EASYBET_BASE_URL
from nautilus_trader.config import PositiveFloat
from nautilus_trader.config import PositiveInt


class EasybetDataClientConfig:
    """
    Configuration for `EasybetDataClient`.

    Parameters
    ----------
    base_url : str
        Base URL for Easybet platform.
    quote_poll_interval : PositiveFloat
        Polling interval for quote updates (seconds).
    use_iframe_source : bool
        If True, navigate directly to AdvBet iframe source for scraping.
    headless : bool
        Run browser in headless mode.
    use_stealth : bool
        Enable stealth mode to avoid bot detection.
    request_delay_min : PositiveFloat
        Minimum delay between requests (seconds).
    request_delay_max : PositiveFloat
        Maximum delay between requests (seconds).
    max_requests_per_minute : PositiveInt
        Maximum requests allowed per minute.

    """

    base_url: str = EASYBET_BASE_URL
    quote_poll_interval: PositiveFloat = DEFAULT_QUOTE_POLL_INTERVAL
    use_iframe_source: bool = True
    headless: bool = True
    use_stealth: bool = True
    request_delay_min: PositiveFloat = DEFAULT_REQUEST_DELAY_MIN
    request_delay_max: PositiveFloat = DEFAULT_REQUEST_DELAY_MAX
    max_requests_per_minute: PositiveInt = DEFAULT_MAX_REQUESTS_PER_MINUTE


class EasybetExecClientConfig:
    """
    Configuration for `EasybetExecutionClient`.

    Parameters
    ----------
    base_url : str
        Base URL for Easybet platform.
    max_stake_zar : Decimal
        Maximum stake in ZAR.
    rollover_multiplier : Decimal
        Rollover requirement multiplier.
    min_rollover_odds : Decimal
        Minimum odds for rollover qualification.
    bonus_amount : Decimal
        Active bonus amount requiring rollover.
    headless : bool
        Run browser in headless mode.

    """

    base_url: str = EASYBET_BASE_URL
    max_stake_zar: Decimal = Decimal(2000)
    rollover_multiplier: Decimal = Decimal(8)
    min_rollover_odds: Decimal = Decimal("1.50")
    bonus_amount: Decimal = Decimal(0)
    headless: bool = True
