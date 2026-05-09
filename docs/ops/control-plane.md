# Symphony Control Plane Runbook

## Host

- EC2 host: `13.51.235.85`
- SSH user: `ubuntu`
- AWS region: `eu-north-1`
- Service: `control-plane.service`
- HTTP entrypoint: nginx site `symphony-control-plane`
- Backend bind: `127.0.0.1:4100`

## Required Secrets

The control plane reads `/srv/symphony/symphony.env`, rendered from AWS Secrets Manager secret `cloudbet-market-maker/credentials` in `eu-north-1`.

Required keys:

- `LINEAR_API_KEY`
- `GITHUB_TOKEN`
- `GH_TOKEN` if different from `GITHUB_TOKEN`
- `GITHUB_REPO`, default `antonga23/cloudbet-market-maker`
- `LINEAR_PROJECT_ID`
- `LINEAR_TEAM_ID`
- `LINEAR_TEAM_KEY`, default `BET`
- `SYMPHONY_DASHBOARD_USER`
- `SYMPHONY_DASHBOARD_PASSWORD`
- `AWS_REGION=eu-north-1`
- `AWS_DEFAULT_REGION=eu-north-1`
- `CONTROL_PLANE_PUBLIC_BASE_URL=https://controlplane.cheapestgames.online`

Provider-specific keys are optional until those providers are enabled:

- `OPEN_ROUTER_API_KEY` or `OPENROUTER_API_KEY`
- `ANTIGRAVITY_GOOGLE_CLIENT_ID`
- `ANTIGRAVITY_GOOGLE_CLIENT_SECRET`
- `ANTIGRAVITY_GOOGLE_SCOPES`
- `GCP_SERVICE_ACCOUNT_JSON_B64` for the ephemeral GCP worker/service-account material
- `GCP_GCLOUD_CONFIG_TAR_B64` for a sanitized `info@unlimitedgames.shop` `gcloud` config bundle when org policy blocks service-account key creation
- per-worker auth blobs referenced by `scripts/symphony/workers.json`

Strategy-node deployment envs and secrets:

- `STRATEGY_NODE_ENV_FILE`
- `STRATEGY_NODE_GHCR_USERNAME`
- `STRATEGY_NODE_GHCR_TOKEN`
- `SXBET_API_KEY`
- `SXBET_API_KEYS`
- `SXBET_PRIVATE_KEY`
- `SXBET_WALLET_ADDRESS`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_PASSPHRASE`
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_FUNDER`

Store strategy-node venue credentials in secret payloads only. Do not commit them to the repo or paste them into operator notes. `STRATEGY_NODE_GHCR_TOKEN` should be a dedicated image-pull PAT rather than a shared general-purpose token.

## Semantic Mining VM

GCP project `shining-sol-493421-h6` currently has two running VMs. Use
`semantic-rule-miner-20260426` (`e2-custom-6-10240`, Ubuntu 24.04, 100GB disk)
for Cloudbet/SXBET/Polymarket corpus refresh and semantic template mining. Do
not run mining jobs on `instance-20260415-214825`; that host is reserved for
runner capacity.

GCP service-account restoration is handled with:

```sh
./scripts/symphony/restore_gcp_service_account_from_secret.sh
```

The semantic mining operator CLI exposes the same restore path for batch jobs and ephemeral workers:

```sh
.venv/bin/python scripts/betting/semantic_rule_mining.py restore-gcp-auth
```

To upsert a rotated service-account JSON payload into the shared secret without exposing raw JSON in repo files:

```sh
./scripts/symphony/upsert_gcp_service_account_secret.sh /path/to/service-account.json
```

If the project enforces `constraints/iam.disableServiceAccountKeyCreation`, persist a sanitized `gcloud` bundle instead:

```sh
./scripts/symphony/upsert_gcloud_auth_bundle_secret.sh /path/to/gcloud-config-dir
```

The gcloud bundle script stores only the active config, credential databases, and config files. It deliberately excludes SDK caches, logs, and virtualenv contents so the payload remains within Secrets Manager limits.

## Deploy

From the repo root on a workstation with SSH access:

```sh
./scripts/symphony/sync_to_ec2.sh
ssh -i ./ec2-dev-betting-project.pem ubuntu@13.51.235.85 \
  'cd /srv/symphony/control-repo && CONTROL_PLANE_DOMAIN=controlplane.cheapestgames.online CONTROL_PLANE_CERTBOT_EMAIL=info@cheapestgames.online bash scripts/symphony/install_control_plane.sh'
```

The installer:

- renders `/srv/symphony/symphony.env` from Secrets Manager
- installs Node.js 20 locally on the EC2 if needed
- runs `npm ci` and `npm run build` for the React app
- installs the systemd unit
- installs the nginx site with basic auth
- runs Certbot only when `controlplane.cheapestgames.online` resolves to the EC2 public IP

## Codex Worker Flow

The recommended worker for mixed-venue validation is `codex-a`.

This auth flow is only required when the control plane will start a remote Codex worker on EC2. The GitHub Actions strategy-node release workflow does not use Codex worker auth; its deploy job runs on the EC2 deploy runner and uses the repo deployment scripts locally.

Operator flow:

1. Capture local ChatGPT auth on a browser-capable machine:

   ```sh
   ./scripts/symphony/capture_worker_auth.sh codex-a
   ```

2. Push the captured `auth.json` to EC2 and persist it to AWS Secrets Manager:

   ```sh
   ./scripts/symphony/install_worker_auths.sh
   ```

