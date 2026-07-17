# Standing semantic-rule miner

A host-side systemd service that keeps an ever-growing master semantic-rule
cache and feeds every deployed strategy node a slim, validated seed — without
restarting the nodes.

## Why a standing miner

Deploy-time fresh mines only see the markets the venues list at that moment.
Market types rotate over days (league phases, live-only offerings), so a
one-shot mine systematically misses templates. The standing miner re-runs the
node's own bootstrap engine every `MINER_INTERVAL_HOURS` against a persistent
master dir. The master is **never reset**: the rule store's indexes are
append-dedup, so each cycle is purely additive and the corpus converges on a
superset of everything the venues have listed.

## Slim export contract

Nodes must not adopt the raw master (it carries snapshots, normalized records,
candidates, and validation artifacts the runtime never reads). Each cycle the
miner projects the master into a minimal seed containing exactly:

- promoted templates — minus templates whose embedded support
  `last_seen_at` is older than `MINER_TEMPLATE_STALE_DAYS` (support stats are
  serialized inside the template, so the separate support sidecars are
  dropped); optional sport/safety-tier filters exist on
  `export_slim_seed(...)` for narrower seeds
- coverage proofs, and hyperedges whose proof survived
- corpus manifests
- rewritten `{"items": [...]}` indexes for exactly those four sets
- a restricted `keys.json` (only kept keys, by construction of the fresh
  output dir)
- a fresh `.semantic-cache-version` marker (current compatibility version +
  the master's scope)
- a regenerated `.semantic-cache-summary.json`

**Acceptance invariant**: the export is checked with the node runtime's own
`semantic_cache_status(...)` and must report `ready`
(`manifest_count > 0 and promoted_template_count > 0`). A non-ready export is
never distributed — this matters because a node's seed adoption is a blind
`copytree` behind a status + compatibility gate, so the exported tree must be
fully self-consistent and bootable on its own.

## Distribution and hot swap

For every dir under `MINER_NODES_ROOT` containing a `manifest.runtime.json`,
the miner swaps the seed into `semantic-rule-cache-staging/` and
`semantic-rule-cache-seed/`. `os.rename` cannot atomically replace a
non-empty directory, so the previous generation is renamed aside first and the
new tree renamed in: a crash in between leaves the destination absent —
complete-or-absent, never partial.

With `MINER_HOT_SWAP=1` the miner then queues a command file (written via a
temp file + `os.replace` into the node's polled `commands/` dir):

```json
{
  "command": "reload_semantic_cache",
  "id": "miner-<utcstamp>",
  "staging_dir": "/var/lib/nautilus-node/semantic-rule-cache-staging"
}
```

`staging_dir` is the **node container's** view of the staging dir (node dirs
are mounted at `/var/lib/nautilus-node`). This schema is shared with the
node-side command consumer; keep the two in sync. One failing node dir never
blocks distribution to the rest.

## Runtime approach

The miner runs inside the strategy-node docker image (`miner.service` shells
`docker run` with host paths mounted at identical container paths). Installing
`nautilus_trader` from the checkout onto the host would need a full
Rust/Cython toolchain and would drift from the image the nodes actually run;
the image already carries the exact engine.

## Install

```bash
sudo tools/miner/install.sh
```

Then fill in `/opt/cloudbet/miner/miner.env` (600, root-owned, not committed)
with `MINER_NODE_IMAGE` (pre-filled from the newest node `current-image.txt`
when present), `SXBET_API_KEY`, and `CLOUDBET_API_KEY`. Re-running the
installer refreshes the code and keeps an existing unit file and env file.

Run a single cycle by hand:

```bash
docker run --rm --entrypoint python --env-file /opt/cloudbet/miner/miner.env \
  -v /opt/cloudbet/miner:/opt/cloudbet/miner \
  -v /opt/cloudbet/strategy-nodes:/opt/cloudbet/strategy-nodes \
  "$MINER_NODE_IMAGE" /opt/cloudbet/miner/miner_service.py --once
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MINER_MASTER_DIR` | `/opt/cloudbet/miner/master-cache` | Persistent accumulating master cache |
| `MINER_INTERVAL_HOURS` | `6` | Hours between cycles |
| `MINER_MANIFEST` | `/opt/cloudbet/miner/mine-manifest.json` | Mine-scope manifest (venue blocks only) |
| `MINER_NODES_ROOT` | `/opt/cloudbet/strategy-nodes` | Root of deployed node dirs |
| `MINER_HOT_SWAP` | `1` | Queue `reload_semantic_cache` commands after distribution |
| `MINER_TEMPLATE_STALE_DAYS` | `14` | Export-time staleness cutoff on template support `last_seen_at` |
| `MINER_MAX_DISK_GB` | `10` | Master-size warning threshold |
| `MINER_LOG_LEVEL` | `INFO` | Service log level |

## Growth caveats

- The master corpus is unbounded by design (snapshots + normalized records
  accumulate every cycle). The disk guard only warns; compaction of the
  corpus prefixes is an explicit follow-up.
- The engine promotes templates but never demotes them. The export-time
  staleness filter is the counter: templates the venues stopped listing age
  out of node seeds after `MINER_TEMPLATE_STALE_DAYS`, while remaining in the
  master for audit.

## Tests

```bash
pytest tools/miner/tests
```

Hermetic: the mine phase (venue corpus refresh) is mocked; slim export and
distribution run against real rule stores on tmp dirs and are asserted with
the real `semantic_cache_status`. No test touches a venue API.
