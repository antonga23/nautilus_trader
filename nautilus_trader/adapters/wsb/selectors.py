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
CSS selectors for WSB web scraping.

NOTE: These selectors are fragile and will break when WSB updates their website.
Regular maintenance required.

"""


class WSBSelectors:
    """
    CSS selectors and XPath expressions for WSB.co.za.
    """

    # Authentication (placeholder for future implementation)
    LOGIN_BUTTON = "button[data-test='login-button'], .login-btn, a[href*='login']"
    EMAIL_INPUT = "input[type='email'], input[name='email'], input[id*='email']"
    PASSWORD_INPUT = "input[type='password'], input[name='password'], input[id*='password']"  # noqa: S105
    SUBMIT_BUTTON = "button[type='submit'], button.submit-btn, input[type='submit']"
    OTP_INPUT = "input[name='otp'], input[id*='otp'], input[placeholder*='code']"

    # Navigation
    SPORTS_MENU = "nav.sports-menu, .sports-navigation, #sports-nav"
    SOCCER_LINK = "a[href*='soccer'], a[data-sport='soccer'], .sport-soccer"
    BASKETBALL_LINK = "a[href*='basketball'], a[data-sport='basketball'], .sport-basketball"

    # Markets and Events
    EVENT_ROWS = ".event-row, .match-row, .market-event, [data-test='event']"
    EVENT_NAME = ".event-name, .match-name, .teams, h3.event-title"
    HOME_TEAM = ".home-team, .team-home, .team:first-child"
    AWAY_TEAM = ".away-team, .team-away, .team:last-child"

    # Odds
    ODDS_BUTTONS = ".odds-btn, .odd, button.selection, [data-test='odd']"
    ODDS_VALUE = ".odds-value, .odd-value, .price"
    MARKET_TYPE = ".market-type, .bet-type, [data-market]"

    # Bet Slip
    BET_SLIP = "#bet-slip, .betslip, .bet-basket"
    BET_SLIP_TOGGLE = "button.betslip-toggle, .open-betslip"
    STAKE_INPUT = "input[name='stake'], input.stake-input, input[placeholder*='stake']"
    PLACE_BET_BUTTON = "button.place-bet, .submit-bet, button[type='submit'].bet-submit"
    BET_CONFIRMATION = ".bet-confirmed, .success-message, .confirmation"

    # Error handling
    ERROR_MESSAGE = ".error-message, .alert-error, .notification-error"
    SESSION_EXPIRED = ".session-expired, .logged-out-message"

    # Live data
    LIVE_INDICATOR = ".live, .in-play, [data-live='true']"
    ODDS_CHANGE_UP = ".odds-up, .price-up"
    ODDS_CHANGE_DOWN = ".odds-down, .price-down"


# Fallback selectors (alternative patterns)
FALLBACK_SELECTORS = {
    "event_rows": [
        ".event-row",
        ".match-row",
        "[class*='event']",
        "[class*='match']",
    ],
    "odds_buttons": [
        ".odds-btn",
        ".odd",
        "button[class*='odd']",
        "[data-odd]",
    ],
}
