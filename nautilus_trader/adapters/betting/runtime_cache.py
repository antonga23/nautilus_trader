import json
from collections.abc import Iterable
from dataclasses import dataclass


ACTIVE_VENUE_INSTRUMENT_INDEX_PREFIX = "betting:active_venue_instruments"
VENUE_QUOTE_POLL_STATS_PREFIX = "betting:venue_quote_poll_stats"


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
    order_count: int
    empty_market_count: int
    one_sided_market_count: int
    two_sided_market_count: int
    concurrency: int
    backlog_count: int
    cycle_elapsed_secs: float
    max_fetch_latency_secs: float
    poll_interval_secs: float


def active_venue_instrument_index_key(venue: str) -> str:
    return f"{ACTIVE_VENUE_INSTRUMENT_INDEX_PREFIX}:{venue.strip().upper()}"


def venue_quote_poll_stats_key(venue: str) -> str:
    return f"{VENUE_QUOTE_POLL_STATS_PREFIX}:{venue.strip().upper()}"


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
    order_count: int = 0,
    empty_market_count: int = 0,
    one_sided_market_count: int = 0,
    two_sided_market_count: int = 0,
    concurrency: int = 1,
    backlog_count: int = 0,
    cycle_elapsed_secs: float = 0.0,
    max_fetch_latency_secs: float = 0.0,
    poll_interval_secs: float = 0.0,
) -> bytes:
    payload = {
        "venue": venue.strip().upper(),
        "updated_at_ns": int(updated_at_ns),
        "cycle_id": int(cycle_id),
        "source": str(source),
        "subscribed_instrument_count": max(0, int(subscribed_instrument_count)),
        "market_count": max(0, int(market_count)),
        "quote_count": max(0, int(quote_count)),
        "order_count": max(0, int(order_count)),
        "empty_market_count": max(0, int(empty_market_count)),
        "one_sided_market_count": max(0, int(one_sided_market_count)),
        "two_sided_market_count": max(0, int(two_sided_market_count)),
        "concurrency": max(1, int(concurrency)),
        "backlog_count": max(0, int(backlog_count)),
        "cycle_elapsed_secs": max(0.0, float(cycle_elapsed_secs)),
        "max_fetch_latency_secs": max(0.0, float(max_fetch_latency_secs)),
        "poll_interval_secs": max(0.0, float(poll_interval_secs)),
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
            order_count=int(payload.get("order_count") or 0),
            empty_market_count=int(payload.get("empty_market_count") or 0),
            one_sided_market_count=int(payload.get("one_sided_market_count") or 0),
            two_sided_market_count=int(payload.get("two_sided_market_count") or 0),
            concurrency=max(1, int(payload.get("concurrency") or 1)),
            backlog_count=int(payload.get("backlog_count") or 0),
            cycle_elapsed_secs=float(payload.get("cycle_elapsed_secs") or 0.0),
            max_fetch_latency_secs=float(payload.get("max_fetch_latency_secs") or 0.0),
            poll_interval_secs=float(payload.get("poll_interval_secs") or 0.0),
        )
    except (TypeError, ValueError):
        return None
