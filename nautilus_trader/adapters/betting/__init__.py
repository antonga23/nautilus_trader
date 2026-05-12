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
Betting adapter package for sports betting venues.
"""

from nautilus_trader.adapters.betting.common.constants import NULL_HANDICAP
from nautilus_trader.adapters.betting.common.constants import SPORT_IDS
from nautilus_trader.adapters.betting.common.enums import BetStatus
from nautilus_trader.adapters.betting.common.enums import BettingMode
from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.common.enums import SelectionSide
from nautilus_trader.adapters.betting.common.odds import calculate_arbitrage_stakes
from nautilus_trader.adapters.betting.common.odds import decimal_to_american
from nautilus_trader.adapters.betting.common.odds import decimal_to_fractional
from nautilus_trader.adapters.betting.common.odds import decimal_to_probability
from nautilus_trader.adapters.betting.common.odds import is_arbitrage_opportunity
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.market_matcher import ArbitrageOpportunity
from nautilus_trader.adapters.betting.market_matcher import HedgeCandidate
from nautilus_trader.adapters.betting.market_matcher import MarketMatcher
from nautilus_trader.adapters.betting.risk_engine import BettingVenueRiskPolicy
from nautilus_trader.adapters.betting.risk_engine import MaxExposureRule
from nautilus_trader.adapters.betting.risk_engine import OddsRequirementRule
from nautilus_trader.adapters.betting.risk_engine import RiskEvaluation
from nautilus_trader.adapters.betting.risk_engine import RiskRule
from nautilus_trader.adapters.betting.risk_engine import RiskRuleType
from nautilus_trader.adapters.betting.risk_engine import RolloverRule
from nautilus_trader.adapters.betting.risk_engine import StakeLimitRule


__all__ = [
    "NULL_HANDICAP",
    "SPORT_IDS",
    "ArbitrageOpportunity",
    "BetStatus",
    "BettingMode",
    "BettingVenueRiskPolicy",
    "CryptoBettingInstrument",
    "HedgeCandidate",
    "MarketMatcher",
    "MarketType",
    "MaxExposureRule",
    "OddsRequirementRule",
    "Outcome",
    "RiskEvaluation",
    "RiskRule",
    "RiskRuleType",
    "RolloverRule",
    "SelectionSide",
    "StakeLimitRule",
    "calculate_arbitrage_stakes",
    "decimal_to_american",
    "decimal_to_fractional",
    "decimal_to_probability",
    "is_arbitrage_opportunity",
]
