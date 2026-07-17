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
# skipcq
# Large matcher surface retained for backward-compatible public APIs while fixture-identity
# logic moves into dedicated helpers.
"""
MarketMatcher - Cross-venue market matching for arbitrage detection.
"""

from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal

from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.fixture_identity import DEFAULT_FIXTURE_IDENTITY_RESOLVER
from nautilus_trader.adapters.betting.fixture_identity import FixtureIdentityProof
from nautilus_trader.adapters.betting.fixture_identity import FixtureIdentityResolver
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics import PromotionStatus
from nautilus_trader.adapters.betting.semantics import RelationshipType
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import MinedRule
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate


@dataclass(slots=True)
class ArbitrageOpportunity:
    """
    Represents a detected arbitrage opportunity between two selections.
    """

    instrument_a: CryptoBettingInstrument
    instrument_b: CryptoBettingInstrument
    probability_a: Decimal
    probability_b: Decimal
    total_probability: Decimal
    profit_margin: Decimal
    odds_a: Decimal
    odds_b: Decimal
    is_same_venue: bool
    match_type: str  # "same_market", "cross_market", "cross_venue"
    raw_probability_a: Decimal | None = None
    raw_probability_b: Decimal | None = None
    raw_total_probability: Decimal | None = None
    raw_profit_margin: Decimal | None = None
    fee_adjusted: bool = False
    fee_drag: Decimal = Decimal(0)
    fee_adjusted_odds_a: Decimal | None = None
    fee_adjusted_odds_b: Decimal | None = None
    taker_fee_rate_a: Decimal = Decimal(0)
    taker_fee_rate_b: Decimal = Decimal(0)
    maker_rebate_rate_a: Decimal = Decimal(0)
    maker_rebate_rate_b: Decimal = Decimal(0)
    winning_profit_fee_rate_a: Decimal = Decimal(0)
    winning_profit_fee_rate_b: Decimal = Decimal(0)
    basket_rebate_rate: Decimal = Decimal(0)
    basket_boost_rate: Decimal = Decimal(0)

    @property
    def is_arbitrage(self) -> bool:
        """
        Check if this represents a true arbitrage (profit_margin > 0).
        """
        return self.profit_margin > 0

    def __repr__(self) -> str:
        return (
            f"ArbitrageOpportunity("
            f"{self.instrument_a.outcome} @ {self.odds_a:.2f} vs "
            f"{self.instrument_b.outcome} @ {self.odds_b:.2f}, "
            f"profit={float(self.profit_margin) * 100:.2f}%, "
            f"type={self.match_type})"
        )


@dataclass(slots=True)
class HedgeCandidate:
    """
    A candidate instrument for hedging.
    """

    instrument: CryptoBettingInstrument
    match_type: str
    confidence: float  # 0-1, how confident we are this is a valid hedge
    rule_id: str | None = None
    template_id: str | None = None
    relationship_type: str | None = None
    caveats: tuple[str, ...] = ()
    push_capable: bool = False
    partial_settlement: bool = False
    execution_safe: bool = True
    same_venue_execution_eligible: bool = False
    safety_tier: str = SafetyTier.AUDIT_ONLY.value
    promotion_status: str = PromotionStatus.CANDIDATE.value


@dataclass(frozen=True)
class HedgeEventMatchDecision:
    """
    Fixture identity decision used before semantic hedge classification.
    """

    matched: bool
    reason: str
    proof: FixtureIdentityProof
    same_venue: bool


