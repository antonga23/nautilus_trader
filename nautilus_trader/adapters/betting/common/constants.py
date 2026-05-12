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
Constants for betting adapters.
"""

# Common sports IDs used across venues for normalization
SPORT_IDS: dict[str, str] = {
    "soccer": "1",
    "football": "1",
    "tennis": "2",
    "basketball": "3",
    "hockey": "4",
    "ice_hockey": "4",
    "american_football": "5",
    "nfl": "5",
    "baseball": "6",
    "mlb": "6",
    "mma": "7",
    "ufc": "7",
    "boxing": "8",
    "cricket": "9",
    "rugby": "10",
    "golf": "11",
    "esports": "12",
}


# Default precision for betting quantities (stake amounts)
DEFAULT_SIZE_PRECISION = 2

# Default precision for betting prices (odds)
DEFAULT_PRICE_PRECISION = 2

# Null handicap value used for non-handicap markets
NULL_HANDICAP = -9999999.0
