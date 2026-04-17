# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
10bet browser automation client using Playwright.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from nautilus_trader.adapters.tenbet.constants import TENBET_BASE_URL
from nautilus_trader.adapters.tenbet.constants import TENBET_SPORTS
from nautilus_trader.adapters.tenbet.selectors import TenBetSelectors
from nautilus_trader.common.component import Logger


if TYPE_CHECKING:
    from playwright.async_api import Browser
    from playwright.async_api import BrowserContext
    from playwright.async_api import Page
    from playwright.async_api import Playwright
    from playwright.async_api import ViewportSize


class RateLimiter:
    """
    Rate limiter for browser requests.

    Implements:
    - Random delays between requests
    - Maximum requests per minute tracking
    """

    def __init__(
        self,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        max_requests_per_minute: int = 20,
    ) -> None:
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._max_requests_per_minute = max_requests_per_minute
        self._request_times: deque = deque()

    async def wait(self) -> None:
        """
        Wait for rate limit compliance.
        """
        delay = random.uniform(self._min_delay, self._max_delay)  # noqa: S311
        await asyncio.sleep(delay)

        now = time.time()
        minute_ago = now - 60

        while self._request_times and self._request_times[0] < minute_ago:
            self._request_times.popleft()

        if len(self._request_times) >= self._max_requests_per_minute:
            sleep_time = self._request_times[0] - minute_ago
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        self._request_times.append(now)


