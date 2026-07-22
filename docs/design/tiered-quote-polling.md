# Tiered quote polling: replacing subscription budgets with value-ranked poll frequency

Status: proposed (follow-up epic; not yet scheduled)

## Problem

Per-venue quote-subscription budgets (`quote_subscription_limit` and the derived
`semantic_quote_subscription_limit_by_venue`) exist to protect the REST pollers from
work they cannot complete inside a poll cycle. They force a binary decision — subscribe
or drop — over instruments whose value differs by orders of magnitude, and the numbers
are static per manifest while books grow and shrink daily. The observed failure modes on
the live fleet:

- Venues pinned at their cap while hundreds of subscribed instruments produce no quotes
  (CLOUDBET basketball: 616 subscriptions, 466 of them unquoted; tennis 611/521).
- Venues holding large budgets that produce zero cross-venue value (POLYMARKET soccer:
  479 subscriptions against a fixture set fully disjoint from the other venues).
- The scarce resource being misallocated *within* a venue: a cross-venue edge leg and a
  never-quoted far-future prop cost the same one slot.

The interim mitigations (probe-pass ceiling enforcement, the gap-releasing rebalance
pass, and the `shardplan budget` planner) tune the budget numbers from observed demand.
This design removes the scarcity the budgets manage.

## Insight

The marginal cost of a subscription is no longer uniform:

- **SXBET** streams over Centrifugo WebSocket — the marginal market is ~free; the REST
  poll loop is only a fallback.
- **CLOUDBET** polls event-batched: all instruments of an already-polled event ride one
  `get_event` request. The scarce unit is *per-event requests + per-line fallbacks*, not
  subscriptions.
- **POLYMARKET** ws/quote flow is likewise not per-instrument-priced.

What actually needs rationing is *poll frequency on the venues' REST paths*. That is a
continuous quantity, not a binary one — so allocate it by value tier instead of gating
membership.

## Design

### Tier model

Subscribe everything the resolution horizon admits (`_instrument_resolution_horizon_quote_allowed`),
then assign each instrument a poll tier:

| Tier | Members | Poll cadence |
|---|---|---|
| hot | cross-venue edge legs, execution-safe-edge nodes, staged/approval-pending pairs | every cycle |
| warm | semantic-connected nodes, common-fixture alias instruments | every 5th cycle |
| cold | unmatched probes, unknown-horizon instruments | every 30th cycle (min once per 60 s) |

Tier assignment is computed inside the existing ranked subscription passes — the signals
already exist (`_cross_venue_common_fixture_quote_priority`,
`_instrument_resolution_horizon_priority`, `_instrument_market_family_quote_priority`,
semantic-edge membership). No new scoring machinery.

### Transport: a runtime-cache tier blob

The strategy writes one blob per venue, `betting:venue_quote_tiers:{VENUE}` (new
encode/decode pair in `adapters/betting/runtime_cache.py`, mirroring the
`active_venue_instrument_index` pattern), refreshed on every graph rebuild/reconcile.
The adapter reads it once per poll cycle.

Chosen over re-issuing `SubscribeQuoteTicks` with params: engine-side subscription
dedupe can swallow re-subscribes, and the cache-key pattern is already established and
fail-open — a missing or unreadable blob means "treat all requested instruments as hot",
which is exactly today's behavior.

### Adapter scheduling

In the poll loop, an instrument is due when
`(cycle_id + stable_hash(event_id)) % tier_interval == 0` — the event-id hash staggers
events across cycles so warm/cold work spreads evenly instead of spiking. The effective
tier of an event is the minimum tier of its instruments, so sibling warm/cold quotes
ride a hot event's batch request for free (the event-batched path already amortizes
this). Per-line fallbacks use the instrument's own tier. The market-pollability
tombstone registry composes orthogonally: suppression always beats tier.

### Budgets become safety valves

With frequency doing the rationing, `quote_subscription_limit` rises 2–3× and only
bounds pathological catalogs. The `shardplan budget` planner relaxes to guardrail duty
(alerting on drift rather than steering allocation).

### Anti-starvation and hysteresis

- Cold instruments are polled at least once per `max(cold_interval_cycles, 60 s)`.
- Promotion is immediate (quote arrival, edge formation, approval staging).
- Demotion requires K consecutive refreshes below the tier's criteria (default K=3), so
  tier membership doesn't flap with transient graph churn.

## Failure modes

- **Tier blob stale/missing** → all-hot (today's behavior); no dark markets.
- **Valuable market tiered too low** → it quotes slowly (cold ≈ 30–60 s) until the next
  refresh promotes it — degraded, recoverable, observable (tier counts in poll stats).
  Contrast with the budget model's failure mode: the market is fully dark, invisibly.
- **Hot set grows beyond poll capacity** → the adaptive concurrency ramp is the
  backstop, and tier counts in the stats make the pressure visible before it hurts.

## Migration

1. **PR-1 (plumbing, no behavior change):** tier blob encode/decode + adapter reads it,
   defaulting every instrument to hot; `quote_tier_scheduling_enabled: bool = False`.
2. **PR-2 (assignment):** strategy computes and publishes tiers behind the flag; stats
   gain per-tier instrument counts and per-tier quote ages.
3. **PR-3 (rollout):** enable on the baseball shard; compare `cycle_elapsed_secs`,
   gap counts, stale-rejection buckets, and `crossVenueQuoteReadiness` before/after;
   then fleet-wide; then raise `quote_subscription_limit` 2–3× and demote the budget
   planner to guardrail mode.

## Interactions with the shipped near-term work

- The probe-pass ceiling fix and the rebalance pass stay: under tiering they act only in
  the pathological-catalog regime, which is the right residual role.
- The market-pollability registry (tombstones) removes structurally dead work regardless
  of tier — the two compose.
- SXBET stream health work reduces the REST fallback's importance; the fallback also
  respects tiers when it does run.
