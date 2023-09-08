# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2023 Nautech Systems Pty Ltd. All rights reserved.
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
from typing import Callable, Optional

import msgspec

from nautilus_trader.common.logging import Logger
from nautilus_trader.common.logging import LoggerAdapter
from nautilus_trader.network.socket import SocketClient

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.model.identifiers import InstrumentId

HOST = "stream-api.betfair.com"
# HOST = "stream-api-integration.betfair.com"
PORT = 443
CRLF = b"\r\n"
ENCODING = "utf-8"
_UNIQUE_ID = 0


class CloudbetStreamClient(SocketClient):
    """
    Provides a streaming client for `Cloudbet`.
    """

    def __init__(
        self,
        client: CloudbetClient,
        logger: Logger,
        message_handler,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        crlf: Optional[bytes] = None,
        encoding: Optional[str] = None,
    ):
        super().__init__(
            loop=loop or asyncio.get_event_loop(),
            logger=logger,
            host=host or HOST,
            port=port or PORT,
            handler=message_handler,
            crlf=crlf or CRLF,
            encoding=encoding or ENCODING,
        )
        self.client = client
        self.unique_id = self.new_unique_id()

    async def connect(self):
        self._log.debug("connecting")
        pass
        # TODO: implement this, also look at the base classs connect
        # return await super().connect()
        # # create HTTP session to allow networking calls
        # # create session if one doesn't already exist
        # if not self.client.connected:
        #     await self.client.connect()
        #
        # # # Schedule a heartbeat in 10s to give us a little more time to load instruments
        # # self._log.debug("scheduling heartbeat")
        # # self.create_task(self._post_connect_heartbeat())

    async def disconnect(self):
        # TODO: implement this, also look at the base classs connect
        # self._log.debug("disconnecting")
        pass
        # # Schedule a heartbeat in 10s to give us a little more time to load instruments
        # self._log.debug("scheduling heartbeat")
        # self.create_task(self._post_connect_heartbeat())
        # return await super().connect()

    async def send_subscription_message(
        self,
        event_id: str,
        market_key: str,
        outcome: str,
        params: Optional[str] = None,
        instrument_id: Optional[str] = None
    ):
        pass

    async def subscribe_instrument(self, instrument_id: InstrumentId):
        " Subscribe to a specific instrument. "
        # TODO: add some loggging
        # await self.send_subscription_message(instrument_id=instrument_id
        #                                      market_key=instrument_id.market_key,
        pass

    # subscribe multiple instruments
    async def subscribe_instruments(self, instrument_ids: list[InstrumentId]):
        "Subscribe to multiple instruments."
        # TODO: add some loggging
        for instrument_id in instrument_ids:
            await self.subscribe_instrument(instrument_id)

    # subscribe_orderbook
    async def subscribe_orderbook(self, instrument_id: InstrumentId):
        " Subscribe to a specific instrument. "
        # TODO: add some loggging
        # await self.send_subscription_message(instrument_id=instrument_id
        #                                      market_key=instrument_id.market_key,
        pass

    # unscubscribe instrument
    async def unsubscribe_instrument(self, instrument_id: InstrumentId):
        " Unsubscribe to a specific instrument. "
        pass
    # unsuscribe multiple instruments
    async def unsubscribe_instruments(self, instrument_ids: list[InstrumentId]):
        " Unsubscribe to multiple instruments. "
        pass

    #unsubscribe_orderboor
    async def unsubscribe_orderbook(self, instrument_id: InstrumentId):
        " Subscribe to a specific instrument. "
        pass

    # unsubscribe_mutliple_orderbooks
    async def unsubscribe_orderbooks(self, instrument_ids: list[InstrumentId]):
        " Subscribe to a specific instrument. "
        pass

    # async def _post_connect_heartbeat(self):
    #     for _ in range(3):
    #         await asyncio.sleep(5)
    #         await self._stream.send(msgspec.json.encode({"op": "heartbeat"}))

    def new_unique_id(self) -> int:
        global _UNIQUE_ID
        _UNIQUE_ID += 1
        return _UNIQUE_ID

    def auth_message(self):
        return {
            "op": "authentication",
        }


