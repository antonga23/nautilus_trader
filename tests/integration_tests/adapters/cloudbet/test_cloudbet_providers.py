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
import asyncio
import msgspec
import pytest
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
import pytest

from nautilus_trader.adapters._template.core import TEMPLATE_VENUE
from nautilus_trader.adapters._template.providers import TemplateInstrumentProvider
from nautilus_trader.common.clock import TestClock
from nautilus_trader.common.logging import Logger

from nautilus_trader.adapters.cloudbet.providers import CloudbetInstrumentProvider
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from tests.integration_tests.adapters.cloudbet.test_kit import CloudbetTestStubs

# pytestmark = pytest.mark.skip(reason="template")


class TestCloudbetInstrumentProvider:
    def setup(self):
        # Fixture Setup
        self.loop = asyncio.get_event_loop()
        self.clock = LiveClock()
        self.logger = Logger(clock=self.clock, bypass=True)
        self.client = CloudbetTestStubs.cloudbet_client(loop=self.loop, logger=self.logger)
        self.provider = CloudbetInstrumentProvider(
            client=self.client,
            logger=TestComponentStubs.logger(),
        )

    @pytest.mark.asyncio()
    async def test_load_all_async(self):
        await self.client.connect()
        await self.provider.load_all_async()
        print(self.provider.count)
        assert self.provider.count > 0


    # def test_load_all(instrument_provider):
    #     pass
    #
    #
    # def test_load(instrument_provider):
    #     pass
