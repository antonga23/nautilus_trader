# skipcq: PYL-C0114, PYL-C0116

from scripts.strategy_nodes.venue_latency_probe import DEFAULT_TARGETS
from scripts.strategy_nodes.venue_latency_probe import ProbeSample
from scripts.strategy_nodes.venue_latency_probe import _error_summary
from scripts.strategy_nodes.venue_latency_probe import compare_region_reports
from scripts.strategy_nodes.venue_latency_probe import placement_recommendations
from scripts.strategy_nodes.venue_latency_probe import summarize_samples


def test_latency_probe_summary_includes_tail_and_error_rate():
    summary = summarize_samples(
        [
            ProbeSample(True, 1.0, 2.0, 3.0, 20.0, 4.0, 30.0, status=200),
            ProbeSample(True, 2.0, 3.0, 4.0, 30.0, 5.0, 40.0, status=200),
            ProbeSample(True, 3.0, 4.0, 5.0, 40.0, 6.0, 60.0, status=200),
            ProbeSample(False, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, error="TimeoutError"),
        ],
    )

    assert summary["samples"] == 4
    assert summary["successful"] == 3
    assert summary["failed"] == 1
    assert summary["errorRate"] == 0.25
    assert summary["total_ms"]["median"] == 40.0
    assert summary["total_ms"]["p95"] == 60.0
    assert summary["total_ms"]["max"] == 60.0
    assert summary["read_ms"]["p95"] == 6.0


def test_latency_probe_error_summary_keeps_short_sanitized_details():
    message = "TLS failed\nwhile connecting " + ("x" * 200)

    summary = _error_summary(RuntimeError(message))

    assert summary.startswith("RuntimeError: TLS failed while connecting ")
    assert "\n" not in summary
    assert len(summary) < 150


def test_latency_probe_uses_safe_post_for_hyperliquid_info_target():
    target = DEFAULT_TARGETS["hyperliquid"]

    assert target.method == "POST"
    assert target.body == '{"type":"allMids"}'
    assert target.headers == {"Content-Type": "application/json"}


def test_latency_probe_recommends_strategy_placement_by_worst_leg():
    summaries = {
        "cloudbet": summarize_samples(
            [
                ProbeSample(True, 1.0, 2.0, 3.0, 20.0, 2.0, 35.0, status=200),
                ProbeSample(True, 1.0, 2.0, 3.0, 24.0, 4.0, 45.0, status=200),
            ],
        ),
        "sxbet": summarize_samples(
            [
                ProbeSample(True, 1.0, 3.0, 4.0, 30.0, 6.0, 80.0, status=200),
                ProbeSample(True, 1.0, 3.0, 4.0, 36.0, 8.0, 90.0, status=200),
            ],
        ),
        "polymarket": summarize_samples(
            [
                ProbeSample(True, 1.0, 2.0, 3.0, 15.0, 1.0, 25.0, status=200),
                ProbeSample(True, 1.0, 2.0, 3.0, 17.0, 2.0, 30.0, status=200),
            ],
        ),
    }

    recommendations = placement_recommendations(
        summaries,
        region="tokyo",
        generated_at_ns=100_000_000_000,
        now_ns=130_000_000_000,
        max_data_age_secs=60.0,
    )

    cloudbet_sxbet = recommendations["cloudbet_sxbet"]
    assert cloudbet_sxbet["region"] == "tokyo"
    assert cloudbet_sxbet["venues"] == ["cloudbet", "sxbet"]
    assert cloudbet_sxbet["worstLegTotalP95Ms"] == 90.0
    assert cloudbet_sxbet["worstLegTotalStddevMs"] == 5.0
    assert cloudbet_sxbet["venueTotalP95SkewMs"] == 45.0
    assert cloudbet_sxbet["placementScoreMs"] == 103.75
    assert cloudbet_sxbet["dominantLatencyVenue"] == "sxbet"
    assert cloudbet_sxbet["venueFirstByteP95SkewMs"] == 12.0
    assert cloudbet_sxbet["worstLegReadP95Ms"] == 8.0
    assert cloudbet_sxbet["venueReadP95SkewMs"] == 4.0
    assert cloudbet_sxbet["worstLegFirstByteStddevMs"] == 3.0
    assert cloudbet_sxbet["worstLegReadStddevMs"] == 1.0
    assert cloudbet_sxbet["venueTotalP95Ms"] == {"cloudbet": 45.0, "sxbet": 90.0}
    assert cloudbet_sxbet["venueTotalStddevMs"] == {"cloudbet": 5.0, "sxbet": 5.0}
    assert cloudbet_sxbet["eligibleForPlacementComparison"] is True
    assert cloudbet_sxbet["blockers"] == []