# class CloudbetOrderStreamClient(CloudbetStreamClient):
#     """
#     Provides an order stream client for `Cloudbet`.
#     """
#
#     def __init__(
#         self,
#         client: CloudbetClient,
#         logger: Logger,
#         message_handler,
#         # partition_matched_by_strategy_ref: bool = True,
#         # include_overall_position: Optional[str] = None,
#         # customer_strategy_refs: Optional[str] = None,
#         # **kwargs,
#     ):
#         super().__init__(
#             client=client,
#             logger_adapter=LoggerAdapter("CloudbetOrderStreamClient", logger),
#             message_handler=message_handler,
#             # **kwargs,
#         )
#         self.order_filter = {
#             "key": "value"
#         }
#
#     async def post_connection(self):
#         subscribe_msg = {
#             "op": "orderSubscription",
#         }
#         await self.send(msgspec.json.encode(self.auth_message()))
#         await self.send(msgspec.json.encode(subscribe_msg))


# class CloudbetMarketStreamClient(CloudbetStreamClient):
#     """
#     Provides a `Cloudbet` market stream client.
#     """
#
#     def __init__(self, client: CloudbetClient, logger: Logger, message_handler: Callable, **kwargs):
#         self.subscription_message = None
#         super().__init__(
#             client=client,
#             logger_adapter=LoggerAdapter("CloudbetMarketStreamClient", logger),
#             message_handler=message_handler,
#             **kwargs,
#         )
#
#         async def post_connection(self):
#             subscribe_msg = {
#                 "op": "orderSubscription",
#             }
#             await self.send(msgspec.json.encode(self.auth_message()))
#             await self.send(msgspec.json.encode(subscribe_msg))
#
#     async def send_subscription_message(
#         self,
#         event_id: str,
#         market_key: str,
#         outcome: str,
#         params: Optional[str] = None,
#         instrument_id: Optional[str] = None
#     ):
#         pass
#
#     async def subscribe_instrument(self, instrument_id: InstrumentId):
#         " Subscribe to a specific instrument. "
#         # await self.send_subscription_message(instrument_id=instrument_id
#         #                                      market_key=instrument_id.market_key,
#         pass
#         # if market_ids is not None:
#         #     # TODO - Log a warning about inefficiencies of specific market ids - Won't receive any updates for new
#         #     #  markets that fit criteria like when using event type / market type etc
#         #     # logging.warning()
#         #     pass
#         # market_filter = {
#         #     "marketIds": market_ids,
#         #     "bettingTypes": betting_types,
#         #     "eventTypeIds": event_type_ids,
#         #     "eventIds": event_ids,
#         #     "turnInPlayEnabled": turn_in_play_enabled,
#         #     "marketTypes": market_types,
#         #     "venues": venues,
#         #     "countryCodes": country_codes,
#         #     "raceTypes": race_types,
#         # }
#         # data_fields = []
#         # if subscribe_book_updates:
#         #     data_fields.append("EX_ALL_OFFERS")
#         # if subscribe_trade_updates:
#         #     data_fields.append("EX_TRADED")
#         # if subscribe_market_definitions:
#         #     data_fields.append("EX_MARKET_DEF")
#         # if subscribe_bsp_updates:
#         #     data_fields.append("SP_TRADED")
#         # if subscribe_bsp_projected:
#         #     data_fields.append("SP_PROJECTED")
#         #
#         # message = {
#         #     "op": "marketSubscription",
#         #     "id": self.unique_id,
#         #     "marketFilter": market_filter,
#         #     "marketDataFilter": {"fields": data_fields},
#         #     "initialClk": initial_clk,
#         #     "clk": clk,
#         #     "conflateMs": conflate_ms,
#         #     "heartbeatMs": heartbeat_ms,
#         #     "segmentationEnabled": segmentation_enabled,
#         # }
#         # await self.send(msgspec.json.encode(message))
#
#     async def post_connection(self):
#         await self.send(msgspec.json.encode(self.auth_message()))
