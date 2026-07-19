# shardplan — smart-sharding allocator

Measures per-sport instrument weights from the live fleet, bin-packs whole
sports into capacity-bounded shard nodes, and emits deployable manifests.
This replaces hand-picking "one manifest per sport" with a measured
allocation: big sports get dedicated nodes, small and seasonal sports share a
node, and out-of-season sports drop out of the plan automatically.

## Usage

Run from the repo root (no dependencies beyond the repo itself; validation
imports the repo manifest loader).

```sh
# Plan only: allocation table + fleet diff + deploy commands
python -m tools.shardplan dry-run --nodes-root /opt/cloudbet/strategy-nodes --capacity 2000

# Plan undeployed sports or override measurements
python -m tools.shardplan dry-run --static-weights weights.json

# Write manifests (each one is loaded, built, and linted before reporting)
python -m tools.shardplan emit \
  --nodes-root /opt/cloudbet/strategy-nodes \
  --capacity 2000 \
  --out deploy/strategy_nodes/betting_arbitrage
```

`--nodes-root` accepts the on-host layout (`<node>/status.json`) or a flat
directory of status JSON files (e.g. scp'd copies). `--static-weights` takes
`{"sport": total}` or `{"sport": {"VENUE": instruments}}`; static entries
replace the measured entry for that sport, so it also serves as a manual
override.

## Where weights come from

`status.json` -> `runtimeProbe.venueCoverage`:

- `nodeCounts` — per-venue live instrument count (the weight itself),
- `eventSportCounts` — apportions each venue's instruments across sports
  (exact for single-sport nodes, largest-remainder proportional for grouped
  nodes),
- `quoteSubscriptionLimitExceededCounts` — the starvation signal, reported in
  the weight table so a starving sport is visible when choosing capacity.

## Invariants

- **Whole-sport atomic** — a sport is never split across bins. League-level
  splitting of an over-capacity sport is a future extension; today such a
  sport gets a dedicated bin flagged `OVER-CAPACITY` in the dry-run output.
- **All venues co-located per bin** — every bin's manifest carries CLOUDBET,
  SXBET, and POLYMARKET scoped to the same sport set (SXBET via its numeric
  sport ids), so cross-venue edges keep forming inside each node. A sport
  with no SXBET listing simply drops the SXBET venue for that bin.
- **Grouped bins set `strategy.sport_filter: null`** — `sport_filter` is a
  post-filter on the merged topology, so it can only be non-null for a
  single-sport bin. Scoping is done per venue via `sport_keys` / `sport_ids`.
- **Structurally unarmed** — emitted manifests are validation-mode, data-only:
  `auto_execute`, `live_execution_armed`, `value_execution_enabled` false and
  `execution_enabled` false on every venue. Arming remains a deliberate,
  separate step outside this tool.

## Budget scaling

Per-venue budgets (`instrument_load_limit`, `market_discovery_limit`,
`quote_subscription_limit`, `top_markets_by_depth`) are

```
max(template_value, ceil(template_value * bin_weight / capacity))
```

The template (the checked-in tennis shard, which carries the quoted-edge
subscription priority shape: CLOUDBET cap 400 at 1s poll, SXBET stream
transport, staged middles) is the proven per-sport floor; budgets only grow,
linearly, once a bin's measured weight exceeds the capacity.

## Capacity guidance

Default capacity is 2000 instruments per node — the scale the per-sport
shards were validated at. The right number is a moving target: once the
incremental-graph and GIL-release work lands, a single node sustains more
instruments, so re-measure (dry-run against the live fleet) and raise
`--capacity` rather than adding nodes.

## Seasonality

Zero-weight sports produce no bin. An out-of-season sport disappears from the
plan on the next run and reappears (usually into a grouped bin) once its
events return. Re-running dry-run against the live fleet is cheap; treat a
sport-set change in the diff as the trigger to redeploy.

## Adoption

This is the standard path for shard redeploys: run `dry-run` against the
fleet, review the diff (bins marked `NEW`, existing shard manifests with no
matching bin are retire candidates), `emit` into
`deploy/strategy_nodes/betting_arbitrage/`, commit, and dispatch the printed
`gh workflow run strategy-node-release.yml ...` command per changed manifest.
The tool never deploys anything itself.
