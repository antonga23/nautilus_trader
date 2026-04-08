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
Constant values for the BlackBet adapter.
"""

from nautilus_trader.model.identifiers import Venue


# Venue identifier for BlackBet
BLACKBET_VENUE = Venue("BLACKBET")

# API Base URLs
BLACKBET_BASE_URL = "https://www.blackbet.co.za"
BLACKBET_SPORTS_URL = f"{BLACKBET_BASE_URL}/sports"

# Sport IDs (blackbet specific)
BLACKBET_SPORTS = {
    "soccer": 1,
    "basketball": 2,
    "tennis": 3,
    "cricket": 4,
    "rugby": 5,
}

# Default settings
DEFAULT_SCRAPE_INTERVAL_SECONDS = 10
DEFAULT_REQUEST_DELAY_MIN = 1.0
DEFAULT_REQUEST_DELAY_MAX = 3.0
DEFAULT_MAX_REQUESTS_PER_MINUTE = 20
DEFAULT_SESSION_TIMEOUT_MINUTES = 30

# Currency
BLACKBET_CURRENCY = "ZAR"
