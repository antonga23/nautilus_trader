# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Helpers for analyzing persisted betting arbitrage strategy logs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal
import json
import re


FIELD_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>'[^']*'|[^ ;|]+)")
INSTRUMENT_PAIR_RE = re.compile(
    r"Instrument A: (?P<a>.*?); Instrument B: (?P<b>.*?)(?:(?:; expected_profit=(?P<expected_profit>\S+) "
    r"max_total_stake=(?P<max_total_stake>\S+))|$)",
)


@dataclass(frozen=True)
class InstrumentLegDiagnostic:
    instrument_id: str | None
    venue: str | None
    event: str | None
    market: str | None
    selection: str | None
    odds: Decimal | None
    bet: Decimal | None
    market_id: str | None
    params: str | None
    available_size: Decimal | None
    quote_cycle_id: str | None
    quote_age_secs: float | None


@dataclass(frozen=True)
class AcceptedOpportunityRecord:
    line_no: int
    opportunity_id: str | None
    classification: str
    classification_reason: str
    profit_margin: str | None
    expected_profit: Decimal | None
    max_total_stake: Decimal | None
    execution_enabled: bool | None
    instrument_a: InstrumentLegDiagnostic
    instrument_b: InstrumentLegDiagnostic


@dataclass(frozen=True)
class SuppressedOpportunityRecord:
    line_no: int
    reason: str
    classification: str
    classification_reason: str
    opportunity_id: str | None
    instrument_a: InstrumentLegDiagnostic | None
    instrument_b: InstrumentLegDiagnostic | None


@dataclass(frozen=True)
class BettingArbitrageLogAnalysis:
    accepted: list[AcceptedOpportunityRecord]
    suppressed: list[SuppressedOpportunityRecord]
    summaries: list[dict[str, int]]

    def summary_counts(self) -> dict[str, object]:
        accepted_by_classification = Counter(record.classification for record in self.accepted)
        suppressed_by_reason = Counter(record.reason for record in self.suppressed)
        executable_candidates = accepted_by_classification.get("valid", 0)
        return {
            "accepted": len(self.accepted),
            "suppressed": len(self.suppressed),
            "accepted_by_classification": dict(accepted_by_classification),
            "suppressed_by_reason": dict(suppressed_by_reason),
            "executable_candidates": executable_candidates,
            "latest_summary": self.summaries[-1] if self.summaries else None,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": [asdict(record) for record in self.accepted],
            "suppressed": [asdict(record) for record in self.suppressed],
            "summaries": self.summaries,
            "summary_counts": self.summary_counts(),
        }


