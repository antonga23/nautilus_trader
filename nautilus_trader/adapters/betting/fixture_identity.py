# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
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
"""
Venue-agnostic fixture identity resolution for betting instruments.

Cross-venue matching cannot rely on provider event identifiers: Cloudbet, SXBET,
and Polymarket all assign independent IDs and their display names drift. This
module centralizes the conservative name/alias/start-time proof used by runtime
matching, graph topology, and diagnostics.

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
import re
from typing import Any
import unicodedata


DEFAULT_START_TIME_TOLERANCE_SECS = 2 * 60 * 60
DEFAULT_SOFT_CROSS_VENUE_START_TIME_TOLERANCE_SECS = 12 * 60 * 60


@dataclass(frozen=True)
class FixtureIdentityProof:
    """
    Auditable proof for deciding whether two instruments represent one fixture.
    """

    same_fixture: bool
    reason: str
    confidence: float
    canonical_event_key_a: str
    canonical_event_key_b: str
    alias_hits: tuple[str, ...] = ()
    matched_fields: tuple[str, ...] = ()
    start_time_delta_secs: float | None = None
    ambiguous: bool = False
    blocker_reason: str | None = None


@dataclass(frozen=True)
class ParticipantMatch:
    matched: bool
    confidence: float
    alias_hits: tuple[str, ...] = ()
    reason: str = "participant_match"


class FixtureIdentityResolver:
    """
    Resolve fixture identity across venues without provider event-ID equality.
    """

    IGNORED_TEAM_TOKENS = frozenset(
        {
            "afc",
            "cf",
            "club",
            "fc",
            "sc",
            "team",
            "the",
            "s",
            "w",
            "women",
        },
    )
    TOKEN_ALIASES = {
        "ari": "arizona",
        "atl": "atlanta",
        "bal": "baltimore",
        "bkn": "brooklyn",
        "bos": "boston",
        "buf": "buffalo",
        "car": "carolina",
        "cha": "charlotte",
        "chi": "chicago",
        "cin": "cincinnati",
        "cle": "cleveland",
        "col": "colorado",
        "dal": "dallas",
        "den": "denver",
        "det": "detroit",
        "gb": "green bay",
        "gs": "golden state",
        "gsw": "golden state",
        "hou": "houston",
        "ind": "indianapolis",
        "jax": "jacksonville",
        "kc": "kansas city",
        "la": "los angeles",
        "lac": "los angeles",
        "lal": "los angeles",
        "lar": "los angeles",
        "lv": "las vegas",
        "mem": "memphis",
        "mia": "miami",
        "mil": "milwaukee",
        "min": "minnesota",
        "ne": "new england",
        "no": "new orleans",
        "nop": "new orleans",
        "ny": "new york",
        "nyc": "new york",
        "nyg": "new york",
        "nyj": "new york",
        "oak": "oakland",
        "okc": "oklahoma city",
        "orl": "orlando",
        "phi": "philadelphia",
        "phx": "phoenix",
        "pit": "pittsburgh",
        "por": "portland",
        "sac": "sacramento",
        "sa": "san antonio",
        "sas": "san antonio",
        "sd": "san diego",
        "sf": "san francisco",
        "sea": "seattle",
        "stl": "st louis",
        "tb": "tampa bay",
        "ten": "tennessee",
        "tor": "toronto",
        "utd": "united",
        "uta": "utah",
        "was": "washington",
        "wsh": "washington",
    }
    GEOGRAPHIC_PREFIX_TOKENS = frozenset(
        {
            "golden",
            "green",
            "las",
            "los",
            "new",
            "oklahoma",
            "san",
            "st",
            "tampa",
            "west",
        },
    )
    GENERIC_SINGLE_TOKEN_ALIASES = (
        IGNORED_TEAM_TOKENS
        | GEOGRAPHIC_PREFIX_TOKENS
        | frozenset(
            {
                "city",
                "county",
                "east",
                "north",
                "south",
                "state",
                "town",
                "united",
            },
        )
    )
    # Tokens that live in GENERIC_SINGLE_TOKEN_ALIASES yet are the distinctive short
    # name a venue emits for a full club ("United" for "Manchester United"/"West Ham
    # United"). Geographic descriptors such as "city"/"state" stay non-distinctive so
    # "New York City"/"Kansas City" cannot collapse onto a bare "City".
    DISTINCTIVE_SUBSET_TOKENS = frozenset({"united"})
    PHRASE_ALIASES = {
        "cle cavaliers": "cleveland cavaliers",
        "cle cavs": "cleveland cavaliers",
        "ind pacers": "indiana pacers",
        "lal lakers": "los angeles lakers",
        "mem grizzlies": "memphis grizzlies",
        "mil bucks": "milwaukee bucks",
        "min timberwolves": "minnesota timberwolves",
        "mn timberwolves": "minnesota timberwolves",
        "nop pelicans": "new orleans pelicans",
        "ny knicks": "new york knicks",
        "ny liberty": "new york liberty",
        "okc thunder": "oklahoma city thunder",
        "orl magic": "orlando magic",
        "por trail blazers": "portland trail blazers",
        "sac kings": "sacramento kings",
        "sa spurs": "san antonio spurs",
        "sas spurs": "san antonio spurs",
        "s a spurs": "san antonio spurs",
        "tor raptors": "toronto raptors",
        "uta jazz": "utah jazz",
        "man city": "manchester city",
        "man utd": "manchester united",
        "man united": "manchester united",
    }
    MARKET_GROUP_SUFFIXES = (
        " exact score",
        " correct score",
        " more markets",
        " alternate lines",
        " alt lines",
        " match odds",
        " moneyline",
        " winner",
        " total goals",
        " total corners",
        " team corners",
        " corners",
        " total cards",
        " team cards",
        " cards",
        " team total",
        " player props",
        " props",
    )
    SPORT_ALIASES = {
        "american football": "american_football",
        "football": "soccer",
        "football soccer": "soccer",
        "association football": "soccer",
        "futbol": "soccer",
        "soccer football": "soccer",
        "ice hockey": "ice_hockey",
        "hockey": "ice_hockey",
    }
    LETTER_SPACED_ALIASES = {
        "l a": "la",
        "n e": "ne",
        "n o": "no",
        "n y": "ny",
        "s a": "sa",
        "s d": "sd",
        "s f": "sf",
    }
    EVENT_SPLIT_PATTERN = re.compile(
        r"\s+(?:v\.?|vs\.?|versus|@|at)\s+|\s+[-/]\s+",
        re.IGNORECASE,
    )

    def __init__(
        self,
        start_time_tolerance_secs: int = DEFAULT_START_TIME_TOLERANCE_SECS,
        soft_cross_venue_start_time_tolerance_secs: int = (
            DEFAULT_SOFT_CROSS_VENUE_START_TIME_TOLERANCE_SECS
        ),
    ) -> None:
        self.start_time_tolerance_secs = start_time_tolerance_secs
        self.soft_cross_venue_start_time_tolerance_secs = soft_cross_venue_start_time_tolerance_secs

    @staticmethod
    def normalize_event_component(value: str | None) -> str:
        if not value:
            return ""
        folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        normalized = re.sub(r"[^a-z0-9]+", " ", folded.lower())
        return " ".join(normalized.split())

    def normalize_sport(self, sport: str | None) -> str:
        normalized = self.normalize_event_component(sport).replace(" ", "_")
        return self.SPORT_ALIASES.get(normalized.replace("_", " "), normalized)

    def normalize_team_name(self, name: str | None) -> str:
        normalized = self.normalize_event_component(name)
        if not normalized:
            return ""
        normalized = self._strip_market_group_suffix(normalized)
        normalized = self._compact_letter_spaced_aliases(normalized)
        normalized = self.PHRASE_ALIASES.get(normalized, normalized)
        tokens: list[str] = []
        alias_hits: list[str] = []
        for token in normalized.split():
            if token in self.IGNORED_TEAM_TOKENS:
                continue
            replacement = self.TOKEN_ALIASES.get(token, token)
            if replacement != token:
                alias_hits.append(f"{token}->{replacement}")
            tokens.extend(replacement.split())
        canonical = " ".join(tokens)
        return self.PHRASE_ALIASES.get(canonical, canonical)

    def _compact_letter_spaced_aliases(self, normalized: str) -> str:
        """
        Recover dotted or spaced city abbreviations before token alias expansion.

        Provider names like "L.A. Clippers" normalize to "l a clippers".
        Without this pass they miss the existing ``la -> los angeles`` alias.

        """
        compacted = normalized
        for alias, replacement in self.LETTER_SPACED_ALIASES.items():
            compacted = re.sub(
                rf"(?<!\w){re.escape(alias)}(?!\w)",
                replacement,
                compacted,
            )
        return " ".join(compacted.split())

    def _strip_market_group_suffix(self, normalized: str) -> str:
        """
        Remove provider UI suffixes accidentally appended to fixture participants.

        Polymarket/Gamma event titles sometimes expose market collection labels like
        "Arsenal Exact Score v West Ham United" or "Arsenal More Markets v West Ham
        United". Those labels are not team identity and should not split cross-venue
        fixture matching.

        """
        cleaned = normalized
        changed = True
        while changed:
            changed = False
            for suffix in self.MARKET_GROUP_SUFFIXES:
                if cleaned.endswith(suffix):
                    cleaned = cleaned[: -len(suffix)].strip()
                    changed = True
                    break
        return cleaned

    def team_key(self, instrument: Any) -> tuple[str, ...]:
        home_name = str(getattr(instrument, "home_name", "") or "")
        away_name = str(getattr(instrument, "away_name", "") or "")
        participants = sorted(
            {
                self.normalize_team_name(home_name),
                self.normalize_team_name(away_name),
            }
            - {""},
        )
        if participants:
            return tuple(participants)
        event_name = str(getattr(instrument, "event_name", "") or "")
        for title in self._fixture_title_candidates(event_name):
            split_names = [
                self.normalize_team_name(part)
                for part in self.EVENT_SPLIT_PATTERN.split(title, maxsplit=1)
            ]
            split_participants = sorted(set(split_names) - {""})
            if len(split_participants) >= 2:
                return tuple(split_participants[:2])
        normalized_event = self.normalize_event_component(event_name)
        return (normalized_event,) if normalized_event else ()

    def team_aliases(self, name: str | None) -> tuple[str, ...]:
        canonical = self.normalize_team_name(name)
        if not canonical:
            return ()
        aliases = {canonical}
        prefix = self._participant_prefix_alias(canonical)
        if prefix:
            aliases.add(prefix)
        return tuple(sorted(aliases))

    def participant_alias_sets(self, instrument: Any) -> tuple[tuple[str, ...], ...]:
        """
        Return participant aliases from explicit home/away fields or event title.

        Some provider payloads expose reliable participant names only in the event
        title. We still need the same alias expansion there so "CLE Cavaliers @ MIN
        Timberwolves" can match a venue that sends full home/away fields.

        """
        home_aliases = self.team_aliases(str(getattr(instrument, "home_name", "") or ""))
        away_aliases = self.team_aliases(str(getattr(instrument, "away_name", "") or ""))
        if home_aliases and away_aliases:
            return (home_aliases, away_aliases)

        event_name = str(getattr(instrument, "event_name", "") or "")
        for title in self._fixture_title_candidates(event_name):
            split_names = [
                self.team_aliases(part)
                for part in self.EVENT_SPLIT_PATTERN.split(title, maxsplit=1)
            ]
            split_aliases = tuple(alias_set for alias_set in split_names if alias_set)
            if len(split_aliases) >= 2:
                return split_aliases[:2]
        return ()

    def _fixture_title_candidates(self, event_name: str) -> tuple[str, ...]:
        """
        Prefer fixture-looking title segments over provider competition prefixes.

        Polymarket/Gamma often labels events as
        ``Tournament: Player A vs Player B``. If explicit home/away fields are
        missing, splitting the full title would incorrectly include the
        tournament as part of Player A. Keep the original as a fallback, but try
        the trailing colon segment first when it contains a fixture separator.

        """
        title = str(event_name or "").strip()
        if not title:
            return ()
        candidates: list[str] = []
        if ":" in title:
            suffix = title.rsplit(":", maxsplit=1)[-1].strip()
            if suffix and self.EVENT_SPLIT_PATTERN.search(suffix):
                candidates.append(suffix)
        candidates.append(title)
        return tuple(dict.fromkeys(candidates))

    def event_alias_keys(
        self,
        instrument: Any,
        *,
        include_start_time: bool = False,
    ) -> tuple[str, ...]:
        sport = self.normalize_sport(str(getattr(instrument, "sport_name", "") or ""))
        participant_aliases = self.participant_alias_sets(instrument)
        if not sport or len(participant_aliases) < 2:
            key = self.event_key(instrument, include_start_time=include_start_time)
            return (key,) if key else ()
        home_aliases, away_aliases = participant_aliases[:2]
        keys = self._participant_event_alias_keys(
            sport,
            home_aliases,
            away_aliases,
            suffix=self._event_alias_suffix(instrument, include_start_time=include_start_time),
        )
        exact = self.event_key(instrument, include_start_time=include_start_time)
        if exact:
            keys.add(exact)
        return tuple(sorted(keys))

    def _event_alias_suffix(self, instrument: Any, *, include_start_time: bool) -> str:
        if include_start_time:
            start_time = self.parsed_start_time(instrument)
            if start_time is not None:
                return f":{start_time.strftime('%Y-%m-%dT%H')}"
        return ""

    @staticmethod
    def _participant_event_alias_keys(
        sport: str,
        home_aliases: tuple[str, ...],
        away_aliases: tuple[str, ...],
        *,
        suffix: str,
    ) -> set[str]:
        keys: set[str] = set()
        for home_alias in home_aliases:
            for away_alias in away_aliases:
                participants = sorted({home_alias, away_alias})
                if len(participants) >= 2:
                    keys.add(f"{sport}:{participants[0]}:{participants[1]}{suffix}")
        return keys

    def event_key(self, instrument: Any, *, include_start_time: bool = True) -> str:
        parts = [self.normalize_sport(str(getattr(instrument, "sport_name", "") or ""))]
        parts.extend(self.team_key(instrument))
        start_time = self.parsed_start_time(instrument)
        if include_start_time and start_time is not None:
            parts.append(start_time.strftime("%Y-%m-%dT%H"))
        return ":".join(part for part in parts if part)

    @staticmethod
    def parsed_start_time(instrument: Any) -> datetime | None:
        start = getattr(instrument, "start_time", None)
        if not start:
            return None
        start_text = str(start).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(start_text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def resolve(self, instrument_a: Any, instrument_b: Any) -> FixtureIdentityProof:
        key_a = self.event_key(instrument_a, include_start_time=False)
        key_b = self.event_key(instrument_b, include_start_time=False)
        provider_match = self._provider_event_id_match_proof(
            instrument_a,
            instrument_b,
            key_a,
            key_b,
        )
        if provider_match is not None:
            return provider_match
        sport_a = self.normalize_sport(str(getattr(instrument_a, "sport_name", "") or ""))
        sport_b = self.normalize_sport(str(getattr(instrument_b, "sport_name", "") or ""))
        if sport_a != sport_b:
            return self._blocked_fixture_proof(key_a, key_b, reason="sport_mismatch")
        participant_match = self._participants_match(
            self.team_key(instrument_a),
            self.team_key(instrument_b),
        )
        if not participant_match.matched:
            return self._participant_mismatch_proof(key_a, key_b, participant_match)
        start_delta = self._start_time_delta_secs(instrument_a, instrument_b)
        if self._is_start_time_mismatch(start_delta):
            if not self._allow_cross_venue_start_time_conflict(
                instrument_a,
                instrument_b,
                key_a=key_a,
                key_b=key_b,
                participant_match=participant_match,
                start_delta_secs=start_delta,
            ):
                return self._start_time_mismatch_proof(
                    key_a,
                    key_b,
                    participant_match,
                    start_delta,
                )
            return self._canonical_match_proof(
                instrument_a,
                instrument_b,
                key_a,
                key_b,
                participant_match,
                start_delta,
                start_time_conflict=True,
            )
        return self._canonical_match_proof(
            instrument_a,
            instrument_b,
            key_a,
            key_b,
            participant_match,
            start_delta,
        )

    @staticmethod
    def _provider_event_id_match_proof(
        instrument_a: Any,
        instrument_b: Any,
        key_a: str,
        key_b: str,
    ) -> FixtureIdentityProof | None:
        venue_a = str(getattr(getattr(instrument_a, "venue_name", ""), "value", "") or "")
        venue_b = str(getattr(getattr(instrument_b, "venue_name", ""), "value", "") or "")
        event_id_a = str(getattr(instrument_a, "event_id", "") or "")
        event_id_b = str(getattr(instrument_b, "event_id", "") or "")
        if not (venue_a and venue_a == venue_b and event_id_a and event_id_a == event_id_b):
            return None
        return FixtureIdentityProof(
            same_fixture=True,
            reason="provider_event_id_match",
            confidence=1.0,
            canonical_event_key_a=key_a,
            canonical_event_key_b=key_b,
            matched_fields=("venue", "event_id"),
        )

    @staticmethod
    def _blocked_fixture_proof(key_a: str, key_b: str, *, reason: str) -> FixtureIdentityProof:
        return FixtureIdentityProof(
            same_fixture=False,
            reason=reason,
            confidence=0.0,
            canonical_event_key_a=key_a,
            canonical_event_key_b=key_b,
            blocker_reason=reason,
        )

    @staticmethod
    def _participant_mismatch_proof(
        key_a: str,
        key_b: str,
        participant_match: ParticipantMatch,
    ) -> FixtureIdentityProof:
        return FixtureIdentityProof(
            same_fixture=False,
            reason=participant_match.reason,
            confidence=participant_match.confidence,
            canonical_event_key_a=key_a,
            canonical_event_key_b=key_b,
            alias_hits=participant_match.alias_hits,
            blocker_reason="participant_mismatch",
        )

    def _start_time_delta_secs(self, instrument_a: Any, instrument_b: Any) -> float | None:
        start_a = self.parsed_start_time(instrument_a)
        start_b = self.parsed_start_time(instrument_b)
        return (
            abs((start_a - start_b).total_seconds())
            if start_a is not None and start_b is not None
            else None
        )

    def _is_start_time_mismatch(self, start_delta_secs: float | None) -> bool:
        return start_delta_secs is not None and start_delta_secs > self.start_time_tolerance_secs

    @staticmethod
    def _start_time_mismatch_proof(
        key_a: str,
        key_b: str,
        participant_match: ParticipantMatch,
        start_delta_secs: float | None,
    ) -> FixtureIdentityProof:
        return FixtureIdentityProof(
            same_fixture=False,
            reason="start_time_mismatch",
            confidence=min(participant_match.confidence, 0.35),
            canonical_event_key_a=key_a,
            canonical_event_key_b=key_b,
            alias_hits=participant_match.alias_hits,
            start_time_delta_secs=start_delta_secs,
            blocker_reason="start_time_mismatch",
        )

    def _allow_cross_venue_start_time_conflict(
        self,
        instrument_a: Any,
        instrument_b: Any,
        *,
        key_a: str,
        key_b: str,
        participant_match: ParticipantMatch,
        start_delta_secs: float | None,
    ) -> bool:
        if start_delta_secs is None:
            return False
        venue_a = str(getattr(getattr(instrument_a, "venue_name", ""), "value", "") or "")
        venue_b = str(getattr(getattr(instrument_b, "venue_name", ""), "value", "") or "")
        if venue_a and venue_a == venue_b:
            return False
        if self._is_date_only_cross_venue_start_time_match(
            instrument_a,
            instrument_b,
            key_a=key_a,
            key_b=key_b,
            participant_match=participant_match,
        ):
            return True
        if start_delta_secs > self.soft_cross_venue_start_time_tolerance_secs:
            return False
        if participant_match.confidence < 0.84:
            return False
        if self._competitions_match(instrument_a, instrument_b):
            return True
        return bool(key_a and key_a == key_b and participant_match.confidence >= 0.9)

    def _is_date_only_cross_venue_start_time_match(
        self,
        instrument_a: Any,
        instrument_b: Any,
        *,
        key_a: str,
        key_b: str,
        participant_match: ParticipantMatch,
    ) -> bool:
        if participant_match.confidence < 0.9:
            return False
        if not (key_a and key_a == key_b):
            return False
        start_a = self.parsed_start_time(instrument_a)
        start_b = self.parsed_start_time(instrument_b)
        if start_a is None or start_b is None:
            return False
        if start_a.date() != start_b.date():
            return False
        return self._is_date_only_midnight(start_a) or self._is_date_only_midnight(start_b)

    @staticmethod
    def _is_date_only_midnight(value: datetime) -> bool:
        return value.hour == 0 and value.minute == 0 and value.second == 0

    def _canonical_match_proof(
        self,
        instrument_a: Any,
        instrument_b: Any,
        key_a: str,
        key_b: str,
        participant_match: ParticipantMatch,
        start_delta_secs: float | None,
        *,
        start_time_conflict: bool = False,
    ) -> FixtureIdentityProof:
        matched_fields = ["sport", "participants"]
        confidence = participant_match.confidence
        reason = "canonical_fixture_match"
        if start_delta_secs is not None and not start_time_conflict:
            matched_fields.append("start_time")
            confidence = min(0.98, confidence + 0.04)
        elif start_time_conflict:
            reason = "canonical_fixture_match_start_time_conflict"
            confidence = min(0.9, confidence)
        else:
            reason = "canonical_fixture_match_missing_start_time"
            confidence = min(confidence, 0.86)
        if self._competitions_match(instrument_a, instrument_b):
            matched_fields.append("competition")
            confidence = min(0.99, confidence + 0.01)
        return FixtureIdentityProof(
            same_fixture=True,
            reason=reason,
            confidence=confidence,
            canonical_event_key_a=key_a,
            canonical_event_key_b=key_b,
            alias_hits=participant_match.alias_hits,
            matched_fields=tuple(matched_fields),
            start_time_delta_secs=start_delta_secs,
        )

    def _competitions_match(self, instrument_a: Any, instrument_b: Any) -> bool:
        competition_a = self.normalize_event_component(
            str(getattr(instrument_a, "competition_name", "") or ""),
        )
        competition_b = self.normalize_event_component(
            str(getattr(instrument_b, "competition_name", "") or ""),
        )
        return bool(competition_a and competition_b and competition_a == competition_b)

    def _participants_match(
        self,
        participants_a: tuple[str, ...],
        participants_b: tuple[str, ...],
    ) -> ParticipantMatch:
        if participants_a == participants_b and len(participants_a) >= 2:
            return ParticipantMatch(True, 0.95, reason="exact_participants")
        if len(participants_a) != len(participants_b) or len(participants_a) < 2:
            return ParticipantMatch(False, 0.0, reason="participant_count_mismatch")

        remaining = list(participants_b)
        alias_hits: list[str] = []
        confidences: list[float] = []
        for participant in participants_a:
            best_index, best_confidence, best_alias = self._best_participant_match(
                participant,
                remaining,
            )
            if best_index < 0 or best_confidence < 0.72:
                return ParticipantMatch(
                    False,
                    best_confidence,
                    tuple(alias_hits),
                    reason="participant_mismatch",
                )
            matched = remaining.pop(best_index)
            confidences.append(best_confidence)
            if best_alias:
                alias_hits.append(f"{participant}<->{matched}:{best_alias}")
        return ParticipantMatch(
            True,
            min(confidences) if confidences else 0.0,
            tuple(alias_hits),
            reason="compatible_participants",
        )

    def _best_participant_match(
        self,
        participant: str,
        remaining: list[str],
    ) -> tuple[int, float, str]:
        best_index = -1
        best_confidence = 0.0
        best_alias = ""
        for index, candidate in enumerate(remaining):
            confidence, alias = self._participant_similarity(participant, candidate)
            if confidence > best_confidence:
                best_index = index
                best_confidence = confidence
                best_alias = alias
        return best_index, best_confidence, best_alias

    @staticmethod
    def _participant_similarity(left: str, right: str) -> tuple[float, str]:
        if left == right:
            return 0.95, ""
        if not left or not right:
            return 0.0, ""
        if left.startswith(f"{right} ") or right.startswith(f"{left} "):
            return 0.86, "prefix"
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        if FixtureIdentityResolver._is_specific_token_subset(left_tokens, right_tokens):
            return 0.84, "token_subset"
        if FixtureIdentityResolver._is_specific_token_subset(right_tokens, left_tokens):
            return 0.84, "token_subset"
        overlap = left_tokens & right_tokens
        if len(overlap) >= 2:
            distinctive_overlap = overlap - FixtureIdentityResolver.GEOGRAPHIC_PREFIX_TOKENS
            if len(distinctive_overlap) < 2:
                return 0.0, ""
            denominator = max(len(left_tokens), len(right_tokens), 1)
            return max(0.74, len(overlap) / denominator), "token_overlap"
        return 0.0, ""

    @classmethod
    def _is_specific_token_subset(cls, subset: set[str], superset: set[str]) -> bool:
        if not subset or not superset or subset == superset:
            return False
        if not subset < superset:
            return False
        if not subset <= cls.GENERIC_SINGLE_TOKEN_ALIASES:
            return True
        return bool(subset & cls.DISTINCTIVE_SUBSET_TOKENS)

    def _participant_prefix_alias(self, canonical: str) -> str:
        tokens = canonical.split()
        if len(tokens) < 2:
            return ""
        if tokens[0] in self.GEOGRAPHIC_PREFIX_TOKENS and len(tokens) >= 2:
            return " ".join(tokens[:2])
        return tokens[0]


DEFAULT_FIXTURE_IDENTITY_RESOLVER = FixtureIdentityResolver()
