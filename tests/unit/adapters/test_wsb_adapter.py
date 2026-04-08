# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Basic tests for WSB adapter components.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

from nautilus_trader.adapters.wsb.constants import WSB_BASE_URL
from nautilus_trader.adapters.wsb.constants import WSB_VENUE
from nautilus_trader.adapters.wsb.risk_engine import WSBRiskEngine


def test_wsb_constants():
    assert WSB_VENUE.value == "WSB"
    assert WSB_BASE_URL.startswith("https://")


def test_wsb_risk_engine_initialization():
    engine = WSBRiskEngine()

    assert engine.venue_name == "WSB"
    assert engine._max_stake_zar == Decimal(1000)
    assert engine._rollover_multiplier == Decimal(5)
    assert engine._min_rollover_odds == Decimal("1.60")
