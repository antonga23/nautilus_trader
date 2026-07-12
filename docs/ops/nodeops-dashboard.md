# Nodeops Dashboard — deployed trading-node CRUD + monitoring

Single self-contained service (`tools/nodeops/`) that runs on the deploy host and gives
operators a browser view to **view, query, and manage deployed strategy nodes**, plus the
metrics history behind the cross-venue runtime gate (#210) and the odds-vs-outcome data
feed for devig validation (#251).

## Design

### Goals

- List every deployed node with live health: container state, heartbeat age, image tag,
  instruments subscribed, graph nodes/edges, quoted edges, `crossVenueCandidateCount`,
  RAG bands, arbitrage counters (raw/valid/executable/executed).
- Answer #210's monitoring question over time: as markets/instruments load, does the
  node find arbs? Time-series history per node, sampled continuously, queryable.
- CRUD: **Create** (deploy a manifest to a new container), **Read** (list/detail/history),
  **Update** (restart, redeploy with a different image/manifest), **Delete** (stop +
  remove, with the node directory archived first).
- Zero new runtime dependencies on the host: Python stdlib only (`http.server`,
  `sqlite3`, `subprocess` → `docker` CLI), single static HTML frontend (repo precedent:
  `tools/bonus-ev` is a no-build static page). Everything auditable in two files.

### Non-goals

- Not a trading UI: no order entry, no execution arming. `live_execution_armed` stays a
  deploy-workflow concern.
- Not multi-host: v1 manages the nodes on the host it runs on (`/opt/cloudbet/strategy-nodes`).

### Architecture

```text
tools/nodeops/
  server.py      # stdlib HTTP server: JSON API + static frontend + sampler thread
  index.html     # single-page frontend (vanilla JS, inline SVG charts, no CDN)
  nodeops.service# systemd unit (Environment=NODEOPS_* / ExecStart=python3 server.py)
  install.sh     # idempotent installer for the deploy host
```

- **Sampler thread** (in-process): every `NODEOPS_SAMPLE_SECS` (default 60) reads each
  node's `status.json` (`runtimeProbe`) + `heartbeat.json` + `docker inspect`/`stats`,
  appends a row to sqlite (`/opt/cloudbet/nodeops/nodeops.db`, WAL). Columns cover the
  #210 gate: subscribed instruments, graph nodes/edges, quoted edges, semantic matches,
  `crossVenueCandidateCount`, ragBands (green/amber/red), arbitrage counters from
  `strategyStats`, container mem/cpu. Retention pruned to `NODEOPS_RETENTION_DAYS`
  (default 30).
- **API** (HTTP Basic auth, credentials from `NODEOPS_USER`/`NODEOPS_PASSWORD` env):
  - `GET  /api/nodes` — list with latest sample + container state.
  - `GET  /api/nodes/{name}` — manifest (already secrets-free), full latest probe,
    container detail.
  - `GET  /api/nodes/{name}/history?hours=24&metrics=a,b` — time series for charts.
  - `POST /api/nodes` — deploy: `{manifest_path, container_name, image}` → runs
    `scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh` (the same script the
    release workflow uses) with the host env file; async job, status pollable at
    `GET /api/jobs/{id}`. The chosen manifest is parsed and **rejected unless it is
    validation-safe** (`validation_mode` set, `auto_execute`/`live_execution_armed`/
    same-/cross-venue-live-execution flags off, every venue `execution_enabled` false) —
    so the committed `*-live-pilot`/`*-execution-readiness` manifests can never be deployed
    from here. This enforces the non-goal in code, not just by convention.
  - `POST /api/nodes/{name}/restart` · `POST /api/nodes/{name}/stop` ·
    `POST /api/nodes/{name}/start` — docker lifecycle.
  - `DELETE /api/nodes/{name}` — stop + `archive_strategy_nodes.sh` the node dir + remove
    container. Two-step confirm in the UI.
  - `GET  /api/nodes/{name}/approvals` — the strategy's `executionApprovals` probe block:
    staged arbs awaiting operator approval (manual execution approval mode), counters,
    and recent decisions.
  - `POST /api/nodes/{name}/approvals/{approval_id}/approve` ·
    `.../reject` — queue an operator decision for a staged arb. The server drops a
    command file into `<node dir>/commands/`; the strategy polls the directory, re-runs
    its **full live gate stack on fresh quotes** (arming, kill switch, caps, staleness)
    before approving anything into `_execute_arbitrage`, and acks through
    `executionApprovals.recent_decisions` on a later probe. Approval is necessary,
    never sufficient. 404 when the id is not currently pending; audited like every
    other mutating control. Pending records are strategy-memory only — a node restart
    clears the queue (the deploy script also clears stale command files).
  - Mutating endpoints are also gated by `NODEOPS_READONLY=1` (deploy the service
    read-only first; flip when comfortable).
- **Frontend**: table view (state chips, heartbeat age, xvCand, RAG chips, arb counters)
  → node drawer with sparkline charts (edges, quotedEdges, xvCand, arbs, mem) over a
  selectable window, manifest viewer, a Pending trades tab (per-arb venue pair, odds,
  stakes, fee-adjusted margin, expected profit, expiry countdown, Approve/Reject with
  confirm), and the action buttons.

### Security

- **Deploys are validation-only in code** (see `POST /api/nodes` above): the server is
  structurally incapable of arming live execution — the deploy endpoint parses the target
  manifest and refuses anything that is not data-only.
- **Binds `127.0.0.1:8090` by default.** Exposing to the operator network is an explicit
  choice (`NODEOPS_HOST=0.0.0.0`) and the server **refuses to start** on a non-loopback
  bind unless HTTP Basic auth is configured — no silent auth-off-on-a-public-port. Set
  `NODEOPS_USER`/`NODEOPS_PASSWORD` (constant-time compared) via a `systemctl edit` drop-in;
  the shipped `CHANGE_ME` placeholders are treated as unset. Restrict TCP `8090` to the
  operator IP range in the host security group as defence in depth.
- Mutating endpoints are gated by `NODEOPS_READONLY` (default `1`). Node names and image
  refs must start alphanumeric (rejecting `-flag`/`.`/`..`), and docker lifecycle calls use
  a `--` end-of-options separator; request bodies are size-capped.
- The API never reads or returns venue credential env vars; deploys reuse the host's
  existing `strategy-node.env` file by path, never exposing its contents.

### #251 data feed (devig validation)

The sampler also records, per node sample, the probe's `topPositiveCandidates` /
`topNegativeNearMisses` odds snapshots (when present) into an `odds_samples` table.
Settlement outcomes continue to come from the semantic corpus (`refresh-corpus` +
`validate` persist settled evidence in the rule store). `scripts/betting/`
`devig_accuracy_benchmark.py --corpus <rule-cache-dir>` (follow-up) will join stored
odds against settled results to score devig methods on real data once enough
accumulates — tracked in #251.

## Operating

```bash
# on the deploy host
sudo tools/nodeops/install.sh            # installs /opt/cloudbet/nodeops + systemd unit
sudo systemctl status nodeops            # health
# browse http://<host>:8090 (basic auth)
```

`install.sh` is idempotent: copies `server.py`/`index.html`, creates the db dir, writes
the unit if missing (leaving existing `NODEOPS_*` overrides intact), enables + restarts.
