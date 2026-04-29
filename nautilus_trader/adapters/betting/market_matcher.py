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
MarketMatcher - Cross-venue market matching for arbitrage detection.
"""

from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal

from nautilus_trader.adapters.betting.common.constants import MARKET_HEDGE_MAP
from nautilus_trader.adapters.betting.common.enums import MarketType
from nautilus_trader.adapters.betting.common.enums import Outcome
from nautilus_trader.adapters.betting.instruments import CryptoBettingInstrument
from nautilus_trader.adapters.betting.semantics import PromotionStatus
from nautilus_trader.adapters.betting.semantics import RelationshipType
from nautilus_trader.adapters.betting.semantics import RuleClassifier
from nautilus_trader.adapters.betting.semantics import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics import RuleStore
from nautilus_trader.adapters.betting.semantics import MinedRule
from nautilus_trader.adapters.betting.semantics import SafetyTier
from nautilus_trader.adapters.betting.semantics import SemanticRuleTemplate


HANDICAP_TOLERANCE = 0.01


@dataclass
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


@dataclass
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

    # Deprecated: semantic rules now drive cross-market matching. Kept for
    # backwards-compatible imports and diagnostics.
    MARKET_MAPPER: dict[str, list[str]] = MARKET_HEDGE_MAP.copy()
    PUSH_CAPABLE_MARKETS = frozenset(
        {
            MarketType.DRAW_NO_BET,
            MarketType.ASIAN_HANDICAP,
        },
    )
    MATCH_ODDS_DOUBLE_CHANCE_CONFIDENCE = {
        (MarketType.MATCH_ODDS, MarketType.DOUBLE_CHANCE): {
            (Outcome.HOME, Outcome.AWAY_DRAW): 0.95,
            (Outcome.DRAW, Outcome.HOME_AWAY): 0.95,
            (Outcome.AWAY, Outcome.HOME_DRAW): 0.95,
        },
        (MarketType.DOUBLE_CHANCE, MarketType.MATCH_ODDS): {
            (Outcome.AWAY_DRAW, Outcome.HOME): 0.95,
            (Outcome.HOME_AWAY, Outcome.DRAW): 0.95,
            (Outcome.HOME_DRAW, Outcome.AWAY): 0.95,
        },
    }

    def __init__(
        self,
        min_confidence: float = 0.5,
        rule_classifier: RuleClassifier | None = None,
        rule_store: RuleStore | None = None,
        allow_unpromoted_topology: bool = True,
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
        allow_unpromoted_topology : bool, default True
            Whether candidate-only semantic edges can be exposed as non-executable topology.

        """
        self.min_confidence = min_confidence
        self._rule_classifier = rule_classifier or RuleClassifier()
        self._rule_store = rule_store
        self._allow_unpromoted_topology = allow_unpromoted_topology
        self._promotion_policy = RulePromotionPolicy()

    def set_rule_store(self, rule_store: RuleStore | None) -> None:
        self._rule_store = rule_store

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
                hedges.append(self._legacy_same_market_candidate(candidate))

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
            execution_safe=rule.safety_tier == SafetyTier.EXECUTION_SAFE.value,
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
        tier, reasons = self._promotion_policy.classify_rule_tier(rule, None)
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
    def _legacy_same_market_candidate(candidate: CryptoBettingInstrument) -> HedgeCandidate:
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
        if not instrument.matches_event(candidate):
            return False

        if instrument.venue_name == candidate.venue_name:
            if instrument.event_id == candidate.event_id:
                return True
            return self.is_trusted_same_venue_event_id_mismatch(instrument, candidate)

        instrument_start = instrument.parsed_start_time()
        candidate_start = candidate.parsed_start_time()
        if instrument_start is not None and candidate_start is not None:
            return True

        event_key = instrument.event_key(include_start_time=False)
        if event_key != candidate.event_key(include_start_time=False):
            return False

        bucket_a = [
            item
            for item in candidates
            if item.venue_name == instrument.venue_name
            and item.event_key(include_start_time=False) == event_key
        ]
        if instrument not in bucket_a:
            bucket_a.append(instrument)

        bucket_b = [
            item
            for item in candidates
            if item.venue_name == candidate.venue_name
            and item.event_key(include_start_time=False) == event_key
        ]
        if candidate not in bucket_b:
            bucket_b.append(candidate)

        return not self._has_ambiguous_missing_start_time(
            instrument,
            candidate,
            bucket_a,
            bucket_b,
        )

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
        if not instrument.matches_event(candidate):
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
        # Must be same event
        if not a.matches_event(b):
            return False
        if (
            a.venue_name == b.venue_name
            and a.event_id != b.event_id
            and not MarketMatcher.is_trusted_same_venue_event_id_mismatch(a, b)
        ):
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

    def _is_cross_market_hedge(
        self,
        a: CryptoBettingInstrument,
        b: CryptoBettingInstrument,
    ) -> float:
        """
        Check if instruments from different markets can hedge each other.

        Returns confidence score (0-1) or 0 if not a hedge.

        """
        if not a.matches_event(b):
            return 0.0

        rule = self._resolve_rule(a, b)
        if (
            rule is None
            or rule.relationship_type == RelationshipType.DANGEROUS_NON_EQUIVALENT.value
        ):
            return 0.0
        return rule.confidence

    def _calculate_cross_market_confidence(  # pylint: disable=too-many-arguments
        self,
        market_a: MarketType,
        market_b: MarketType,
        outcome_a: Outcome,
        outcome_b: Outcome,
        inst_a: CryptoBettingInstrument,
        inst_b: CryptoBettingInstrument,
    ) -> float:
        """
        Calculate confidence score for cross-market hedge.
        """
        # Match odds + Double chance (1X2 + 1X/X2/12)
        result = self._confidence_match_odds_double_chance(
            market_a,
            market_b,
            outcome_a,
            outcome_b,
        )
        if result is not None:
            return result

        # Asian handicap hedging
        result = self._confidence_asian_handicap(
            market_a,
            market_b,
            outcome_a,
            outcome_b,
            inst_a,
            inst_b,
        )
        if result is not None:
            return result

        # Draw no bet hedging
        if (
            market_a in (MarketType.DRAW_NO_BET, MarketType.ASIAN_HANDICAP)
            and market_b in (MarketType.DRAW_NO_BET, MarketType.ASIAN_HANDICAP)
            and outcome_a.opposite() == outcome_b
        ):
            return 0.75

        return 0.0

    @staticmethod
    def _confidence_match_odds_double_chance(  # pylint: disable=too-many-return-statements
        market_a: MarketType,
        market_b: MarketType,
        outcome_a: Outcome,
        outcome_b: Outcome,
    ) -> float | None:
        """
        Calculate confidence for match odds vs double chance hedges.
        """
        confidence_by_outcome = MarketMatcher.MATCH_ODDS_DOUBLE_CHANCE_CONFIDENCE.get(
            (market_a, market_b),
        )
        if confidence_by_outcome is None:
            return None

        return confidence_by_outcome.get((outcome_a, outcome_b), 0.0)

    @staticmethod
    def _confidence_asian_handicap(  # pylint: disable=too-many-arguments
        market_a: MarketType,
        market_b: MarketType,
        outcome_a: Outcome,
        outcome_b: Outcome,
        inst_a: CryptoBettingInstrument,
        inst_b: CryptoBettingInstrument,
    ) -> float | None:
        """
        Calculate confidence for asian handicap hedges.
        """
        if market_a != MarketType.ASIAN_HANDICAP or market_b != MarketType.ASIAN_HANDICAP:
            return None

        handicap_a = inst_a.handicap or 0
        handicap_b = inst_b.handicap or 0

        # Opposite handicaps (e.g., +1.5 vs -1.5)
        if abs(handicap_a + handicap_b) < HANDICAP_TOLERANCE:
            if outcome_a == Outcome.HOME and outcome_b == Outcome.AWAY:
                return 0.85
            if outcome_a == Outcome.AWAY and outcome_b == Outcome.HOME:
                return 0.85
        return 0.0

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
        if rule is not None:
            promoted_or_legacy = rule.execution_safe and (
                self._rule_store is None or rule.promotion_status == PromotionStatus.PROMOTED.value
            )
            promoted_same_venue_eligible = (
                allow_same_venue_execution_eligible
                and rule.same_venue_execution_eligible
                and instrument_a.venue_name == instrument_b.venue_name
                and (
                    self._rule_store is None
                    or rule.promotion_status == PromotionStatus.PROMOTED.value
                )
            )
            if not promoted_or_legacy and (
                not promoted_same_venue_eligible
                and (
                    not self._is_same_market_hedge(instrument_a, instrument_b)
                    or rule.has_void
                    or rule.has_partial
                    or rule.has_unknown
                )
            ):
                return None
        elif not self._is_same_market_hedge(instrument_a, instrument_b):
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

        # Determine match type
        is_same_venue = instrument_a.venue_name == instrument_b.venue_name

        if instrument_a.market_name == instrument_b.market_name:
            match_type = "same_market"
        elif is_same_venue:
            match_type = "cross_market"
        else:
            match_type = "cross_venue"

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
            match_type=match_type,
        )

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

        # Group instruments by normalized event identifiers
        events_a: dict[str, list[CryptoBettingInstrument]] = {}
        for inst in instruments_a:
            key = self._make_event_key(inst)
            events_a.setdefault(key, []).append(inst)

        events_b: dict[str, list[CryptoBettingInstrument]] = {}
        for inst in instruments_b:
            key = self._make_event_key(inst)
            events_b.setdefault(key, []).append(inst)

        # Find matching events
        for key_a, insts_a in events_a.items():
            if key_a in events_b:
                for inst_a in insts_a:
                    matches.extend(
                        (inst_a, inst_b)
                        for inst_b in events_b[key_a]
                        if not self._has_ambiguous_missing_start_time(
                            inst_a,
                            inst_b,
                            insts_a,
                            events_b[key_a],
                        )
                        if self._are_matching_selections(inst_a, inst_b)
                    )

        return matches

    @staticmethod
    def _make_event_key(instrument: CryptoBettingInstrument) -> str:
        """
        Create a normalized key for event matching.
        """
        return instrument.event_key(include_start_time=False)

    @staticmethod
    def _has_ambiguous_missing_start_time(
        instrument_a: CryptoBettingInstrument,
        instrument_b: CryptoBettingInstrument,
        bucket_a: list[CryptoBettingInstrument],
        bucket_b: list[CryptoBettingInstrument],
    ) -> bool:
        if (
            instrument_a.parsed_start_time() is not None
            and instrument_b.parsed_start_time() is not None
        ):
            return False

        # Missing start times are only safe to match when the combined bucket
        # still points at exactly one parsed fixture-time cluster.
        return MarketMatcher._start_time_cluster_count([*bucket_a, *bucket_b]) != 1

    @staticmethod
    def _start_time_cluster_count(instruments: list[CryptoBettingInstrument]) -> int:
        starts = sorted(
            start
            for instrument in instruments
            if (start := instrument.parsed_start_time()) is not None
        )
        if not starts:
            return 0

        clusters = 1
        cluster_anchor = starts[0]
        for start in starts[1:]:
            if (start - cluster_anchor).total_seconds() > 6 * 60 * 60:
                clusters += 1
                cluster_anchor = start

        return clusters

    @staticmethod
    def _normalize_team_name(name: str) -> str:
        """
        Normalize team/participant name for matching.
        """
        name = name.lower()

        # Remove common prefixes/suffixes
        for removal in ["fc", "afc", "sc", "cf", "united", "city"]:
            name = name.replace(f" {removal}", "").replace(f"{removal} ", "")

        # Remove extra whitespace
        return " ".join(name.split())

    @staticmethod
    def _are_matching_selections(
        a: CryptoBettingInstrument,
        b: CryptoBettingInstrument,
    ) -> bool:
        """
        Check if two selections from different venues match.
        """
        if not a.matches_event(b):
            return False

        if a.params != b.params:
            return False

        # Must be same market type
        market_a = MarketType.from_string(a.market_name)
        market_b = MarketType.from_string(b.market_name)

        if market_a != market_b:
            return False

        return a.matches_selection(b)