class MarketMatcher:
    """
    Finds hedging opportunities across selections and venues.

    This class identifies pairs of selections that can be used together
    to create arbitrage opportunities or hedge positions.

    Examples
    --------
    >>> matcher = MarketMatcher()
    >>> hedges = matcher.find_hedges(instrument, all_instruments)
    >>> for candidate in hedges:
    ...     opportunity = matcher.check_arbitrage(instrument, candidate.instrument)
    ...     if opportunity.is_arbitrage:
    ...         print(f"Found arbitrage: {opportunity.profit_margin:.2%}")

    """

    PUSH_CAPABLE_MARKETS = frozenset(
        {
            MarketType.DRAW_NO_BET,
            MarketType.ASIAN_HANDICAP,
        },
    )

    def __init__(
        self,
        min_confidence: float = 0.5,
        rule_classifier: RuleClassifier | None = None,
        rule_store: RuleStore | None = None,
        fixture_identity_resolver: FixtureIdentityResolver | None = None,
        allow_unpromoted_topology: bool = True,
        execute_void_compatible_middles: bool = False,
    ) -> None:
        """
        Initialize the MarketMatcher.

        Parameters
        ----------
        min_confidence : float, default 0.5
            Minimum confidence threshold for hedge candidates.
        rule_classifier : RuleClassifier, optional
            Semantic classifier for market settlement relationships.
        rule_store : RuleStore, optional
            Persisted semantic rules/templates used for runtime gating.
        fixture_identity_resolver : FixtureIdentityResolver, optional
            Venue-agnostic fixture identity resolver. Cross-venue matching uses
            resolver proofs instead of raw event names or provider event IDs.
        allow_unpromoted_topology : bool, default True
            Whether candidate-only semantic edges can be exposed as non-executable topology.
        execute_void_compatible_middles : bool, default False
            When True, a positive-EV ``VOID_COMPATIBLE_HEDGE`` with only void/push
            settlement risk is elevated by the promotion tier instead of being demoted for
            void handling. Off by default so tier resolution is unchanged.

        """
        self.min_confidence = min_confidence
        self._rule_classifier = rule_classifier or RuleClassifier()
        self._rule_store = rule_store
        self._fixture_identity_resolver = (
            fixture_identity_resolver or DEFAULT_FIXTURE_IDENTITY_RESOLVER
        )
        self._allow_unpromoted_topology = allow_unpromoted_topology
        self._execute_void_compatible_middles = execute_void_compatible_middles
        self._promotion_policy = RulePromotionPolicy()

    def set_rule_store(self, rule_store: RuleStore | None) -> None:
        self._rule_store = rule_store

    @property
    def rule_store(self) -> RuleStore | None:
        return self._rule_store

    def find_hedges(
        self,
        instrument: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
        include_cross_venue: bool = True,
    ) -> list[HedgeCandidate]:
        """
        Find instruments that can hedge the given selection.

        Parameters
        ----------
        instrument : CryptoBettingInstrument
            The instrument to find hedges for.
        candidates : list[CryptoBettingInstrument]
            List of candidate instruments to search.
        include_cross_venue : bool, default True
            Whether to include cross-venue hedges.

        Returns
        -------
        list[HedgeCandidate]
            List of hedge candidates sorted by confidence.

        """
        hedges: list[HedgeCandidate] = []

        for candidate in candidates:
            if candidate.id == instrument.id:
                continue

            # Check if venues match
            is_same_venue = candidate.venue_name == instrument.venue_name
            if not include_cross_venue and not is_same_venue:
                continue

            if not self._is_hedge_event_match(instrument, candidate, candidates):
                continue

            semantic_candidate = self._semantic_hedge_candidate(instrument, candidate)
            if semantic_candidate is not None:
                hedges.append(semantic_candidate)
                continue

            if self._is_same_market_hedge(instrument, candidate):
                hedges.append(self._same_market_complement_candidate(candidate))

        # Filter by confidence and sort
        hedges = [h for h in hedges if h.confidence >= self.min_confidence]
        hedges.sort(key=lambda h: (-h.confidence, h.match_type))

        return hedges

    def _semantic_hedge_candidate(
        self,
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
    ) -> HedgeCandidate | None:
        rule = self._resolve_rule(instrument, candidate)
        if (
            rule is None
            or rule.relationship_type == RelationshipType.DANGEROUS_NON_EQUIVALENT.value
        ):
            return None

        if rule.confidence < self.min_confidence:
            return None

        same_market_runtime_safe = self._same_market_rule_has_no_settlement_blockers(
            rule,
            instrument,
            candidate,
        )

        return HedgeCandidate(
            instrument=candidate,
            match_type=self._match_type_for_rule(instrument, candidate),
            confidence=rule.confidence,
            rule_id=rule.rule_id,
            template_id=rule.template_id,
            relationship_type=rule.relationship_type,
            caveats=rule.caveats,
            push_capable=rule.has_void,
            partial_settlement=rule.has_partial,
            execution_safe=(
                rule.safety_tier == SafetyTier.EXECUTION_SAFE.value or same_market_runtime_safe
            ),
            same_venue_execution_eligible=(
                rule.safety_tier == SafetyTier.EXECUTION_SAFE_SAME_VENUE_ELIGIBLE.value
                and instrument.venue_name == candidate.venue_name
            ),
            safety_tier=rule.safety_tier,
            promotion_status=rule.promotion_status,
        )

    def _resolve_rule(
        self,
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
    ) -> MinedRule | None:
        rule = self._rule_classifier.classify(instrument, candidate)
        if rule is None:
            return None
        tier, reasons = self._promotion_policy.classify_rule_tier(
            rule,
            None,
            allow_void_compatible_middles=self._execute_void_compatible_middles,
        )
        rule = replace(rule, safety_tier=tier.value, eligibility_reasons=reasons)
        if self._rule_store is None:
            return (
                rule if self._allow_unpromoted_topology and tier != SafetyTier.AUDIT_ONLY else None
            )
        promoted = self._rule_store.load_promoted(rule.rule_id)
        if promoted is not None:
            return promoted
        template = SemanticRuleTemplate.from_rule(rule)
        promoted_template = self._rule_store.load_promoted_template(template.template_id)
        if promoted_template is not None and promoted_template.applies_to_venues(rule.venue_scope):
            return self._rule_from_template(rule, promoted_template)
        return (
            replace(rule, template_id=template.template_id)
            if self._allow_unpromoted_topology and tier != SafetyTier.AUDIT_ONLY
            else None
        )

    @staticmethod
    def _rule_from_template(
        rule: MinedRule,
        template: SemanticRuleTemplate,
    ) -> MinedRule:
        return replace(
            rule,
            relationship_type=template.relationship_type,
            sport=template.sport,
            scope=template.scope,
            market_a=template.pattern_a.market_type,
            selection_a=template.pattern_a.selection,
            params_a=template.pattern_a.params,
            market_b=template.pattern_b.market_type,
            selection_b=template.pattern_b.selection,
            params_b=template.pattern_b.params,
            result_states=template.result_states,
            settlement_a=template.settlement_a,
            settlement_b=template.settlement_b,
            confidence=template.confidence,
            caveats=template.caveats,
            promotion_status=template.promotion_status,
            safety_tier=template.safety_tier,
            eligibility_reasons=template.eligibility_reasons,
            template_id=template.template_id,
        )

    @staticmethod
    def _same_market_complement_candidate(candidate: CryptoBettingInstrument) -> HedgeCandidate:
        return HedgeCandidate(
            instrument=candidate,
            match_type="same_market",
            confidence=1.0,
            relationship_type=RelationshipType.COMPLEMENTARY_COVERAGE.value,
            execution_safe=True,
            same_venue_execution_eligible=False,
            safety_tier=SafetyTier.EXECUTION_SAFE.value,
            promotion_status=PromotionStatus.PROMOTED.value,
        )

    @staticmethod
    def _match_type_for_rule(
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
    ) -> str:
        if (
            instrument.market_name == candidate.market_name
            and instrument.params == candidate.params
        ):
            return "same_market"
        return "cross_market"

    def _is_hedge_event_match(
        self,
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
    ) -> bool:
        return self._hedge_event_match_decision(instrument, candidate, candidates).matched

    def explain_hedge_event_match(
        self,
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument] | None = None,
    ) -> dict[str, object]:
        decision = self._hedge_event_match_decision(instrument, candidate, candidates or [])
        return {
            "matched": decision.matched,
            "reason": decision.reason,
            "sameVenue": decision.same_venue,
            "sameFixture": decision.proof.same_fixture,
            "confidence": decision.proof.confidence,
            "ambiguous": decision.proof.ambiguous,
            "blockerReason": decision.proof.blocker_reason,
            "aliasHits": list(decision.proof.alias_hits),
            "matchedFields": list(decision.proof.matched_fields),
            "startTimeDeltaSeconds": decision.proof.start_time_delta_secs,
        }

    def _hedge_event_match_decision(
        self,
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
    ) -> HedgeEventMatchDecision:
        proof = self._fixture_identity_resolver.resolve(instrument, candidate)
        same_venue = instrument.venue_name == candidate.venue_name
        if (
            not same_venue
            and proof.same_fixture
            and proof.reason == "canonical_fixture_match_start_time_conflict"
        ):
            unique_conflict = self._is_cross_venue_unique_start_time_conflict(
                instrument,
                candidate,
                candidates,
            )
            return HedgeEventMatchDecision(
                matched=unique_conflict,
                reason=(
                    "cross_venue_unique_start_time_conflict"
                    if unique_conflict
                    else "ambiguous_start_time_conflict"
                ),
                proof=proof,
                same_venue=False,
            )
        if not proof.same_fixture:
            if not same_venue and proof.blocker_reason == "start_time_mismatch":
                unique_conflict = self._is_cross_venue_unique_start_time_conflict(
                    instrument,
                    candidate,
                    candidates,
                )
                return HedgeEventMatchDecision(
                    matched=unique_conflict,
                    reason=(
                        "cross_venue_unique_start_time_conflict"
                        if unique_conflict
                        else "ambiguous_start_time_conflict"
                    ),
                    proof=proof,
                    same_venue=False,
                )
            return HedgeEventMatchDecision(
                matched=False,
                reason=proof.blocker_reason or proof.reason or "fixture_identity_mismatch",
                proof=proof,
                same_venue=same_venue,
            )

        if same_venue:
            if instrument.event_id == candidate.event_id:
                return HedgeEventMatchDecision(
                    matched=True,
                    reason="same_venue_event_id_match",
                    proof=proof,
                    same_venue=True,
                )
            trusted = self.is_trusted_same_venue_event_id_mismatch(instrument, candidate)
            return HedgeEventMatchDecision(
                matched=trusted,
                reason=(
                    "trusted_same_venue_event_id_mismatch"
                    if trusted
                    else "same_venue_event_id_mismatch"
                ),
                proof=proof,
                same_venue=True,
            )

        if proof.ambiguous:
            return HedgeEventMatchDecision(
                matched=False,
                reason="ambiguous_fixture",
                proof=proof,
                same_venue=False,
            )
        if proof.confidence < 0.72:
            return HedgeEventMatchDecision(
                matched=False,
                reason="low_fixture_confidence",
                proof=proof,
                same_venue=False,
            )
        if instrument.parsed_start_time() is not None and candidate.parsed_start_time() is not None:
            # Both legs carry start times, but the target can still be ambiguous when the
            # opposing venue lists the same teams more than once the same day (a
            # doubleheader) and both games fall inside the cross-venue soft tolerance. The
            # start-time-aware fixture cluster count catches that; without this guard the
            # branch asserted a match against an arbitrary one of the games (#231/#237).
            if self._has_ambiguous_missing_fixture_evidence(instrument, candidate, candidates):
                return HedgeEventMatchDecision(
                    matched=False,
                    reason="ambiguous_fixture",
                    proof=replace(proof, ambiguous=True),
                    same_venue=False,
                )
            return HedgeEventMatchDecision(
                matched=True,
                reason="cross_venue_fixture_proof",
                proof=proof,
                same_venue=False,
            )
        ambiguous_missing_time = self._has_ambiguous_missing_fixture_evidence(
            instrument,
            candidate,
            candidates,
        )
        return HedgeEventMatchDecision(
            matched=not ambiguous_missing_time,
            reason=(
                "ambiguous_missing_start_time"
                if ambiguous_missing_time
                else "cross_venue_unique_missing_start_time"
            ),
            proof=proof,
            same_venue=False,
        )

    def _is_cross_venue_fixture_proof_safe(
        self,
        proof: FixtureIdentityProof,
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
    ) -> bool:
        if proof.ambiguous or proof.confidence < 0.72:
            return False

        if proof.reason == "canonical_fixture_match_start_time_conflict":
            return self._is_cross_venue_unique_start_time_conflict(
                instrument,
                candidate,
                candidates,
            )

        if instrument.parsed_start_time() is not None and candidate.parsed_start_time() is not None:
            return True

        return not self._has_ambiguous_missing_fixture_evidence(
            instrument,
            candidate,
            candidates,
        )

    def _has_ambiguous_missing_fixture_evidence(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
    ) -> bool:
        bucket_a = self._fixture_bucket_for_pair(instrument_a, instrument_b, candidates)
        bucket_b = self._fixture_bucket_for_pair(instrument_b, instrument_a, candidates)
        return (
            self._fixture_cluster_count(bucket_a) != 1 or self._fixture_cluster_count(bucket_b) != 1
        )

    def _is_cross_venue_unique_start_time_conflict(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
    ) -> bool:
        bucket_a = self._fixture_bucket_for_pair(
            instrument_a,
            instrument_b,
            candidates,
            allow_start_time_conflicts=True,
        )
        bucket_b = self._fixture_bucket_for_pair(
            instrument_b,
            instrument_a,
            candidates,
            allow_start_time_conflicts=True,
        )
        return (
            self._fixture_cluster_count(bucket_a) == 1
            and self._fixture_cluster_count(bucket_b) == 1
        )

    def _fixture_bucket_for_pair(
        self,
        source: CryptoBettingInstrument,
        target: CryptoBettingInstrument,
        candidates: list[CryptoBettingInstrument],
        *,
        allow_start_time_conflicts: bool = False,
    ) -> list[CryptoBettingInstrument]:
        bucket: list[CryptoBettingInstrument] = []
        for item in [source, *candidates]:
            if item.venue_name != source.venue_name:
                continue
            proof = self._fixture_identity_resolver.resolve(item, target)
            if proof.same_fixture or (
                allow_start_time_conflicts
                and proof.blocker_reason == "start_time_mismatch"
                and proof.canonical_event_key_a
                and proof.canonical_event_key_a == proof.canonical_event_key_b
            ):
                bucket.append(item)
        return bucket

    @staticmethod
    def _fixture_cluster_count(instruments: list[CryptoBettingInstrument]) -> int:
        clusters: list[set[str]] = []
        for instrument in instruments:
            MarketMatcher._merge_fixture_alias_cluster(
                clusters,
                MarketMatcher._fixture_cluster_aliases(instrument),
            )
        return len(clusters)

    @staticmethod
    def _fixture_cluster_aliases(instrument: CryptoBettingInstrument) -> set[str]:
        aliases = set(
            DEFAULT_FIXTURE_IDENTITY_RESOLVER.event_alias_keys(
                instrument,
                include_start_time=False,
            ),
        )
        if not aliases:
            aliases = {instrument.event_key(include_start_time=False)}
        start = instrument.parsed_start_time()
        if start is None:
            return aliases
        bucket_minute = 0 if start.minute < 30 else 30
        bucketed = start.replace(minute=bucket_minute, second=0, microsecond=0)
        suffix = bucketed.strftime("%Y-%m-%dT%H:%M")
        return {f"{alias}:{suffix}" for alias in aliases}

    @staticmethod
    def _merge_fixture_alias_cluster(clusters: list[set[str]], aliases: set[str]) -> None:
        matching_indexes = [index for index, cluster in enumerate(clusters) if cluster & aliases]
        if not matching_indexes:
            clusters.append(set(aliases))
            return

        primary_index = matching_indexes[0]
        clusters[primary_index].update(aliases)
        for index in reversed(matching_indexes[1:]):
            clusters[primary_index].update(clusters.pop(index))

    @staticmethod
    def _is_two_way_match_odds_market(instrument: CryptoBettingInstrument) -> bool:
        if MarketType.from_string(instrument.market_name) != MarketType.MATCH_ODDS:
            return False
        info = instrument.info if isinstance(instrument.info, dict) else {}
        return info.get("is_two_way_market") is True

    @staticmethod
    def is_trusted_same_venue_event_id_mismatch(
        instrument: CryptoBettingInstrument,
        candidate: CryptoBettingInstrument,
    ) -> bool:
        if instrument.venue_name != candidate.venue_name:
            return False
        if str(instrument.venue_name) != "SXBET":
            return False
        proof = DEFAULT_FIXTURE_IDENTITY_RESOLVER.resolve(instrument, candidate)
        if not proof.same_fixture or proof.ambiguous or proof.confidence < 0.72:
            return False
        if instrument.market_name != candidate.market_name:
            return False
        if instrument.params != candidate.params:
            return False
        if not (
            MarketMatcher._is_two_way_match_odds_market(instrument)
            and MarketMatcher._is_two_way_match_odds_market(candidate)
        ):
            return False
        return instrument.is_opposite_outcome(candidate)

    @staticmethod
    def _is_same_market_hedge(
        a: CryptoBettingInstrument,
        b: CryptoBettingInstrument,
    ) -> bool:
        """
        Check if two instruments are opposite selections in the same market.

        Returns True if they're in the same market with opposite outcomes.

        """
        if (
            a.venue_name == b.venue_name
            and a.event_id != b.event_id
            and not MarketMatcher.is_trusted_same_venue_event_id_mismatch(a, b)
        ):
            return False
        if a.venue_name != b.venue_name:
            proof = DEFAULT_FIXTURE_IDENTITY_RESOLVER.resolve(a, b)
            if not proof.same_fixture or proof.ambiguous or proof.confidence < 0.72:
                return False

        # Must be same market type and params
        if a.market_name != b.market_name:
            return False
        if a.params != b.params:
            return False

        if MarketType.from_string(a.market_name) == MarketType.MATCH_ODDS and not (
            MarketMatcher._is_two_way_match_odds_market(a)
            and MarketMatcher._is_two_way_match_odds_market(b)
        ):
            return False

        # Must be opposite outcomes
        return a.is_opposite_outcome(b)

    def check_arbitrage(
        self,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        odds_a: Decimal | None = None,
        odds_b: Decimal | None = None,
        *,
        allow_same_venue_execution_eligible: bool = False,
    ) -> ArbitrageOpportunity | None:
        """
        Check if two instruments create an arbitrage opportunity.

        Parameters
        ----------
        instrument_a : CryptoBettingInstrument
            First instrument.
        instrument_b : CryptoBettingInstrument
            Second instrument (should be a hedge of first).
        odds_a : Decimal, optional
            Override odds for instrument A, typically from a live quote.
        odds_b : Decimal, optional
            Override odds for instrument B, typically from a live quote.
        allow_same_venue_execution_eligible : bool, default False
            Whether to allow promoted same-venue execution-eligible semantic rules
            to be priced as theoretical opportunities without treating them as
            auto-executable runtime opportunities.

        Returns
        -------
        ArbitrageOpportunity | None
            The calculated arbitrage opportunity, or ``None`` when the pair is
            not a complete hedge.

        """
        rule = self._resolve_rule(instrument_a, instrument_b)
        if not self._semantic_rule_allows_arbitrage(
            rule,
            instrument_a,
            instrument_b,
            allow_same_venue_execution_eligible=allow_same_venue_execution_eligible,
        ):
            return None

        if odds_a is None:
            odds_a = Decimal(str(instrument_a.price))
        if odds_b is None:
            odds_b = Decimal(str(instrument_b.price))

        # Calculate implied probabilities
        prob_a = Decimal(1) / odds_a
        prob_b = Decimal(1) / odds_b

        total_prob = prob_a + prob_b
        profit_margin = (Decimal(1) / total_prob) - Decimal(1)

        is_same_venue = instrument_a.venue_name == instrument_b.venue_name

        return ArbitrageOpportunity(
            instrument_a=instrument_a,
            instrument_b=instrument_b,
            probability_a=prob_a,
            probability_b=prob_b,
            total_probability=total_prob,
            profit_margin=profit_margin,
            odds_a=odds_a,
            odds_b=odds_b,
            is_same_venue=is_same_venue,
            match_type=self._arbitrage_match_type(instrument_a, instrument_b),
            raw_probability_a=prob_a,
            raw_probability_b=prob_b,
            raw_total_probability=total_prob,
            raw_profit_margin=profit_margin,
        )

    def _semantic_rule_allows_arbitrage(
        self,
        rule: MinedRule | None,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        *,
        allow_same_venue_execution_eligible: bool,
    ) -> bool:
        if rule is None:
            return self._is_same_market_hedge(instrument_a, instrument_b)
        if self._rule_is_execution_safe(rule):
            return True
        if self._rule_is_allowed_same_venue_eligible(
            rule,
            instrument_a,
            instrument_b,
            allow_same_venue_execution_eligible=allow_same_venue_execution_eligible,
        ):
            return True
        return self._same_market_rule_has_no_settlement_blockers(rule, instrument_a, instrument_b)

    def _rule_is_execution_safe(self, rule: MinedRule) -> bool:
        return rule.execution_safe and (
            self._rule_store is None or rule.promotion_status == PromotionStatus.PROMOTED.value
        )

    def _rule_is_allowed_same_venue_eligible(
        self,
        rule: MinedRule,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        *,
        allow_same_venue_execution_eligible: bool,
    ) -> bool:
        return (
            allow_same_venue_execution_eligible
            and rule.same_venue_execution_eligible
            and instrument_a.venue_name == instrument_b.venue_name
            and (
                self._rule_store is None or rule.promotion_status == PromotionStatus.PROMOTED.value
            )
        )

    def _same_market_rule_has_no_settlement_blockers(
        self,
        rule: MinedRule,
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> bool:
        return (
            self._is_same_market_hedge(instrument_a, instrument_b)
            and not rule.has_void
            and not rule.has_partial
            and not rule.has_unknown
        )

    @staticmethod
    def _arbitrage_match_type(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
    ) -> str:
        if instrument_a.market_name == instrument_b.market_name:
            return "same_market"
        if instrument_a.venue_name == instrument_b.venue_name:
            return "cross_market"
        return "cross_venue"

    def find_arbitrage_opportunities(
        self,
        instruments: list[CryptoBettingInstrument],
        min_profit_margin: Decimal = Decimal("0.01"),
    ) -> list[ArbitrageOpportunity]:
        """
        Find all arbitrage opportunities in a list of instruments.

        Parameters
        ----------
        instruments : list[CryptoBettingInstrument]
            List of instruments to search.
        min_profit_margin : Decimal, default 0.01
            Minimum profit margin (1% = 0.01) to include.

        Returns
        -------
        list[ArbitrageOpportunity]
            List of profitable arbitrage opportunities.

        """
        opportunities: list[ArbitrageOpportunity] = []
        seen_pairs: set[tuple] = set()

        for instrument in instruments:
            hedges = self.find_hedges(instrument, instruments)

            for hedge in hedges:
                # Avoid duplicate pairs
                pair_id = tuple(sorted([instrument.id.value, hedge.instrument.id.value]))
                if pair_id in seen_pairs:
                    continue
                seen_pairs.add(pair_id)

                opportunity = self.check_arbitrage(instrument, hedge.instrument)

                if (
                    opportunity is not None
                    and opportunity.is_arbitrage
                    and opportunity.profit_margin >= min_profit_margin
                ):
                    opportunities.append(opportunity)

        # Sort by profit margin
        opportunities.sort(key=lambda o: -o.profit_margin)

        return opportunities

    @staticmethod
    def normalize_event_name(event_name: str) -> str:
        """
        Normalize event name for cross-venue matching.

        Parameters
        ----------
        event_name : str
            The event name to normalize.

        Returns
        -------
        str
            Normalized event name.

        """
        # Convert to lowercase
        name = event_name.lower()

        # Remove common suffixes/prefixes
        for removal in ["vs", "v", "@", "-", "at"]:
            name = name.replace(f" {removal} ", " ")

        # Remove extra whitespace
        return " ".join(name.split())

    def match_events_cross_venue(
        self,
        instruments_a: list[CryptoBettingInstrument],
        instruments_b: list[CryptoBettingInstrument],
    ) -> list[tuple[CryptoBettingInstrument, CryptoBettingInstrument]]:
        """
        Match events across two venues based on team names and times.

        Parameters
        ----------
        instruments_a : list[CryptoBettingInstrument]
            Instruments from venue A.
        instruments_b : list[CryptoBettingInstrument]
            Instruments from venue B.

        Returns
        -------
        list[tuple[CryptoBettingInstrument, CryptoBettingInstrument]]
            List of matched instrument pairs.

        """
        matches: list[tuple[CryptoBettingInstrument, CryptoBettingInstrument]] = []

        candidates = [*instruments_a, *instruments_b]
        for inst_a in instruments_a:
            for inst_b in instruments_b:
                proof = self._fixture_identity_resolver.resolve(inst_a, inst_b)
                if not proof.same_fixture:
                    continue
                if not self._is_cross_venue_fixture_proof_safe(
                    proof,
                    inst_a,
                    inst_b,
                    candidates,
                ):
                    continue
                if self._are_matching_selections(inst_a, inst_b):
                    matches.append((inst_a, inst_b))

        return matches

    @staticmethod
    def _normalize_team_name(name: str) -> str:
        """
        Normalize team/participant name for matching.
        """
        return DEFAULT_FIXTURE_IDENTITY_RESOLVER.normalize_team_name(name)

    @staticmethod
    def _are_matching_selections(
        a: CryptoBettingInstrument,
        b: CryptoBettingInstrument,
    ) -> bool:
        """
        Check if two selections from different venues match.
        """
        if a.params != b.params:
            return False

        # Must be same market type
        market_a = MarketType.from_string(a.market_name)
        market_b = MarketType.from_string(b.market_name)

        if market_a != market_b:
            return False

        return a.selection_key() == b.selection_key()