def test_latency_probe_blocks_stale_or_missing_placement_samples():
    summaries = {
        "sxbet": summarize_samples(
            [
                ProbeSample(True, 1.0, 3.0, 4.0, 30.0, 6.0, 80.0, status=200),
            ],
        ),
    }

    recommendations = placement_recommendations(
        summaries,
        region="us-east",
        generated_at_ns=100_000_000_000,
        now_ns=200_000_000_000,
        max_data_age_secs=60.0,
    )

    polymarket_sxbet = recommendations["polymarket_sxbet"]
    assert polymarket_sxbet["missingVenues"] == ["polymarket"]
    assert polymarket_sxbet["dataFresh"] is False
    assert polymarket_sxbet["eligibleForPlacementComparison"] is False
    assert polymarket_sxbet["blockers"] == ["missing_venue_samples", "stale_probe_data"]


def test_latency_probe_compares_regions_by_worst_leg_then_skew():
    tokyo = {
        "region": "tokyo",
        "generatedAtNs": 100,
        "targets": {"cloudbet": "https://example.test/cloudbet"},
        "placementRecommendations": {
            "cloudbet_sxbet": {
                "region": "tokyo",
                "venues": ["cloudbet", "sxbet"],
                "eligibleForPlacementComparison": True,
                "worstLegTotalP95Ms": 80.0,
                "venueTotalP95SkewMs": 35.0,
                "worstLegFirstByteP95Ms": 50.0,
                "venueFirstByteP95SkewMs": 20.0,
                "worstLegReadP95Ms": 12.0,
                "venueReadP95SkewMs": 9.0,
                "worstLegErrorRate": 0.0,
                "blockers": [],
            },
        },
    }
    virginia = {
        "region": "virginia",
        "generatedAtNs": 200,
        "targets": {"cloudbet": "https://example.test/cloudbet"},
        "placementRecommendations": {
            "cloudbet_sxbet": {
                "region": "virginia",
                "venues": ["cloudbet", "sxbet"],
                "eligibleForPlacementComparison": True,
                "worstLegTotalP95Ms": 80.0,
                "venueTotalP95SkewMs": 12.0,
                "worstLegFirstByteP95Ms": 44.0,
                "venueFirstByteP95SkewMs": 8.0,
                "worstLegReadP95Ms": 10.0,
                "venueReadP95SkewMs": 4.0,
                "worstLegErrorRate": 0.0,
                "blockers": [],
            },
        },
    }

    comparison = compare_region_reports([tokyo, virginia])

    best = comparison["bestRegionByStrategy"]["cloudbet_sxbet"]
    assert best["region"] == "virginia"
    assert best["worstLegTotalP95Ms"] == 80.0
    assert best["venueTotalP95SkewMs"] == 12.0
    assert best["worstLegReadP95Ms"] == 10.0
    assert best["venueReadP95SkewMs"] == 4.0
    assert comparison["regions"]["tokyo"]["generatedAtNs"] == 100


def test_latency_probe_compare_reports_blocked_regions_when_no_candidate_is_eligible():
    comparison = compare_region_reports(
        [
            {
                "region": "stale-region",
                "placementRecommendations": {
                    "polymarket_sxbet": {
                        "region": "stale-region",
                        "eligibleForPlacementComparison": False,
                        "blockers": ["stale_probe_data"],
                    },
                },
            },
        ],
    )

    best = comparison["bestRegionByStrategy"]["polymarket_sxbet"]
    assert best["region"] is None
    assert best["eligibleForPlacementComparison"] is False
    assert best["blockers"] == {"stale-region": ["stale_probe_data"]}
    assert comparison["blockersByRegion"] == {
        "stale-region": {"polymarket_sxbet": ["stale_probe_data"]},
    }
