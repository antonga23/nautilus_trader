# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2023 . All rights reserved.
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


from functools import lru_cache
from typing import Optional

from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.adapters.cloudbet.common import VENUE


def make_symbol(event_id: int, submarket_name: str, outcome: str, params: Optional[str] = "") -> Symbol:
    """
    Make symbol with arbitrary number of arguments.
    Each argument will be converted to a string, cleaned, and joined with "|" to form the symbol.

    Arguments are provided as keyword arguments, and order does not matter.

    Example:
    >>> make_symbol(event_id=2135245, submarket_name="1st_half_1x2_period=1h", outcome="home", params="")
    Symbol('2135245|1st_half_1x2_period=1h|home')
    Note: The generated symbol must be no longer than 32 characters.
    """

    def _clean(s):
        return str(s).replace(" ", "").replace(":", "")

    value: str = "|".join(
        [_clean(k) for k in (event_id, submarket_name, outcome, params)],
    )
    # ToDo: add some sanity checks here eg order of arguments, length of arguments, etc
    # assert len(value) <= 32, f"Symbol too long ({len(value)}): '{value}'"
    return Symbol(value)


@lru_cache
def extract_cloudbet_symbol(symbol: Symbol) -> tuple[int, str, str, str]:
    """
    Extract the event_id, market_name, outcome and params from a symbol
    """
    # TODO: test this handles when params = "" or None
    event_id, market_name, outcome, params = symbol.value.split("|")
    return int(event_id), market_name, outcome, params



# TODO: test CryptoBettingInstrument with new cloudbet_instrument_id generator
@lru_cache
def cloudbet_instrument_id(
    event_id: int,
    market_name: str,
    outcome: str,
    params: Optional[str] = "",
) -> InstrumentId:
    """
    Create an instrument ID from CLOUDBET fields
    """

    symbol = make_symbol(event_id=event_id, submarket_name=market_name, outcome=outcome, params=params)
    return InstrumentId(symbol=symbol, venue=VENUE)
