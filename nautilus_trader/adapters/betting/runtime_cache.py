import json
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass


ACTIVE_VENUE_INSTRUMENT_INDEX_PREFIX = "betting:active_venue_instruments"
VENUE_QUOTE_POLL_STATS_PREFIX = "betting:venue_quote_poll_stats"
VENUE_QUOTE_TIERS_PREFIX = "betting:venue_quote_tiers"

QUOTE_TIER_INTERVALS_DEFAULT = {"hot": 1, "warm": 5, "cold": 30}
QUOTE_TIER_RANK = {"hot": 0, "warm": 1, "cold": 2}


@dataclass(frozen=True)
class ActiveVenueInstrumentIndex:
    venue: str
    instrument_ids: tuple[str, ...]
    updated_at_ns: int


@dataclass(frozen=True)
class VenueQuotePollStats:
    venue: str
    updated_at_ns: int
    cycle_id: int
    source: str
    subscribed_instrument_count: int
    market_count: int
    quote_count: int
    request_count: int
    event_request_count: int
    line_request_count: int
    pruned_subscription_count: int
    refilled_subscription_count: int
    order_count: int
    empty_market_count: int
    one_sided_market_count: int
    two_sided_market_count: int
    concurrency: int
    backlog_count: int
    cycle_elapsed_secs: float
    max_fetch_latency_secs: float
    poll_interval_secs: float
    fetch_latency_p50_secs: float = 0.0
    fetch_latency_p95_secs: float = 0.0
    fetch_latency_p99_secs: float = 0.0
    poll_target_cycle_secs: float = 0.0
    next_poll_sleep_secs: float = 0.0
    min_concurrency: int = 1
    max_concurrency: int = 1
    adaptive_concurrency: bool = False
    quote_event_timestamp_source: str = ""
    quote_init_timestamp_source: str = ""
    failure_count: int = 0
    rate_limit_count: int = 0
    delisted_count: int = 0
    backoff_secs: float = 0.0
    last_error: str | None = None
    stream_connected: bool = False
    stream_connected_since_ns: int = 0
    stream_reconnect_count: int = 0
    stream_fallback_activation_count: int = 0
    stream_publication_count: int = 0
    stream_subscribed_channel_count: int = 0
    stream_subscribe_error_count: int = 0
    stream_seed_failure_count: int = 0
    stream_last_disconnect_reason: str | None = None
    tombstoned_market_count: int = 0
    tombstone_skipped_count: int = 0
    revalidation_probe_count: int = 0
    hot_instrument_count: int = 0
    warm_instrument_count: int = 0
    cold_instrument_count: int = 0
    tier_due_count: int = 0


@dataclass(frozen=True)
class VenueQuoteTiers:
    venue: str
    updated_at_ns: int
    tier_by_instrument_id: dict[str, str]
    tier_intervals: dict[str, int]


def active_venue_instrument_index_key(venue: str) -> str:
    return f"{ACTIVE_VENUE_INSTRUMENT_INDEX_PREFIX}:{venue.strip().upper()}"


def venue_quote_poll_stats_key(venue: str) -> str:
    return f"{VENUE_QUOTE_POLL_STATS_PREFIX}:{venue.strip().upper()}"


def venue_quote_tiers_key(venue: str) -> str:
    return f"{VENUE_QUOTE_TIERS_PREFIX}:{venue.strip().upper()}"


def latency_percentiles(values: Iterable[float]) -> tuple[float, float, float]:
    ordered = sorted(max(0.0, float(value)) for value in values)
    if not ordered:
        return (0.0, 0.0, 0.0)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
        return ordered[index]

    return (percentile(0.50), percentile(0.95), percentile(0.99))


