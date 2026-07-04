# Betting Arbitrage Trading Nodes

This repo now contains a repo-owned deployment path for live betting arbitrage nodes based on `nautilus_trader/examples/strategies/betting_arbitrage.py`.

## Runtime contract

- Strategy runtime is config-driven via `BettingArbitrageConfig`.
- Cross-market execution should consume promoted semantic rules from cache when a persisted corpus manifest exists.
- Raw provider snapshots, normalized selections, event candidate rules, reusable semantic templates, template support stats, validation stats, and promoted rules are persisted through the cache `general` table via `RuleStore`.
- Persisted mining uses provider-agnostic canonical event keys derived from sport, participants, and scheduled start time so the same fixture can be bucketed across SXBET, Cloudbet, and transformed Polymarket sports markets.
- Live node manifests live under `deploy/strategy_nodes/betting_arbitrage/`.
- The shared runner is `python -m nautilus_trader.live.strategy_nodes.betting_arbitrage`.
- The builder renders a `TradingNodeConfig` using:
  - `ImportableStrategyConfig` for `BettingArbitrageStrategy`
  - `ImportableConfig` plus adapter factories for live data and execution clients
- Validation mode forces `auto_execute=false` and suppresses execution clients.

## Supported venues today

Supported in the live-node builder:

- `SXBET`
- `POLYMARKET`

Semantic mining corpus coverage currently includes:

- `SXBET` betting instruments
- `Cloudbet` feed/trading snapshots normalized from current `v2` feed and `v4` trading schemas
- `Polymarket` sports `BinaryOption` instruments transformed into betting-style semantic selections when metadata is sufficient

Promotion remains strict:

- `mine-candidates` produces candidate topology only
- `generalize-templates` turns event-specific catalog relationships into reusable selection-pattern templates
- `validate` turns optional settled provider evidence into `RuleValidationStats`
- `promote-templates` lifts catalog-derived templates only when deterministic payoff semantics and repeated corpus observations agree
- `promote` remains available for legacy event-rule promotion with validation stats
- runtime arbitrage execution only consumes promoted execution-safe rules

Explicitly blocked for live-node deployment until adapter hardening is completed:

- `10BET`
- `BLACKBET`
- `WSB`
- `EASYBET`

## Example manifests

- `deploy/strategy_nodes/betting_arbitrage/sxbet-single-venue.json`
- `deploy/strategy_nodes/betting_arbitrage/polymarket-single-venue.json`
- `deploy/strategy_nodes/betting_arbitrage/polymarket-plus-sxbet.example.json`

## Dummy credentials

For manifest validation and CI smoke validation, the builder can synthesize deterministic dummy credentials when:

- `allow_dummy_credentials=true`
- the required venue env vars are not present

This is for validation-only flows. Live execution must provide real env vars on the deployment host.

## Polymarket mixed-venue validation

Use the mixed manifest at `deploy/strategy_nodes/betting_arbitrage/polymarket-plus-sxbet.example.json` for the control-plane mediated validation path.

Recommended worker:

- `codex-a`

Local auth capture:

```bash
./scripts/symphony/capture_worker_auth.sh codex-a
```

Remote install and Secrets Manager persistence:

```bash
./scripts/symphony/install_worker_auths.sh
```

Control-plane UI path:

1. Open `Auth & Providers` and confirm the worker auth shows up on EC2.
2. Open `Strategy Deployments`.
3. Select `polymarket-plus-sxbet.example.json`.
4. Queue a request in `validate_only` mode.
5. Monitor the resulting request row and the worker timeline.

Real live execution for this track requires:

- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_PASSPHRASE`
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_FUNDER`
- `SXBET_API_KEY`
- `SXBET_API_KEYS` for multiple realtime/WebSocket API keys, comma- or newline-separated
- `SXBET_PRIVATE_KEY`
- `SXBET_WALLET_ADDRESS`
- `STRATEGY_NODE_ENV_FILE`
- `STRATEGY_NODE_GHCR_USERNAME`
- `STRATEGY_NODE_GHCR_TOKEN`

After the control-plane request is approved, the EC2 deploy step runs on the EC2
host and uses the same local deploy script as `strategy-node-release`:

```sh
scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh \
  --manifest /tmp/strategy-node-manifest.json \
  --image ghcr.io/antonga23/cloudbet-market-maker/betting-arbitrage-node:<tag> \
  --name betting-arbitrage-node \
  --env-file /tmp/strategy-node.env \
  --registry-user <ghcr-username> \
  --registry-token-file /tmp/strategy-node-ghcr-token
```

The node writes status and heartbeat files to:

