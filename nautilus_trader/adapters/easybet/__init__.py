# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Easybet adapter package initialization.
# -------------------------------------------------------------------------------------------------

from typing import Any


__all__ = [
    "EasybetLiveDataClientFactory",
    "EasybetLiveExecClientFactory",
]


def __getattr__(name: str) -> Any:
    """
    Lazily resolve optional factory imports.

    The Easybet browser stack depends on Playwright. Importing the package should not
    fail at module-import time when that optional dependency is not installed and only
    venue risk policy code is being exercised.

    """
    if name in __all__:
        from nautilus_trader.adapters.easybet.factories import EasybetLiveDataClientFactory
        from nautilus_trader.adapters.easybet.factories import EasybetLiveExecClientFactory

        return {
            "EasybetLiveDataClientFactory": EasybetLiveDataClientFactory,
            "EasybetLiveExecClientFactory": EasybetLiveExecClientFactory,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
