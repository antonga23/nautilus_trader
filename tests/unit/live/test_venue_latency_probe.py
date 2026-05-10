# skipcq: PYL-C0114, PYL-C0116

from scripts.strategy_nodes.venue_latency_probe import ProbeSample
from scripts.strategy_nodes.venue_latency_probe import placement_recommendations
from scripts.strategy_nodes.venue_latency_probe import summarize_samples


def test_latency_probe_summary_includes_tail_and_error_rate():
    summary = summarize_samples(
        [
            ProbeSample(True, 1.0, 2.0, 3.0, 20.0, 30.0, status=200),
            ProbeSample(True, 2.0, 3.0, 4.0, 30.0, 40.0, status=200),
            ProbeSample(True, 3.0, 4.0, 5.0, 40.0, 60.0, status=200),
            ProbeSample(False, 0.0, 0.0, 0.0, 0.0, 10.0, error="TimeoutError"),
        ],
    )

    assert summary["samples"] == 4
    assert summary["successful"] == 3
    assert summary["failed"] == 1
    assert summary["errorRate"] == 0.25
    assert summary["total_ms"]["median"] == 40.0
    assert summary["total_ms"]["p95"] == 60.0
    assert summary["total_ms"]["max"] == 60.0


def test_latency_probe_recommends_strategy_placement_by_worst_leg():
    summaries = {
        "cloudbet": summarize_samples(
            [
                ProbeSample(True, 1.0, 2.0, 3.0, 20.0, 35.0, status=200),
                ProbeSample(True, 1.0, 2.0, 3.0, 24.0, 45.0, status=200),
            ],
        ),
        "sxbet": summarize_samples(
            [
                ProbeSample(True, 1.0, 3.0, 4.0, 30.0, 80.0, status=200),
                ProbeSample(True, 1.0, 3.0, 4.0, 36.0, 90.0, status=200),
            ],
        ),
        "polymarket": summarize_samples(
            [
                ProbeSample(True, 1.0, 2.0, 3.0, 15.0, 25.0, status=200),
                ProbeSample(True, 1.0, 2.0, 3.0, 17.0, 30.0, status=200),
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
    assert cloudbet_sxbet["venueTotalP95Ms"] == {"cloudbet": 45.0, "sxbet": 90.0}
    assert cloudbet_sxbet["eligibleForPlacementComparison"] is True
    assert cloudbet_sxbet["blockers"] == []


def test_latency_probe_blocks_stale_or_missing_placement_samples():
    summaries = {
        "sxbet": summarize_samples(
            [
                ProbeSample(True, 1.0, 3.0, 4.0, 30.0, 80.0, status=200),
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
