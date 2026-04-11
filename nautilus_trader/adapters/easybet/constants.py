# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet adapter constants.
# -------------------------------------------------------------------------------------------------

# Direct ADV.bet sportsbook URL (recommended - skips iframe wrapper)
# ADV.bet is the B2B white-label platform provider for Easybet
EASYBET_DIRECT_URL = (
    "https://sa01.sportsbook.adv.bet/"
    "?lang=en-ZA-easybet&oddsFormat=DECIMAL"
    "&orgUuid=0191fb10-7058-7104-aac5-02b11bbebf23"
    "&origin=https%3A%2F%2Fsport.easybet.co.za%2Fsports&theme=dark#/match/football"
)

# Main Easybet URL (has iframe redirect to ADV.bet)
EASYBET_BASE_URL = "https://sport.easybet.co.za"

# Legacy iframe URL (kept for reference)
EASYBET_SPORTSBOOK_URL = (
    "https://sa-gateway.gameshub.advbet.com/operators/"
    "0191fb10-7058-7104-aac5-02b11bbebf23/providers/sportsbook/url"
    "?lang=en-ZA-easybet&oddsFormat=DECIMAL&theme=dark"
)

# Venue identifier
EASYBET_VENUE = "EASYBET"

# Default polling intervals (seconds)
DEFAULT_QUOTE_POLL_INTERVAL = 3.0
DEFAULT_INSTRUMENT_POLL_INTERVAL = 60.0

# Rate limiting
DEFAULT_REQUEST_DELAY_MIN = 1.0
DEFAULT_REQUEST_DELAY_MAX = 3.0
DEFAULT_MAX_REQUESTS_PER_MINUTE = 20
