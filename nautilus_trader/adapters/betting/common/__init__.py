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
Common utilities for betting adapters.
"""

from nautilus_trader.adapters.betting.common.enums import BettingMode
from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.fees import DEFAULT_TAKER_FEE_RATES
from nautilus_trader.adapters.betting.common.fees import FeeAdjustedBasket
from nautilus_trader.adapters.betting.common.fees import FeeAdjustedOdds
from nautilus_trader.adapters.betting.common.fees import fee_adjusted_basket_margin
from nautilus_trader.adapters.betting.common.fees import fee_adjusted_odds
from nautilus_trader.adapters.betting.common.fees import normalize_venue_fee_rates
from nautilus_trader.adapters.betting.common.odds import DeviggedBook
from nautilus_trader.adapters.betting.common.odds import devig_probabilities


__all__ = [
    "DEFAULT_TAKER_FEE_RATES",
    "BettingMode",
    "DeviggedBook",
    "FeeAdjustedBasket",
    "FeeAdjustedOdds",
    "MarketType",
    "SelectionSide",
    "devig_probabilities",
    "fee_adjusted_basket_margin",
    "fee_adjusted_odds",
    "normalize_venue_fee_rates",
]
