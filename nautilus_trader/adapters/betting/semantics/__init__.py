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
Semantic market normalization and payoff-rule mining for betting adapters.
"""

from nautilus_trader.adapters.betting.semantics.corpus import SnapshotIngestor
from nautilus_trader.adapters.betting.semantics.linear_sync import LinearIssueSync
from nautilus_trader.adapters.betting.semantics.linear_sync import LinearSyncError
from nautilus_trader.adapters.betting.semantics.classifier import RuleClassifier
from nautilus_trader.adapters.betting.semantics.completion import build_completion_report
from nautilus_trader.adapters.betting.semantics.completion import DEFAULT_MIN_CANDIDATES
from nautilus_trader.adapters.betting.semantics.completion import DEFAULT_REQUIRED_PROVIDERS
from nautilus_trader.adapters.betting.semantics.completion import DEFAULT_TARGET_CANDIDATES
from nautilus_trader.adapters.betting.semantics.completion import DEFAULT_TARGET_SPORTS
from nautilus_trader.adapters.betting.semantics.completion import ProviderCompletion
from nautilus_trader.adapters.betting.semantics.completion import SemanticMiningCompletionReport
from nautilus_trader.adapters.betting.semantics.completion import SEMANTIC_TARGET_SPORTS
from nautilus_trader.adapters.betting.semantics.completion import SportCompletion
from nautilus_trader.adapters.betting.semantics.coverage import CoverageEngine
from nautilus_trader.adapters.betting.semantics.coverage import CoverageMiningReport
from nautilus_trader.adapters.betting.semantics.coverage import OutcomeUniverseBuilder
from nautilus_trader.adapters.betting.semantics.coverage import SelectionPredicateBuilder
from nautilus_trader.adapters.betting.semantics.miner import RuleMiner
from nautilus_trader.adapters.betting.semantics.normalization import MarketNormalizer
from nautilus_trader.adapters.betting.semantics.payoffs import PayoffVectorBuilder
from nautilus_trader.adapters.betting.semantics.payoffs import SettlementPluginRegistry
from nautilus_trader.adapters.betting.semantics.polymarket_transform import (
    PolymarketSportsTransformer,
)
from nautilus_trader.adapters.betting.semantics.promotion import RulePromotionPolicy
from nautilus_trader.adapters.betting.semantics.secrets import load_aws_secret_payload
from nautilus_trader.adapters.betting.semantics.secrets import restore_gcp_service_account
from nautilus_trader.adapters.betting.semantics.secrets import SecretManagerError
from nautilus_trader.adapters.betting.semantics.store import FileRuleCache
from nautilus_trader.adapters.betting.semantics.store import RuleStore
from nautilus_trader.adapters.betting.semantics.types import CanonicalMarketType
from nautilus_trader.adapters.betting.semantics.types import CorpusSnapshot
from nautilus_trader.adapters.betting.semantics.types import CoverageBlockerReason
from nautilus_trader.adapters.betting.semantics.types import CoverageGap
from nautilus_trader.adapters.betting.semantics.types import CoverageHyperedge
from nautilus_trader.adapters.betting.semantics.types import CoverageProof
from nautilus_trader.adapters.betting.semantics.types import CoverageRisk
from nautilus_trader.adapters.betting.semantics.types import CoverageSet
from nautilus_trader.adapters.betting.semantics.types import MinedRule
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelection
from nautilus_trader.adapters.betting.semantics.types import NormalizedSelectionRecord
from nautilus_trader.adapters.betting.semantics.types import ARB_MARGIN_RELATIONSHIP_TYPES
from nautilus_trader.adapters.betting.semantics.types import HALF_GRADE_SETTLEMENT_VENUES
from nautilus_trader.adapters.betting.semantics.types import NON_PARTIAL_SETTLEMENT_RISK_CAVEATS
from nautilus_trader.adapters.betting.semantics.types import NON_VOID_SETTLEMENT_RISK_CAVEATS
from nautilus_trader.adapters.betting.semantics.types import PARTIAL_LOCK_RELATIONSHIP_TYPES
from nautilus_trader.adapters.betting.semantics.types import PARTIAL_SETTLEMENT_CAVEATS
from nautilus_trader.adapters.betting.semantics.types import VOID_PUSH_SETTLEMENT_CAVEATS
from nautilus_trader.adapters.betting.semantics.types import has_only_partial_settlement_risk
from nautilus_trader.adapters.betting.semantics.types import has_only_void_push_settlement_risk
from nautilus_trader.adapters.betting.semantics.types import is_partial_compatible_lock
from nautilus_trader.adapters.betting.semantics.types import is_void_compatible_middle
from nautilus_trader.adapters.betting.semantics.types import (
    venue_scope_supports_half_grade_settlement,
)
from nautilus_trader.adapters.betting.semantics.types import OutcomeState
from nautilus_trader.adapters.betting.semantics.types import OutcomeUniverse
from nautilus_trader.adapters.betting.semantics.types import PayoffVector
from nautilus_trader.adapters.betting.semantics.types import PromotionStatus
from nautilus_trader.adapters.betting.semantics.types import RelationshipType
from nautilus_trader.adapters.betting.semantics.types import RuleCorpusManifest
from nautilus_trader.adapters.betting.semantics.types import RuleValidationStats
from nautilus_trader.adapters.betting.semantics.types import SafetyTier
from nautilus_trader.adapters.betting.semantics.types import SettlementState
from nautilus_trader.adapters.betting.semantics.types import SelectionPattern
from nautilus_trader.adapters.betting.semantics.types import SelectionPredicate
from nautilus_trader.adapters.betting.semantics.types import SemanticRuleTemplate
from nautilus_trader.adapters.betting.semantics.types import TemplateSupportStats
from nautilus_trader.adapters.betting.semantics.validation import HistoricalRuleValidator


__all__ = [
    "ARB_MARGIN_RELATIONSHIP_TYPES",
    "DEFAULT_MIN_CANDIDATES",
    "DEFAULT_REQUIRED_PROVIDERS",
    "DEFAULT_TARGET_CANDIDATES",
    "DEFAULT_TARGET_SPORTS",
    "HALF_GRADE_SETTLEMENT_VENUES",
    "NON_PARTIAL_SETTLEMENT_RISK_CAVEATS",
    "NON_VOID_SETTLEMENT_RISK_CAVEATS",
    "PARTIAL_LOCK_RELATIONSHIP_TYPES",
    "PARTIAL_SETTLEMENT_CAVEATS",
    "SEMANTIC_TARGET_SPORTS",
    "VOID_PUSH_SETTLEMENT_CAVEATS",
    "CanonicalMarketType",
    "CorpusSnapshot",
    "CoverageBlockerReason",
    "CoverageEngine",
    "CoverageGap",
    "CoverageHyperedge",
    "CoverageMiningReport",
    "CoverageProof",
    "CoverageRisk",
    "CoverageSet",
    "FileRuleCache",
    "HistoricalRuleValidator",
    "LinearIssueSync",
    "LinearSyncError",
    "MarketNormalizer",
    "MinedRule",
    "NormalizedSelection",
    "NormalizedSelectionRecord",
    "OutcomeState",
    "OutcomeUniverse",
    "OutcomeUniverseBuilder",
    "PayoffVector",
    "PayoffVectorBuilder",
    "PolymarketSportsTransformer",
    "PromotionStatus",
    "ProviderCompletion",
    "RelationshipType",
    "RuleClassifier",
    "RuleCorpusManifest",
    "RuleMiner",
    "RulePromotionPolicy",
    "RuleStore",
    "RuleValidationStats",
    "SafetyTier",
    "SecretManagerError",
    "SelectionPattern",
    "SelectionPredicate",
    "SelectionPredicateBuilder",
    "SemanticMiningCompletionReport",
    "SemanticRuleTemplate",
    "SettlementPluginRegistry",
    "SettlementState",
    "SnapshotIngestor",
    "SportCompletion",
    "TemplateSupportStats",
    "build_completion_report",
    "has_only_partial_settlement_risk",
    "has_only_void_push_settlement_risk",
    "is_partial_compatible_lock",
    "is_void_compatible_middle",
    "load_aws_secret_payload",
    "restore_gcp_service_account",
    "venue_scope_supports_half_grade_settlement",
]
