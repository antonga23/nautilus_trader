# skipcq: PYL-C0114, PYL-C0116

from decimal import Decimal
import json

import pytest

from nautilus_trader.adapters.betting import fx_feeds


class _Response:
    def __init__(self, payload: object):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_fetch_eur_usd_rate_prefers_hyperliquid_when_available(monkeypatch):
    def fake_urlopen(_req, timeout=None, **_kwargs):
        return _Response({"EURUSDC": "1.083"})

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    quote = fx_feeds.fetch_eur_usd_rate(timeout_secs=0.1)

    assert quote.source == "hyperliquid"
    assert quote.rate == Decimal("1.083")


def test_fetch_eur_usd_rate_falls_back_to_binance(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(req, timeout=None, **_kwargs):
        url = getattr(req, "full_url", req)
        calls.append(str(url))
        if "hyperliquid" in str(url):
            return _Response({})
        return _Response({"price": "1.0815"})

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    quote = fx_feeds.fetch_eur_usd_rate(timeout_secs=0.1)

    assert quote.source == "binance"
    assert quote.rate == Decimal("1.0815")
    assert len(calls) == 2


def test_fetch_eur_usd_rate_uses_pyth_when_hyperliquid_has_no_pair(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(req, timeout=None, **_kwargs):
        url = getattr(req, "full_url", req)
        calls.append(str(url))
        if "hyperliquid" in str(url):
            return _Response({})
        return _Response(
            {
                "parsed": [
                    {
                        "price": {
                            "price": "108375000",
                            "expo": -8,
                        },
                    },
                ],
            },
        )

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    quote = fx_feeds.fetch_eur_usd_rate(pyth_price_id="0xeurusd", timeout_secs=0.1)

    assert quote.source == "pyth_hermes"
    assert quote.rate == Decimal("1.08375000")
    assert len(calls) == 2


def test_fetch_crypto_usd_rate_prefers_hyperliquid_when_available(monkeypatch):
    def fake_urlopen(_req, timeout=None, **_kwargs):
        return _Response({"BTC": "97012.5", "ETH": "3412.1"})

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    quote = fx_feeds.fetch_crypto_usd_rate("BTC", timeout_secs=0.1)

    assert quote.pair == "BTC/USD"
    assert quote.source == "hyperliquid"
    assert quote.rate == Decimal("97012.5")


def test_fetch_crypto_usd_rate_falls_back_to_binance_usdt_symbol(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(req, timeout=None, **_kwargs):
        url = str(getattr(req, "full_url", req))
        calls.append(url)
        if "hyperliquid" in url:
            return _Response({})
        return _Response({"price": "3412.10"})

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    quote = fx_feeds.fetch_crypto_usd_rate("ETH", timeout_secs=0.1)

    assert quote.pair == "ETH/USD"
    assert quote.source == "binance"
    assert quote.rate == Decimal("3412.10")
    assert len(calls) == 2
    assert "symbol=ETHUSDT" in calls[1]


def test_fetch_crypto_usd_rate_uses_pyth_when_hyperliquid_has_no_pair(monkeypatch):
    def fake_urlopen(req, timeout=None, **_kwargs):
        url = str(getattr(req, "full_url", req))
        if "hyperliquid" in url:
            return _Response({})
        return _Response(
            {
                "parsed": [
                    {
                        "price": {
                            "price": "9701250000000",
                            "expo": -8,
                        },
                    },
                ],
            },
        )

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    quote = fx_feeds.fetch_crypto_usd_rate("BTC", pyth_price_id="0xbtcusd", timeout_secs=0.1)

    assert quote.source == "pyth_hermes"
    assert quote.rate == Decimal("97012.5")


def test_fetch_crypto_usd_rate_raises_when_all_sources_fail(monkeypatch):
    def fake_urlopen(_req, timeout=None, **_kwargs):
        raise OSError("down")

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="BTC/USD"):
        fx_feeds.fetch_crypto_usd_rate("BTC", timeout_secs=0.1)


def test_fetch_crypto_usd_rate_rejects_unsupported_symbol():
    with pytest.raises(ValueError, match="DOGE"):
        fx_feeds.fetch_crypto_usd_rate("DOGE")


def test_fetch_fx_rate_dispatches_supported_pairs(monkeypatch):
    def fake_urlopen(_req, timeout=None, **_kwargs):
        return _Response({"EURUSDC": "1.083", "BTC": "97012.5"})

    monkeypatch.setattr(fx_feeds.request, "urlopen", fake_urlopen)

    eur = fx_feeds.fetch_fx_rate("EUR/USD", timeout_secs=0.1)
    btc = fx_feeds.fetch_fx_rate("btc/usd", timeout_secs=0.1)

    assert eur.pair == "EUR/USD"
    assert eur.rate == Decimal("1.083")
    assert btc.pair == "BTC/USD"
    assert btc.rate == Decimal("97012.5")


def test_fetch_fx_rate_rejects_unsupported_pair():
    with pytest.raises(ValueError, match="EUR/GBP"):
        fx_feeds.fetch_fx_rate("EUR/GBP")
