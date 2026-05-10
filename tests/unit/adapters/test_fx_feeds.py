# skipcq: PYL-C0114, PYL-C0116

from decimal import Decimal
import json

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
