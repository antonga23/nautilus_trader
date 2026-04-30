# Multi-Venue Betting Node Plan

## Current State

- `BettingArbitrageStrategy` consumes `CryptoBettingInstrument` topology through `MarketMatcher` and `OpportunityGraph`.
- SXBET and Cloudbet now expose betting-style instruments to the strategy-node builder.
- Polymarket still publishes Nautilus `BinaryOption` instruments at the adapter boundary.
- Semantic mining already has `PolymarketSportsTransformer` for sports binary markets, but the live strategy must bridge those binaries into betting-style topology before multi-venue validation can be meaningful.

## Phase 1: Sports Binary Bridge

- Convert sports-tagged Polymarket `BinaryOption` instruments into `CryptoBettingInstrument` snapshots for strategy topology.
- Keep the original Polymarket `BinaryOption` instrument ID for quote subscriptions and market-data routing.
- Remap inbound Polymarket quote ticks to the transformed betting instrument ID only inside the strategy graph/evaluation path.
- Leave non-sports or unresolved-resolution Polymarket binaries out of topology.

## Phase 2: Multi-Venue Manifest

- Add a validation-mode manifest for `SXBET + CLOUDBET + POLYMARKET`.
- Set `auto_execute=false` and `execution_enabled=false` for all venues until the risk engine has explicit multi-venue execution policy.
- Use `semantic_rule_cache_dir` so runtime consumes promoted templates and semantic topology diagnostics.
- Keep Polymarket instrument loading bounded by explicit IDs or sports filters until live corpus coverage is reliable.

## Phase 3: Runtime Validation

- Run `strategy-node-release` in validation mode for the multi-venue manifest.
- Require nonzero loaded instruments from at least two venues.
- Require nonzero semantic graph edges or hedge candidates.
- Require quoted match instruments where provider data is available.
- Do not require positive-margin arbitrage for the initial multi-venue validation gate.
- Capture the node status payload, semantic diagnostics, and strategy summary in the PR so reviewers can distinguish:
  - instruments loaded but unmatched by semantics,
  - semantic matches that are topology-only,
  - same-venue eligible matches that remain risk-engine gated,
  - strict execution-safe candidates.

## Phase 4: Execution Readiness

- Add risk-engine policy for same-event identity, venue scope, settlement caveats, stake sizing, and execution ordering.
- Keep real order submission disabled until a human explicitly approves live execution.
- Use mocks, simulation, or validation-mode dry runs for all execution-path tests before any funded deployment.

## Release Workflow

The validation dispatch should use the normal strategy-node release path, with deployment enabled only for a validation-mode node:

```sh
gh workflow run strategy-node-release.yml \
  --repo antonga23/cloudbet-market-maker \
  --ref codex/multi-venue-polymarket-sports \
  -f manifest_path=deploy/strategy_nodes/betting_arbitrage/multi-venue-validation.json \
  -f deploy_enabled=true \
  -f container_name=betting-arbitrage-node-multivenue \
  -f image_transport=archive \
  -f force_rebuild=true
```

After deploy, run the runtime verifier against `betting-arbitrage-node-multivenue`.
The verifier should require a ready semantic cache, Rust semantic topology when available, nonzero graph edges, and nonzero semantic match instruments. Positive-margin arbitrage remains a reported metric, not a hard gate for the first multi-venue validation.

## Safety Boundary

- `auto_execute=false` in the manifest.
- All venue `execution_enabled=false` until the risk engine explicitly enables funded execution.
- Polymarket quote subscriptions use original `BinaryOption` instrument IDs; only in-memory strategy topology sees transformed betting instruments.
- Any execution-ordering or stake-sizing test must use mocks, simulated clients, or validation mode unless a human explicitly approves real funds in the current thread.

## Acceptance Criteria

- PR validation is green.
- The single-venue Cloudbet node remains green after this branch is rebased onto the Cloudbet node PR.
- Multi-venue validation node reports graph topology, matched instruments, and semantic cache metadata.
- Polymarket sports binaries are visible to the strategy as betting-style topology while preserving original quote subscriptions.
- No code path enables real-money execution by default.
- Runtime logs include a clear count of loaded instruments by venue, connected semantic matches, duplicate suppressions, matcher-suspect suppressions, and executable candidate counts.
