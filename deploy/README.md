# Deploy assets

Host-side deployment inputs for betting-arbitrage trading nodes.

- `strategy_nodes/` — per-venue node manifests consumed by
  `scripts/deploy/strategy_nodes/deploy_betting_strategy_node.sh`.
- `hosts.example.yaml` — the trading-host registry schema (see below).

## Host registry (`hosts.yaml`)

`hosts.example.yaml` is the committed template for a provider-agnostic, SSH-only host
registry, consumed by `scripts/host/hostctl.sh` for multi-host bootstrap and deploy.
Copy it to `hosts.yaml` (gitignored) and fill in real hosts:

```bash
cp deploy/hosts.example.yaml deploy/hosts.yaml
$EDITOR deploy/hosts.yaml
```

The file is parsed by `scripts/host/registry.py` without PyYAML, so `hosts.yaml`
must keep the example's simple list-of-maps shape: 2-space indentation, scalar
values, one nested map per host (`ssh`), one scalar list (`labels`), and `#`
comments only. Alternatively commit the registry as `hosts.json` (also gitignored)
— any valid JSON of the same shape works and is preferred when both files exist.

Each record describes one SSH-reachable Ubuntu host that runs trading nodes plus the
nodeops dashboard:

| Field | Meaning |
| --- | --- |
| `name` | Stable identifier for the host. |
| `kind` | Always `ssh` — the only transport this toolkit uses. |
| `ssh.host` / `ssh.user` | How the operator connects. |
| `ssh.identity_file_hint` | A local key path or label; not a secret. |
| `provider` | Informational: `aws`, `gcp`, or `other`. No provider-specific code. |
| `region` | Informational region/zone. |
| `nodes_root` | Strategy-node root on the host (default `/opt/cloudbet/strategy-nodes`). |
| `nodeops_url` | The host's nodeops dashboard URL (loopback unless exposed). |
| `labels` | Free-form tags for selection (`dev`, `primary`, ...). |

This registry is desired state only. Runtime truth (container state, heartbeats,
status files) is discovered per host and merged against it. The shape matches the
`kind: ssh` proposal in `docs/ops/betting-arbitrage-nodes.md`, whose runtime copy
lives at `/srv/symphony/worker-state/trading-hosts/hosts.json`.

Real hosts and IPs are operator-supplied and never committed — only the example is
tracked. See `docs/ops/host-management.md` for the full provisioning runbook and the
`hostctl` multi-host operations guide.
