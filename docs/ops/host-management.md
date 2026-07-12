# Trading-node host management

Portable, provider-agnostic (SSH-only) toolkit for running betting-arbitrage trading
nodes and the nodeops dashboard on any Ubuntu VM. Everything here is host-ops
scripting plus systemd units — it does not touch the trading-node Python.

## Why this exists

A single betting-dev EC2 host wedged: the root disk hit `ENOSPC`, two ~9 GB
processes were OOM-killed, Docker failed, and the host was unreachable until a manual
reboot. There was no auto-remediation. This toolkit adds:

- continuous host health monitoring with auto-remediation (disk + OOM + containers),
- a committed disk-hygiene routine (replacing an ad-hoc host cron),
- a memory preflight guard so a deploy can refuse to over-subscribe the box,
- a provider-agnostic host registry so any SSH-reachable VM can host nodes.

All components live under `scripts/host/` and are idempotent, POSIX/bash-portable,
and shellcheck-clean.

## Components

- `scripts/host/bootstrap.sh` — provision a fresh Ubuntu host end to end.
- `scripts/host/node-host-health.sh` (+ `.service` / `.timer`) — the ~2 min health
  monitor with auto-remediation and the memory preflight subcommand.
- `scripts/host/disk-hygiene.sh` (+ `.service` / `.timer`) — the scheduled daily
  cleanup.
- `scripts/host/host_common.sh` — shared, unit-tested helper library.
- `scripts/host/install-node-host-health.sh` / `install-disk-hygiene.sh` — idempotent
  systemd installers (same pattern as `scripts/ci/install_runner_hygiene.sh`).
- `deploy/hosts.example.yaml` — the host registry schema.

The monitor and hygiene units install into `/opt/cloudbet/host-tools/`; their config
lives in `/etc/cloudbet/node-host-health.conf` and
`/etc/cloudbet/node-host-disk-hygiene.conf` (seeded once, operator overrides
preserved on re-run).

## Provision a new host

The host must have a checkout of this repo (the bootstrap reuses
`tools/nodeops/install.sh` and the deploy scripts from it). Secrets are never
committed — you supply the venue env file.

```bash
# 1. Put the repo on the host (scp a checkout, or git clone on the host).
scp -r . ubuntu@<host>:/opt/cloudbet/cloudbet-market-maker

# 2. Run the bootstrap over SSH, supplying the venue env file by path...
ssh ubuntu@<host> 'sudo /opt/cloudbet/cloudbet-market-maker/scripts/host/bootstrap.sh \
  --env-file /home/ubuntu/venue.env \
  --webhook https://hooks.example.com/xyz'

# ...or stream the env file over stdin so it never lands on disk elsewhere:
ssh ubuntu@<host> 'sudo /opt/cloudbet/cloudbet-market-maker/scripts/host/bootstrap.sh \
  --env-file - --webhook https://hooks.example.com/xyz' < ./venue.env
```

`bootstrap.sh` installs Docker + python3 if absent, creates the strategy-node root,
installs the venue env file (mode `0600`), optionally logs in to GHCR
(`--registry-user` + `--registry-token-file`), brings up nodeops, and installs the
health monitor and disk-hygiene timers. It is safe to re-run. Flags:
`--nodes-root`, `--env-file`, `--env-dest`, `--registry-user`,
`--registry-token-file`, `--nodeops on|off`, `--health on|off`, `--hygiene on|off`,
`--webhook`.

## Health monitor

`node-host-health.service` runs `node-host-health.sh check` every ~2 minutes.

### Disk

- Reads `/` usage. Over the soft threshold (`NODE_HOST_DISK_PCT`, default 85%) it
  auto-remediates: `docker image prune -f`, `docker container prune -f`, and gzips
  `node.log` / `events.jsonl` in session dirs beyond the newest
  `NODE_HOST_SESSION_KEEP` (default 5) per node, then re-checks.
- If still over the hard threshold (`NODE_HOST_DISK_HARD_PCT`, default 92%) it alerts.

### Memory / OOM

- Reads `MemAvailable`. Below the floor (`NODE_HOST_MEM_FLOOR_MB`, default 1500) it
  alerts.
- Scans `journalctl -k` (falling back to `dmesg`) for a recent OOM kill and alerts
  with the killed process, de-duplicated so the same event alerts once.
