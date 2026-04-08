#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for WSB browser client input validation.
# -------------------------------------------------------------------------------------------------

from unittest.mock import AsyncMock

import pytest

from nautilus_trader.adapters.wsb.browser_client import WSBBrowserClient
from nautilus_trader.adapters.wsb.constants import WSB_BASE_URL


def test_wsb_browser_client_builds_allowed_sport_urls():
    client = WSBBrowserClient(base_url=WSB_BASE_URL)

    assert client._sport_url(" Soccer ") == f"{WSB_BASE_URL}/sports/soccer"


def test_wsb_browser_client_rejects_unknown_sports():
    client = WSBBrowserClient(base_url=WSB_BASE_URL)

    with pytest.raises(ValueError, match="Unsupported WSB sport"):
        client._sport_url("../admin")


@pytest.mark.asyncio
async def test_wsb_browser_client_requires_connected_page_for_content():
    client = WSBBrowserClient(base_url=WSB_BASE_URL)

    with pytest.raises(RuntimeError, match="Browser page not initialized"):
        await client.get_page_content()


@pytest.mark.asyncio
async def test_wsb_browser_client_disconnect_clears_connection_state():
    client = WSBBrowserClient(base_url=WSB_BASE_URL)
    client._page = AsyncMock()
    client._context = AsyncMock()
    client._browser = AsyncMock()
    client._playwright = AsyncMock()
    client._is_logged_in = True
    client._session_start_time = 123.0

    await client.disconnect()

    assert client.is_connected is False
    assert client._page is None
    assert client._context is None
    assert client._browser is None
    assert client._playwright is None
    assert client._is_logged_in is False
    assert client._session_start_time is None
