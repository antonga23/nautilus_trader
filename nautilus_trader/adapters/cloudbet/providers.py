# -----------------------------------book--------------------------------------------------------------
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

import time
from typing import Optional, Union

import msgspec.json
import pandas as pd

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.betfair.client.enums import MarketProjection
from nautilus_trader.adapters.cloudbet.client.schema import Selection
from nautilus_trader.adapters.cloudbet.common import VENUE
from nautilus_trader.adapters.betfair.parsing.common import chunk
from nautilus_trader.adapters.betfair.parsing.requests import parse_handicap
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import Instrument


# async def load_markets(client: CloudbetClient,
#                        market_filter: Optional[dict] = None,
#                        ) -> list[Selection]:
#
#     """Search Cloudbet markets and perform data validation."""
#     # ToDo: annotate type for sports
#     # eg sports: list[Sport] = await client.get_sports()
#     sports = await client.get_sports()
#     for sport in sports:
#         # ToDo: annotate type for events
#         # eg events: list[Event] = await client.get_events_for_sport(sport['key'])
#         events = await client.get_events_for_sport(sport['key'])
#         for event in events:
#             client.get_markets(event)
#             for market in markets:
#                 client.get_selections(market)
#                 for selection in selections:
#                     pass
#     navigation: Navigation = await client.list_navigation()
#     markets = navigation_to_flatten_markets(navigation, **market_filter)
#     return markets


