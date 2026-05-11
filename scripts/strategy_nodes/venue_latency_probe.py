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

DEFAULT_STRATEGY_VENUES = {
    "cloudbet_single_venue": ("cloudbet",),
    "sxbet_single_venue": ("sxbet",),
    "polymarket_sxbet": ("polymarket", "sxbet"),
    "cloudbet_sxbet": ("cloudbet", "sxbet"),
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

        # The socket above already captures TCP and optional TLS timing. Use a
        # plain HTTPConnection over that prepared socket to avoid a second TLS
        # wrap inside HTTPSConnection.
        connection = http.client.HTTPConnection(host, port, timeout=timeout_secs)
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
            error=_error_summary(e),
        )


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is part of the normal dev env
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _error_summary(error: Exception) -> str:
    detail = str(error).replace("\n", " ").strip()
    if len(detail) > 120:
        detail = f"{detail[:117]}..."
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


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


def placement_recommendations(
    summary_by_venue: dict[str, dict[str, object]],
    *,
    region: str,
    generated_at_ns: int,
    now_ns: int | None = None,
    max_data_age_secs: float = 3600.0,
    strategy_venues: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, dict[str, object]]:
    now = time.time_ns() if now_ns is None else now_ns
    data_age_secs = max((now - generated_at_ns) / 1_000_000_000, 0.0)
    data_fresh = data_age_secs <= max_data_age_secs
    return {
        strategy: _strategy_placement_recommendation(
            summary_by_venue,
            region=region,
            venues=venues,
            data_age_secs=data_age_secs,
            data_fresh=data_fresh,
        )
        for strategy, venues in (strategy_venues or DEFAULT_STRATEGY_VENUES).items()
    }


def compare_region_reports(reports: list[dict[str, object]]) -> dict[str, object]:
    regions: dict[str, dict[str, object]] = {}
    candidates_by_strategy: dict[str, list[dict[str, object]]] = {}
    blockers_by_region: dict[str, dict[str, list[str]]] = {}

    for report in reports:
        region = str(report.get("region") or "unknown")
        recommendations = report.get("placementRecommendations")
        if not isinstance(recommendations, dict):
            continue
        regions[region] = {
            "generatedAtNs": report.get("generatedAtNs"),
            "targets": report.get("targets") if isinstance(report.get("targets"), dict) else {},
        }
        for strategy, raw_recommendation in recommendations.items():
            if not isinstance(raw_recommendation, dict):
                continue
            recommendation = dict(raw_recommendation)
            recommendation["region"] = region
            blockers = [
                str(blocker)
                for blocker in recommendation.get("blockers", [])
                if isinstance(blocker, str)
            ]
            if blockers:
                blockers_by_region.setdefault(region, {})[str(strategy)] = blockers
            candidates_by_strategy.setdefault(str(strategy), []).append(recommendation)

    best_by_strategy = {
        strategy: _best_strategy_region(candidates)
        for strategy, candidates in sorted(candidates_by_strategy.items())
    }
    return {
        "regions": regions,
        "bestRegionByStrategy": best_by_strategy,
        "blockersByRegion": blockers_by_region,
    }