- `/opt/cloudbet/strategy-nodes/betting-arbitrage-node/status.json`
- `/opt/cloudbet/strategy-nodes/betting-arbitrage-node/heartbeat.json`

Use `./scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh` against the status file to monitor validation, startup, or recovery progress.

## CLI

Validate a manifest:

```bash
python -m nautilus_trader.live.strategy_nodes.betting_arbitrage validate-manifest \
  --manifest deploy/strategy_nodes/betting_arbitrage/sxbet-single-venue.json
```

Render the trading-node config JSON:

```bash
python -m nautilus_trader.live.strategy_nodes.betting_arbitrage render-node-config \
  --manifest deploy/strategy_nodes/betting_arbitrage/polymarket-single-venue.json
```

Run a node from a manifest:

```bash
python -m nautilus_trader.live.strategy_nodes.betting_arbitrage run \
  --manifest deploy/strategy_nodes/betting_arbitrage/sxbet-single-venue.json
```

Probe live validation-mode semantic coverage without enabling execution:

```bash
python -m nautilus_trader.live.strategy_nodes.betting_arbitrage probe-runtime \
  --manifest deploy/strategy_nodes/betting_arbitrage/sxbet-single-venue.json \
  --timeout-seconds 420 \
  --poll-interval-secs 5 \
  --min-connected-nodes 2 \
  --min-match-instruments 2 \
  --min-positive-margin-candidates 1
```

The runtime node uses the host-mounted semantic cache at:

- `/opt/cloudbet/strategy-nodes/<container-name>/semantic-rule-cache` on the deploy host
- `/var/lib/nautilus-node/semantic-rule-cache` inside the container

### Semantic cache mining mode

`BettingArbitrageNodeManifest.semantic_rule_cache_mode` controls how the node populates that cache at startup. It **defaults to `fresh`** so a deploy always mines against the current live market instead of silently reusing a stale cache (a reused pre-classifier cache masks classifier/topology changes — promoted templates, not code, drive runtime cross-venue edge formation).

| Mode | Behavior |
| --- | --- |
| `fresh` (default) | Reset the cache and re-mine the corpus every boot. If `semantic_rule_cache_default_root` is set, the freshly mined cache is registered there under the config signature. |
| `reuse` | Reuse `semantic_rule_cache_dir` when ready + compatible; else seed; else mine. (Pre-existing behavior — pin this to keep a baked cache.) |
| `default` | Reuse the config-signature entry under `semantic_rule_cache_default_root` when present, compatible, and newer than `semantic_rule_cache_max_age_hours`; otherwise mine fresh and register it. Mine once per trading-node config, reuse across deploys/restarts. |

The signature is `_semantic_cache_scope_key` (enabled venues + sport/league coverage + limits), so `default` shares a mine across deploys of the same config but never across differing configs. A cold single-pass mine promotes fewer templates than an accumulated corpus (`manifest_count` grows across passes), so `default` also preserves accumulated richness between deploys.