- The preflight guard is the specific anti-recurrence measure for the OOM incident:

  ```bash
  # A deploy calls this first; non-zero exit ⇒ refuse to start the node.
  node-host-health.sh preflight --need-mb 3072
  ```

  It refuses when `MemAvailable - need_mb` would drop below the floor, so the box is
  never over-subscribed into an OOM.
- Capacity heuristic: `node-host-health.sh recommend` prints
  `floor((MemTotal - NODE_HOST_MEM_FLOOR_MB) / NODE_HOST_PER_NODE_MB)` where a node's
  steady-state budget is `NODE_HOST_PER_NODE_MB` (default 3072 MB, ~3 GB). Example: a
  16 GB host reserves 1.5 GB and fits ~4 nodes.

### Containers

For each `betting-arbitrage-node*` directory under the nodes root, it alerts when the
container is not `running`, its `heartbeat.json` `at` is older than
`NODE_HOST_HEARTBEAT_STALE_SECS` (default 180), or its `status.json` `updatedAt` is
older than `NODE_HOST_STATUS_STALE_SECS` (default 300). With `--auto-restart` (env
`NODE_HOST_AUTO_RESTART`, default on) it `docker restart`s a wedged container, guarded
by a restart-storm cap (`NODE_HOST_MAX_RESTARTS_PER_HOUR`, default 3) tracked in a
state file under `NODE_HOST_STATE_DIR` (default `/var/lib/cloudbet/node-host-health`).

### Alerts

Alerts POST the same JSON shape the nodeops sampler uses
(`{ts_utc, node, condition, severity, detail}`, plus `source`/`host`) to the webhook.
The webhook is resolved as: `--webhook`/`NODE_HOST_WEBHOOK`, then `NODEOPS_ALERT_WEBHOOK`,
then a best-effort read of the running nodeops unit's `NODEOPS_ALERT_WEBHOOK`. Every
action is logged to journald.

## Disk hygiene

`disk-hygiene.service` runs `disk-hygiene.sh` daily (`disk-hygiene.timer`) as the
steady routine (the monitor handles urgent pressure between runs):

- journald vacuum (`DISK_HYGIENE_JOURNAL_MAX_SIZE` / `_MAX_AGE`),
- docker prune (dangling images, stopped containers, build cache older than
  `DISK_HYGIENE_DOCKER_BUILD_CACHE_UNTIL`),
- session-log rotation (keep newest `DISK_HYGIENE_SESSION_KEEP` per node, gzip older,
  delete dirs past `DISK_HYGIENE_SESSION_MAX_AGE_DAYS`),
- archive retention under `<nodes_root>/archives`
  (`DISK_HYGIENE_ARCHIVE_RETENTION_DAYS`).

## Host registry

`deploy/hosts.example.yaml` is the provider-agnostic, SSH-only registry schema (see
`deploy/README.md` for the field table). Copy it to the gitignored `deploy/hosts.yaml`
for real hosts. It is the seam Phase 2 (multi-host deploy targeting) and Phase 3
(nodeops multi-host roll-up) build on. Its `kind: ssh` record shape matches the
proposal in `betting-arbitrage-nodes.md`, whose runtime copy lives at
`/srv/symphony/worker-state/trading-hosts/hosts.json`.

## Add a new VM in 3 steps

Works identically for EC2, GCP, or any other Ubuntu VM — the only contract is SSH.

1. Add a record to `deploy/hosts.yaml` (copy a block from `hosts.example.yaml`, set
   `provider` to `aws` / `gcp` / `other`).
2. `scp` a repo checkout to the host and run `scripts/host/bootstrap.sh` over SSH with
   the venue env file (see above).
3. Deploy a node and confirm health:

   ```bash
   ssh <host> 'sudo /opt/cloudbet/cloudbet-market-maker/scripts/host/node-host-health.sh recommend'
   ```

GCP note: there is no cloud-specific code — a GCP VM is just another SSH target. The
GCP account/billing must be active (it was disabled earlier in this project), and the
VM must allow inbound SSH.

## Self-test

The pure-logic helpers (disk-pct parse, memory preflight, capacity heuristic,
restart-storm guard, session-rotation selection) are unit-tested without touching the
host:

```bash
scripts/host/node-host-health.sh self-test
```