3. In the dashboard, open `Auth & Providers` and confirm `Remote auth: present` and `Secret stored: yes` for the worker.

4. Open `Strategy Deployments`, select `polymarket-plus-sxbet.example.json`, and queue a request in `validate_only` mode first.

5. Monitor request state in `Strategy Deployments` or via `GET /control/api/deployments/requests`.

If the request is later promoted to a real deploy, the missing live secrets must be present on the deployment host or in the env file used by `deploy_betting_strategy_node.sh`.
Authenticated image pulls for the remote host also require `STRATEGY_NODE_GHCR_USERNAME` and `STRATEGY_NODE_GHCR_TOKEN` if the image is private.

## Read-Only Dev Plane

`CONTROL_PLANE_READ_ONLY=1` turns the control plane into a non-mutating environment. When enabled, every `POST`, `PUT`, `PATCH`, and `DELETE` request is rejected with `403`, while `GET`, `HEAD`, and `OPTIONS` continue to work.

Use this mode for non-production control-plane deployments that should expose live state without any path to mutate production Symphony, Linear, or GitHub state.

This includes strategy-node operations:

- `GET /control/api/deployments/catalog` remains available for manifest discovery.
- `GET /control/api/deployments/requests` remains available for request history.
- `POST /control/api/deployments/requests` is blocked in read-only mode.

Local smoke test:

```sh
node --test scripts/symphony/control_plane/__tests__/read_only.test.mjs
```

## Trading Node Ops V1

The first trading-node operations release should treat the EC2 host as the live runtime source of truth and a durable registry as the operator record.

- Durable node and host records belong under `/srv/symphony/worker-state`.
- Runtime truth comes from host discovery:
  - Docker containers
  - `/opt/cloudbet/strategy-nodes/*`
  - `status.json`
  - `heartbeat.json`
  - `release.json`
  - `manifest.runtime.json`
  - `current-image.txt`
- Effective node state should merge registry intent with discovered runtime state.
- V1 lifecycle actions are local-host only:
  - `start`
  - `stop`
  - `restart`
  - `restart with bounded override`
- Restart overrides must be narrow and operator-safe:
  - logging level
  - strategy knobs
  - venue timing/filter knobs
  - validation mode
  - execution enablement flags
  - image ref override
- Credentials stay externalized in env/Secrets Manager payloads and are never edited from the UI.
- Multi-host discovery should be designed into the registry schema now, but only the current EC2 host is active in this release.

Control-plane UI routes:

- `/control` for the mission-control view
- `/trading-nodes` for the trading-node host/node overview
- `/trading-nodes/:nodeId` for the GitHub-runner-style node drilldown
- `/nodes` redirects to `/trading-nodes` for compatibility

Trading-node API surface:

- `GET /control/api/trading-nodes`
- `GET /control/api/trading-nodes/:nodeId`
- `GET /control/api/trading-nodes/:nodeId/sessions`
- `GET /control/api/trading-nodes/:nodeId/sessions/:sessionId`
- `GET /control/api/trading-nodes/:nodeId/logs?sessionId=current&limit=...`
- `GET /control/api/trading-nodes/:nodeId/logs/stream?sessionId=current`
- `POST /control/api/trading-nodes/:nodeId/start`
- `POST /control/api/trading-nodes/:nodeId/stop`
- `POST /control/api/trading-nodes/:nodeId/restart`
- `POST /control/api/trading-nodes/:nodeId/render-config`

The legacy `/control/api/nodes*` endpoints remain as compatibility aliases.
Trading-node logs are persisted per session under
`/opt/cloudbet/strategy-nodes/<container>/sessions/<sessionId>/`.

`render-config` remains available in read-only mode because it is a non-mutating preview. Start/stop/restart are blocked when `CONTROL_PLANE_READ_ONLY=1`.

## Mixed-Venue Deploy Start And Monitor

If you are doing the actual EC2 rollout after the request is approved:

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

The runtime status files are written under:

- `/opt/cloudbet/strategy-nodes/betting-arbitrage-node/status.json`
- `/opt/cloudbet/strategy-nodes/betting-arbitrage-node/heartbeat.json`

Monitor the node with:

```sh
./scripts/deploy/strategy_nodes/wait_for_strategy_node_status.sh \
  --status-file /opt/cloudbet/strategy-nodes/betting-arbitrage-node/status.json \
  --timeout-seconds 600 \
  --success-status running,completed,validated,built
```

## DNS And TLS

Let's Encrypt/Certbot cannot issue a trusted certificate for the raw EC2 IP address. The domain must resolve first.

Create this DNS record at the authoritative DNS provider for `cheapestgames.online`:

```text
controlplane.cheapestgames.online.  A  13.51.235.85
```

After DNS propagation, rerun `scripts/symphony/install_control_plane.sh` on the EC2. The script will use Certbot's nginx integration to issue the certificate and enable HTTPS redirect.

## Validation

```sh
systemctl is-active control-plane.service
systemctl is-active nginx
sudo nginx -t
curl -fsS http://127.0.0.1:4100/control/api/overview | jq -r .generatedAt
curl -I -H 'Host: controlplane.cheapestgames.online' http://127.0.0.1/
```

Expected external behavior before DNS/TLS is configured:

- `http://controlplane.cheapestgames.online` only works when resolved manually to the EC2 IP.
- nginx returns `401 Unauthorized` without basic auth.
- HTTPS is not available until the DNS `A` record exists and Certbot completes.
