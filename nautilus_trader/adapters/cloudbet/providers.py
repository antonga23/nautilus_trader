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
from functools import lru_cache
from typing import Optional, Union, List

import msgspec.json
import pandas as pd

from nautilus_trader.adapters.cloudbet.client.core import CloudbetClient
from nautilus_trader.adapters.betfair.client.enums import MarketProjection
from nautilus_trader.adapters.cloudbet.client.schema import Selection, SelectionStatus, SelectionSide, SelectionId, \
    GetLatestOddsResponse, GetEventResponse
from nautilus_trader.adapters.cloudbet.common import VENUE
from nautilus_trader.adapters.betfair.parsing.common import chunk
from nautilus_trader.adapters.betfair.parsing.requests import parse_handicap
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId

from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.model.currencies import EUR
from nautilus_trader.model.instruments import BettingInstrument
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments.crypto_betting import CryptoBettingInstrument


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
    """

    def __init__(
        self,
        client: CloudbetClient,
        logger: Logger,
        config: Optional[InstrumentProviderConfig] = None,
    ):

        super().__init__(
            venue=VENUE,
            logger=logger,
            config=config,
        )

        self._client = client
        # TODO: test if this is needed as the _cache should be a Nautilus cache/component nont a dict
        self._cache: dict[InstrumentId, CryptoBettingInstrument] = {}
        self._account_currency = EUR
        self._missing_instruments: set[CryptoBettingInstrument] = set()

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
        if not instrument_ids:
            self._log.info("No instrument IDs given for loading.")
            return
        # Check all instrument IDs
        for instrument_id in instrument_ids:
            PyCondition.equal(instrument_id.venue, self.venue, "instrument_id.venue", "self.venue")
        filters_str = "..." if not filters else f" with filters {filters}..."
        self._log.info(f"Loading instruments {instrument_ids}{filters_str}.")
        # For bulk loading instruments we need to make some performance optimisations
        # we want to reduce the number of network requests to the API primarily, but also
        # reduce unnecessary CPU cache hits.
        instruments: list[CryptoBettingInstrument] = []
        if len(instrument_ids) <= 10:
            for instrument_id in instrument_ids:
                event_id, market_name, outcome, params = instrument_id.symbol.value.split("|")
                event_id = int(event_id)  # necessary type conversion
                # TODO: Check if event is already in cache/instrument_provider, if so, load event and prepend to selection data. check if selection idupdate it
                # event: GetEventResponse = await self._client.get_event(selection_id.event_id)
                market_url = market_name + '/' + outcome + '?' + params if params is not None else market_name + '/' + outcome
                assert self._client.connected is True, "Client is not connected"
                updated_selection: GetLatestOddsResponse = await self._client.get_latest_odds(event_id, market_url)
                selection = Selection(event_id=event_id,
                                  status=updated_selection.status,
                                  market_name=market_name,
                                  outcome=outcome,
                                  price=updated_selection.price,
                                  min_stake=updated_selection.min_stake,
                                  max_stake=updated_selection.max_stake,
                                  probability=updated_selection.probability,
                                  selection_status=SelectionStatus(updated_selection.status),
                                  side=updated_selection.side.value,
                                  params=params if params is not None else None)
                instruments.append(self.selection_to_instrument(selection))
        elif len(instrument_ids) <= 10 and filters is None:
        # for a larger number of instruments, we should get the events first, then filter out the selections based on the selection ids
            selection_ids = [
                SelectionId(
                    event_id=int(instrument_id.symbol.value.split("|")[0]),
                    market_name=instrument_id.symbol.value.split("|")[1],
                    outcome=instrument_id.symbol.value.split("|")[2],
                    params=instrument_id.symbol.value.split("|")[3]
                )
                for instrument_id in instrument_ids
            ]
            event_ids = list(set([selection_id.event_id for selection_id in selection_ids]))
            market_names = list(set([selection_id.market_name for selection_id in selection_ids]))

            for event_id in event_ids:
            # TODO: only load events that are not already in cache
            # TODO: There are too many godamn for loops! implement a faster filtering mechanism to match and extract the selection level data
                event: GetEventResponse = await self._client.get_event(event_id)
                for market_name, market_value in event.markets.items():
                    if market_name in market_names:
                        for submarket_period, submarket_value in market_value.submarkets.items():
                            # Iterate over all the selections in the current submarket
                            for selection in submarket_value.selections:
                                selection_id = SelectionId(event_id=event_id, market_name=market_name, outcome=selection.outcome, params=selection.params)
                                # we're only interested in creating/loading instruments that have selection ids which are contained in the selection_id list
                                if selection_id in selection_ids:
                                    selection = Selection(
                                        event_id=event_id,
                                        status=event.status,
                                        market_name=market_name,
                                        outcome=selection.outcome,
                                        price=selection.price,
                                        min_stake=selection.minStake,
                                        max_stake=selection.maxStake,
                                        probability=selection.probability,
                                        selection_status=SelectionStatus(selection.status),
                                        side=selection.side,
                                        params=selection.params
                                    )
                                    instruments.append(self.selection_to_instrument(selection))
        else:
            if filters and filters.get("selection_id") is not None:
                selection_ids: List[SelectionId] = filters.get("selection_id")
                event_ids = list(set([selection_id.event_id for selection_id in selection_ids]))
                market_names = list(set([selection_id.market_name for selection_id in selection_ids]))
                for event_id in event_ids:
                    event: GetEventResponse = await self._client.get_event(event_id)
                    for market_name, market_value in event.markets.items():
                        if market_name in market_names:
                            for submarket_period, submarket_value in market_value.submarkets.items():
                                # Iterate over all the selections in the current submarket
                                for selection in submarket_value.selections:
                                    selection_id = SelectionId(event_id=event_id, market_name=market_name,
                                                               outcome=selection.outcome, params=selection.params)
                                    # we're only interested in creating/loading instruments that have selection ids which are contained in the selection_id list
                                    if selection_id in selection_ids:
                                        selection = Selection(
                                            event_id=event_id,
                                            status=event.status,
                                            market_name=market_name,
                                            outcome=selection.outcome,
                                            price=selection.price,
                                            min_stake=selection.minStake,
                                            max_stake=selection.maxStake,
                                            probability=selection.probability,
                                            selection_status=SelectionStatus(selection.status),
                                            side=selection.side,
                                            params=selection.params
                                        )
                                        instruments.append(self.selection_to_instrument(selection))
        self.add_bulk(instruments)

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
        PyCondition.not_none(instrument_id, "instrument_id")
        # TODO: create session for instrument provider if none exists
        # if client.is_connected is False then connect
        PyCondition.equal(instrument_id.venue, self.venue, "instrument_id.venue", "self.venue")
        filters_str = "..." if not filters else f" with filters {filters}..."
        self._log.debug(f"Loading instrument {instrument_id}{filters_str}.")
        # Symbol format >>>> 19490102|basketball.totals|over|total=164
        event_id , market_name , outcome , params = instrument_id.symbol.value.split("|")
        event_id = int(event_id) # necessary type conversion
        # TODO: Check if event is already in cache/instrument_provider, if so, load event and prepend to selection data. check if selection idupdate it
        # event: GetEventResponse = await self._client.get_event(selection_id.event_id)
        market_url = market_name + '/' + outcome + '?' + params if params is not None else market_name + '/' + outcome
        updated_selection: GetLatestOddsResponse = await self._client.get_latest_odds(event_id, market_url)
        selection = Selection(event_id=event_id,
                              status=updated_selection.status,
                              market_name=market_name,
                              outcome=outcome,
                              price=updated_selection.price,
                              min_stake=updated_selection.min_stake,
                              max_stake=updated_selection.max_stake,
                              probability=updated_selection.probability,
                              selection_status=SelectionStatus(updated_selection.status),
                              side=updated_selection.side.value,
                              # if params is None, set to None else set to params
                              params=params if params is not None else None)
        instrument = self.selection_to_instrument(selection)
        self.add(instrument=instrument)
        self._log.debug(f"Loaded instrument {instrument.id}")

    async def load_all_async(self, filters: Optional[dict] = None) -> int:
        """
        Load the latest instruments into the provider asynchronously, optionally
        applying the given filters.

        Parameters
        ----------
        filters : Optional[dict]
            The venue specific instrument loading filters to apply.
            example:
            filters = {
                'sport_key': 'tennis',
                'from_timestamp': 1614556800,
                'to_timestamp': 1614643200,
                'live': 'false',
                'limit': 100,
            }
        Returns
        -------
        int
            The number of instruments loaded.
        """
        selection_filter = filters or self._filters
        self._log.info(f"Loading selections with selection_filter={selection_filter}")
        selections: list[list[Selection]] = await self._client.load_selection(selection_filter)
        self._log.info("Creating instruments..")
        instrument_count = 0
        for events in selections:
            if len(events) > 0:
                for selection in events:
                    self.add(instrument=self.selection_to_instrument(selection))
                    instrument_count += 1
            else:
                self._log.info(f"Selection {events} has no markets")
                continue
        self._log.info(f"{len(selections)} Instruments created")
        return instrument_count

    def selection_to_instrument(self, selection: Selection) -> CryptoBettingInstrument:
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
        # ToDO: add some error handling
        # eg. try:
        #         instrument = CryptoBettingInstrument()
        # except ValueError as e:
        #     if self._log_warnings:
        #         self._log.warning(f"Unable to parse instrument {symbol_info.symbol}, {e}.")
        instrument = CryptoBettingInstrument(
            home_name=selection.home_name,
            away_name=selection.away_name,
            sport_name=selection.sport_name,
            competition_name=selection.competition_name,
            price=selection.price,
            currency=selection.currency,
            event_name=selection.event_name,
            market_name=selection.market_name,
            venue=self.venue,
            live=False if selection.status != "TRADING_LIVE" else True,
            enabled=True if selection.selection_status != SelectionStatus.DISABLED else False,
            side=SelectionSide.BACK if selection.side == "BACK" else SelectionSide.LAY if selection.side == "LAY" else SelectionSide.UNKNOWN,
            outcome=selection.outcome,
            # ToDo: set at Selection level with Enums + caching for performance
            market_type=selection.submarket_name,
            trading_status=selection.status,
            event_id=selection.event_id,
            params=selection.params,
            max_size=int(selection.max_stake),
            min_size=selection.min_stake,
            end_time=selection.cutoff_time,
            # handicap=
        )
        return instrument

    async def get_instruments_update_async(self, instrument_ids: List[InstrumentId]) -> CryptoBettingInstrument:
        """
        Get the latest instruments update for the given instrument IDs.

        Parameters
        ----------
        instrument_ids : List[InstrumentId]
            The instrument IDs to get the latest updates for.

        Returns
        -------
        List[BettingInstrument]
            The latest instrument updates for the given instrument IDs.

        """
        raise NotImplementedError("get_instruments_update_async is not supported for Cloudbet")

    def search_instruments(self, instrument_filter: Optional[dict] = None) -> Optional[List[CryptoBettingInstrument]]:
        """Search for instruments within the cache. Useful for debugging / interactive use"""
        instruments = self.list_all()
        if instrument_filter:
            instruments = [
                ins
                for ins in instruments
                if all(getattr(ins, k) == v for k, v in instrument_filter.items())
            ]
        return instruments

    #TODO: TEST get_betting_instrument
    def get_betting_instrument(
        self,
        market_id: str,
        selection_id: str,
        handicap: str,
    ) -> BettingInstrument:
        """Return a betting instrument with performance friendly lookup."""
        key = (market_id, selection_id, handicap)
        if key not in self._cache:
            instrument_filter = {
                "market_id": market_id,
                "selection_id": selection_id,
                "selection_handicap": parse_handicap(handicap),
            }
            instruments = self.search_instruments(instrument_filter=instrument_filter)
            count = len(instruments)
            if count < 1:
                key = (market_id, selection_id, parse_handicap(handicap))
                if key not in self._missing_instruments:
                    self._log.warning(f"Found 0 instrument for filter: {instrument_filter}")
                    self._missing_instruments.add(key)
                return
            # assert count == 1, f"Wrong number of instruments: {len(instruments)} for filter: {instrument_filter}"
            self._cache[key] = instruments[0]
        return self._cache[key]

    @staticmethod
    @lru_cache
    def id_to_selection_id(instrument_id: InstrumentId) -> SelectionId:
        """
        Convert an instrument ID to a Cloudbet Selection ID.
        Extract the event_id, market_name, outcome and params from a symbol

        Parameters
        ----------
        instrument_id : InstrumentId
            The instrument ID to convert.

        Returns
        -------
        str
            The selection ID.

        """
        event_id, market_name, outcome, params = instrument_id.value.split("|")
        return SelectionId(int(event_id), market_name, outcome, params)

