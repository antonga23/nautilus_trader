# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

from decimal import Decimal
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "strategy_nodes"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from betting_arbitrage_log_analysis import analyze_betting_arbitrage_log_text


def test_analyze_betting_arbitrage_log_parses_accepted_manual_execution_records():
    text = """
2026-04-27T12:30:00Z [INFO] Arbitrage found: OVER-2.5 @ 2.30 vs UNDER-2.5 @ 2.45 | Profit: 8.48% | opportunity_id=opp-1 match_type=same_market hedge_match_type=same_market confidence=1.00 classification=valid classification_reason=none venue_a=SXBET venue_b=SXBET event_id_a=market-1 event_id_b=market-1 market_id_a=market-1-over market_id_b=market-1-under market_a=total_goals market_b=total_goals outcome_a=over outcome_b=under quote_ts_a=10000000000 quote_ts_b=10050000000 quote_age_a_secs=1.00 quote_age_b_secs=0.50 quote_delta_secs=0.50 same_quote_cycle=True | Manual execution plan: execution_enabled=False Instrument A: bet=51.58 instrument_id=OVER-2.5.SXBET venue=SXBET event='Team A vs Team B' market='total_goals' selection='over' odds=2.30 market_id=market-1-over available_size=300; Instrument B: bet=48.42 instrument_id=UNDER-2.5.SXBET venue=SXBET event='Team A vs Team B' market='total_goals' selection='under' odds=2.45 market_id=market-1-under available_size=280; expected_profit=8.48 max_total_stake=100
"""

    analysis = analyze_betting_arbitrage_log_text(text)

    assert len(analysis.accepted) == 1
    record = analysis.accepted[0]
    assert record.classification == "valid"
    assert record.execution_enabled is False
    assert record.expected_profit == Decimal("8.48")
    assert record.instrument_a.event == "Team A vs Team B"
    assert record.instrument_a.available_size == Decimal(300)
    assert record.instrument_b.selection == "under"


def test_analyze_betting_arbitrage_log_parses_suppressed_classifications_and_summaries():
    text = """
2026-04-27T12:31:00Z [INFO] Arbitrage candidate suppressed: reason=liquidity_insufficient classification=liquidity_insufficient classification_reason=top_of_book_size opportunity_id=opp-2 suggested_stake_a=51.58 available_size_a=10 suggested_stake_b=48.42 available_size_b=10 | Instrument A: instrument_id=OVER-2.5.SXBET venue=SXBET event='Team A vs Team B' market='total_goals' selection='over' odds=2.30 market_id=market-1-over available_size=10 quote_age_secs=1.00; Instrument B: instrument_id=UNDER-2.5.SXBET venue=SXBET event='Team A vs Team B' market='total_goals' selection='under' odds=2.45 market_id=market-1-under available_size=10 quote_age_secs=0.50
2026-04-27T12:32:00Z [INFO] Arbitrage quality summary: raw_detections=12 unique_opportunities=2 duplicate_suppressions=3 stale_quote_suppressions=1 matcher_suspect_suppressions=2 liquidity_suppressions=4 manual_review_suppressions=1 executable_candidates=1 executed=0
"""

    analysis = analyze_betting_arbitrage_log_text(text)

    assert len(analysis.suppressed) == 1
    suppressed = analysis.suppressed[0]
    assert suppressed.reason == "liquidity_insufficient"
    assert suppressed.classification == "liquidity_insufficient"
    assert suppressed.classification_reason == "top_of_book_size"
    assert suppressed.instrument_a is not None
    assert suppressed.instrument_a.available_size == Decimal(10)

    assert analysis.summaries[-1]["liquidity_suppressions"] == 4
    assert analysis.summary_counts()["suppressed_by_reason"] == {"liquidity_insufficient": 1}
