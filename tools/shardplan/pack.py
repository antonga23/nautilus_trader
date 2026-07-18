"""
First-fit-decreasing bin packing of whole sports into capacity-bounded shards.

Invariants:

- Whole-sport atomic: a sport is never split across bins.
- A sport heavier than the capacity still gets a bin (dedicated), flagged
  over-capacity for a future league-level split; it never receives co-tenants.
- Zero-weight sports produce no bin at all, so out-of-season sports drop out
  of the plan automatically and reappear when they carry instruments again.
- Deterministic: sports are sorted by descending weight with the sport name
  as the tie-break, so the same weights always produce the same bins.

"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Bin:
    """
    One shard node: an ordered set of whole sports and their combined weight.
    """

    sports: tuple[str, ...]
    weight: int
    capacity: int

    @property
    def dedicated(self) -> bool:
        return len(self.sports) == 1

    @property
    def over_capacity(self) -> bool:
        return self.weight > self.capacity

    @property
    def name(self) -> str:
        return "shard-" + "-".join(sport.replace("_", "-") for sport in self.sports)

    @property
    def sport_set(self) -> frozenset[str]:
        return frozenset(self.sports)


@dataclass(frozen=True)
class PackResult:
    """
    The full allocation: bins plus the zero-weight sports that were dropped.
    """

    bins: tuple[Bin, ...]
    dropped: tuple[str, ...]
    capacity: int


def pack(totals: Mapping[str, int], capacity: int) -> PackResult:
    """
    Pack sports into bins of at most ``capacity`` instruments via first-fit-decreasing.
    """
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    dropped = tuple(sorted(sport for sport, weight in ordered if weight <= 0))
    open_bins: list[tuple[list[str], int]] = []
    for sport, weight in ordered:
        if weight <= 0:
            continue
        placed = False
        for index, (sports, bin_weight) in enumerate(open_bins):
            if bin_weight + weight <= capacity:
                sports.append(sport)
                open_bins[index] = (sports, bin_weight + weight)
                placed = True
                break
        if not placed:
            open_bins.append(([sport], weight))
    bins = tuple(
        Bin(sports=tuple(sports), weight=weight, capacity=capacity) for sports, weight in open_bins
    )
    return PackResult(bins=bins, dropped=dropped, capacity=capacity)