def _strip_quotes(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    return value


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except Exception:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _parse_fields(fragment: str) -> dict[str, str]:
    return {
        match.group("key"): _strip_quotes(match.group("value")) or ""
        for match in FIELD_RE.finditer(fragment)
    }


def _parse_instrument_leg(fragment: str) -> InstrumentLegDiagnostic:
    fields = _parse_fields(fragment)
    return InstrumentLegDiagnostic(
        instrument_id=fields.get("instrument_id"),
        venue=fields.get("venue"),
        event=fields.get("event"),
        market=fields.get("market"),
        selection=fields.get("selection"),
        odds=_parse_decimal(fields.get("odds")),
        bet=_parse_decimal(fields.get("bet")),
        market_id=fields.get("market_id"),
        params=fields.get("params"),
        available_size=_parse_decimal(fields.get("available_size")),
        quote_cycle_id=fields.get("quote_cycle_id"),
        quote_age_secs=_parse_float(fields.get("quote_age_secs")),
    )


def _parse_instrument_pair(
    fragment: str,
) -> tuple[InstrumentLegDiagnostic | None, InstrumentLegDiagnostic | None, dict[str, str]]:
    match = INSTRUMENT_PAIR_RE.search(fragment)
    if not match:
        return None, None, {}

    trailer = {
        "expected_profit": match.group("expected_profit") or "",
        "max_total_stake": match.group("max_total_stake") or "",
    }
    return (
        _parse_instrument_leg(match.group("a")),
        _parse_instrument_leg(match.group("b")),
        trailer,
    )


def _parse_summary_line(line: str) -> dict[str, int] | None:
    if "Arbitrage quality summary:" not in line:
        return None

    fields = _parse_fields(line)
    summary: dict[str, int] = {}
    for key, value in fields.items():
        try:
            summary[key] = int(value)
        except ValueError:
            continue
    return summary


def _parse_accepted_line(line_no: int, line: str) -> AcceptedOpportunityRecord | None:
    if "Arbitrage found:" not in line:
        return None

    profit_margin_match = re.search(r"Profit: (?P<profit>[^|]+)", line)
    fields = _parse_fields(line)
    leg_a, leg_b, trailer = _parse_instrument_pair(line)
    return AcceptedOpportunityRecord(
        line_no=line_no,
        opportunity_id=fields.get("opportunity_id"),
        classification=fields.get("classification", "valid"),
        classification_reason=fields.get("classification_reason", "none"),
        profit_margin=profit_margin_match.group("profit").strip() if profit_margin_match else None,
        expected_profit=_parse_decimal(trailer.get("expected_profit")),
        max_total_stake=_parse_decimal(trailer.get("max_total_stake")),
        execution_enabled=_parse_bool(fields.get("execution_enabled")),
        instrument_a=leg_a
        or InstrumentLegDiagnostic(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        instrument_b=leg_b
        or InstrumentLegDiagnostic(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )


def _parse_suppressed_line(line_no: int, line: str) -> SuppressedOpportunityRecord | None:
    if "Arbitrage candidate suppressed:" not in line:
        return None

    fields = _parse_fields(line)
    leg_a, leg_b, _ = _parse_instrument_pair(line)
    return SuppressedOpportunityRecord(
        line_no=line_no,
        reason=fields.get("reason", "unknown"),
        classification=fields.get("classification", fields.get("reason", "unknown")),
        classification_reason=fields.get(
            "classification_reason",
            fields.get("suspect_reason", "none"),
        ),
        opportunity_id=fields.get("opportunity_id"),
        instrument_a=leg_a,
        instrument_b=leg_b,
    )


def analyze_betting_arbitrage_log_lines(lines: list[str]) -> BettingArbitrageLogAnalysis:
    accepted: list[AcceptedOpportunityRecord] = []
    suppressed: list[SuppressedOpportunityRecord] = []
    summaries: list[dict[str, int]] = []

    for line_no, line in enumerate(lines, start=1):
        summary = _parse_summary_line(line)
        if summary is not None:
            summaries.append(summary)
            continue

        accepted_record = _parse_accepted_line(line_no, line)
        if accepted_record is not None:
            accepted.append(accepted_record)
            continue

        suppressed_record = _parse_suppressed_line(line_no, line)
        if suppressed_record is not None:
            suppressed.append(suppressed_record)

    return BettingArbitrageLogAnalysis(
        accepted=accepted,
        suppressed=suppressed,
        summaries=summaries,
    )


def analyze_betting_arbitrage_log_text(text: str) -> BettingArbitrageLogAnalysis:
    return analyze_betting_arbitrage_log_lines(text.splitlines())


def top_matcher_suspect_clusters(
    analysis: BettingArbitrageLogAnalysis,
    *,
    limit: int = 10,
) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for record in analysis.suppressed:
        if record.reason != "matcher_suspect":
            continue
        leg_a = record.instrument_a
        leg_b = record.instrument_b
        counter[
            "|".join(
                [
                    record.classification_reason,
                    leg_a.event if leg_a and leg_a.event else "unknown",
                    leg_a.market if leg_a and leg_a.market else "unknown",
                    leg_b.event if leg_b and leg_b.event else "unknown",
                    leg_b.market if leg_b and leg_b.market else "unknown",
                ],
            )
        ] += 1
    return counter.most_common(limit)


def render_betting_arbitrage_analysis(
    analysis: BettingArbitrageLogAnalysis,
    *,
    limit: int = 10,
) -> str:
    counts = analysis.summary_counts()
    suspect_clusters = top_matcher_suspect_clusters(analysis, limit=limit)
    lines = [
        "Betting arbitrage log analysis",
        f"accepted={counts['accepted']}",
        f"suppressed={counts['suppressed']}",
        f"executable_candidates={counts['executable_candidates']}",
        f"accepted_by_classification={json.dumps(counts['accepted_by_classification'], sort_keys=True)}",
        f"suppressed_by_reason={json.dumps(counts['suppressed_by_reason'], sort_keys=True)}",
    ]

    latest_summary = counts["latest_summary"]
    if latest_summary:
        lines.append(f"latest_summary={json.dumps(latest_summary, sort_keys=True)}")

    if suspect_clusters:
        lines.append("top_matcher_suspect_clusters=" + json.dumps(suspect_clusters))

    for record in analysis.accepted[:limit]:
        lines.append(
            "accepted_opportunity "
            f"line={record.line_no} classification={record.classification} "
            f"profit_margin={record.profit_margin} expected_profit={record.expected_profit}",
        )
        lines.append(
            "  Instrument A: "
            f"venue={record.instrument_a.venue} "
            f"event={record.instrument_a.event!r} "
            f"market={record.instrument_a.market!r} "
            f"params={record.instrument_a.params!r} "
            f"selection={record.instrument_a.selection!r} "
            f"odds={record.instrument_a.odds} "
            f"bet={record.instrument_a.bet} "
            f"available_size={record.instrument_a.available_size} "
            f"quote_cycle_id={record.instrument_a.quote_cycle_id} "
            f"quote_age_secs={record.instrument_a.quote_age_secs}",
        )
        lines.append(
            "  Instrument B: "
            f"venue={record.instrument_b.venue} "
            f"event={record.instrument_b.event!r} "
            f"market={record.instrument_b.market!r} "
            f"params={record.instrument_b.params!r} "
            f"selection={record.instrument_b.selection!r} "
            f"odds={record.instrument_b.odds} "
            f"bet={record.instrument_b.bet} "
            f"available_size={record.instrument_b.available_size} "
            f"quote_cycle_id={record.instrument_b.quote_cycle_id} "
            f"quote_age_secs={record.instrument_b.quote_age_secs}",
        )

    return "\n".join(lines)
