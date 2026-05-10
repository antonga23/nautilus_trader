#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Read-only venue latency probe for regional placement benchmarking.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from dataclasses import dataclass
import http.client
import json
import math
import socket
import ssl
import statistics
import time
from urllib.parse import urlparse


DEFAULT_URLS = {
    "cloudbet": "https://sports-api.cloudbet.com/pub/v2/odds/events?sport=soccer&limit=1",
    "sxbet": "https://api.sx.bet/sports",
    "polymarket": "https://gamma-api.polymarket.com/events?limit=1&active=true",
    "hyperliquid": "https://api.hyperliquid.xyz/info",
    "pyth": "https://hermes.pyth.network/v2/updates/price/latest?ids[]=",
    "binance": "https://api.binance.com/api/v3/ticker/price?symbol=EURUSDT",
}


@dataclass(frozen=True)
class ProbeSample:
    ok: bool
    dns_ms: float
    tcp_ms: float
    tls_ms: float
    first_byte_ms: float
    total_ms: float
    status: int | None = None
    error: str | None = None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def summarize_samples(samples: list[ProbeSample]) -> dict[str, object]:
    successful = [sample for sample in samples if sample.ok]
    summary: dict[str, object] = {
        "samples": len(samples),
        "successful": len(successful),
        "failed": len(samples) - len(successful),
        "errorRate": (len(samples) - len(successful)) / len(samples) if samples else 0.0,
    }
    for field in ("dns_ms", "tcp_ms", "tls_ms", "first_byte_ms", "total_ms"):
        values = [float(getattr(sample, field)) for sample in successful]
        summary[field] = {
            "median": round(statistics.median(values), 3) if values else 0.0,
            "stddev": round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0,
            "p75": round(_percentile(values, 0.75), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "p99": round(_percentile(values, 0.99), 3),
            "max": round(max(values), 3) if values else 0.0,
        }
    return summary


def probe_url(url: str, *, timeout_secs: float = 5.0) -> ProbeSample:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    started = time.perf_counter()
    try:
        dns_started = time.perf_counter()
        address_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_ms = (time.perf_counter() - dns_started) * 1000
        address = address_info[0][4]
        connect_address = (str(address[0]), int(address[1]))

        tcp_started = time.perf_counter()
        sock = socket.create_connection(connect_address, timeout=timeout_secs)
        tcp_ms = (time.perf_counter() - tcp_started) * 1000

        tls_ms = 0.0
        if parsed.scheme == "https":
            tls_started = time.perf_counter()
            sock = _ssl_context().wrap_socket(sock, server_hostname=host)
            tls_ms = (time.perf_counter() - tls_started) * 1000

        connection_cls = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_cls(host, port, timeout=timeout_secs)
        connection.sock = sock
        request_started = time.perf_counter()
        connection.request(
            "GET",
            path,
            headers={"User-Agent": "cloudbet-market-maker-latency-probe"},
        )
        response = connection.getresponse()
        first_byte_ms = (time.perf_counter() - request_started) * 1000
        response.read(256)
        status = int(response.status)
        connection.close()
        return ProbeSample(
            ok=200 <= status < 500,
            dns_ms=dns_ms,
            tcp_ms=tcp_ms,
            tls_ms=tls_ms,
            first_byte_ms=first_byte_ms,
            total_ms=(time.perf_counter() - started) * 1000,
            status=status,
        )
    except Exception as e:  # pragma: no cover - exact network errors vary by region
        return ProbeSample(
            ok=False,
            dns_ms=0.0,
            tcp_ms=0.0,
            tls_ms=0.0,
            first_byte_ms=0.0,
            total_ms=(time.perf_counter() - started) * 1000,
            error=type(e).__name__,
        )


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is part of the normal dev env
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _recommendation(summary_by_venue: dict[str, dict[str, object]]) -> dict[str, object]:
    total_p95 = {
        venue: _summary_percentile_ms(summary, "total_ms", "p95")
        for venue, summary in summary_by_venue.items()
    }
    worst_leg = max(total_p95.values()) if total_p95 else 0.0
    return {
        "worstLegTotalP95Ms": worst_leg,
        "cloudbetSingleVenue": total_p95.get("cloudbet"),
        "sxbetSingleVenue": total_p95.get("sxbet"),
        "polymarketSxbet": max(total_p95.get("polymarket", 0.0), total_p95.get("sxbet", 0.0)),
        "cloudbetSxbet": max(total_p95.get("cloudbet", 0.0), total_p95.get("sxbet", 0.0)),
    }


def _summary_percentile_ms(
    summary: dict[str, object],
    metric: str,
    percentile: str,
) -> float:
    metric_summary = summary.get(metric)
    if not isinstance(metric_summary, dict):
        return 0.0
    raw = metric_summary.get(percentile)
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", action="append", choices=sorted(DEFAULT_URLS), default=[])
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout-secs", type=float, default=5.0)
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    targets = {venue: DEFAULT_URLS[venue] for venue in (args.venue or DEFAULT_URLS)}
    for custom in args.url:
        name, _, url = custom.partition("=")
        if name and url:
            targets[name.strip().lower()] = url.strip()

    summaries: dict[str, dict[str, object]] = {}
    raw_samples: dict[str, list[dict[str, object]]] = {}
    for venue, url in targets.items():
        samples = [probe_url(url, timeout_secs=args.timeout_secs) for _ in range(args.samples)]
        summaries[venue] = summarize_samples(samples)
        raw_samples[venue] = [asdict(sample) for sample in samples]

    payload = {
        "generatedAtNs": time.time_ns(),
        "targets": targets,
        "summaries": summaries,
        "recommendation": _recommendation(summaries),
        "samples": raw_samples,
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as output:
            output.write(text)
            output.write("\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
