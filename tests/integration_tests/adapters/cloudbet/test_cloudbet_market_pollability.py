from nautilus_trader.adapters.cloudbet.market_pollability import MarketPollabilityRegistry
from nautilus_trader.adapters.cloudbet.market_pollability import RECORD_TTL_NS


SECOND_NS = 1_000_000_000


class TestMarketPollabilityRegistry:
    def test_record_miss_tombstones_once_at_threshold(self):
        registry = MarketPollabilityRegistry(miss_threshold=3, revalidate_secs=600.0)

        assert registry.record_miss(1, "baseball.inning_1x2", reason="line_404", now_ns=0) is False
        assert registry.record_miss(1, "baseball.inning_1x2", reason="line_404", now_ns=1) is False
        assert registry.record_miss(1, "baseball.inning_1x2", reason="line_404", now_ns=2) is True
        assert registry.record_miss(1, "baseball.inning_1x2", reason="line_404", now_ns=3) is False
        assert registry.is_poll_suppressed(1, "baseball.inning_1x2", now_ns=4) is True

    def test_not_suppressed_below_threshold(self):
        registry = MarketPollabilityRegistry(miss_threshold=3, revalidate_secs=600.0)

        registry.record_miss(1, "m", reason="market_absent", now_ns=0)
        registry.record_miss(1, "m", reason="market_absent", now_ns=1)

        assert registry.is_poll_suppressed(1, "m", now_ns=2) is False
        assert registry.claim_revalidation_probe(1, "m", now_ns=2) is False

    def test_suppression_lifts_when_revalidation_due(self):
        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)

        registry.record_miss(1, "m", reason="line_404", now_ns=0)

        assert registry.is_poll_suppressed(1, "m", now_ns=599 * SECOND_NS) is True
        assert registry.is_poll_suppressed(1, "m", now_ns=601 * SECOND_NS) is False

    def test_claim_revalidation_probe_granted_once_per_window(self):
        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)
        due_ns = 601 * SECOND_NS

        assert registry.claim_revalidation_probe(1, "m", now_ns=due_ns) is True
        # Sibling selections of the same market share the single probe.
        assert registry.claim_revalidation_probe(1, "m", now_ns=due_ns) is False
        assert registry.is_poll_suppressed(1, "m", now_ns=due_ns + 1) is True

    def test_record_success_clears_tombstone(self):
        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)

        assert registry.record_success(1, "m") is True
        assert registry.is_poll_suppressed(1, "m", now_ns=1) is False
        assert registry.record_success(1, "m") is False
        assert registry.snapshot()["tombstoned_market_count"] == 0

    def test_record_success_below_threshold_resets_misses(self):
        registry = MarketPollabilityRegistry(miss_threshold=3, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)
        registry.record_miss(1, "m", reason="line_404", now_ns=1)

        assert registry.record_success(1, "m") is False
        assert registry.record_miss(1, "m", reason="line_404", now_ns=2) is False

    def test_market_key_escalation_across_distinct_events(self):
        registry = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=600.0,
            market_key_event_threshold=3,
        )

        registry.record_miss(1, "m", reason="line_404", now_ns=0)
        registry.record_miss(2, "m", reason="line_404", now_ns=0)
        assert registry.is_market_key_unpollable("m") is False

        registry.record_miss(3, "m", reason="line_404", now_ns=0)
        assert registry.is_market_key_unpollable("m") is True

        registry.record_success(2, "m")
        assert registry.is_market_key_unpollable("m") is False

    def test_prune_expired_drops_stale_records(self):
        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)
        registry.record_miss(2, "m", reason="line_404", now_ns=RECORD_TTL_NS)

        assert registry.prune_expired(now_ns=RECORD_TTL_NS + 1) == 1
        assert registry.is_poll_suppressed(1, "m", now_ns=RECORD_TTL_NS + 1) is False
        assert registry.snapshot()["tombstoned_market_count"] == 1

    def test_snapshot_counts(self):
        registry = MarketPollabilityRegistry(miss_threshold=2, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)
        registry.record_miss(1, "m", reason="line_404", now_ns=1)
        registry.record_miss(2, "other", reason="market_absent", now_ns=1)

        snapshot = registry.snapshot()

        assert snapshot["tracked_market_count"] == 2
        assert snapshot["tombstoned_market_count"] == 1
        assert snapshot["unpollable_market_key_count"] == 0

    def test_exclude_from_discovery_while_tombstoned(self):
        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)

        assert registry.exclude_from_discovery(1, "m", now_ns=1) is True
        # Tombstones stay excluded from discovery even past the revalidation window:
        # poll-side probes (or the 24h record TTL) are the way back in.
        assert registry.exclude_from_discovery(1, "m", now_ns=601 * SECOND_NS) is True

    def test_exclude_from_discovery_not_tombstoned_included(self):
        registry = MarketPollabilityRegistry(miss_threshold=3, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)
        registry.record_miss(1, "m", reason="line_404", now_ns=1)

        assert registry.exclude_from_discovery(1, "m", now_ns=2) is False
        assert registry.exclude_from_discovery(2, "m", now_ns=2) is False
        assert registry.exclude_from_discovery(1, "other", now_ns=2) is False

    def test_exclude_from_discovery_cleared_by_success(self):
        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="line_404", now_ns=0)
        registry.record_success(1, "m")

        assert registry.exclude_from_discovery(1, "m", now_ns=1) is False

    def test_exclude_from_discovery_escalated_key_excludes_new_events(self):
        registry = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=600.0,
            market_key_event_threshold=3,
        )
        for event_id in (1, 2, 3):
            registry.record_miss(event_id, "m", reason="line_404", now_ns=0)

        # A never-tombstoned event on an escalated key is excluded too.
        assert registry.exclude_from_discovery(99, "m", now_ns=1) is True
        assert registry.exclude_from_discovery(99, "other", now_ns=1) is False

    def test_exclude_from_discovery_admits_canary_when_revalidation_due(self):
        registry = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=600.0,
            market_key_event_threshold=3,
        )
        for event_id in (1, 2, 3):
            registry.record_miss(event_id, "m", reason="line_404", now_ns=0)
        due_ns = 601 * SECOND_NS

        assert registry.exclude_from_discovery(1, "m", now_ns=599 * SECOND_NS) is True
        # Only the smallest tombstoned event_id is admitted, and only once due.
        assert registry.exclude_from_discovery(1, "m", now_ns=due_ns) is False
        assert registry.exclude_from_discovery(2, "m", now_ns=due_ns) is True
        assert registry.exclude_from_discovery(3, "m", now_ns=due_ns) is True

    def test_exclude_from_discovery_does_not_consume_probe(self):
        registry = MarketPollabilityRegistry(
            miss_threshold=1,
            revalidate_secs=600.0,
            market_key_event_threshold=3,
        )
        for event_id in (1, 2, 3):
            registry.record_miss(event_id, "m", reason="line_404", now_ns=0)
        due_ns = 601 * SECOND_NS

        assert registry.exclude_from_discovery(1, "m", now_ns=due_ns) is False
        assert registry.exclude_from_discovery(1, "m", now_ns=due_ns) is False
        # The poll-side revalidation probe is still available to claim.
        assert registry.claim_revalidation_probe(1, "m", now_ns=due_ns) is True

    def test_clear_resets_state(self):
        registry = MarketPollabilityRegistry(miss_threshold=1, revalidate_secs=600.0)
        registry.record_miss(1, "m", reason="malformed_request", now_ns=0)

        registry.clear()

        assert registry.is_poll_suppressed(1, "m", now_ns=1) is False
        assert registry.snapshot()["tracked_market_count"] == 0
