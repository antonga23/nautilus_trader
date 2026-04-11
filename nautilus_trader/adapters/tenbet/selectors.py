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
"""
CSS selectors for 10bet web scraping.

NOTE: These selectors were extracted from live 10bet.co.za inspection (Feb 2026).
They will break when 10bet updates their website. Regular maintenance required.

Last updated: 2026-02-08

"""


class TenBetSelectors:
    """
    CSS selectors and XPath expressions for 10bet.co.za (actual values from DOM
    inspection).
    """

    # ============ NAVIGATION (Actual) ============
    # Main tabs
    TAB_POPULAR_EVENTS = (
        "button#tab_POPULAR_EVENTS_WIDGET, [date-testid='widget.navigation.tab.popular']"
    )
    TAB_LIVE = "button#tab_LIVE_HIGHLIGHTS_WIDGET, [date-testid='widget.navigation.tab.live']"
    TAB_UPCOMING = (
        "button#tab_UPCOMING_EVENTS_WIDGET, [date-testid='widget.navigation.tab.upcoming']"
    )
    ACTIVE_TAB_CLASS = "activeTab__primary"

    # Sport selector
    SPORT_SELECTOR = "button[class*='_tab_']"

    # ============ EVENTS & TEAMS (Actual) ============
    # Team names - both home and away use this class
    TEAM_NAME = "div[data-testid='team.names'] span[class*='_truncateWrapper_'], span[class*='_selection-name_']"

    # League/Competition info
    LEAGUE_INFO = "div[data-testid='league.name'] span, span"  # Usually precedes team names

    # ============ ODDS (Actual) ============
    # Odds buttons
    ODDS_BUTTON = "button._container_c3av8_1"
    ODDS_BUTTON_PARTIAL = "button[class*='_container_c3av8_']"  # Partial match for stability

    # Odds value - nested inside button
    ODDS_VALUE = "button._container_c3av8_1 span:last-child span"

    # Odds label (1, X, 2, etc.)
    ODDS_LABEL = "button._container_c3av8_1 span:first-child"

    # Market container
    MARKET_1X2_CONTAINER = "div.1x2-container"  # If exists
    MARKET_HEADER = "div"  # Contains text like "1x2", "Total", etc.

    # ============ AUTHENTICATION (Placeholder) ============
    LOGIN_BUTTON = "button[data-test='login-button'], .login-btn, a[href*='login']"
    EMAIL_INPUT = "input[type='email'], input[name='email'], input[id*='email']"
    PASSWORD_INPUT = "input[type='password'], input[name='password'], input[id*='password']"  # noqa: S105
    SUBMIT_BUTTON = "button[type='submit'], button.submit-btn, input[type='submit']"
    OTP_INPUT = "input[name='otp'], input[id*='otp'], input[placeholder*='code']"

    # ============ BET SLIP (Placeholder - requires auth) ============
    BET_SLIP = "#bet-slip, .betslip, .bet-basket"
    BET_SLIP_TOGGLE = "button.betslip-toggle, .open-betslip"
    STAKE_INPUT = "input[name='stake'], input.stake-input, input[placeholder*='stake']"
    PLACE_BET_BUTTON = "button.place-bet, .submit-bet, button[type='submit'].bet-submit"
    BET_CONFIRMATION = ".bet-confirmed, .success-message, .confirmation"

    # ============ ERROR HANDLING ============
    ERROR_MESSAGE = ".error-message, .alert-error, .notification-error"
    SESSION_EXPIRED = ".session-expired, .logged-out-message"

    # ============ LIVE DATA ============
    LIVE_TAB = "button#tab_LIVE_HIGHLIGHTS_WIDGET"
    LIVE_BADGE = ".live-badge, .in-play"


# Documented selector patterns from inspection
ACTUAL_HTML_EXAMPLES = """
Example HTML structure from 10bet.co.za (2026-02-08):

1. Event Row:
<div>
    <span>Italy</span><span>Serie A</span>
    <span class="OpenTagWrapper_openTagWrapper__xzjLy">Juventus</span>
    <span class="OpenTagWrapper_openTagWrapper__xzjLy">Lazio</span>
    <div class="1x2-container">...</div>
</div>

2. Odds Button:
<button class="_container_c3av8_1 _vertical_c3av8_26 _selection_1y7wp_18">
    <span>1</span>  <!-- Label -->
    <span>
        <span>1.41</span>  <!-- Actual Odds -->
    </span>
</button>

3. Navigation:
<button id="tab_POPULAR_EVENTS_WIDGET" class="activeTab__primary">Pre-live</button>
<button id="tab_LIVE_HIGHLIGHTS_WIDGET">Live</button>
"""


# Fallback selectors (in priority order)
FALLBACK_SELECTORS = {
    "team_names": [
        "div[data-testid='team.names'] span[class*='_truncateWrapper_']",
        "span[class*='_selection-name_']",
        "span.OpenTagWrapper_openTagWrapper__xzjLy",
        "span[class*='OpenTagWrapper']",
        "span[class*='team']",
    ],
    "odds_buttons": [
        "button._container_c3av8_1",
        "button[class*='_container_c3av8_']",
        "button[class*='_container_']",
        "button.selection",
    ],
    "navigation_tabs": [
        "button#tab_POPULAR_EVENTS_WIDGET",
        "button[id*='tab_']",
        "button[class*='Tab']",
    ],
}
