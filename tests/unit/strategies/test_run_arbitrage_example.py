# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#
#  Unit tests for the betting arbitrage example entry point.
# -------------------------------------------------------------------------------------------------

import importlib
from types import SimpleNamespace

import pytest


class DummyInstrumentProvider:
    async def load_all_async(self):
        return None

    def list_all(self):
        return []


class DummyClient:
    def __init__(self):
        self._instrument_provider = DummyInstrumentProvider()

    async def connect(self):
        return None

    async def disconnect(self):
        return None


class DummyStrategyConfig:
    def __init__(self, **kwargs):
        self.min_profit_margin = kwargs["min_profit_margin"]
        self.auto_execute = kwargs["auto_execute"]


class DummyStrategy:
    def __init__(self, config):
        self.config = config

    def subscribe_instruments(self, instruments):
        self._instruments = list(instruments)

    def on_start(self):
        return None

    def on_stop(self):
        return None

    def get_stats(self):
        return {
            "subscribed_instruments": len(getattr(self, "_instruments", [])),
            "opportunities_found": 0,
            "opportunities_executed": 0,
            "success_rate": 0.0,
        }


def _patch_example_runtime(module, monkeypatch, requested_env, captured_config):
    def fake_require_env(name: str) -> str:
        requested_env.append(name)
        return f"{name.lower()}-value"

    def fake_sxbet_config(**kwargs):
        captured_config.update(kwargs)
        return SimpleNamespace(**kwargs)

    async def fake_sleep(_seconds: float):
        return None

    monkeypatch.setattr(module, "_load_cloudbet_support", lambda: None)
    monkeypatch.setattr(module, "_require_env", fake_require_env)
    monkeypatch.setattr(module, "SXBetDataClientConfig", fake_sxbet_config)
    monkeypatch.setattr(
        module,
        "SXBetLiveDataClientFactory",
        SimpleNamespace(create=lambda **_kwargs: DummyClient()),
    )
    monkeypatch.setattr(module, "BettingArbitrageConfig", DummyStrategyConfig)
    monkeypatch.setattr(module, "BettingArbitrageStrategy", DummyStrategy)
    monkeypatch.setattr(module, "LiveClock", lambda: object())
    monkeypatch.setattr(module, "Logger", lambda name: object())
    monkeypatch.setattr(module, "TraderId", lambda value: value)
    monkeypatch.setattr(module, "MessageBus", lambda **_kwargs: object())
    monkeypatch.setattr(module, "Cache", lambda **_kwargs: object())
    monkeypatch.setattr(module, "Portfolio", lambda **_kwargs: object())
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)


def test_run_arbitrage_example_imports_without_optional_cloudbet_adapter():
    module = importlib.import_module("nautilus_trader.examples.strategies.run_arbitrage_example")

    assert module.__name__.endswith("run_arbitrage_example")


@pytest.mark.asyncio
async def test_run_arbitrage_example_uses_supported_sxbet_data_config(monkeypatch):
    module = importlib.import_module("nautilus_trader.examples.strategies.run_arbitrage_example")
    requested_env: list[str] = []
    captured_config: dict[str, object] = {}
    _patch_example_runtime(module, monkeypatch, requested_env, captured_config)

    await module.main()

    assert requested_env == ["SXBET_API_KEY"]
    assert captured_config == {
        "api_key": "sxbet_api_key-value",
        "api_url": "https://api.sx.bet",
    }
