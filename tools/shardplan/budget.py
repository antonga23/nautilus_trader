"""
Plan per-venue ``quote_subscription_limit`` budgets from live node status signals.

For each (node, venue) the planner reads ``status.json`` ->
``runtimeProbe.venueCoverage``:

- ``quoteSubscriptionCounts`` / ``quoteSubscriptionLimits`` -- current usage vs cap.
- ``quoteSubscriptionLimitExceededCounts`` -- starvation: subscriptions wanted
  beyond the cap.
- ``quoteSubscriptionGapCounts`` -- waste: subscribed-but-unquoted instruments.
- ``crossVenueQuoteReadiness`` -- when every pair involving a venue reports a
  ``no_common_fixture`` status, that venue shares no fixtures with any co-located
  venue, so its whole budget is provably wasted.

Deterministic rule::

    effective_demand = max(counts + exceeded - gap, 0)
    floor            = max(1, int(current * min_floor_ratio))
    proposed         = clamp(round_up_to_10(effective_demand * headroom), floor, cap)

with ``proposed = floor`` outright when the venue has no common fixture anywhere
(a minimum probe budget so recovery is still observable).

``cap`` is a poll-capacity bound, derived for polled venues (CLOUDBET) from
``runtimeProbe.providerQuotePollStats``::

    requests_per_sec = request_count / cycle_elapsed_secs
    capacity_bound   = int(requests_per_sec * poll_target_cycle_secs
                           * subscribed_instrument_count / max(request_count, 1))

i.e. requests-per-second throughput times the target cycle length gives the
request budget per cycle, and ``subscribed_instrument_count / request_count`` is
the observed instruments-served-per-request ratio, so the product is the number
of subscriptions the poller can actually serve inside one target cycle.
Stream/websocket venues (SXBET, POLYMARKET) have no poll cycle, hence no bound.

"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from tools.shardplan import emit
from tools.shardplan.collect import discover_status_paths


DEFAULT_HEADROOM = 1.10
DEFAULT_MIN_FLOOR_RATIO = 0.25
POLLED_VENUES = frozenset({"CLOUDBET"})
BUDGET_FIELD = "quote_subscription_limit"


@dataclass(frozen=True)
class VenueSignals:
    """
    Measured subscription-budget signals for one venue on one node.
    """

    venue: str
    counts: int
    limit: int
    exceeded: int
    gap: int
    no_common_fixture_everywhere: bool
    capacity_bound: int | None


@dataclass(frozen=True)
class BudgetProposal:
    """
    One (node, venue) quote-subscription budget change with its rationale.
    """

    node: str
    venue: str
    current: int
    proposed: int
    reason: str


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _round_up_to_10(value: float) -> int:
    return math.ceil(value / 10.0) * 10


def capacity_bound(poll_stats: dict[str, Any]) -> int | None:
    request_count = _as_int(poll_stats.get("request_count"))
    cycle_elapsed_secs = float(poll_stats.get("cycle_elapsed_secs") or 0.0)
    poll_target_cycle_secs = float(poll_stats.get("poll_target_cycle_secs") or 0.0)
    subscribed = _as_int(poll_stats.get("subscribed_instrument_count"))
    if request_count <= 0 or cycle_elapsed_secs <= 0.0 or poll_target_cycle_secs <= 0.0:
        return None
    requests_per_sec = request_count / cycle_elapsed_secs
    return int(requests_per_sec * poll_target_cycle_secs * subscribed / max(request_count, 1))


def _no_common_fixture_everywhere(readiness: list[Any], venue: str) -> bool:
    involved = [
        entry
        for entry in readiness
        if isinstance(entry, dict) and venue in str(entry.get("venuePair") or "").split("->")
    ]
    return bool(involved) and all(
        "no_common_fixture" in str(entry.get("status") or "") for entry in involved
    )


def extract_signals(payload: dict[str, Any]) -> dict[str, VenueSignals]:
    """
    Extract per-venue budget signals from one node status payload.
    """
    probe = _as_dict(payload.get("runtimeProbe"))
    coverage = _as_dict(probe.get("venueCoverage"))
    limits = _as_dict(coverage.get("quoteSubscriptionLimits"))
    counts = _as_dict(coverage.get("quoteSubscriptionCounts"))
    exceeded_counts = _as_dict(coverage.get("quoteSubscriptionLimitExceededCounts"))
    gap_counts = _as_dict(coverage.get("quoteSubscriptionGapCounts"))
    readiness = coverage.get("crossVenueQuoteReadiness")
    readiness_list = readiness if isinstance(readiness, list) else []
    poll_stats = _as_dict(probe.get("providerQuotePollStats"))

    signals: dict[str, VenueSignals] = {}
    for venue in sorted(limits):
        bound = None
        if venue in POLLED_VENUES:
            bound = capacity_bound(_as_dict(poll_stats.get(venue)))
        signals[venue] = VenueSignals(
            venue=venue,
            counts=_as_int(counts.get(venue)),
            limit=_as_int(limits.get(venue)),
            exceeded=_as_int(exceeded_counts.get(venue, 0)),
            gap=_as_int(gap_counts.get(venue, 0)),
            no_common_fixture_everywhere=_no_common_fixture_everywhere(readiness_list, venue),
            capacity_bound=bound,
        )
    return signals


def propose(
    signals: VenueSignals,
    current: int,
    headroom: float = DEFAULT_HEADROOM,
    min_floor_ratio: float = DEFAULT_MIN_FLOOR_RATIO,
) -> tuple[int, str]:
    """
    Apply the deterministic budget rule to one venue's signals; returns ``(proposed,
    reason)``.
    """
    floor = max(1, int(current * min_floor_ratio))
    if signals.no_common_fixture_everywhere:
        return floor, "wasted: no_common_fixture"

    effective_demand = max(signals.counts + signals.exceeded - signals.gap, 0)
    target = _round_up_to_10(effective_demand * headroom)
    cap = signals.capacity_bound
    proposed = max(floor, min(target, cap) if cap is not None else target)

    if signals.exceeded > 0 and signals.gap < signals.exceeded:
        reason = f"starved(+{signals.exceeded}) low-gap"
    elif signals.gap > 0:
        reason = f"gap-heavy({signals.gap})"
    elif signals.counts >= signals.limit > 0:
        reason = "at-cap"
    else:
        reason = "steady"
    if cap is not None and target > cap:
        reason += f" capped({cap})"
    return proposed, reason


def load_node_manifests(manifests_dir: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    """
    Map ``node_id`` -> (path, payload) for every parseable manifest in the dir.
    """
    manifests: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        node_id = payload.get("node_id")
        if isinstance(node_id, str) and node_id:
            manifests[node_id] = (path, payload)
    return manifests


def _node_id_for_status(path: Path, payload: dict[str, Any]) -> str:
    node_id = payload.get("nodeId")
    if isinstance(node_id, str) and node_id:
        return node_id
    return path.parent.name if path.name == "status.json" else path.stem


def plan_budgets(
    nodes_root: Path,
    manifests_dir: Path,
    headroom: float = DEFAULT_HEADROOM,
    min_floor_ratio: float = DEFAULT_MIN_FLOOR_RATIO,
) -> list[BudgetProposal]:
    """
    Join node status signals to manifests and propose per-venue budgets, sorted by
    (node, venue).
    """
    manifests = load_node_manifests(manifests_dir)
    proposals: list[BudgetProposal] = []
    for status_path in discover_status_paths(nodes_root):
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        node = _node_id_for_status(status_path, payload)
        entry = manifests.get(node)
        if entry is None:
            continue
        _, manifest = entry
        current_by_venue = {
            str(venue.get("venue")): venue.get(BUDGET_FIELD)
            for venue in manifest.get("venues") or []
            if isinstance(venue, dict)
        }
        for venue, signals in extract_signals(payload).items():
            current = current_by_venue.get(venue)
            if not isinstance(current, int):
                continue
            proposed, reason = propose(signals, current, headroom, min_floor_ratio)
            proposals.append(
                BudgetProposal(
                    node=node,
                    venue=venue,
                    current=current,
                    proposed=proposed,
                    reason=reason,
                ),
            )
    return sorted(proposals, key=lambda proposal: (proposal.node, proposal.venue))


def format_proposals(proposals: list[BudgetProposal]) -> str:
    lines = [f"{'node':<40} {'venue':<12} {'current':>8} {'proposed':>9}  reason"]
    for proposal in proposals:
        lines.append(
            f"{proposal.node:<40} {proposal.venue:<12} "
            f"{proposal.current:>8} {proposal.proposed:>9}  {proposal.reason}",
        )
    return "\n".join(lines)


def apply_proposals(
    proposals: list[BudgetProposal],
    manifests_dir: Path,
    template_path: Path = emit.TEMPLATE_PATH,
) -> list[Path]:
    """
    Rewrite ``quote_subscription_limit`` in each affected manifest and validate every
    rewritten file end-to-end; returns the rewritten paths.
    """
    manifests = load_node_manifests(manifests_dir)
    changed_by_node: dict[str, dict[str, int]] = {}
    for proposal in proposals:
        if proposal.proposed != proposal.current:
            changed_by_node.setdefault(proposal.node, {})[proposal.venue] = proposal.proposed
    rewritten: list[Path] = []
    for node in sorted(changed_by_node):
        path, payload = manifests[node]
        for venue in payload.get("venues") or []:
            if not isinstance(venue, dict):
                continue
            proposed = changed_by_node[node].get(str(venue.get("venue")))
            if proposed is not None and isinstance(venue.get(BUDGET_FIELD), int):
                venue[BUDGET_FIELD] = proposed
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        emit.validate_manifest_file(path, template_path=template_path)
        rewritten.append(path)
    return rewritten
