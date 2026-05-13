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
Compatibility re-export for venue risk policies.

New code should import from `nautilus_trader.adapters.betting.venue_risk` so the
adapter layer is not confused with Nautilus Trader's platform `RiskEngine`.
"""

from nautilus_trader.adapters.betting.venue_risk import BettingVenueRiskPolicy
from nautilus_trader.adapters.betting.venue_risk import MaxExposureRule
from nautilus_trader.adapters.betting.venue_risk import OddsRequirementRule
from nautilus_trader.adapters.betting.venue_risk import RiskEvaluation
from nautilus_trader.adapters.betting.venue_risk import RiskRule
from nautilus_trader.adapters.betting.venue_risk import RiskRuleType
from nautilus_trader.adapters.betting.venue_risk import RolloverRule
from nautilus_trader.adapters.betting.venue_risk import StakeLimitRule


__all__ = [
    "BettingVenueRiskPolicy",
    "MaxExposureRule",
    "OddsRequirementRule",
    "RiskEvaluation",
    "RiskRule",
    "RiskRuleType",
    "RolloverRule",
    "StakeLimitRule",
]
