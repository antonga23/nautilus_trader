# Betting Arbitrage Trading Nodes

This repo now contains a repo-owned deployment path for live betting arbitrage nodes based on `nautilus_trader/examples/strategies/betting_arbitrage.py`.

## Runtime contract

- Strategy runtime is config-driven via `BettingArbitrageConfig`.
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
- `STRATEGY_NODE_HOST`
- `STRATEGY_NODE_SSH_USER`
- `STRATEGY_NODE_SSH_KEY`
- `STRATEGY_NODE_ENV_FILE`
- `STRATEGY_NODE_GHCR_USERNAME`
- `STRATEGY_NODE_GHCR_TOKEN`

After the control-plane request is approved, the EC2 deploy step uses:

```sh
ssh -i ./ec2-dev-betting-project.pem ubuntu@13.51.235.85 \
  'chmod +x /tmp/deploy_betting_strategy_node.sh && \
   /tmp/deploy_betting_strategy_node.sh \
     --manifest /tmp/strategy-node-manifest.json \
     --image ghcr.io/antonga23/cloudbet-market-maker/betting-arbitrage-node:<tag> \
     --name betting-arbitrage-node \
     --env-file /tmp/strategy-node.env \
     --registry-user <ghcr-username> \
     --registry-token-file /tmp/strategy-node-ghcr-token'
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

## Deployment

Runner split for this workflow:

- GCP self-hosted runner: validation, wheel build, image build, release orchestration
- EC2 deploy/trading host: strategy-node runtime host and SSH deployment target

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

- `STRATEGY_NODE_HOST`
- `STRATEGY_NODE_SSH_USER`
- `STRATEGY_NODE_SSH_KEY`
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

Accepted and suppressed arbitrage logs include manual-readable instrument
context. Accepted candidates include a `Manual execution plan` section with the
two instruments, events, selections, odds, suggested stake split, expected
profit, and `execution_enabled` state. This is intended to make a candidate
auditable before live execution is enabled.

## Control plane backend hooks

The control-plane backend now exposes:

- `GET /control/api/deployments/catalog`
- `GET /control/api/deployments/requests`
- `POST /control/api/deployments/requests`

These endpoints list repo manifests and persist deployment requests for later UI/operator flows.

The `Strategy Deployments` panel in the control plane surfaces those same endpoints and shows per-manifest required secrets, dummy fallback keys, and the recommended worker/auth flow.

The worker auth flow is only required when the control plane will start a remote Codex worker on EC2 for `validate_only` or operator-driven execution. The GitHub Actions strategy-node release workflow deploys over SSH using `STRATEGY_NODE_*` secrets and does not consume Codex worker auth.
