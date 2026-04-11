# -------------------------------------------------------------------------------------------------
#  Custom unit tests conftest.py
#  Minimal conftest that does NOT import Cython-compiled modules.
# -------------------------------------------------------------------------------------------------

import asyncio
import sys

import pytest


@pytest.fixture(scope="session")
def event_loop_policy():
    """
    Provide uvloop event loop policy for pytest-asyncio.
    """
    if sys.platform == "win32":
        return asyncio.DefaultEventLoopPolicy()

    try:
        import uvloop

        return uvloop.EventLoopPolicy()
    except ImportError:
        return asyncio.DefaultEventLoopPolicy()
