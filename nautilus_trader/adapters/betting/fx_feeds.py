# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Public FX-rate fetchers used by operators to seed live USD-equivalent policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import ssl
import time
from typing import Any
from urllib import request
from urllib.parse import urlsplit


@dataclass(frozen=True)
class FxRateQuote:
    pair: str
    rate: Decimal
    source: str
    ts_event_ns: int
    ts_init_ns: int

    @property
    def age_secs(self) -> float:
        return max(0.0, (self.ts_init_ns - self.ts_event_ns) / 1_000_000_000)


def fetch_eur_usd_rate(
    *,
    pyth_price_id: str | None = None,
    timeout_secs: float = 3.0,
) -> FxRateQuote:
    """
    Fetch EUR/USD using the configured public-source priority.

    Hyperliquid and Binance do not require credentials. Pyth Hermes requires a feed ID,
    so it is attempted only when supplied by configuration.

    """
    errors: list[str] = []
    for fetcher in (
        _fetch_hyperliquid_eur_usd,
        (lambda timeout: _fetch_pyth_eur_usd(pyth_price_id, timeout)),
        _fetch_binance_eur_usdt,
    ):
        try:
            quote = fetcher(timeout_secs)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(type(exc).__name__)
            continue
        if quote is not None:
            return quote
    raise RuntimeError(f"No EUR/USD FX source available: {errors}")


def _fetch_hyperliquid_eur_usd(timeout_secs: float) -> FxRateQuote | None:
    started_ns = time.time_ns()
    payload = json.dumps({"type": "allMids"}).encode()
    req = request.Request(
        "https://api.hyperliquid.xyz/info",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    data = _open_https_json(req, timeout_secs=timeout_secs)
    mids = data if isinstance(data, dict) else {}
    for symbol in ("EUR", "EUR/USDC", "EUR-USDC", "EURUSDC", "EURUSD"):
        raw = mids.get(symbol)
        if raw is not None:
            return FxRateQuote(
                "EUR/USD",
                Decimal(str(raw)),
                "hyperliquid",
                started_ns,
                time.time_ns(),
            )
    return None


def _fetch_pyth_eur_usd(pyth_price_id: str | None, timeout_secs: float) -> FxRateQuote | None:
    if not pyth_price_id:
        return None
    started_ns = time.time_ns()
    url = f"https://hermes.pyth.network/v2/updates/price/latest?ids[]={pyth_price_id}"
    data = _open_https_json(url, timeout_secs=timeout_secs)
    parsed = data.get("parsed") or []
    if not parsed:
        return None
    price = (parsed[0].get("price") or {}).get("price")
    expo = int((parsed[0].get("price") or {}).get("expo") or 0)
    if price is None:
        return None
    rate = Decimal(str(price)) * (Decimal(10) ** Decimal(expo))
    return FxRateQuote("EUR/USD", rate, "pyth_hermes", started_ns, time.time_ns())


def _fetch_binance_eur_usdt(timeout_secs: float) -> FxRateQuote | None:
    started_ns = time.time_ns()
    url = "https://api.binance.com/api/v3/ticker/price?symbol=EURUSDT"
    data = _open_https_json(url, timeout_secs=timeout_secs)
    price = data.get("price")
    if price is None:
        return None
    return FxRateQuote("EUR/USD", Decimal(str(price)), "binance", started_ns, time.time_ns())


def _open_https_json(target: str | request.Request, *, timeout_secs: float) -> Any:
    url = target.full_url if isinstance(target, request.Request) else target
    if urlsplit(url).scheme != "https":
        raise ValueError(f"FX feed URL must use HTTPS: {url!r}")
    with request.urlopen(target, timeout=timeout_secs, context=_ssl_context()) as response:  # noqa: S310  # skipcq: BAN-B310
        return json.loads(response.read().decode())


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is part of the normal dev env
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