def encode_active_venue_instrument_index(
    *,
    venue: str,
    instrument_ids: Iterable[str],
    updated_at_ns: int,
) -> bytes:
    payload = {
        "venue": venue.strip().upper(),
        "instrument_ids": sorted(
            {str(instrument_id) for instrument_id in instrument_ids if instrument_id},
        ),
        "updated_at_ns": int(updated_at_ns),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_venue_quote_poll_stats(
    *,
    venue: str,
    updated_at_ns: int,
    cycle_id: int,
    source: str,
    subscribed_instrument_count: int,
    market_count: int,
    quote_count: int,
    request_count: int = 0,
    event_request_count: int = 0,
    line_request_count: int = 0,
    pruned_subscription_count: int = 0,
    refilled_subscription_count: int = 0,
    order_count: int = 0,
    empty_market_count: int = 0,
    one_sided_market_count: int = 0,
    two_sided_market_count: int = 0,
    concurrency: int = 1,
    backlog_count: int = 0,
    cycle_elapsed_secs: float = 0.0,
    max_fetch_latency_secs: float = 0.0,
    poll_interval_secs: float = 0.0,
    fetch_latency_p50_secs: float = 0.0,
    fetch_latency_p95_secs: float = 0.0,
    fetch_latency_p99_secs: float = 0.0,
    poll_target_cycle_secs: float = 0.0,
    next_poll_sleep_secs: float = 0.0,
    min_concurrency: int = 1,
    max_concurrency: int = 1,
    adaptive_concurrency: bool = False,
    quote_event_timestamp_source: str = "",
    quote_init_timestamp_source: str = "",
    failure_count: int = 0,
    rate_limit_count: int = 0,
    delisted_count: int = 0,
    backoff_secs: float = 0.0,
    last_error: str | None = None,
    stream_connected: bool = False,
    stream_connected_since_ns: int = 0,
    stream_reconnect_count: int = 0,
    stream_fallback_activation_count: int = 0,
    stream_publication_count: int = 0,
    stream_subscribed_channel_count: int = 0,
    stream_subscribe_error_count: int = 0,
    stream_seed_failure_count: int = 0,
    stream_last_disconnect_reason: str | None = None,
    tombstoned_market_count: int = 0,
    tombstone_skipped_count: int = 0,
    revalidation_probe_count: int = 0,
    hot_instrument_count: int = 0,
    warm_instrument_count: int = 0,
    cold_instrument_count: int = 0,
    tier_due_count: int = 0,
) -> bytes:
    payload = {
        "venue": venue.strip().upper(),
        "updated_at_ns": int(updated_at_ns),
        "cycle_id": int(cycle_id),
        "source": str(source),
        "subscribed_instrument_count": max(0, int(subscribed_instrument_count)),
        "market_count": max(0, int(market_count)),
        "quote_count": max(0, int(quote_count)),
        "request_count": max(0, int(request_count)),
        "event_request_count": max(0, int(event_request_count)),
        "line_request_count": max(0, int(line_request_count)),
        "pruned_subscription_count": max(0, int(pruned_subscription_count)),
        "refilled_subscription_count": max(0, int(refilled_subscription_count)),
        "order_count": max(0, int(order_count)),
        "empty_market_count": max(0, int(empty_market_count)),
        "one_sided_market_count": max(0, int(one_sided_market_count)),
        "two_sided_market_count": max(0, int(two_sided_market_count)),
        "concurrency": max(1, int(concurrency)),
        "backlog_count": max(0, int(backlog_count)),
        "cycle_elapsed_secs": max(0.0, float(cycle_elapsed_secs)),
        "max_fetch_latency_secs": max(0.0, float(max_fetch_latency_secs)),
        "poll_interval_secs": max(0.0, float(poll_interval_secs)),
        "fetch_latency_p50_secs": max(0.0, float(fetch_latency_p50_secs)),
        "fetch_latency_p95_secs": max(0.0, float(fetch_latency_p95_secs)),
        "fetch_latency_p99_secs": max(0.0, float(fetch_latency_p99_secs)),
        "poll_target_cycle_secs": max(0.0, float(poll_target_cycle_secs)),
        "next_poll_sleep_secs": max(0.0, float(next_poll_sleep_secs)),
        "min_concurrency": max(1, int(min_concurrency)),
        "max_concurrency": max(1, int(max_concurrency)),
        "adaptive_concurrency": bool(adaptive_concurrency),
        "quote_event_timestamp_source": str(quote_event_timestamp_source or ""),
        "quote_init_timestamp_source": str(quote_init_timestamp_source or ""),
        "failure_count": max(0, int(failure_count)),
        "rate_limit_count": max(0, int(rate_limit_count)),
        "delisted_count": max(0, int(delisted_count)),
        "backoff_secs": max(0.0, float(backoff_secs)),
        "last_error": str(last_error)[:240] if last_error else None,
        "stream_connected": bool(stream_connected),
        "stream_connected_since_ns": max(0, int(stream_connected_since_ns)),
        "stream_reconnect_count": max(0, int(stream_reconnect_count)),
        "stream_fallback_activation_count": max(0, int(stream_fallback_activation_count)),
        "stream_publication_count": max(0, int(stream_publication_count)),
        "stream_subscribed_channel_count": max(0, int(stream_subscribed_channel_count)),
        "stream_subscribe_error_count": max(0, int(stream_subscribe_error_count)),
        "stream_seed_failure_count": max(0, int(stream_seed_failure_count)),
        "stream_last_disconnect_reason": (
            str(stream_last_disconnect_reason)[:240] if stream_last_disconnect_reason else None
        ),
        "tombstoned_market_count": max(0, int(tombstoned_market_count)),
        "tombstone_skipped_count": max(0, int(tombstone_skipped_count)),
        "revalidation_probe_count": max(0, int(revalidation_probe_count)),
        "hot_instrument_count": max(0, int(hot_instrument_count)),
        "warm_instrument_count": max(0, int(warm_instrument_count)),
        "cold_instrument_count": max(0, int(cold_instrument_count)),
        "tier_due_count": max(0, int(tier_due_count)),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_venue_quote_tiers(
    *,
    venue: str,
    updated_at_ns: int,
    tier_by_instrument_id: Mapping[str, str],
    tier_intervals: Mapping[str, int],
) -> bytes:
    payload = {
        "venue": venue.strip().upper(),
        "updated_at_ns": int(updated_at_ns),
        "tier_by_instrument_id": {
            str(instrument_id): str(tier)
            for instrument_id, tier in tier_by_instrument_id.items()
            if instrument_id
        },
        "tier_intervals": {str(tier): int(interval) for tier, interval in tier_intervals.items()},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_active_venue_instrument_index(raw: bytes | None) -> ActiveVenueInstrumentIndex | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    venue = str(payload.get("venue") or "").strip().upper()
    instrument_ids_raw = payload.get("instrument_ids")
    updated_at_ns = int(payload.get("updated_at_ns") or 0)
    if not venue or not isinstance(instrument_ids_raw, list):
        return None
    instrument_ids = tuple(
        str(instrument_id) for instrument_id in instrument_ids_raw if instrument_id
    )
    return ActiveVenueInstrumentIndex(
        venue=venue,
        instrument_ids=instrument_ids,
        updated_at_ns=updated_at_ns,
    )


def decode_venue_quote_poll_stats(raw: bytes | None) -> VenueQuotePollStats | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    venue = str(payload.get("venue") or "").strip().upper()
    if not venue:
        return None
    try:
        return VenueQuotePollStats(
            venue=venue,
            updated_at_ns=int(payload.get("updated_at_ns") or 0),
            cycle_id=int(payload.get("cycle_id") or 0),
            source=str(payload.get("source") or ""),
            subscribed_instrument_count=int(payload.get("subscribed_instrument_count") or 0),
            market_count=int(payload.get("market_count") or 0),
            quote_count=int(payload.get("quote_count") or 0),
            request_count=int(payload.get("request_count") or 0),
            event_request_count=int(payload.get("event_request_count") or 0),
            line_request_count=int(payload.get("line_request_count") or 0),
            pruned_subscription_count=int(payload.get("pruned_subscription_count") or 0),
            refilled_subscription_count=int(payload.get("refilled_subscription_count") or 0),
            order_count=int(payload.get("order_count") or 0),
            empty_market_count=int(payload.get("empty_market_count") or 0),
            one_sided_market_count=int(payload.get("one_sided_market_count") or 0),
            two_sided_market_count=int(payload.get("two_sided_market_count") or 0),
            concurrency=max(1, int(payload.get("concurrency") or 1)),
            backlog_count=int(payload.get("backlog_count") or 0),
            cycle_elapsed_secs=float(payload.get("cycle_elapsed_secs") or 0.0),
            max_fetch_latency_secs=float(payload.get("max_fetch_latency_secs") or 0.0),
            poll_interval_secs=float(payload.get("poll_interval_secs") or 0.0),
            fetch_latency_p50_secs=float(payload.get("fetch_latency_p50_secs") or 0.0),
            fetch_latency_p95_secs=float(payload.get("fetch_latency_p95_secs") or 0.0),
            fetch_latency_p99_secs=float(payload.get("fetch_latency_p99_secs") or 0.0),
            poll_target_cycle_secs=float(payload.get("poll_target_cycle_secs") or 0.0),
            next_poll_sleep_secs=float(payload.get("next_poll_sleep_secs") or 0.0),
            min_concurrency=max(1, int(payload.get("min_concurrency") or 1)),
            max_concurrency=max(1, int(payload.get("max_concurrency") or 1)),
            adaptive_concurrency=bool(payload.get("adaptive_concurrency")),
            quote_event_timestamp_source=str(
                payload.get("quote_event_timestamp_source") or "",
            ),
            quote_init_timestamp_source=str(payload.get("quote_init_timestamp_source") or ""),
            failure_count=int(payload.get("failure_count") or 0),
            rate_limit_count=int(payload.get("rate_limit_count") or 0),
            delisted_count=int(payload.get("delisted_count") or 0),
            backoff_secs=float(payload.get("backoff_secs") or 0.0),
            last_error=str(payload.get("last_error") or "") or None,
            stream_connected=bool(payload.get("stream_connected")),
            stream_connected_since_ns=int(payload.get("stream_connected_since_ns") or 0),
            stream_reconnect_count=int(payload.get("stream_reconnect_count") or 0),
            stream_fallback_activation_count=int(
                payload.get("stream_fallback_activation_count") or 0,
            ),
            stream_publication_count=int(payload.get("stream_publication_count") or 0),
            stream_subscribed_channel_count=int(
                payload.get("stream_subscribed_channel_count") or 0,
            ),
            stream_subscribe_error_count=int(payload.get("stream_subscribe_error_count") or 0),
            stream_seed_failure_count=int(payload.get("stream_seed_failure_count") or 0),
            stream_last_disconnect_reason=(
                str(payload.get("stream_last_disconnect_reason") or "") or None
            ),
            tombstoned_market_count=int(payload.get("tombstoned_market_count") or 0),
            tombstone_skipped_count=int(payload.get("tombstone_skipped_count") or 0),
            revalidation_probe_count=int(payload.get("revalidation_probe_count") or 0),
            hot_instrument_count=int(payload.get("hot_instrument_count") or 0),
            warm_instrument_count=int(payload.get("warm_instrument_count") or 0),
            cold_instrument_count=int(payload.get("cold_instrument_count") or 0),
            tier_due_count=int(payload.get("tier_due_count") or 0),
        )
    except (TypeError, ValueError):
        return None


def decode_venue_quote_tiers(raw: bytes | None) -> VenueQuoteTiers | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    venue = str(payload.get("venue") or "").strip().upper()
    if not venue:
        return None
    tier_by_instrument_id_raw = payload.get("tier_by_instrument_id")
    if not isinstance(tier_by_instrument_id_raw, dict):
        return None
    tier_by_instrument_id = {
        str(instrument_id): str(tier)
        for instrument_id, tier in tier_by_instrument_id_raw.items()
        if instrument_id
    }
    tier_intervals = dict(QUOTE_TIER_INTERVALS_DEFAULT)
    tier_intervals_raw = payload.get("tier_intervals")
    if isinstance(tier_intervals_raw, dict):
        for tier, interval in tier_intervals_raw.items():
            try:
                tier_intervals[str(tier)] = int(interval)
            except (TypeError, ValueError):
                continue
    return VenueQuoteTiers(
        venue=venue,
        updated_at_ns=int(payload.get("updated_at_ns") or 0),
        tier_by_instrument_id=tier_by_instrument_id,
        tier_intervals=tier_intervals,
    )
