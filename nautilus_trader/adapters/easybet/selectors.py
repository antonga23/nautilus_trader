# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  CSS selectors for Easybet web scraping.
#
#  NOTE: These selectors were extracted from live easybet.co.za inspection (Feb 2026).
#  Easybet uses AdvBet/GamesHub iframe provider which may update independently.
#  Regular maintenance required.
#
#  Last updated: 2026-02-08
# -------------------------------------------------------------------------------------------------
"""
CSS selectors for Easybet web scraping.

TECHNICAL NOTE:
Easybet embeds sportsbook content via iframe from AdvBet/GamesHub provider:
https://sa-gateway.gameshub.advbet.com/

For optimal scraping, navigate directly to the iframe source URL rather than
the main easybet.co.za page to avoid cross-origin iframe complexity.

"""


class EasybetSelectors:
    """
    CSS selectors and XPath expressions for Easybet (actual values from DOM inspection).
    """

    # ============ NAVIGATION (Actual) ============
    # Sports menu in sidebar
    SPORTS_MENU_LINK = "a.sports-filter__router-link"
    SPORT_NAME = "span.sports-filter__list-item-title"
    SPORT_COUNT = "span.sports-filter__list-item-count"

    # In-play / Live tab
    IN_PLAY_TAB = "a[href='#/in-play']"

    # ============ EVENTS & TEAMS (Actual) ============
    # Event rows
    EVENT_ROW_PRE_MATCH = "a[href*='#/match/']"
    EVENT_ROW_LIVE = "a[href*='#/in-play/']"

    # Team names (ordered within event container)
    TEAM_NAMES = "span"  # Home first, Away second within event details

    # Live event specifics
    LIVE_EVENT_CONTAINER = "div.live-event"
    LIVE_EVENT_TIME = "span.live-event__time"  # e.g., "85'", "Half-Time"
    LIVE_EVENT_PERIOD = "span.live-event__period"  # e.g., "2nd Half"
    LIVE_EVENT_SCORE = "div.live-event__score span"  # Home score, Away score

    # ============ ODDS (Actual) ============
    # Odds buttons
    ODDS_BUTTON = "div.bid-option"
    ODDS_VALUE = "div.option-odds-current"  # The numeric value (e.g., "1.67")

    # Market selection
    MARKET_TYPE_FILTER = "div.event-list__filter-market-type"
    MARKET_TYPE_LABEL = "div.event-list__filter-market-type span"

    # ============ AUTHENTICATION (Placeholder) ============
    LOGIN_BUTTON = "button[data-test='login'], .login-btn, a[href*='login']"
    EMAIL_INPUT = "input[type='email'], input[name='email'], input[id*='email']"
    PASSWORD_INPUT = "input[type='password'], input[name='password'], input[id*='password']"  # noqa: S105
    SUBMIT_BUTTON = "button[type='submit'], button.submit-btn"

    # ============ BET SLIP (Placeholder) ============
    BET_SLIP = "#betslip, .betslip, .bet-basket"
    BET_SLIP_TOGGLE = "button.betslip-toggle"
    STAKE_INPUT = "input[name='stake'], input.stake-input"
    PLACE_BET_BUTTON = "button.place-bet, .submit-bet"
    BET_CONFIRMATION = ".bet-confirmed, .success-message"

    # ============ ERROR HANDLING ============
    ERROR_MESSAGE = ".error-message, .alert-error"
    SESSION_EXPIRED = ".session-expired"


# Documented selector patterns from inspection
ACTUAL_HTML_EXAMPLES = """
Example HTML structure from easybet.co.za (2026-02-08):

1. Sports Menu Item:
<a class="sports-filter__router-link" href="#/match/football">
  <li class="sports-filter__list-item">
    <span class="sports-filter__list-item-title">Soccer</span>
    <span class="sports-filter__list-item-count">1028</span>
  </li>
</a>

2. Live Event Row:
<a href="#/in-play/football/italy/serie-a/juventus-turin-lazio-rome-2359">
  <div class="live-event">
    <div class="event-details-container">
      <span>Juventus Turin</span>
      <span>Lazio Rome</span>
      <span class="live-event__time">85'</span>
      <span class="live-event__period">2nd Half</span>
    </div>
    <div class="live-event__score">
      <span>1</span>  <!-- Home Score -->
      <span>2</span>  <!-- Away Score -->
    </div>
  </div>
</a>

3. Odds Button:
<div class="bid-option">
  <div class="option-value">
    <div class="option-odds-current">1.67</div>
  </div>
</div>

4. Market Type Filter:
<div class="event-list__filter-market-type">
  <span>3 Way</span>
</div>
"""


# Fallback selectors (in priority order)
FALLBACK_SELECTORS = {
    "sports_menu": [
        "a.sports-filter__router-link",
        "a[class*='sports-filter']",
        "a[href*='#/match/']",
    ],
    "odds_buttons": [
        "div.bid-option",
        "div[class*='bid-option']",
        "div[class*='option']",
    ],
    "odds_values": [
        "div.option-odds-current",
        "div[class*='odds-current']",
        "div[class*='odds']",
    ],
    "event_rows": [
        "a[href*='#/match/']",
        "a[href*='#/in-play/']",
        "a[class*='event']",
    ],
}
