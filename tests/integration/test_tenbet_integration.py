# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Integration tests for 10bet web scraper.
# -------------------------------------------------------------------------------------------------

import asyncio
import os

import pytest
import pytest_asyncio


if os.getenv("RUN_LIVE_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_LIVE_INTEGRATION=1 to run live TenBet integration tests.",
        allow_module_level=True,
    )

pytest.importorskip("playwright.async_api")

from nautilus_trader.adapters.tenbet.browser_client import TenBetBrowserClient
from nautilus_trader.adapters.tenbet.constants import TENBET_BASE_URL
from nautilus_trader.adapters.tenbet.selectors import TenBetSelectors


@pytest.mark.asyncio
@pytest.mark.integration
class TestTenBetBrowserClient:
    """
    Integration tests for 10bet browser client.
    """

    @pytest_asyncio.fixture
    async def browser_client(self):
        """
        Create and connect browser client.
        """
        client = TenBetBrowserClient(
            base_url=TENBET_BASE_URL,
            headless=True,
            use_stealth=True,
            request_delay_min=1.0,
            request_delay_max=2.0,
            max_requests_per_minute=10,
        )
        await client.connect()
        yield client
        await client.disconnect()

    async def test_browser_connection(self, browser_client):
        """
        Test browser can connect and navigate.
        """
        # Should be connected after fixture setup
        assert browser_client._browser is not None
        assert browser_client._page is not None

    async def test_navigate_to_sports_page(self, browser_client):
        """
        Test navigation to sports page.
        """
        await browser_client.navigate_to(f"{TENBET_BASE_URL}/sports")

        # Check page loaded
        content = await browser_client.get_page_content()
        assert "10bet" in content.lower() or "sports" in content.lower()

    async def test_find_navigation_tabs(self, browser_client):
        """
        Test finding navigation tabs on sports page.
        """
        await browser_client.navigate_to(f"{TENBET_BASE_URL}/sports")
        await asyncio.sleep(5)  # Wait for page load

        # Try to find popular events tab
        popular_tab = await browser_client._page.wait_for_selector(
            TenBetSelectors.TAB_POPULAR_EVENTS,
            timeout=10000,
        )
        assert popular_tab is not None, (
            f"Popular events tab should be found using {TenBetSelectors.TAB_POPULAR_EVENTS}"
        )

        # Check live tab exists
        live_tab = await browser_client._page.query_selector(TenBetSelectors.TAB_LIVE)
        assert live_tab is not None, "Live tab should be found"

    async def test_find_team_names(self, browser_client):
        """
        Test finding team names on sports page.
        """
        await browser_client.navigate_to(f"{TENBET_BASE_URL}/sports")
        await asyncio.sleep(5)  # Wait for events to load

        # Find all team name elements
        team_elements = await browser_client._page.query_selector_all(TenBetSelectors.TEAM_NAME)

        # Should find multiple team names
        assert len(team_elements) > 0, (
            f"Should find at least one team name using {TenBetSelectors.TEAM_NAME}"
        )

        # Get text from first team
        if team_elements:
            first_team = await team_elements[0].text_content()
            assert len(first_team) > 0, "Team name should not be empty"

    async def test_find_odds_buttons(self, browser_client):
        """
        Test finding odds buttons on sports page.
        """
        await browser_client.navigate_to(f"{TENBET_BASE_URL}/sports")
        await browser_client._page.wait_for_selector(
            TenBetSelectors.ODDS_BUTTON_PARTIAL,
            timeout=10000,
        )

        # Find odds buttons using actual selector
        odds_buttons = await browser_client._page.query_selector_all(
            TenBetSelectors.ODDS_BUTTON_PARTIAL,
        )

        assert len(odds_buttons) > 0, "Should find at least one odds button"

        # Try to get odds value from first button
        if odds_buttons:
            # Get all spans in button
            spans = await odds_buttons[0].query_selector_all("span")
            assert len(spans) >= 2, "Odds button should have at least 2 spans (label + value)"

    async def test_rate_limiter(self, browser_client):
        """
        Test rate limiter enforces delays.
        """
        import time

        start = time.time()

        # Make 3 requests
        for _ in range(3):
            await browser_client.navigate_to(f"{TENBET_BASE_URL}/sports")

        duration = time.time() - start

        # Should take at least 3 seconds (3 requests * 1s min delay)
        assert duration >= 3.0, "Rate limiter should enforce minimum delays"

    async def test_session_info(self, browser_client):
        """
        Test session info tracking.
        """
        await browser_client.navigate_to(f"{TENBET_BASE_URL}/sports")

        info = browser_client.get_session_info()

        assert info["request_count"] > 0
        assert info["session_duration"] > 0
        assert info["is_logged_in"] is False  # No auth yet


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
class TestTenBetMarketScraping:
    """
    Integration tests for market data scraping.
    """

    async def test_scrape_soccer_markets(self):
        """
        Test scraping soccer markets from live site.
        """
        client = TenBetBrowserClient(
            base_url=TENBET_BASE_URL,
            headless=True,
            use_stealth=True,
        )

        try:
            await client.connect()

            # Navigate to soccer
            await client.navigate_to(f"{TENBET_BASE_URL}/sports")
            await asyncio.sleep(3)

            # Get page content
            content = await client.get_page_content()

            # Verify we got content
            assert len(content) > 1000, "Should receive substantial HTML content"

            # Basic checks
            assert "button" in content.lower(), "Should contain buttons"

        finally:
            await client.disconnect()


# Note: These tests require actual internet connection and will fail if:
# - 10bet.co.za is down or blocks requests
# - CSS selectors have changed
# - Network is unavailable

# Run with: pytest tests/integration/test_tenbet_integration.py -v -m integration
