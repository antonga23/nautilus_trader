# skipcq: PYL-C0114, PYL-C0115, PYL-C0116

from nautilus_trader.adapters.polymarket.config import PolymarketDataClientConfig


def test_max_ws_clients_defaults_to_unbounded():
    config = PolymarketDataClientConfig()

    assert config.max_ws_clients is None


def test_max_ws_clients_accepts_positive_int():
    config = PolymarketDataClientConfig(max_ws_clients=4)

    assert config.max_ws_clients == 4
