# skipcq: PYL-C0114, PYL-C0116

from scripts.strategy_nodes.venue_latency_probe import ProbeSample
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
