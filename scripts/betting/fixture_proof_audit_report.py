#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Audit cross-venue fixture-proof blockers from strategy-node runtime artifacts.

The normal runtime probe report answers "how many candidates were found". This
report focuses on the false-negative surface: common venue pairs that were
quoted or discovered but did not become semantic edges because fixture identity
proof failed or was ambiguous.

"""

from __future__ import annotations

import argparse
from collections import Counter
from collections import defaultdict
from collections.abc import Iterable
import json
from pathlib import Path
from typing import Any


FIXTURE_BLOCKER_KEYS = (
    "ambiguous_fixture",
    "fixture_identity_mismatch",
    "no_common_fixture",
    "participant_mismatch",
    "sport_mismatch",
    "start_time_mismatch",
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _runtime_probe(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = _as_dict(payload.get("runtimeProbe"))
    return runtime or payload


def _candidate_quality(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(_runtime_probe(payload).get("candidateQuality"))


def _node_id(payload: dict[str, Any], path: Path) -> str:
    value = payload.get("nodeId") or _runtime_probe(payload).get("nodeId")
    return str(value or path.stem)


def _str_field(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _instrument_venue(value: Any) -> str:
    instrument_id = str(value or "")
    return instrument_id.rsplit(".", maxsplit=1)[-1] if "." in instrument_id else ""


def _venue_pair(sample: dict[str, Any]) -> str:
    explicit = _str_field(sample, "venuePair", "venue_pair")
    if explicit:
        return explicit
    venue_a = _str_field(sample, "venueA", "venue_a")
    venue_b = _str_field(sample, "venueB", "venue_b")
    if venue_a and venue_b:
        return f"{venue_a}->{venue_b}"
    venue_a = _instrument_venue(sample.get("instrumentIdA") or sample.get("instrument_id_a"))
    venue_b = _instrument_venue(sample.get("instrumentIdB") or sample.get("instrument_id_b"))
    return f"{venue_a}->{venue_b}" if venue_a and venue_b else "unknown"


def _sample_key(sample: dict[str, Any]) -> str:
    left = sample.get("instrumentIdA") or sample.get("instrument_id_a") or ""
    right = sample.get("instrumentIdB") or sample.get("instrument_id_b") or ""
    return f"{left}|{right}"


def _reason_from_sample(sample: dict[str, Any], fallback: str) -> str:
    for key in (
        "fixtureProofBlockerReason",
        "fixture_proof_blocker_reason",
        "blockerReason",
        "blocker_reason",
        "reason",
    ):
        value = sample.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _add_count(counter: Counter[str], key: str, value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        counter[key] += int(value)


def _fixture_overlap_counts(quality: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for overlap in _as_list(quality.get("fixtureOverlapDiagnostics")):
        if not isinstance(overlap, dict):
            continue
        for key, value in _as_dict(overlap.get("fixtureProofBlockerCounts")).items():
            _add_count(counts, str(key), value)
    return counts


def _zero_candidate_counts(quality: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for key, value in _as_dict(quality.get("zeroCandidateFixtureProofBlockerCounts")).items():
        _add_count(counts, str(key), value)
    for key, value in _as_dict(quality.get("zeroCandidateBlockerCounts")).items():
        if str(key) in FIXTURE_BLOCKER_KEYS:
            _add_count(counts, str(key), value)
    return counts


def _blocker_sample_counts(
    quality: dict[str, Any],
) -> tuple[Counter[str], dict[str, dict[str, Any]]]:
    counts: Counter[str] = Counter()
    samples_by_key: dict[str, dict[str, Any]] = {}
    for bucket, samples in _as_dict(quality.get("blockerSamples")).items():
        fallback = str(bucket)
        for sample in _as_list(samples):
            if not isinstance(sample, dict):
                continue
            reason = _reason_from_sample(sample, fallback)
            if reason not in FIXTURE_BLOCKER_KEYS and fallback not in FIXTURE_BLOCKER_KEYS:
                continue
            reason = reason if reason in FIXTURE_BLOCKER_KEYS else fallback
            counts[reason] += 1
            key = _sample_key(sample) or f"{reason}:{len(samples_by_key)}"
            samples_by_key.setdefault(key, {**sample, "blockerReason": reason})
    return counts, samples_by_key


def _add_overlap_counts(
    venue_pair_counts: dict[str, Counter[str]],
    overlap: dict[str, Any],
) -> None:
    venue_pair = str(overlap.get("venuePair") or "unknown")
    for reason, count in _as_dict(overlap.get("fixtureProofBlockerCounts")).items():
        if isinstance(count, int | float) and not isinstance(count, bool):
            venue_pair_counts[venue_pair][str(reason)] += int(count)


def _overlap_sample(
    overlap: dict[str, Any],
    sample: dict[str, Any],
    *,
    venue_pair: str,
) -> dict[str, Any]:
    return {
        **sample,
        "venuePair": venue_pair,
        "blockerReason": sample.get("blockerReason")
        or overlap.get("discoveryGapReason")
        or "fixture_proof_blocker",
    }


def _collect_samples_and_venue_pairs(
    quality: dict[str, Any],
    samples_by_key: dict[str, dict[str, Any]],
    *,
    sample_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Counter[str]]]:
    venue_pair_counts: dict[str, Counter[str]] = defaultdict(Counter)
    samples: list[dict[str, Any]] = []
    for sample in samples_by_key.values():
        reason = str(sample.get("blockerReason") or "unknown")
        venue_pair = _venue_pair(sample)
        venue_pair_counts[venue_pair][reason] += 1
        if len(samples) < sample_limit:
            samples.append(sample)

    for overlap in _as_list(quality.get("fixtureOverlapDiagnostics")):
        if not isinstance(overlap, dict):
            continue
        venue_pair = str(overlap.get("venuePair") or "unknown")
        _add_overlap_counts(venue_pair_counts, overlap)
        for sample in _as_list(overlap.get("sampleProofs")):
            if isinstance(sample, dict) and len(samples) < sample_limit:
                samples.append(_overlap_sample(overlap, sample, venue_pair=venue_pair))
    return samples, venue_pair_counts


def summarize_payload(
    payload: dict[str, Any],
    *,
    path: Path = Path("<memory>"),
    sample_limit: int = 5,
) -> dict[str, Any]:
    quality = _candidate_quality(payload)
    reason_counts: Counter[str] = Counter()
    reason_counts.update(_fixture_overlap_counts(quality))
    reason_counts.update(_zero_candidate_counts(quality))
    sample_counts, samples_by_key = _blocker_sample_counts(quality)
    reason_counts.update(sample_counts)

    samples, venue_pair_counts = _collect_samples_and_venue_pairs(
        quality,
        samples_by_key,
        sample_limit=sample_limit,
    )

    return {
        "nodeId": _node_id(payload, path),
        "artifact": str(path),
        "fixtureProofBlockerCounts": dict(sorted(reason_counts.items())),
        "venuePairBlockerCounts": {
            venue_pair: dict(sorted(counts.items()))
            for venue_pair, counts in sorted(venue_pair_counts.items())
        },
        "samples": samples[:sample_limit],
        "recommendations": _recommendations(reason_counts),
    }


def summarize_files(paths: Iterable[Path], *, sample_limit: int = 5) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    aggregate_counts: Counter[str] = Counter()
    aggregate_venue_pairs: dict[str, Counter[str]] = defaultdict(Counter)
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        node_summary = summarize_payload(payload, path=path, sample_limit=sample_limit)
        nodes.append(node_summary)
        aggregate_counts.update(node_summary["fixtureProofBlockerCounts"])
        for venue_pair, counts in node_summary["venuePairBlockerCounts"].items():
            aggregate_venue_pairs[venue_pair].update(counts)
    return {
        "artifactCount": len(nodes),
        "fixtureProofBlockerCounts": dict(sorted(aggregate_counts.items())),
        "venuePairBlockerCounts": {
            venue_pair: dict(sorted(counts.items()))
            for venue_pair, counts in sorted(aggregate_venue_pairs.items())
        },
        "nodes": nodes,
        "recommendations": _recommendations(aggregate_counts),
    }


def _recommendations(counts: Counter[str]) -> list[str]:
    recommendations: list[str] = []
    if counts.get("participant_mismatch", 0) > 0:
        recommendations.append("audit_participant_aliases_and_provider_suffixes")
    if counts.get("start_time_mismatch", 0) > 0:
        recommendations.append("audit_start_time_source_and_tolerance_by_provider")
    if counts.get("ambiguous_fixture", 0) > 0:
        recommendations.append("add_competition_or_league_disambiguation")
    if counts.get("no_common_fixture", 0) > 0:
        recommendations.append("increase_common_fixture_discovery_or_quote_capacity")
    if counts.get("sport_mismatch", 0) > 0:
        recommendations.append("audit_provider_sport_normalization")
    if counts.get("fixture_identity_mismatch", 0) > 0:
        recommendations.append("inspect_raw_fixture_identity_proofs")
    return recommendations


def _format_text(summary: dict[str, Any]) -> str:
    lines = [
        "Fixture proof audit",
        "===================",
        f"Artifacts: {summary['artifactCount']}",
        "",
        "Blockers:",
    ]
    for reason, count in summary["fixtureProofBlockerCounts"].items():
        lines.append(f"- {reason}: {count}")
    if not summary["fixtureProofBlockerCounts"]:
        lines.append("- none")
    lines.append("")
    lines.append("Venue pairs:")
    for venue_pair, counts in summary["venuePairBlockerCounts"].items():
        rendered = ", ".join(f"{reason}={count}" for reason, count in counts.items())
        lines.append(f"- {venue_pair}: {rendered}")
    if not summary["venuePairBlockerCounts"]:
        lines.append("- none")
    lines.append("")
    lines.append("Recommendations:")
    for item in summary["recommendations"]:
        lines.append(f"- {item}")
    if not summary["recommendations"]:
        lines.append("- none")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = summarize_files(args.artifacts, sample_limit=max(0, args.sample_limit))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(_format_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
