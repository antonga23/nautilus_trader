import json

from nautilus_trader.adapters.betting.runtime_cache import ActiveVenueInstrumentIndex
from nautilus_trader.adapters.betting.runtime_cache import VenueQuotePollStats
from nautilus_trader.adapters.betting.runtime_cache import active_venue_instrument_index_key
from nautilus_trader.adapters.betting.runtime_cache import decode_active_venue_instrument_index
from nautilus_trader.adapters.betting.runtime_cache import decode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import encode_active_venue_instrument_index
from nautilus_trader.adapters.betting.runtime_cache import encode_venue_quote_poll_stats
from nautilus_trader.adapters.betting.runtime_cache import latency_percentiles
from nautilus_trader.adapters.betting.runtime_cache import venue_quote_poll_stats_key


def test_active_venue_instrument_index_key_normalizes_venue() -> None:
    assert active_venue_instrument_index_key(" cloudbet ") == (
        "betting:active_venue_instruments:CLOUDBET"
    )


def test_active_venue_instrument_index_round_trip() -> None:
    raw = encode_active_venue_instrument_index(
        venue="sxbet",
        instrument_ids=["b", "a", "a"],
        updated_at_ns=123,
    )

    payload = decode_active_venue_instrument_index(raw)

    assert payload == ActiveVenueInstrumentIndex(
        venue="SXBET",
        instrument_ids=("a", "b"),
        updated_at_ns=123,
    )


def test_active_venue_instrument_index_decode_rejects_invalid_payload() -> None:
    assert decode_active_venue_instrument_index(None) is None
    assert decode_active_venue_instrument_index(b"not-json") is None


def test_venue_quote_poll_stats_round_trip() -> None:
    raw = encode_venue_quote_poll_stats(
        venue="sxbet",
        updated_at_ns=456,
        cycle_id=7,
        source="rest_order_book_poll",
        subscribed_instrument_count=10,
        market_count=5,
        quote_count=8,
        request_count=4,
        event_request_count=3,
        line_request_count=1,
        pruned_subscription_count=2,
        refilled_subscription_count=1,
        order_count=12,
        empty_market_count=1,
        one_sided_market_count=2,
        two_sided_market_count=3,
        concurrency=4,
        backlog_count=1,
        cycle_elapsed_secs=0.75,
        max_fetch_latency_secs=0.25,
        fetch_latency_p50_secs=0.1,
        fetch_latency_p95_secs=0.2,
        fetch_latency_p99_secs=0.25,
        poll_interval_secs=3.0,
        poll_target_cycle_secs=4.0,
        next_poll_sleep_secs=1.25,
        min_concurrency=2,
        max_concurrency=16,
        adaptive_concurrency=True,
        quote_event_timestamp_source="request_started",
        quote_init_timestamp_source="response_received",
        failure_count=2,
        rate_limit_count=1,
        delisted_count=3,
        backoff_secs=1.0,
        last_error="rate limit",
        stream_connected=True,
        stream_connected_since_ns=789,
        stream_reconnect_count=5,
        stream_fallback_activation_count=6,
        stream_publication_count=1000,
        stream_subscribed_channel_count=450,
        stream_subscribe_error_count=78,
        stream_seed_failure_count=2,
        stream_last_disconnect_reason="realtime connection stale",
        tombstoned_market_count=4,
        tombstone_skipped_count=6,
        revalidation_probe_count=2,
    )

    payload = decode_venue_quote_poll_stats(raw)

    assert venue_quote_poll_stats_key(" sxbet ") == "betting:venue_quote_poll_stats:SXBET"
    assert payload == VenueQuotePollStats(
        venue="SXBET",
        updated_at_ns=456,
        cycle_id=7,
        source="rest_order_book_poll",
        subscribed_instrument_count=10,
        market_count=5,
        quote_count=8,
        request_count=4,
        event_request_count=3,
        line_request_count=1,
        pruned_subscription_count=2,
        refilled_subscription_count=1,
        order_count=12,
        empty_market_count=1,
        one_sided_market_count=2,
        two_sided_market_count=3,
        concurrency=4,
        backlog_count=1,
        cycle_elapsed_secs=0.75,
        max_fetch_latency_secs=0.25,
        poll_interval_secs=3.0,
        fetch_latency_p50_secs=0.1,
        fetch_latency_p95_secs=0.2,
        fetch_latency_p99_secs=0.25,
        poll_target_cycle_secs=4.0,
        next_poll_sleep_secs=1.25,
        min_concurrency=2,
        max_concurrency=16,
        adaptive_concurrency=True,
        quote_event_timestamp_source="request_started",
        quote_init_timestamp_source="response_received",
        failure_count=2,
        rate_limit_count=1,
        delisted_count=3,
        backoff_secs=1.0,
        last_error="rate limit",
        stream_connected=True,
        stream_connected_since_ns=789,
        stream_reconnect_count=5,
        stream_fallback_activation_count=6,
        stream_publication_count=1000,
        stream_subscribed_channel_count=450,
        stream_subscribe_error_count=78,
        stream_seed_failure_count=2,
        stream_last_disconnect_reason="realtime connection stale",
        tombstoned_market_count=4,
        tombstone_skipped_count=6,
        revalidation_probe_count=2,
    )


def test_venue_quote_poll_stats_decode_defaults_stream_fields_for_old_payloads() -> None:
    raw = encode_venue_quote_poll_stats(
        venue="sxbet",
        updated_at_ns=1,
        cycle_id=1,
        source="rest_order_book_poll",
        subscribed_instrument_count=1,
        market_count=1,
        quote_count=1,
    )
    payload = json.loads(raw.decode("utf-8"))
    for key in list(payload):
        if key.startswith("stream_"):
            payload.pop(key)

    stats = decode_venue_quote_poll_stats(json.dumps(payload).encode("utf-8"))

    assert stats is not None
    assert stats.stream_connected is False
    assert stats.stream_reconnect_count == 0
    assert stats.stream_last_disconnect_reason is None


def test_venue_quote_poll_stats_decode_defaults_missing_tombstone_fields() -> None:
    raw = encode_venue_quote_poll_stats(
        venue="cloudbet",
        updated_at_ns=1,
        cycle_id=1,
        source="rest_event_poll",
        subscribed_instrument_count=1,
        market_count=1,
        quote_count=1,
    )
    payload_dict = json.loads(raw)
    for key in (
        "tombstoned_market_count",
        "tombstone_skipped_count",
        "revalidation_probe_count",
    ):
        payload_dict.pop(key)
    old_raw = json.dumps(payload_dict).encode("utf-8")

    payload = decode_venue_quote_poll_stats(old_raw)

    assert payload is not None
    assert payload.tombstoned_market_count == 0
    assert payload.tombstone_skipped_count == 0
    assert payload.revalidation_probe_count == 0


def test_latency_percentiles_normalize_empty_and_negative_values() -> None:
    assert latency_percentiles([]) == (0.0, 0.0, 0.0)
    assert latency_percentiles([-1.0, 0.1, 0.2, 0.3]) == (0.2, 0.3, 0.3)


def test_venue_quote_poll_stats_decode_rejects_invalid_payload() -> None:
    assert decode_venue_quote_poll_stats(None) is None
    assert decode_venue_quote_poll_stats(b"not-json") is None
