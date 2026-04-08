# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet instrument provider.
# -------------------------------------------------------------------------------------------------

from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.common.component import Logger
from nautilus_trader.model.identifiers import InstrumentId


class EasybetInstrumentProvider:
    """
    Placeholder instrument provider for Easybet.

    In production, this would scrape available markets from the sportsbook.

    """

    def __init__(self, logger: Logger):
        self._logger = logger
        self._instruments: dict[InstrumentId, CryptoBettingInstrument] = {}

    async def load_all_async(self) -> None:
        """
        Load all available instruments.
        """
        self._logger.info("Loading Easybet instruments (placeholder)")
        # TODO: Implement actual scraping of available markets

    def list_all(self) -> list[CryptoBettingInstrument]:
        """
        Return all loaded instruments.
        """
        return list(self._instruments.values())

    def get(self, instrument_id: InstrumentId) -> CryptoBettingInstrument | None:
        """
        Get instrument by ID.
        """
        return self._instruments.get(instrument_id)
