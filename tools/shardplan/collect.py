"""
Collect per-sport instrument weights from live node status probes.

Weights come from ``status.json`` -> ``runtimeProbe.venueCoverage``:

- ``nodeCounts`` is the per-venue live instrument (graph node) count.
- ``eventSportCounts`` is the per-venue event count keyed by sport, used to
  apportion each venue's instrument count across the sports it carries
  (exact for single-sport nodes, proportional for grouped nodes).
- ``quoteSubscriptionLimitExceededCounts`` is the starvation signal: how many
  quote subscriptions a venue wanted beyond its configured cap.

"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import json
from pathlib import Path
from typing import Any


@dataclass
class SportWeight:
    """
    Measured (or declared) weight of one whole sport across venues.
    """

    sport: str
    venues: dict[str, int] = field(default_factory=dict)
    total: int = 0
    starvation: int = 0


WeightTable = dict[str, SportWeight]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def apportion(total: int, shares: dict[str, int]) -> dict[str, int]:
    """
    Apportion ``total`` across ``shares`` proportionally with largest-remainder rounding
    so the parts always sum to ``total`` (ties broken by sport name).
    """
    positive = {name: count for name, count in shares.items() if count > 0}
    if total <= 0 or not positive:
        return {}
    denominator = sum(positive.values())
    quotas = {name: total * count / denominator for name, count in positive.items()}
    result = {name: int(quota) for name, quota in quotas.items()}
    remainder = total - sum(result.values())
    by_fraction = sorted(
        positive,
        key=lambda name: (-(quotas[name] - result[name]), name),
    )
    for name in by_fraction[:remainder]:
        result[name] += 1
    return result


def discover_status_paths(nodes_root: Path) -> list[Path]:
    """
    Find status payloads under a nodes root: either ``<node>/status.json``
    per-node directories (the on-host layout) or a flat directory of
    ``*.json`` status files (test fixtures, scp'd copies).
    """
    nested = sorted(nodes_root.glob("*/status.json"))
    flat = sorted(path for path in nodes_root.glob("*.json") if path.is_file())
    return nested + flat


def _venue_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    probe = _as_dict(payload.get("runtimeProbe"))
    coverage = _as_dict(probe.get("venueCoverage"))
    if coverage:
        return coverage
    return _as_dict(payload.get("venueCoverage"))


def collect_weights(nodes_root: Path) -> WeightTable:
    """
    Parse every status payload under ``nodes_root`` into a sport -> {venue ->
    instruments, total, starvation} weight table.
    """
    table: WeightTable = {}
    for path in discover_status_paths(nodes_root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        coverage = _venue_coverage(_as_dict(payload))
        if not coverage:
            continue
        node_counts = _as_dict(coverage.get("nodeCounts"))
        event_sport_counts = _as_dict(coverage.get("eventSportCounts"))
        exceeded_counts = _as_dict(coverage.get("quoteSubscriptionLimitExceededCounts"))
        for venue, sport_events in event_sport_counts.items():
            events = {str(sport): _as_int(count) for sport, count in _as_dict(sport_events).items()}
            instruments = apportion(_as_int(node_counts.get(venue)), events)
            starvation = apportion(_as_int(exceeded_counts.get(venue)), events)
            for sport, count in instruments.items():
                weight = table.setdefault(sport, SportWeight(sport=sport))
                weight.venues[venue] = weight.venues.get(venue, 0) + count
                weight.total += count
            for sport, count in starvation.items():
                weight = table.setdefault(sport, SportWeight(sport=sport))
                weight.starvation += count
    return table


def load_static_weights(path: Path) -> WeightTable:
    """
    Load a static weights JSON of the form ``{"sport": total}`` or ``{"sport": {"VENUE":

    instruments, ...}}`` for planning sports that are not deployed yet (or for manual
    overrides of measured weights).

    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Static weights file {path} must contain a JSON object")
    table: WeightTable = {}
    for sport, value in payload.items():
        name = str(sport)
        if isinstance(value, dict):
            venues = {str(venue): _as_int(count) for venue, count in value.items()}
            table[name] = SportWeight(sport=name, venues=venues, total=sum(venues.values()))
        else:
            table[name] = SportWeight(sport=name, total=_as_int(value))
    return table


def merge_weights(measured: WeightTable, overrides: WeightTable) -> WeightTable:
    """
    Merge static overrides into measured weights; an override replaces the measured
    entry for that sport wholesale.
    """
    merged = dict(measured)
    merged.update(overrides)
    return merged
