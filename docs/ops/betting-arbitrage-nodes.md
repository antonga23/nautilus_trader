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

Container image build:
- `.docker/strategy_node.dockerfile`

Host-side scripts:
- `scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh`
- `scripts/deploy/strategy_nodes/rollback_betting_strategy_node.sh`
- `scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh`

The deploy script writes a runtime manifest with node-local status/heartbeat paths under `/var/lib/nautilus-node/` and starts the node as a Docker container.

## Control plane backend hooks

The control-plane backend now exposes:

- `GET /control/api/deployments/catalog`
- `GET /control/api/deployments/requests`
- `POST /control/api/deployments/requests`

These endpoints list repo manifests and persist deployment requests for later UI/operator flows.
