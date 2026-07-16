# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Public FX-rate fetchers used by operators to seed live USD-equivalent policy.
"""

from __future__ import annotations

from collections.abc import Callable
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


CRYPTO_USD_BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}
SUPPORTED_FX_REFRESH_PAIRS = frozenset(
    {"EUR/USD", *(f"{symbol}/USD" for symbol in CRYPTO_USD_BINANCE_SYMBOLS)},
)


def fetch_fx_rate(
    pair: str,
    *,
    pyth_price_id: str | None = None,
    timeout_secs: float = 3.0,
) -> FxRateQuote:
    """
    Fetch one supported SRC/USD pair using the configured public-source priority.
    """
    normalized = str(pair).strip().upper()
    if normalized == "EUR/USD":
        return fetch_eur_usd_rate(pyth_price_id=pyth_price_id, timeout_secs=timeout_secs)
    base, _, quote = normalized.partition("/")
    if quote == "USD" and base in CRYPTO_USD_BINANCE_SYMBOLS:
        return fetch_crypto_usd_rate(base, pyth_price_id=pyth_price_id, timeout_secs=timeout_secs)
    raise ValueError(
        f"Unsupported FX pair: {pair!r}. Must be one of {sorted(SUPPORTED_FX_REFRESH_PAIRS)}",
    )


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
    return _fetch_first_available(
        "EUR/USD",
        (
            lambda timeout: _fetch_hyperliquid_mid(
                "EUR/USD",
                ("EUR", "EUR/USDC", "EUR-USDC", "EURUSDC", "EURUSD"),
                timeout,
            ),
            lambda timeout: _fetch_pyth_rate("EUR/USD", pyth_price_id, timeout),
            lambda timeout: _fetch_binance_rate("EUR/USD", "EURUSDT", timeout),
        ),
        timeout_secs,
    )


def fetch_crypto_usd_rate(
    symbol: str,
    *,
    pyth_price_id: str | None = None,
    timeout_secs: float = 3.0,
) -> FxRateQuote:
    """
    Fetch a crypto/USD rate (BTC or ETH) using the same source priority as EUR/USD.

    Binance quotes against USDT; the stablecoin basis versus USD is absorbed by the
    portfolio policy's conservative haircut, mirroring the EUR/USDT fallback.

    """
    normalized = str(symbol).strip().upper()
    binance_symbol = CRYPTO_USD_BINANCE_SYMBOLS.get(normalized)
    if binance_symbol is None:
        msg = (
            f"Unsupported crypto FX symbol: {symbol!r}. "
            f"Must be one of {sorted(CRYPTO_USD_BINANCE_SYMBOLS)}"
        )
        raise ValueError(msg)
    pair = f"{normalized}/USD"
    return _fetch_first_available(
        pair,
        (
            lambda timeout: _fetch_hyperliquid_mid(pair, (normalized,), timeout),
            lambda timeout: _fetch_pyth_rate(pair, pyth_price_id, timeout),
            lambda timeout: _fetch_binance_rate(pair, binance_symbol, timeout),
        ),
        timeout_secs,
    )


def _fetch_first_available(
    pair: str,
    fetchers: tuple[Callable[[float], FxRateQuote | None], ...],
    timeout_secs: float,
) -> FxRateQuote:
    errors: list[str] = []
    for fetcher in fetchers:
        try:
            quote = fetcher(timeout_secs)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(type(exc).__name__)
            continue
        if quote is not None:
            return quote
    raise RuntimeError(f"No {pair} FX source available: {errors}")


def _fetch_hyperliquid_mid(
    pair: str,
    symbols: tuple[str, ...],
    timeout_secs: float,
) -> FxRateQuote | None:
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
    for symbol in symbols:
        raw = mids.get(symbol)
        if raw is not None:
            return FxRateQuote(
                pair,
                Decimal(str(raw)),
                "hyperliquid",
                started_ns,
                time.time_ns(),
            )
    return None


def _fetch_pyth_rate(
    pair: str,
    pyth_price_id: str | None,
    timeout_secs: float,
) -> FxRateQuote | None:
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
    return FxRateQuote(pair, rate, "pyth_hermes", started_ns, time.time_ns())


def _fetch_binance_rate(
    pair: str,
    binance_symbol: str,
    timeout_secs: float,
) -> FxRateQuote | None:
    started_ns = time.time_ns()
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}"
    data = _open_https_json(url, timeout_secs=timeout_secs)
    price = data.get("price")
    if price is None:
        return None
    return FxRateQuote(pair, Decimal(str(price)), "binance", started_ns, time.time_ns())


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
