# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2023 . All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

from dataclasses import dataclass


RECORD_TTL_NS = 24 * 60 * 60 * 1_000_000_000


@dataclass
class TombstoneRecord:
    miss_count: int = 0
    first_miss_at_ns: int = 0
    last_miss_at_ns: int = 0
    next_revalidate_at_ns: int = 0
    reason: str = ""


class MarketPollabilityRegistry:
    """
    Tracks Cloudbet (event_id, market_key) pairs that repeatedly fail to poll so the
    quote-poll scheduler can suppress them without touching subscription membership.
    """

    def __init__(
        self,
        miss_threshold: int = 3,
        revalidate_secs: float = 600.0,
        market_key_event_threshold: int = 3,
    ) -> None:
        self._miss_threshold = max(1, int(miss_threshold))
        self._revalidate_ns = int(max(0.0, float(revalidate_secs)) * 1_000_000_000)
        self._market_key_event_threshold = max(1, int(market_key_event_threshold))
        self._records: dict[tuple[int, str], TombstoneRecord] = {}
        self._events_by_market_key: dict[str, set[int]] = {}

    def record_miss(
        self,
        event_id: int,
        market_key: str,
        *,
        reason: str,
        now_ns: int,
    ) -> bool:
        key = (event_id, market_key)
        record = self._records.get(key)
        if record is None:
            record = TombstoneRecord(first_miss_at_ns=now_ns)
            self._records[key] = record
        record.miss_count += 1
        record.last_miss_at_ns = now_ns
        record.reason = reason
        if record.miss_count < self._miss_threshold:
            return False
        record.next_revalidate_at_ns = now_ns + self._revalidate_ns
        if record.miss_count > self._miss_threshold:
            return False
        self._events_by_market_key.setdefault(market_key, set()).add(event_id)
        return True

    def record_success(self, event_id: int, market_key: str) -> bool:
        record = self._records.pop((event_id, market_key), None)
        events = self._events_by_market_key.get(market_key)
        if events is not None:
            events.discard(event_id)
            if not events:
                self._events_by_market_key.pop(market_key, None)
        return record is not None and record.miss_count >= self._miss_threshold

    def is_poll_suppressed(self, event_id: int, market_key: str, now_ns: int) -> bool:
        record = self._records.get((event_id, market_key))
        if record is None or record.miss_count < self._miss_threshold:
            return False
        return now_ns < record.next_revalidate_at_ns

    def claim_revalidation_probe(self, event_id: int, market_key: str, now_ns: int) -> bool:
        record = self._records.get((event_id, market_key))
        if record is None or record.miss_count < self._miss_threshold:
            return False
        if now_ns < record.next_revalidate_at_ns:
            return False
        record.next_revalidate_at_ns = now_ns + self._revalidate_ns
        return True

    def is_market_key_unpollable(self, market_key: str) -> bool:
        events = self._events_by_market_key.get(market_key)
        return events is not None and len(events) >= self._market_key_event_threshold

    def exclude_from_discovery(self, event_id: int, market_key: str, now_ns: int) -> bool:
        record = self._records.get((event_id, market_key))
        tombstoned = record is not None and record.miss_count >= self._miss_threshold
        if not self.is_market_key_unpollable(market_key):
            return tombstoned
        if not tombstoned:
            return True
        # Escalated market key: admit a single canary event — the lexicographically
        # smallest currently-tombstoned event_id — once its own revalidation is due,
        # so a venue-wide market that comes back is eventually rediscovered. Read-only:
        # the poll-side probe claim stays the only prober.
        events = self._events_by_market_key[market_key]
        if event_id != min(events, key=str):
            return True
        return now_ns < record.next_revalidate_at_ns

    def prune_expired(self, now_ns: int) -> int:
        cutoff = now_ns - RECORD_TTL_NS
        expired = [
            key for key, record in self._records.items() if record.last_miss_at_ns < cutoff
        ]
        for event_id, market_key in expired:
            self._records.pop((event_id, market_key), None)
            events = self._events_by_market_key.get(market_key)
            if events is not None:
                events.discard(event_id)
                if not events:
                    self._events_by_market_key.pop(market_key, None)
        return len(expired)

    def snapshot(self) -> dict[str, int]:
        tombstoned = sum(
            1 for record in self._records.values() if record.miss_count >= self._miss_threshold
        )
        return {
            "tracked_market_count": len(self._records),
            "tombstoned_market_count": tombstoned,
            "unpollable_market_key_count": sum(
                1
                for events in self._events_by_market_key.values()
                if len(events) >= self._market_key_event_threshold
            ),
        }

    def clear(self) -> None:
        self._records.clear()
        self._events_by_market_key.clear()
