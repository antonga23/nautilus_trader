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
- per-worker auth blobs referenced by `scripts/symphony/workers.json`

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