Override per deploy via `strategy-node-release` inputs `semantic_cache_mode` / `semantic_cache_default_root` (applied to the deployed manifest copy only; empty keeps the manifest's declared value).

Semantic corpus and promotion workflow:

```bash
.venv/bin/python scripts/betting/semantic_rule_mining.py refresh-corpus \
  --provider cloudbet \
  --initial-window-seconds 86400 \
  --max-window-days 7 \
  --min-events-per-sport 1 \
  --cache-dir artifacts/semantic-cache/live-cloudbet \
  --persist-cache

.venv/bin/python scripts/betting/semantic_rule_mining.py mine-candidates \
  --cache-dir artifacts/semantic-cache/live-cloudbet
.venv/bin/python scripts/betting/semantic_rule_mining.py generalize-templates \
  --cache-dir artifacts/semantic-cache/live-cloudbet
.venv/bin/python scripts/betting/semantic_rule_mining.py promote-templates \
  --cache-dir artifacts/semantic-cache/live-cloudbet
.venv/bin/python scripts/betting/semantic_rule_mining.py report-coverage \
  --cache-dir artifacts/semantic-cache/live-cloudbet
```

Settled bets can be appended as secondary evidence, but they are not required to build the semantic repository:

```bash
.venv/bin/python scripts/betting/semantic_rule_mining.py refresh-corpus \
  --provider cloudbet \
  --include-bets \
  --settled-bets \
  --bet-from-date 2023-01-01T00:00:00Z \
  --bet-to-date 2026-04-27T23:59:59Z \
  --cache-dir artifacts/semantic-cache/live-cloudbet \
  --persist-cache

.venv/bin/python scripts/betting/semantic_rule_mining.py validate \
  --provider cloudbet \
  --cache-dir artifacts/semantic-cache/live-cloudbet \
  --persist-cache
```

Use `--cache-dir` for local or VM operator runs when Postgres cache envs are not configured. It uses the same `Cache.add/get` contract and keeps refresh, mine, generalize, validate, promote, and coverage-report state durable across separate commands.

## Deployment

Runner split for this workflow:

- GCP self-hosted runner: validation, wheel build, image build, release orchestration
- EC2 deploy/trading host: strategy-node runtime host and local deployment runner

Container image build:

- `.docker/strategy_node.dockerfile`
- Archive-based workflow dispatches store reusable image archives in Cloudflare
  R2 under `strategy-node-images/<image_tag>/betting-arbitrage-node.tar.gz`.
  The release workflow reuses that archive when it exists and only rebuilds when
  the archive is missing or `force_rebuild=true`.
- Use `image_transport=archive` for EC2 deploys unless an explicit registry
  release is required. Archive transport avoids GHCR push/pull failures on the
  runtime path and keeps transient release handoffs in repo-owned R2 storage.

Host-side scripts:

- `scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh`
- `scripts/deploy/strategy_nodes/rollback_betting_strategy_node.sh`
- `scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh`

The deploy script writes a runtime manifest with node-local status/heartbeat paths under `/var/lib/nautilus-node/` and starts the node as a Docker container.

## Node Registry And Discovery

Trading-node operations should use a durable registry plus host discovery:

- Registry is the operator source of truth for desired state, host assignment, and last applied config.
- Discovery is the runtime truth from Docker, the node status files, and the heartbeat files.
- The effective node view is the merged result of registry state and discovered runtime state.
- The current release only manages the EC2 trading host locally.
- Future multi-host discovery should reuse the same host record shape and add `kind: ssh` without changing the node contract.

Recommended registry locations:

- `/srv/symphony/worker-state/trading-hosts/hosts.json`
- `/srv/symphony/worker-state/trading-nodes/registry.json`
- `/srv/symphony/worker-state/trading-nodes/discovery/<hostId>.json`

Node lifecycle in v1:

- `start` and `stop` act on the current EC2 host only.
- `restart` may accept a bounded override payload, but not arbitrary secret mutation.
- The UI exposes a host/node overview and a dedicated drilldown page for each
  discovered node, with process state, sessions, stage inference, persisted logs,
  lifecycle controls, and effective config preview.
- `CONTROL_PLANE_READ_ONLY=1` must still allow inventory and log reads, but block node mutation requests.
- The control-plane routes are:
  - `/trading-nodes` for the merged host/node inventory
  - `/trading-nodes/:nodeId` for node detail, sessions, logs, lifecycle actions,
    and effective config preview

The deploy script persists every container run as a session under:

- `/opt/cloudbet/strategy-nodes/<container>/sessions/<sessionId>/node.log`
- `/opt/cloudbet/strategy-nodes/<container>/sessions/<sessionId>/events.jsonl`
- `/opt/cloudbet/strategy-nodes/<container>/current-session.json`

Docker logs are treated as a fallback only; session logs are the source of truth
for the control-plane log viewer and survive container removal/redeployment.

GitHub deploy workflow secrets:

- `STRATEGY_NODE_ENV_FILE`
- `STRATEGY_NODE_GHCR_USERNAME`
- `STRATEGY_NODE_GHCR_TOKEN`

`STRATEGY_NODE_ENV_FILE` should contain the venue runtime env vars required by the selected manifest.
Store it as a secret payload in GitHub Actions or Secrets Manager. Do not commit venue credentials to the repo.

`STRATEGY_NODE_GHCR_TOKEN` should be a dedicated PAT used only for strategy-node image pull access on the deploy host.

## SXBET Discovery And Liquidity

SXBET REST market reads are public, but the API still applies baseline request
limits. Do not try to bypass REST limits with extra API keys. Use API keys for
realtime/WebSocket-capable surfaces and future connection sharding.

The SXBET live manifest intentionally separates three limits:

- `market_discovery_limit`: how many paginated active markets may be scanned
  during bootstrap. Use `null` to scan until SXBET pagination is exhausted.
- `liquidity_probe_limit`: how many discovered markets may have their order
  books probed while searching for liquid candidates.
- `instrument_load_limit`: how many Nautilus instruments are created and sent
  to the data engine.

For the first runtime phase, "liquid" means a market has active orders on both
SXBET outcomes. The node should load only a bounded liquid subset, then report
order-book cycle metrics including discovered/probed/selected markets,
one-sided markets, two-sided markets, quote count, max request latency, and
cycle elapsed time.

Keep execution disabled until the node shows stable two-sided order-book
updates and the betting-arbitrage strategy is observing the loaded instruments.

Arbitrage detections are candidate signals until the diagnostics show they are
fresh, unique, and matched against the same event. The strategy logs structured
fields for opportunity id, canonical pair id, quote timestamps/ages, market ids,
event ids, match type, hedge confidence, same-cycle status, and periodic
quality summaries. Treat `matcher_suspect` and `stale_quote` suppressions as
non-executable until the matcher or data timing is corrected.

## Opportunity Graph Engine

The betting-arbitrage strategy now keeps a persistent opportunity graph in the
Python strategy layer. It still uses `MarketMatcher` as the source of betting
domain rules, but it applies those rules when instruments are loaded or added
instead of searching the whole subscribed instrument universe on every quote.

Runtime flow:

- instrument load/subscription builds graph nodes and hedge edges
- quote ticks update one node's quote state
- only edges connected to that node are re-evaluated
- stale, duplicate, and matcher-suspect opportunities are suppressed before any
  execution path

Graph concepts:

- node: one venue-specific `CryptoBettingInstrument`
- canonical outcome key: semantic event/market/outcome identity independent of
  venue-local instrument identity
- edge: precomputed hedge relationship between two nodes
- quote state: latest odds and timestamps for one node
- candidate: a computed edge opportunity that still must pass freshness and risk
  gates

Semantic rule mining now supplies the graph edge metadata. Betting selections
from live/upcoming provider catalogs are normalized into canonical
market/selection/parameter tuples, converted into payoff vectors, classified
inside event buckets, and generalized into reusable selection-pattern
templates. The graph can retain non-executable semantic edges, such as
draw-no-bet pairs that void on draws or quarter handicap pairs with
half-win/half-loss settlement, but only promoted `COMPLEMENTARY_COVERAGE`
templates without void, partial, or unknown settlement states are evaluated for
live arbitrage execution.

Rule persistence uses the generic Nautilus cache path so configured cache
databases store the same JSON bytes durably:

- `betting:semantic_rules:candidate:<rule_id>`
- `betting:semantic_rules:template:candidate:<template_id>`
- `betting:semantic_rules:template:support:<template_id>`
- `betting:semantic_rules:template:promoted:<template_id>`
- `betting:semantic_rules:promoted:<rule_id>`
- `betting:semantic_rules:validation:<rule_id>`

Accepted and suppressed arbitrage logs include manual-readable instrument
context. Accepted candidates include a `Manual execution plan` section with the
two instruments, events, market names, market params, selections, odds,
suggested stake split, available top-of-book size, quote cycle ids, quote age,
expected profit, and `execution_enabled` state. This is intended to make a
candidate auditable before live execution is enabled.

The runtime quality gate now classifies opportunities before they are counted
as executable:

- `valid`: fresh, same-cycle candidate with sufficient top-of-book size on both
  legs
- `stale`: quote age breached the configured threshold
- `event_mismatch`: the matched instruments do not resolve to the same event
- `line_mismatch`: the matched instruments disagree on market params/line
- `liquidity_insufficient`: suggested stake exceeds top-of-book size on at
  least one leg
- `needs_manual_review`: the candidate is fresh but spans different quote
  cycles or another non-fatal review condition

Use the persisted-log analyzer to turn a session log into a structured summary:

```bash
python scripts/strategy_nodes/analyze_betting_arbitrage_log.py \
  /opt/cloudbet/strategy-nodes/betting-arbitrage-node-sxbet/sessions/<sessionId>/node.log \
  --limit 20
```

Pass `--json` when you want the accepted/suppressed opportunities as machine-
readable records for deeper review.

Text mode also prints the top repeated `matcher_suspect` clusters so you can
see which event/market combinations are driving false positives before touching
execution logic.

## Control plane backend hooks

The control-plane backend now exposes:

- `GET /control/api/deployments/catalog`
- `GET /control/api/deployments/requests`
- `POST /control/api/deployments/requests`

These endpoints list repo manifests and persist deployment requests for later UI/operator flows.

The `Strategy Deployments` panel in the control plane surfaces those same endpoints and shows per-manifest required secrets, dummy fallback keys, and the recommended worker/auth flow.

The worker auth flow is only required when the control plane will start a remote Codex worker on EC2 for `validate_only` or operator-driven execution. The GitHub Actions strategy-node release workflow deploys from the EC2 deploy runner using local repo scripts and does not consume Codex worker auth.