class TenBetBrowserClient:
    """
    Playwright-based browser automation client for 10bet.co.za.

    The client supports three operating modes:
    - real browser login with credentials
    - persisted browser storage state
    - synthetic auth for CI or offline validation
    """

    def __init__(
        self,
        base_url: str = TENBET_BASE_URL,
        headless: bool = True,
        use_stealth: bool = True,
        request_delay_min: float = 1.0,
        request_delay_max: float = 3.0,
        max_requests_per_minute: int = 20,
        login_url: str | None = None,
        email: str | None = None,
        password: str | None = None,
        otp_code: str | None = None,
        session_state_path: str | None = None,
        allow_synthetic_auth: bool = False,
        logger: Logger | None = None,
    ) -> None:
        self._base_url = base_url
        self._login_url = login_url or f"{base_url.rstrip('/')}/account/login"
        self._email = email
        self._password = password
        self._otp_code = otp_code
        self._session_state_path = session_state_path
        self._allow_synthetic_auth = allow_synthetic_auth
        self._headless = headless
        self._use_stealth = use_stealth
        self._logger = logger

        self._rate_limiter = RateLimiter(
            min_delay=request_delay_min,
            max_delay=request_delay_max,
            max_requests_per_minute=max_requests_per_minute,
        )

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._is_logged_in = False
        self._auth_mode = "unauthenticated"
        self._session_start_time: float | None = None
        self._request_count = 0

    @property
    def is_connected(self) -> bool:
        return (
            self._playwright is not None
            and self._browser is not None
            and self._context is not None
            and self._page is not None
        )

    @property
    def is_logged_in(self) -> bool:
        return self._is_logged_in

    @property
    def auth_mode(self) -> str:
        return self._auth_mode

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser context not initialized. Call connect() first.")
        return self._context

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser page not initialized. Call connect() first.")
        return self._page

    async def connect(self) -> None:
        """
        Initialize browser and create context.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImportError(
                "Playwright not installed. Run: pip install playwright && playwright install chromium",
            ) from exc

        if self._logger:
            self._logger.info("Initializing Playwright browser...")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        viewport_widths = [1366, 1440, 1536, 1920]
        viewport_heights = [768, 900, 864, 1080]
        viewport: ViewportSize = {
            "width": random.choice(viewport_widths),  # noqa: S311
            "height": random.choice(viewport_heights),  # noqa: S311
        }

        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]

        context_kwargs: dict[str, Any] = {
            "viewport": viewport,
            "user_agent": random.choice(user_agents),  # noqa: S311
            "locale": "en-ZA",
            "timezone_id": "Africa/Johannesburg",
        }
        if self._session_state_path and Path(self._session_state_path).is_file():
            context_kwargs["storage_state"] = self._session_state_path

        self._context = await self._browser.new_context(**context_kwargs)

        if self._use_stealth:
            await self._apply_stealth()

        self._page = await self._context.new_page()
        self._session_start_time = time.time()

        if self._logger:
            self._logger.info(f"Browser connected: viewport={viewport}, headless={self._headless}")

    async def _apply_stealth(self) -> None:
        """
        Apply stealth techniques to avoid detection.
        """
        context = self._require_context()
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            """,
        )

        await context.add_init_script(
            """
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            """,
        )

    async def disconnect(self) -> None:
        """
        Close browser and cleanup.
        """
        request_count = self._request_count
        page = self._page
        context = self._context
        browser = self._browser
        playwright = self._playwright
        session_state_path = self._session_state_path

        if context is not None and session_state_path and self._is_logged_in:
            try:
                await context.storage_state(path=session_state_path)
            except Exception as exc:  # pragma: no cover - defensive cleanup
                if self._logger:
                    self._logger.warning(f"Failed to persist 10bet storage state: {exc}")

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._is_logged_in = False
        self._auth_mode = "unauthenticated"
        self._session_start_time = None

        first_error: Exception | None = None
        for cleanup in (
            page.close if page is not None else None,
            context.close if context is not None else None,
            browser.close if browser is not None else None,
            playwright.stop if playwright is not None else None,
        ):
            if cleanup is None:
                continue
            try:
                await cleanup()
            except Exception as exc:  # pragma: no cover - defensive cleanup
                if first_error is None:
                    first_error = exc

        if self._logger:
            self._logger.info(f"Browser disconnected. Total requests: {request_count}")
        if first_error is not None:
            raise first_error

    async def navigate_to(self, url: str) -> None:
        """
        Navigate to a URL with rate limiting.
        """
        await self._rate_limiter.wait()

        if self._logger:
            self._logger.debug(f"Navigating to: {url}")

        page = self._require_page()
        await page.goto(url, wait_until="domcontentloaded")
        self._request_count += 1

    async def login_placeholder(
        self,
        email: str | None = None,
        password: str | None = None,
        otp_code: str | None = None,
        allow_synthetic_auth: bool | None = None,
    ) -> bool:
        """
        Authenticate the browser session.

        If real credentials are available, this attempts the login form.
        If credentials are absent and synthetic auth is allowed, it marks the
        session as authenticated for validation-only flows.
        """
        allow_synthetic = (
            self._allow_synthetic_auth if allow_synthetic_auth is None else allow_synthetic_auth
        )
        email = email or self._email
        password = password or self._password
        otp_code = otp_code or self._otp_code

        if not email or not password:
            if allow_synthetic:
                self._is_logged_in = True
                self._auth_mode = "synthetic"
                if self._logger:
                    self._logger.warning(
                        "10bet credentials missing; using synthetic authenticated session for validation",
                    )
                return True
            if self._logger:
                self._logger.warning(
                    "10bet authentication skipped because credentials were missing"
                )
            return False

        try:
            await self.navigate_to(self._login_url)
            page = self._require_page()

            await self._fill_first(page, TenBetSelectors.EMAIL_INPUT, email)
            await self._fill_first(page, TenBetSelectors.PASSWORD_INPUT, password)
            await self._click_first(page, TenBetSelectors.SUBMIT_BUTTON)

            if otp_code:
                await self._fill_first(page, TenBetSelectors.OTP_INPUT, otp_code)
                await self._click_first(page, TenBetSelectors.SUBMIT_BUTTON)

            await self._wait_for_login_resolution(page)
            self._is_logged_in = not await self._has_any_selector(
                page, TenBetSelectors.SESSION_EXPIRED
            )
            self._auth_mode = "authenticated" if self._is_logged_in else "unauthenticated"
        except Exception as exc:
            if allow_synthetic:
                self._is_logged_in = True
                self._auth_mode = "synthetic"
                if self._logger:
                    self._logger.warning(
                        f"10bet live login failed ({exc}); continuing with synthetic auth",
                    )
                return True
            if self._logger:
                self._logger.error(f"10bet login failed: {exc}")
            return False

        if self._is_logged_in and self._session_state_path and self._context is not None:
            try:
                await self._context.storage_state(path=self._session_state_path)
            except Exception as exc:  # pragma: no cover - defensive cleanup
                if self._logger:
                    self._logger.warning(f"Failed to save 10bet session state: {exc}")

        if self._logger:
            self._logger.info(f"10bet login resolved using auth_mode={self._auth_mode}")
        return self._is_logged_in

    async def get_markets_for_sport(self, sport: str) -> list[dict[str, Any]]:
        """
        Scrape markets for a specific sport.
        """
        if self._logger:
            self._logger.info(f"Scraping markets for sport: {sport}")

        sport_url = f"{self._base_url.rstrip('/')}/sports/{sport}"
        await self.navigate_to(sport_url)
        page = self._require_page()

        await self._wait_for_page_ready(page)
        team_elements = await page.query_selector_all(TenBetSelectors.TEAM_NAME)
        odds_buttons = await page.query_selector_all(TenBetSelectors.ODDS_BUTTON_PARTIAL)

        team_names = [name for name in (await self._collect_texts(team_elements)) if name]
        odds_payloads = await self._parse_odds_buttons(odds_buttons)

        if len(team_names) < 2 or not odds_payloads:
            if self._logger:
                self._logger.warning(
                    f"No scrapeable 10bet markets found for sport={sport} "
                    f"(teams={len(team_names)}, odds_buttons={len(odds_buttons)})",
                )
            return []

        league_name = await self._extract_text(page, TenBetSelectors.LEAGUE_INFO) or sport.title()
        sport_id = TENBET_SPORTS.get(sport.lower(), 0)
        is_live = await self._has_any_selector(page, TenBetSelectors.LIVE_BADGE)
        start_time = await self._extract_start_time(page)
        markets: list[dict[str, Any]] = []

        market_count = min(len(team_names) // 2, max(1, len(odds_payloads) // 2))
        for index in range(market_count):
            home_name = team_names[index * 2].strip()
            away_name = team_names[index * 2 + 1].strip()
            market_hash = self._make_market_hash(
                sport=sport,
                home_name=home_name,
                away_name=away_name,
                league_name=league_name,
                index=index,
            )
            selected_odds = odds_payloads[index * 2 : index * 2 + 2]
            markets.append(
                {
                    "marketHash": market_hash,
                    "teamOneName": home_name,
                    "teamTwoName": away_name,
                    "sportId": sport_id,
                    "leagueName": league_name,
                    "type": 1 if len(selected_odds) >= 3 else 0,
                    "line": None,
                    "gameTime": start_time,
                    "isLive": is_live,
                    "orders": selected_odds,
                },
            )

        return markets

    async def get_page_content(self) -> str:
        """
        Get current page HTML content.
        """
        return await self._require_page().content()

    def get_session_info(self) -> dict[str, Any]:
        """
        Get session information.
        """
        return {
            "is_logged_in": self._is_logged_in,
            "auth_mode": self._auth_mode,
            "request_count": self._request_count,
            "session_duration": time.time() - self._session_start_time
            if self._session_start_time
            else 0,
            "session_state_path": self._session_state_path,
        }

    async def _wait_for_page_ready(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            return

    async def _wait_for_login_resolution(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle")
        except Exception:
            return

    async def _has_any_selector(self, page: Page, selector: str) -> bool:
        try:
            return await page.query_selector(selector) is not None
        except Exception:
            return False

    async def _fill_first(self, page: Page, selector: str, value: str) -> bool:
        try:
            element = await page.query_selector(selector)
            if element is None:
                return False
            await element.fill(value)
            return True
        except Exception:
            return False

    async def _click_first(self, page: Page, selector: str) -> bool:
        try:
            element = await page.query_selector(selector)
            if element is None:
                return False
            await element.click()
            return True
        except Exception:
            return False

    async def _collect_texts(self, elements: list[Any]) -> list[str]:
        texts: list[str] = []
        for element in elements:
            text = await self._extract_element_text(element)
            if text:
                normalized = " ".join(text.split())
                if normalized:
                    texts.append(normalized)
        return self._dedupe_consecutive(texts)

    @staticmethod
    def _dedupe_consecutive(values: list[str]) -> list[str]:
        deduped: list[str] = []
        for value in values:
            if not deduped or deduped[-1] != value:
                deduped.append(value)
        return deduped

    async def _extract_text(self, page: Page, selector: str) -> str | None:
        try:
            element = await page.query_selector(selector)
            return await self._extract_element_text(element)
        except Exception:
            return None

    async def _extract_element_text(self, element: Any) -> str | None:
        if element is None:
            return None
        text = None
        for accessor in ("text_content", "inner_text"):
            method = getattr(element, accessor, None)
            if method is None:
                continue
            try:
                text = await method()
            except Exception:
                text = None
            if text:
                break
        if not text:
            return None
        return str(text).strip()

    async def _parse_odds_buttons(self, buttons: list[Any]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for index, button in enumerate(buttons):
            text = await self._extract_element_text(button) or ""
            odds_value = self._extract_decimal_odds(text)
            if odds_value is None:
                continue

            label = self._extract_odds_label(text)
            payloads.append(
                {
                    "isMakerBettingOutcomeOne": self._is_outcome_one(label, index),
                    "percentageOdds": self._decimal_odds_to_percentage(odds_value),
                },
            )
        return payloads

    @staticmethod
    def _extract_decimal_odds(text: str) -> float | None:
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _extract_odds_label(text: str) -> str:
        tokens = [token.strip().lower() for token in re.split(r"\s+", text) if token.strip()]
        for token in tokens:
            if token in {"1", "x", "2", "home", "away", "draw", "yes", "no"}:
                return token
        return tokens[0] if tokens else ""

    @staticmethod
    def _is_outcome_one(label: str, index: int) -> bool:
        if label in {"1", "home", "yes"}:
            return True
        if label in {"2", "away", "no", "draw", "x"}:
            return False
        return index % 2 == 0

    @staticmethod
    def _decimal_odds_to_percentage(decimal_odds: float) -> int:
        if decimal_odds <= 0:
            return 0
        implied_probability = Decimal(1) / Decimal(str(decimal_odds))
        return int((implied_probability * Decimal(10000)).to_integral_value())

    @staticmethod
    def _make_market_hash(
        sport: str,
        home_name: str,
        away_name: str,
        league_name: str,
        index: int,
    ) -> str:
        signature = f"{sport}:{league_name}:{home_name}:{away_name}:{index}"
        return hashlib.sha256(signature.encode()).hexdigest()[:32]

    async def _extract_start_time(self, page: Page) -> str | None:
        title = await self._extract_text(page, "title")
        if not title:
            return None
        return title
