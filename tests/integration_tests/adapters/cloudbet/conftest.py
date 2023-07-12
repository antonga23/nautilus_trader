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

import pytest

from nautilus_trader.model.events.account import AccountState
from nautilus_trader.model.identifiers import Venue

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.cloudbet.common import VENUE
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetTestStubs


@pytest.fixture()
def venue() -> Venue:
    return VENUE


@pytest.fixture(autouse=False)
def cloudbet_client(event_loop, logger):
    return CloudbetTestStubs.cloudbet_client(event_loop, logger)

@pytest.fixture()
def instrument_provider(cloudbet_client):
    return CloudbetTestStubs.instrument_provider(cloudbet_client=cloudbet_client)


@pytest.fixture()
def data_client():
    pass  # pragma: no cover


@pytest.fixture()
def exec_client():
    pass  # pragma: no cover


@pytest.fixture()
def instrument():
    pass  # pragma: no cover


@pytest.fixture()
def account_state() -> AccountState:
    pass  # pragma: no cover
