# BET-266 Dry-Run Multi-Venue Experiment

## Objective

Prove that Cloudbet and sports-only Polymarket can participate in the same
semantic arbitrage discovery loop without placing orders or spending funds.
The experiment should produce candidate evidence from real strategy/node code
and keep execution disabled until an explicit risk-engine policy exists.

## Current Baseline

- `develop` includes the multi-venue validation node with SXBET, Cloudbet, and
  one bounded Polymarket sports market.
- `BettingArbitrageStrategy` transforms supported Polymarket `BinaryOption`
  instruments into betting-style topology while keeping quote subscriptions on
  the original Polymarket instrument IDs.
- The deployed validation node has proven Rust semantic topology and strict
  execution-safe candidate discovery with `auto_execute=false`.

## Experiment Tasks

1. Expand fixture-backed Polymarket sports coverage for clear-resolution
   moneyline, spread, and totals-style markets.
2. Add dry-run execution diagnostics that report what would be submitted per
   venue without calling live execution clients.
3. Add candidate provenance to runtime summaries: venue pair, semantic template
   ID, safety tier, execution-safe flag, and dry-run eligibility reason.
4. Add a bounded multi-venue validation manifest variant focused on Cloudbet
   plus Polymarket sports markets.
5. Validate with unit tests, strategy integration tests, `pr-validation`, and
   strategy-node release when node behavior changes.

## Safety Boundary

- `auto_execute` remains `false`.
- Venue `execution_enabled` remains `false` for validation manifests.
- Polymarket and Cloudbet order placement is out of scope; use mocks or dry-run
  records only.
- Any real funding, signing, or order submission requires explicit human
  approval in the active thread.

## Acceptance Criteria

- A strategy-level test proves Cloudbet + Polymarket sports instruments can
  produce at least one semantic match or candidate in dry-run mode.
- Runtime/node summary includes dry-run candidate provenance without exposing
  secrets.
- PR validation passes.
- If a node manifest or runtime path changes, `strategy-node-release` succeeds
  in validation mode and shows zero executions.
