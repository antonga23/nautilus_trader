# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybetbrowser client with anti-bot measures.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser
    from playwright.async_api import BrowserContext
    from playwright.async_api import Page
    from playwright.async_api import Playwright
else:
    Browser = BrowserContext = Page = Playwright = Any

try:
    from playwright.async_api import async_playwright
except ModuleNotFoundError:  # pragma: no cover - exercised when dependency is absent
    async_playwright = None

from nautilus_trader.adapters.easybet.constants import EASYBET_BASE_URL
from nautilus_trader.adapters.easybet.constants import EASYBET_SPORTSBOOK_URL


class RateLimiter:
    """
    Rate limiter for browser requests with random delays.
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
        self._request_times: list[float] = []

    async def wait(self) -> None:
        """
        Apply rate limiting with random delays.
        """
        # Random delay
        delay = random.uniform(self._min_delay, self._max_delay)  # noqa: S311
        await asyncio.sleep(delay)

        # Track request time
        now = time.time()
        self._request_times.append(now)

        # Clean up old requests (>1 minute ago)
        cutoff = now - 60
        self._request_times = [t for t in self._request_times if t > cutoff]

        # Check if we're over rate limit
        if len(self._request_times) >= self._max_requests_per_minute:
            # Wait until oldest request is over 1 minute old
            oldest = self._request_times[0]
            wait_time = 60 - (now - oldest)
            if wait_time > 0:
                await asyncio.sleep(wait_time)


class EasybetBrowserClient:
    """
    Browser automation client for Easybet web scraping.

    Implements anti-bot measures:
    - Rate limiting with random delays
    - Browser fingerprint randomization
    - Stealth mode scripts
    - Session rotation

    """

    def __init__(
        self,
        base_url: str = EASYBET_BASE_URL,
        use_iframe_source: bool = True,
        headless: bool = True,
        use_stealth: bool = True,
        request_delay_min: float = 1.0,
        request_delay_max: float = 3.0,
        max_requests_per_minute: int = 20,
    ):
        self._base_url = base_url
        self._use_iframe_source = use_iframe_source
        self._headless = headless
        self._use_stealth = use_stealth

        # Anti-bot components
        self._rate_limiter = RateLimiter(
            min_delay=request_delay_min,
            max_delay=request_delay_max,
            max_requests_per_minute=max_requests_per_minute,
        )

        # Browser state
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        # Session tracking
        self._session_start: float | None = None
        self._request_count = 0
        self._is_logged_in = False

    async def connect(self) -> None:
        """
        Initialize browser and create context.
        """
        if async_playwright is None:
            raise ModuleNotFoundError(
                "playwright is required for EasybetBrowserClient.connect(); "
                "install playwright to enable Easybet browser automation",
            )

        self._playwright = await async_playwright().start()

        # Launch browser
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        # Create context with randomized fingerprint
        self._context = await self._browser.new_context(
            # Pseudo-randomized browser fingerprints are intentional here.
            viewport={
                "width": random.randint(1200, 1920),  # noqa: S311
                "height": random.randint(800, 1080),  # noqa: S311
            },
            user_agent=self._get_random_user_agent(),
            locale="en-ZA",
            timezone_id="Africa/Johannesburg",
        )

        # Create page
        self._page = await self._context.new_page()

        # Apply stealth scripts if enabled
        if self._use_stealth:
            await self._apply_stealth_scripts()

        self._session_start = time.time()

    async def disconnect(self) -> None:
        """
        Close browser and cleanup.
        """
        page = self._page
        context = self._context
        browser = self._browser
        playwright = self._playwright

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._is_logged_in = False
        self._session_start = None
        self._request_count = 0

        if page:
            await page.close()
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()

    async def navigate_to(self, url: str) -> None:
        """
        Navigate to URL with rate limiting.
        """
        await self._rate_limiter.wait()

        if not self._page:
            raise RuntimeError("Browser not connected")

        await self._page.goto(url, wait_until="domcontentloaded")
        self._request_count += 1

    async def navigate_to_sportsbook(self) -> None:
        """
        Navigate to sportsbook (iframe source or main page).
        """
        if self._use_iframe_source:
            # Navigate directly to AdvBet iframe source
            await self.navigate_to(EASYBET_SPORTSBOOK_URL + f"&origin={self._base_url}")
        else:
            # Navigate to main Easybet page
            await self.navigate_to(f"{self._base_url}/sports")

    async def get_page_content(self) -> str:
        """
        Get current page HTML content.
        """
        if not self._page:
            raise RuntimeError("Browser not connected")
        return await self._page.content()

    async def _apply_stealth_scripts(self) -> None:
        """
        Apply anti-detection scripts.
        """
        if not self._page:
            return

        # Override navigator.webdriver
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        # Override permissions query
        await self._page.add_init_script("""
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

    @staticmethod
    def _get_random_user_agent() -> str:
        """
        Get randomized user agent string.
        """
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        return random.choice(agents)  # noqa: S311

    def get_session_info(self) -> dict[str, Any]:
        """
        Get current session information.
        """
        session_duration = time.time() - self._session_start if self._session_start else 0

        return {
            "session_duration": session_duration,
            "request_count": self._request_count,
            "is_logged_in": self._is_logged_in,
            "base_url": self._base_url,
            "use_iframe_source": self._use_iframe_source,
        }

    # Placeholder authentication methods
    @staticmethod
    async def login(email: str, password: str) -> bool:
        """
        Log in to Easybet (placeholder).
        """
        # TODO: Implement actual login flow when needed
        return False

    @staticmethod
    async def handle_otp(otp_code: str) -> bool:
        """
        Handle OTP verification (placeholder).
        """
        # TODO: Implement OTP verification when needed
        return False