def _best_strategy_region(candidates: list[dict[str, object]]) -> dict[str, object]:
    eligible = [
        candidate
        for candidate in candidates
        if bool(candidate.get("eligibleForPlacementComparison"))
    ]
    if not eligible:
        return {
            "region": None,
            "eligibleForPlacementComparison": False,
            "blockers": _aggregate_candidate_blockers(candidates),
        }
    best = min(
        eligible,
        key=lambda item: (
            _payload_float(item, "placementScoreMs"),
            _payload_float(item, "worstLegTotalP95Ms"),
            _payload_float(item, "venueTotalP95SkewMs"),
            _payload_float(item, "worstLegErrorRate"),
        ),
    )
    return {
        "region": best.get("region"),
        "eligibleForPlacementComparison": True,
        "worstLegTotalP95Ms": _payload_float(best, "worstLegTotalP95Ms"),
        "venueTotalP95SkewMs": _payload_float(best, "venueTotalP95SkewMs"),
        "worstLegFirstByteP95Ms": _payload_float(best, "worstLegFirstByteP95Ms"),
        "venueFirstByteP95SkewMs": _payload_float(best, "venueFirstByteP95SkewMs"),
        "worstLegTotalStddevMs": _payload_float(best, "worstLegTotalStddevMs"),
        "worstLegFirstByteStddevMs": _payload_float(best, "worstLegFirstByteStddevMs"),
        "worstLegErrorRate": _payload_float(best, "worstLegErrorRate"),
        "placementScoreMs": _payload_float(best, "placementScoreMs"),
        "dominantLatencyVenue": best.get("dominantLatencyVenue"),
        "venues": best.get("venues") if isinstance(best.get("venues"), list) else [],
    }


def _aggregate_candidate_blockers(candidates: list[dict[str, object]]) -> dict[str, list[str]]:
    return {
        str(candidate.get("region") or "unknown"): _blocker_list(candidate)
        for candidate in candidates
    }


def _blocker_list(payload: dict[str, object]) -> list[str]:
    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        return []
    return [str(blocker) for blocker in blockers if isinstance(blocker, str)]


def _strategy_placement_recommendation(
    summary_by_venue: dict[str, dict[str, object]],
    *,
    region: str,
    venues: tuple[str, ...],
    data_age_secs: float,
    data_fresh: bool,
) -> dict[str, object]:
    venue_payloads = _venue_payloads(summary_by_venue, venues)
    missing_venues = [venue for venue in venues if venue not in venue_payloads]
    total_p95 = _venue_percentiles_ms(venue_payloads, "total_ms")
    first_byte_p95 = _venue_percentiles_ms(venue_payloads, "first_byte_ms")
    total_stddev = _venue_metric_values(venue_payloads, "total_ms", "stddev")
    first_byte_stddev = _venue_metric_values(venue_payloads, "first_byte_ms", "stddev")
    error_rates = _venue_error_rates(venue_payloads)
    worst_error_rate = max(error_rates.values()) if error_rates else 1.0
    blockers = _placement_blockers(
        missing_venues=missing_venues,
        data_fresh=data_fresh,
        worst_error_rate=worst_error_rate,
    )
    worst_leg_total_p95 = max(total_p95.values()) if total_p95 else 0.0
    total_skew = _metric_skew_ms(total_p95)
    worst_total_stddev = max(total_stddev.values()) if total_stddev else 0.0
    placement_score_ms = _placement_score_ms(
        worst_leg_total_p95=worst_leg_total_p95,
        venue_total_p95_skew_ms=total_skew,
        worst_leg_total_stddev_ms=worst_total_stddev,
        worst_error_rate=worst_error_rate,
    )
    return {
        "region": region,
        "venues": list(venues),
        "missingVenues": missing_venues,
        "dataAgeSecs": round(data_age_secs, 3),
        "dataFresh": data_fresh,
        "worstLegTotalP95Ms": worst_leg_total_p95,
        "worstLegFirstByteP95Ms": max(first_byte_p95.values()) if first_byte_p95 else 0.0,
        "worstLegTotalStddevMs": worst_total_stddev,
        "worstLegFirstByteStddevMs": max(first_byte_stddev.values()) if first_byte_stddev else 0.0,
        "venueTotalP95SkewMs": total_skew,
        "venueFirstByteP95SkewMs": _metric_skew_ms(first_byte_p95),
        "worstLegErrorRate": worst_error_rate,
        "placementScoreMs": placement_score_ms,
        "dominantLatencyVenue": _dominant_latency_venue(total_p95),
        "venueTotalP95Ms": total_p95,
        "venueFirstByteP95Ms": first_byte_p95,
        "venueTotalStddevMs": total_stddev,
        "venueFirstByteStddevMs": first_byte_stddev,
        "venueErrorRates": error_rates,
        "blockers": blockers,
        "eligibleForPlacementComparison": not blockers,
    }


