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
WSB browser automation client using Playwright.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import TYPE_CHECKING
from typing import Any

from nautilus_trader.adapters.wsb.constants import WSB_SPORTS
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
    ):
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._max_requests_per_minute = max_requests_per_minute
        self._request_times: deque = deque()

    async def wait(self) -> None:
        """
        Wait for rate limit compliance.
        """
        # Random delay
        delay = random.uniform(self._min_delay, self._max_delay)  # noqa: S311
        await asyncio.sleep(delay)

        # Check requests per minute
        now = time.time()
        minute_ago = now - 60

        # Remove old requests
        while self._request_times and self._request_times[0] < minute_ago:
            self._request_times.popleft()

        # If at limit, wait
        if len(self._request_times) >= self._max_requests_per_minute:
            sleep_time = self._request_times[0] - minute_ago
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Record this request
        self._request_times.append(now)


class WSBBrowserClient:
    """
    Playwright-based browser automation client for WSB.co.za.

    Implements anti-bot measures:
    - Random delays between requests
    - Rate limiting (requests per minute)
    - Browser fingerprint randomization
    - Stealth mode
    - Session rotation capability

    NOTE: This is a placeholder implementation. Full authentication
    requires email/password/OTP which will be implemented later.

    """

    def __init__(
        self,
        base_url: str,
        headless: bool = True,
        use_stealth: bool = True,
        request_delay_min: float = 1.0,
        request_delay_max: float = 3.0,
        max_requests_per_minute: int = 20,
        logger: Logger | None = None,
    ):
        self._base_url = base_url
        self._headless = headless
        self._use_stealth = use_stealth
        self._logger = logger

        # Rate limiter
        self._rate_limiter = RateLimiter(
            min_delay=request_delay_min,
            max_delay=request_delay_max,
            max_requests_per_minute=max_requests_per_minute,
        )

        # Playwright objects (initialized in connect())
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._is_logged_in = False

        # Session management
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

        # Launch browser with fingerprint randomization
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        # Create context with randomized viewport
        viewport_widths = [1366, 1440, 1536, 1920]
        viewport_heights = [768, 900, 864, 1080]
        viewport: ViewportSize = {
            "width": random.choice(viewport_widths),  # noqa: S311
            "height": random.choice(viewport_heights),  # noqa: S311
        }

        # Randomize user agent
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]

        self._context = await self._browser.new_context(
            viewport=viewport,
            user_agent=random.choice(user_agents),  # noqa: S311
            locale="en-ZA",
            timezone_id="Africa/Johannesburg",
        )

        # Apply stealth if enabled
        if self._use_stealth:
            await self._apply_stealth()

        # Create page
        self._page = await self._context.new_page()

        self._session_start_time = time.time()

        if self._logger:
            self._logger.info(f"Browser connected: viewport={viewport}, headless={self._headless}")

    async def _apply_stealth(self) -> None:
        """
        Apply stealth techniques to avoid detection.
        """
        context = self._require_context()
        # Override navigator.webdriver
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        # Override permissions
        await context.add_init_script("""
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

    async def disconnect(self) -> None:
        """
        Close browser and cleanup.
        """
        request_count = self._request_count
        page = self._page
        context = self._context
        browser = self._browser
        playwright = self._playwright

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._is_logged_in = False
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
            except Exception as e:  # pragma: no cover - defensive cleanup
                if first_error is None:
                    first_error = e

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

    async def login_placeholder(self, email: str, password: str) -> bool:
        """
        Provide placeholder for future authentication implementation.

        TODO: Implement full login flow:
        1. Navigate to login page
        2. Fill email/password
        3. Handle OTP if required
        4. Verify session established

        Returns
        -------
        bool
            True if login successful (placeholder always returns False).

        """
        if self._logger:
            self._logger.warning(
                "login_placeholder called but not implemented. "
                "Authentication will be added in future updates.",
            )

        if not email or not password:
            return False

        # For now, just navigate to main page
        await self.navigate_to(self._base_url)
        return False

    async def get_markets_for_sport(self, sport: str) -> list[dict[str, Any]]:
        """
        Scrape markets for a specific sport.

        This is a placeholder that returns empty data.
        Actual implementation will parse HTML using selectors.

        Parameters
        ----------
        sport : str
            Sport name (e.g., "soccer", "basketball").

        Returns
        -------
        list[dict[str, Any]]
            List of market data dictionaries.

        """
        if self._logger:
            self._logger.info(f"Scraping markets for sport: {sport}")

        # Navigate to sport page
        sport_url = self._sport_url(sport)
        await self.navigate_to(sport_url)

        # Wait for content to load
        await asyncio.sleep(2)

        markets: list[dict[str, Any]] = []

        try:
            # Try to find event rows
            page = self._require_page()
            event_elements = await page.query_selector_all(".event-row")

            if self._logger:
                self._logger.info(f"Found {len(event_elements)} events")

            # Placeholder: return empty for now
            # Full implementation will extract:
            # - Event names, teams
            # - Market types
            # - Odds values
            # - Event IDs

        except Exception as e:
            if self._logger:
                self._logger.error(f"Error scraping markets: {e}")

        return markets

    def _sport_url(self, sport: str) -> str:
        sport_key = sport.strip().lower()
        if sport_key not in WSB_SPORTS:
            raise ValueError(f"Unsupported WSB sport: {sport}")

        return f"{self._base_url}/sports/{sport_key}"

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
            "request_count": self._request_count,
            "session_duration": time.time() - self._session_start_time
            if self._session_start_time
            else 0,
        }
