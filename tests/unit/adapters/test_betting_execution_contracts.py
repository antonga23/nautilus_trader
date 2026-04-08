import inspect

import pytest

from nautilus_trader.adapters.blackbet.execution import BlackBetExecutionClient
from nautilus_trader.adapters.tenbet.execution import TenBetExecutionClient
from nautilus_trader.adapters.wsb.execution import WSBExecutionClient
from nautilus_trader.live.execution_client import LiveExecutionClient


@pytest.mark.parametrize(
    "client_cls",
    [
        BlackBetExecutionClient,
        TenBetExecutionClient,
        WSBExecutionClient,
    ],
)
def test_generate_order_status_report_matches_live_execution_client_contract(client_cls):
    assert inspect.signature(client_cls.generate_order_status_report) == inspect.signature(
        LiveExecutionClient.generate_order_status_report,
    )