class CloudbetInstrumentProvider(InstrumentProvider):
    """
    Provides a means of loading `BettingInstruments` from the Cloudbet API.

    Parameters
    ----------
    client : CloudbetClient
        The client for the provider. provides some convenience methods for loading config.
    logger : Logger
        The logger for the provider.
    config : InstrumentProviderConfig, optional
        The configuration for the provider.
        # ToDo: add filters
        # ToDo: test loading all instruments time
    limit : int, not optional (default=100) for performance reasons
        The maximum number of instruments to load.
    """

    def __init__(
        self,
        client: CloudbetClient,
        logger: Logger,
        filters: Optional[dict] = None,
        config: Optional[InstrumentProviderConfig] = None,
        limit: int = 100,
    ):
        if config is None:
            config = InstrumentProviderConfig(
                load_all=True,
                filters=filters,
            )
        super().__init__(
            venue=VENUE,
            logger=logger,
            config=config,
        )

        self._client = client
        self._cache: dict[InstrumentId, BettingInstrument] = {}
        self._account_currency = None
        self._missing_instruments: set[BettingInstrument] = set()

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: Optional[dict] = None,
    ) -> None:
        """
        Load the instruments for the given IDs into the provider, optionally
        applying the given filters.

        Parameters
        ----------
        instrument_ids : list[InstrumentId]
            The instrument IDs to load.
        filters : dict, optional
            The venue specific instrument loading filters to apply.

        Raises
        ------
        ValueError
            If any `instrument_id.venue` is not equal to `self.venue`.

        """
        raise NotImplementedError("method must be implemented in the subclass")

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: Optional[dict] = None,
    ) -> None:
        """
        Load the instrument for the given ID into the provider asynchronously, optionally
        applying the given filters.

        Parameters
        ----------
        instrument_id : InstrumentId
            The instrument ID to load.
        filters : dict, optional
            The venue specific instrument loading filters to apply.

        Raises
        ------
        ValueError
            If `instrument_id.venue` is not equal to `self.venue`.

        """
        raise NotImplementedError("method must be implemented in the subclass")

    async def load_all_async(self, filters: Optional[dict] = None):
        """
        Load the latest instruments into the provider asynchronously, optionally
        applying the given filters.
        """
        # ToDO: pass + process parameters filters
        market_filter = filters or self._filters
        self._log.info(f"Loading markets with market_filter={market_filter}")
        # A market/event roughly corresponds to a group of events filtered by
        # 1) the competition/tournament the event is from eg. French Open
        # 2) the event Sport eg . Tennis
        # 3) the start and endtime of the event
        # NB for now we don't dynamically load markets, we load all markets
        # NB we filter at the market level but return selections for all markets
        # NB we don't filter at the selection level eg. handicap, over/under
        # ToDO: pass + process parameters filters
        # eg only load 100 eevnts for next 24 hours
        selections: list[list[Selection]] = await self._client.load_selection()

        self._log.info(f"Found {len(selections)} unique selections")
        self._log.info("Creating instruments..")
        for events in selections:
            if len(events) > 0:
                for selection in events:
                    self.add(instrument=self.selection_to_instrument(selection))
            else:
                self._log.info(f"Selection {events} has no markets")
                continue

        self._log.info(f"{len(selections)} Instruments created")

    def selection_to_instrument(self, selection: Selection) -> BettingInstrument:
        """
        Create a `BettingInstrument` from a selection.

        Parameters
        ----------
        selection : Selection
            The selection to create the instrument from.

        Returns
        -------
        BettingInstrument
            The instrument created from the selection.

        """
        betting_instrument = BettingInstrument.__new__(unicode_venue_name=self.venue,
            unicode_event_type_id=selection.event_id,
            unicode_event_type_name=selection.selection_status,
            unicode_competition_id=self.venue,
            unicode_competition_name=selection.event_name,
            unicode_event_id=selection.event_id,
            unicode_event_name=selection.event_name,
            unicode_event_country_code=selection.competition_name,
            datetime_event_open_date=pd.Timestamp(0, tz="UTC"),
            unicode_betting_type=selection.side,
            unicode_market_id=selection.market_name,
            unicode_market_name=selection.market_name,
            datetime_market_start_time=selection.cutoff_time,
            unicode_market_type=selection.market_name,
            unicode_selection_id=selection.market_name,
            unicode_selection_name=selection.market_name,
            unicode_currency=self._account_currency,
            unicode_selection_handicap=selection.market_name,
            uint64_t_ts_event=selection.event_id,
            uint64_t_ts_init=selection.event_id,
            Price_min_price=selection.price,
            Price_max_price=selection.price)
        # betting_instrument = BettingInstrument.__init__(unicode_venue_name=self.venue,
        #     unicode_event_type_id=selection.event_id,
        #     unicode_event_type_name=selection.selection_status,
        #     unicode_competition_id=self.venue,
        #     unicode_competition_name=selection.event_name,
        #     unicode_event_id=selection.event_id,
        #     unicode_event_name=selection.event_name,
        #     unicode_event_country_code=selection.competition_name,
        #     datetime_event_open_date=pd.Timestamp(0, tz="UTC"),
        #     unicode_betting_type=selection.side,
        #     unicode_market_id=selection.market_name,
        #     unicode_market_name=selection.market_name,
        #     datetime_market_start_time=selection.cutoff_time,
        #     unicode_market_type=selection.market_name,
        #     unicode_selection_id=selection.market_name,
        #     unicode_selection_name=selection.market_name,
        #     unicode_currency=self._account_currency,
        #     unicode_selection_handicap=selection.market_name,
        #     uint64_t_ts_event=selection.event_id,
        #     uint64_t_ts_init=selection.event_id,
        #     Price_min_price=selection.price,
        #     Price_max_price=selection.price)
        # betting_instrument = BettingInstrument(
        #     unicode_venue_name=self.venue,
        #     unicode_event_type_id=selection.event_id,
        #     unicode_event_type_name=selection.selection_status,
        #     unicode_competition_id=self.venue,
        #     unicode_competition_name=selection.event_name,
        #     unicode_event_id=selection.event_id,
        #     unicode_event_name=selection.event_name,
        #     unicode_event_country_code=selection.competition_name,
        #     datetime_event_open_date=pd.Timestamp(0, tz="UTC"),
        #     unicode_betting_type=selection.side,
        #     unicode_market_id=selection.market_name,
        #     unicode_market_name=selection.market_name,
        #     datetime_market_start_time=selection.cutoff_time,
        #     unicode_market_type=selection.market_name,
        #     unicode_selection_id=selection.market_name,
        #     unicode_selection_name=selection.market_name,
        #     unicode_currency=self._account_currency,
        #     unicode_selection_handicap=selection.market_name,
        #     uint64_t_ts_event=selection.event_id,
        #     uint64_t_ts_init=selection.event_id,
        #     Price_min_price=selection.price,
        #     Price_max_price=selection.price,
        #     dict_info={
        #         'max_stake': selection.max_stake,
        #         'min_stake': selection.min_stake,
        #     }
        # )
        return betting_instrument
