# M1.8 — SX.bet same-venue live-execution loop test

Acceptance runbook for milestone **M1.8**: prove the SX.bet same-venue real-money
execution loop end-to-end **without placing a real order**, then document the
operator arming step that closes the live loop.

M1 shipped the full same-venue execution stack — fills and account state (#283),
pricing (#284), the `ArbPositionTracker` (#285), naked-leg flatten (#286), the
handicap-outcome fix (#288), settlement to realized P&L (#289), and the manual
approval queue (#290). This runbook is the acceptance gate over that stack.

## Idle is correct

With correct pricing, a same-venue SX.bet book is a normal book: the two sides of
a market carry a small negative overround (about `-2%`), so **there is no
positive-margin arb to take**. A correctly-priced node therefore stages **zero**
candidates and holds an **empty approval queue**. That idle state is the expected,
correct outcome — not a failure. Do not fabricate an arb to force a trade.

Because the live fill to settlement to realized-P&L path can only be driven by a
real order — which is gated on a genuine positive-margin candidate appearing
**and** operator approval — the acceptance is split into two provable halves plus a
documented arming procedure:

- **Phase A (deterministic, offline)** proves the
  fill to `OrderFilled` to `ArbPositionTracker` to settlement to realized-P&L path,
  and the manual stage to approve to execute to gates path, via the merged
  integration tests. No network, fully repeatable.
- **Phase B (live, validation-mode, no orders)** proves the observability and
  plumbing loop against live SX.bet with execution unarmed: real depth, correct
  pricing (mostly negative-margin), an empty approval queue, and populated
  status/heartbeat/nodeops surfaces.
- **Phase C (arming procedure)** documents — but does not execute — the exact
  operator steps to take a single genuine arb live and confirm the loop closed.

## The manifest

`deploy/strategy_nodes/betting_arbitrage/sxbet-same-venue-live.json` is the
committed, safe-by-default, armed-capable same-venue manifest.

| Setting | Value | Why |
| --- | --- | --- |
| `validation_mode` | `true` | Dry run. The builder skips every execution client and forces `auto_execute=false` while this is set, so the committed file submits no orders. |
| `strategy.live_execution_armed` | `false` | Manifest arming switch off. Arming also requires the `BETTING_LIVE_EXECUTION_ARMED` env gate — never hardcode this to `true`. |
| `strategy.execution_approval_mode` | `manual` | Even fully armed, each staged candidate needs an explicit operator approve; the approve command re-runs the full gate stack on fresh quotes before any order. |
| `strategy.execution_venue_mode` | `same_venue` | Only same-venue candidates are eligible; cross-venue candidates are blocked. |
| `strategy.allow_same_venue_live_execution` | `true` | Permits same-venue execution after strict same-venue risk checks. |
| `strategy.min_profit_margin` | `0.05` | Above SX.bet's 4% winning-profit commission hurdle, so only a genuine post-fee arb ever stages. |
| `strategy.max_leg_stake` | `5` | Tiny-pilot per-leg cap (USDC). |
| `strategy.max_daily_notional` | `50` | Tiny-pilot process-lifetime notional cap. |
| `strategy.max_daily_loss` | `10` | Tiny-pilot realized-loss guardrail. |
| `strategy.live_execution_kill_switch_path` | node dir `/KILL_SWITCH` | Drop this file (or export `BETTING_LIVE_EXECUTION_KILL_SWITCH=1`) to halt live submission immediately. |
| `strategy.unwind_filled_leg_enabled` | `true` | Naked-leg flatten (#286): auto-exit a filled leg whose sibling terminally failed. |
| venue `SXBET.execution_enabled` | `true` | Armed-capable — the exec client is still only built when `validation_mode=false`. |
| venue `SXBET.prefer_liquid_markets` | `true` | Probe order books and prefer two-sided liquid markets. |

Arming is a deliberate operator step (Phase C): flip `validation_mode` to `false`,
flip `strategy.live_execution_armed` to `true`, and export
`BETTING_LIVE_EXECUTION_ARMED=1`. The committed file stays dry.

### Grounding notes

- **Risk-cap field names are snake_case Decimals**: `max_leg_stake`,
  `max_daily_notional`, `max_daily_loss` (not camelCase). The status.json
  `executionReadiness.riskCaps` block renders them as `maxLegStake` etc.
- **`fill_poll_interval_secs` and `settlement_poll_interval_secs` are not
  manifest-settable.** They are `SXBetExecClientConfig` fields; the node builder
  only forwards `execution_mode` and `odds_slippage` from a venue's `metadata`.
  The loop uses the adapter defaults: `fill_poll_interval_secs=3.0`,
  `settlement_poll_interval_secs=30.0`, `account_state_interval_secs=30.0`. SX.bet
  has no user fill/settlement push feed, so fills and gradings are reconciled by
  polling.
- **`executionReadiness.autoExecute` reports the manifest field (`true`), but the
  rendered `trading-node-config.json` has `auto_execute=false`** while
  `validation_mode=true`. `validation_mode` is the operative gate in Phase B.

## Phase A — deterministic offline proof

Run the merged integration suites that exercise the loop with mocked quotes and
fills. No network. All commands run from the repo root with the betting node
importable (`PYTHONPATH` set to this checkout when using an external venv).

Group 1 — fill to settlement to realized-P&L to flatten to position tracker:

```bash
python -m pytest -q \
  tests/unit/strategies/test_betting_arbitrage_settlement.py \
  tests/unit/strategies/test_betting_arbitrage_sxbet_flatten.py \
  tests/unit/strategies/test_arb_position_tracker.py \
  tests/unit/adapters/test_sxbet_execution.py
```

Expected: **59 passed** — settlement to realized P&L (10), naked-leg flatten (8),
`ArbPositionTracker` (13), SX.bet fills/account-state/settlement-poll (28).

Group 2 — manual approval queue (stage to approve to execute to gates) and the
nodeops approvals panel:

```bash
python -m pytest -q tests/unit/strategies/test_betting_arbitrage.py \
  -k "manual or approval"
python -m pytest -q tests/unit/tools/test_nodeops.py -k "approval"
```

Expected: **16 passed** for the strategy approval-queue subset (staging, gate
re-run on approve, reject, expiry, capacity eviction, command-file processing) and
**6 passed** for the nodeops approval routes (probe extraction, node-detail
surfacing, readonly/invalid-id guards, authed HTTP actions).

Phase A total: **81 passed**. These deterministic tests are the acceptance proof
for the half of the loop that a real order would otherwise be needed to observe.

## Phase B — live validation-mode observability (no orders)

Deploy the committed manifest against live SX.bet with execution unarmed and
confirm the loop is populated and correctly idle.

> The nodeops dashboard deploy endpoint (`POST /api/nodes`) deliberately refuses
> any manifest that is not structurally data-only — it rejects `auto_execute`,
> `allow_same_venue_live_execution`, and any venue `execution_enabled`. This
> armed-capable manifest is therefore deployed from the CLI or the deploy script,
> **not** the dashboard button. The dashboard is used to observe and (in Phase C)
> approve the node it did not deploy.

### B.1 Validate and probe from the CLI

```bash
python -m nautilus_trader.live.strategy_nodes.betting_arbitrage validate-manifest \
  --manifest deploy/strategy_nodes/betting_arbitrage/sxbet-same-venue-live.json

python -m nautilus_trader.live.strategy_nodes.betting_arbitrage probe-runtime \
  --manifest deploy/strategy_nodes/betting_arbitrage/sxbet-same-venue-live.json \
  --timeout-seconds 420 \
  --poll-interval-secs 5 \
  --min-positive-margin-candidates 0 \
  --min-quoted-node-count SXBET:1
```

`validate-manifest` writes `status.json` with `status: "validated"`.
`probe-runtime` runs a validation-mode node briefly against live SX.bet, mines the
semantic cache, and writes a populated `runtimeProbe` block. `probe-runtime`
requires `validation_mode=true` and never arms execution.

Both commands need real `SXBET_API_KEY` / `SXBET_API_KEYS` (and,
because `allow_dummy_credentials=false`, `SXBET_PRIVATE_KEY` /
`SXBET_WALLET_ADDRESS`) in the environment. `validate-manifest` in `fresh`
semantic-cache mode reaches the live SX.bet corpus.

### B.2 Assert the loop with the helper

`scripts/betting/verify_same_venue_loop.py` reads a node's `status.json`, reuses
the `runtime_probe_report` normalization, and asserts the observability loop is
populated while treating idle as correct:

```bash
python scripts/betting/verify_same_venue_loop.py \
  artifacts/strategy-nodes/sxbet-same-venue-live/status.json \
  --require-quoted-depth
```

It fails only when the plumbing is broken (wrong venue mode, missing risk caps, no
SX.bet quote subscriptions, or — with `--require-quoted-depth` — no quoted
real-depth nodes). Zero positive-margin candidates and an empty approval queue are
a **pass**; a genuine positive-margin candidate or a non-empty queue is surfaced as
an operator-attention warning, not a failure.

### B.3 status.json fields to confirm

- `status` — `validated`, then a running state once a node is live.
- `executionReadiness.executionVenueMode` — `same_venue`.
- `executionReadiness.allowSameVenueLiveExecution` — `true`.
- `executionReadiness.validationMode` — `true`; `liveExecutionArmed` — `false`;
  `liveExecutionEnvArmed` — `false`.
- `executionReadiness.riskCaps` — `{maxLegStake: "5", maxDailyNotional: "50",
  maxDailyLoss: "10"}`.
- `semanticCache.ready` — `true`, with a non-zero
  `sameVenueExecutionEligibleTemplateCount`.
- `heartbeatPath` — present, and the referenced `heartbeat.json` refreshes on the
  heartbeat interval.
- `runtimeProbe.venueCoverage.quoteSubscriptionCounts.SXBET` — greater than zero
  (SX.bet quotes are actually subscribed).
- `runtimeProbe.venueCoverage.quotedNodeCounts.SXBET` — greater than zero (real,
  non-synthetic bid/ask depth is arriving).
- `runtimeProbe.positiveMarginCandidates` — `0` (expected idle: same-venue books
  carry a normal overround).
- `runtimeProbe.executionApprovals.pending` — `[]` (empty; validation mode never
  stages because `auto_execute` is forced off).
- `runtimeProbe.graphEngine` — `semantic_rust`.

### B.4 nodeops surfaces to confirm

The nodeops dashboard binds `127.0.0.1:8090` (HTTP Basic auth); browse
`http://<host>:8090`.

- `GET /api/nodes` — the node lists with its latest sample and container state; the
  `pending_approvals` column reads `0`.
- `GET /api/nodes/<name>` — node detail includes the manifest (secrets-free), the
  latest probe, and an `executionApprovals` block.
- `GET /api/nodes/<name>/approvals` — the `executionApprovals` probe block: `mode`
  `manual`, `staged` `0`, `pending` `[]`.
- UI drawer — the **Pending trades** tab renders (empty), alongside the edges /
  quotedEdges / arb-counter sparklines.

At the end of Phase B the loop is proven live: real SX.bet depth flows, pricing is
correct (mostly negative-margin, `xvCand`/positive candidates at zero), the
approval queue is empty, and every operator surface is populated — with execution
structurally unarmed.

## Phase C — arming procedure (documented, not executed)

This closes the live loop for a single genuine arb. Do not run this as part of
acceptance; it is the operator's go-live checklist and requires real funds.

Prerequisite — **fund visibility (known gap)**: SX.bet exposes no public
wallet-balance REST endpoint. The adapter's `http_client.get_balance` raises, so
the node only publishes an account state when a usable balance is available
elsewhere. Before arming, confirm the SX.bet USDC wallet balance out of band — an
on-chain read of the wallet, or an authenticated SX.bet endpoint if one is
provisioned. Treat this as a hard prerequisite for real arming.

Steps:

1. **Confirm caps.** Re-read `status.json` `executionReadiness.riskCaps`:
   `maxLegStake=5`, `maxDailyNotional=50`, `maxDailyLoss=10`. Keep them tiny for the
   first live arb.
2. **Fund the wallet.** Ensure the SX.bet USDC wallet holds at least
   `2 x max_leg_stake` for a two-leg same-venue fill, plus fee/slippage headroom.
   Confirm the balance out of band (see the known gap above).
3. **Arm.** Set `validation_mode` to `false` and `strategy.live_execution_armed` to
   `true` in an armed copy of the manifest, and export
   `BETTING_LIVE_EXECUTION_ARMED=1` in the node's environment. All three are
   required — the strategy's arming gate blocks submission unless the manifest flag,
   the env gate, and the absence of a kill switch all hold.
4. **Run armed.** Launch the node from the CLI / deploy script with the armed
   manifest. The exec client is now built (`execution_enabled=true`,
   `validation_mode=false`) and the fill and settlement poll loops start.
5. **Wait for a genuine candidate.** With `execution_approval_mode=manual`, a
   same-venue candidate that clears every live gate (post-fee margin above 5%, fresh
   quotes, sufficient depth, within caps) is **staged**, not submitted. It appears in
   `runtimeProbe.executionApprovals.pending` and the nodeops **Pending trades** tab.
   Most of the time nothing stages — that is correct.
6. **Approve exactly one.** In the nodeops UI (with `NODEOPS_READONLY=0`) click
   Approve on the single staged candidate, or
   `POST /api/nodes/<name>/approvals/<approval_id>/approve`. The approve command
   re-runs the full gate stack on fresh quotes; if the edge has decayed it is blocked
   and nothing is submitted.
7. **Watch the loop close.** On approval both legs are submitted; each fill emits
   `OrderFilled`, the `ArbPositionTracker` records the paired position, the
   settlement poll (every 30s) publishes a `BetSettlement` on grading, and the
   strategy realizes arbitrage P&L.

Kill switch: at any point, drop the file at
`strategy.live_execution_kill_switch_path` (the node-dir `KILL_SWITCH`) or export
`BETTING_LIVE_EXECUTION_KILL_SWITCH=1`. The arming gate then blocks all further
live submission immediately; a resting sibling leg is cancelled, and a filled leg
whose sibling failed is auto-exited when `unwind_filled_leg_enabled=true`.

Loop-closed checklist:

- Both legs show `OrderFilled` in the node log.
- `ArbPositionTracker` reports the paired same-venue position.
- `runtimeProbe.executionApprovals.pending` returns to `[]` and
  `recent_decisions` records the approve.
- On grading, a `BetSettlement` is published and realized P&L is booked.
- Daily notional / loss counters in `status.json` reflect the single arb and stay
  within caps.

## References

- Manifest: `deploy/strategy_nodes/betting_arbitrage/sxbet-same-venue-live.json`
- Loop verifier: `scripts/betting/verify_same_venue_loop.py`
- Node CLI: `python -m nautilus_trader.live.strategy_nodes.betting_arbitrage`
- nodeops dashboard: `docs/ops/nodeops-dashboard.md`
- Node operations: `docs/ops/betting-arbitrage-nodes.md`
