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

from decimal import Decimal

from nautilus_trader.live.strategy_nodes.betting_arbitrage.runner import _probe_rag_band


def test_probe_rag_band_classifies_green_amber_red() -> None:
    # profitable -> green
    assert _probe_rag_band(Decimal("0.05")) == "green"
    assert _probe_rag_band(Decimal("0.0001")) == "green"
    # slightly unprofitable (0% to -5%, inclusive) -> amber
    assert _probe_rag_band(Decimal(0)) == "amber"
    assert _probe_rag_band(Decimal("-0.03")) == "amber"
    assert _probe_rag_band(Decimal("-0.05")) == "amber"
    # unprofitable (worse than -5%) -> red
    assert _probe_rag_band(Decimal("-0.0501")) == "red"
    assert _probe_rag_band(Decimal("-0.20")) == "red"