def _venue_payloads(
    summary_by_venue: dict[str, dict[str, object]],
    venues: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    return {
        venue: payload for venue in venues if (payload := summary_by_venue.get(venue)) is not None
    }


def _venue_percentiles_ms(
    venue_payloads: dict[str, dict[str, object]],
    metric: str,
) -> dict[str, float]:
    return _venue_metric_values(venue_payloads, metric, "p95")


def _venue_metric_values(
    venue_payloads: dict[str, dict[str, object]],
    metric: str,
    statistic: str,
) -> dict[str, float]:
    return {
        venue: _summary_percentile_ms(summary, metric, statistic)
        for venue, summary in venue_payloads.items()
    }


def _venue_error_rates(venue_payloads: dict[str, dict[str, object]]) -> dict[str, float]:
    return {
        venue: _summary_float(summary, "errorRate") for venue, summary in venue_payloads.items()
    }


def _metric_skew_ms(values_by_venue: dict[str, float]) -> float:
    values = list(values_by_venue.values())
    if len(values) < 2:
        return 0.0
    return round(max(values) - min(values), 3)


def _dominant_latency_venue(values_by_venue: dict[str, float]) -> str | None:
    if not values_by_venue:
        return None
    return max(sorted(values_by_venue), key=lambda venue: values_by_venue[venue])


def _placement_score_ms(
    *,
    worst_leg_total_p95: float,
    venue_total_p95_skew_ms: float,
    worst_leg_total_stddev_ms: float,
    worst_error_rate: float,
) -> float:
    error_penalty_ms = max(worst_error_rate, 0.0) * 1000.0
    score = (
        worst_leg_total_p95
        + 0.25 * venue_total_p95_skew_ms
        + 0.5 * worst_leg_total_stddev_ms
        + error_penalty_ms
    )
    return round(score, 3)


def _placement_blockers(
    *,
    missing_venues: list[str],
    data_fresh: bool,
    worst_error_rate: float,
) -> list[str]:
    blockers: list[str] = []
    if missing_venues:
        blockers.append("missing_venue_samples")
    if not data_fresh:
        blockers.append("stale_probe_data")
    if worst_error_rate > 0.05:
        blockers.append("high_error_rate")
    return blockers


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


def _summary_float(summary: dict[str, object], field: str) -> float:
    value = summary.get(field)
    if not isinstance(value, int | float | str):
        return 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _payload_float(payload: dict[str, object], field: str) -> float:
    value = payload.get(field)
    if not isinstance(value, int | float | str):
        return 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", action="append", choices=sorted(DEFAULT_URLS), default=[])
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout-secs", type=float, default=5.0)
    parser.add_argument("--region", default="local")
    parser.add_argument("--max-data-age-secs", type=float, default=3600.0)
    parser.add_argument("--compare-json", action="append", default=[])
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    if args.compare_json:
        reports = []
        for path in args.compare_json:
            with open(path, encoding="utf-8") as report_file:
                loaded = json.load(report_file)
            if isinstance(loaded, dict):
                reports.append(loaded)
        payload = compare_region_reports(reports)
        text = json.dumps(payload, indent=2, sort_keys=True)
        if args.output_json:
            with open(args.output_json, "w", encoding="utf-8") as output:
                output.write(text)
                output.write("\n")
        print(text)
        return 0

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

    generated_at_ns = time.time_ns()
    payload = {
        "generatedAtNs": generated_at_ns,
        "region": args.region,
        "targets": targets,
        "summaries": summaries,
        "recommendation": _recommendation(summaries),
        "placementRecommendations": placement_recommendations(
            summaries,
            region=args.region,
            generated_at_ns=generated_at_ns,
            max_data_age_secs=args.max_data_age_secs,
        ),
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
