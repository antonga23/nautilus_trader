# nodeops data shipper

Host-side sidecar that persists the on-host nodeops data and trading-node logs into an
AWS RDS Postgres instance for durable, queryable retention.

It is **purely additive and zero-restart**. It does not import `nautilus_trader`, does
not touch the nodeops service, and does not touch the trading nodes. It opens the
nodeops SQLite database **read-only** and tails the node directory files, so nothing it
does can block or corrupt the concurrent nodeops writer or the running nodes.

## What it ships

| Postgres table | Source | Notes |
| --- | --- | --- |
| `samples` | nodeops.db `samples` | metric time series; PK `(node, ts_utc)` |
| `odds_samples` | nodeops.db `odds_samples` | `payload_json` text → `payload` jsonb |
| `audit_log` | nodeops.db `audit_log` | control-action audit trail |
| `status_snapshots` | `<node>/status.json` | full `runtimeProbe` blob, deduped by content hash |
| `node_logs` | `<node>/sessions/<id>/node.log` | line-by-line, exactly-once by `(node, session, seq)` |
| `node_events` | `<node>/sessions/<id>/events.jsonl` | parsed JSON events |
| `arb_pnl_samples` | `status.json` `runtimeProbe.arbPositionPnl` | P&L tracker rollup per snapshot; PK `(node, snapshot_ts)` |
| `live_execution_samples` | `runtimeProbe.strategyStats.live_execution` | execution counters + kill switch per snapshot |
| `arb_approvals` | `runtimeProbe.executionApprovals.pending[]` | one row per approval, odds/stakes upserted, `last_seen` tracks the newest snapshot |
| `arb_approval_stats` | `runtimeProbe.executionApprovals` envelope | approval-queue counters per snapshot |
| `arb_pairs` | `runtimeProbe.arbPositionPnl.pairs[]` | one row per tracked arb pair (upserted) |
| `trade_legs` | `runtimeProbe.arbPositionPnl.pairs[].legs[]` | one row per leg, PK `(node, pair_id, client_order_id)` |
| `shipper_state` | — | persistent high-water marks (see below) |

The six flat trade tables are defensive flatteners over the already-parsed status
payload, keyed by the probe's own `updatedAt` (`snapshot_ts`) so re-shipping an
unchanged status upserts idempotently. A missing or malformed block skips only that
table with a warning, never the cycle. `arbPositionPnl.pairs` is shipped by a separate
node-side change, so its absence is expected and tolerated silently until every node
carries it.

Schema is `schema.sql` (all `CREATE TABLE IF NOT EXISTS`); the shipper runs it every
startup, so it self-heals and is safe to re-run.

## Architecture

Each cycle (`SHIPPER_INTERVAL_SECS`, default 30s):

1. **Ensure schema** once at startup (idempotent).
2. **Replicate nodeops.db.** Opens SQLite with `file:...?mode=ro` + `PRAGMA
   query_only=ON` — never takes a write lock, never creates or mutates the file, so the
   concurrent WAL writer is undisturbed. For each source table it reads rows with
   `rowid > cursor ORDER BY rowid`, batch-inserts into Postgres with `ON CONFLICT DO
   NOTHING`, and advances the cursor.
3. **Status snapshots.** For each node, hashes the canonical (sorted-key) `status.json`
   and inserts `ON CONFLICT (node, content_sha) DO NOTHING`, so an unchanged status is
   never re-stored.
4. **Log tailing.** For each `sessions/<id>/{node.log,events.jsonl}` it resumes from the
   stored byte offset, reads only complete (newline-terminated) lines — a partial
   trailing line waits for the next cycle — truncates each to `SHIPPER_MAX_LINE_LEN`,
   assigns a monotonic `seq`, and inserts `ON CONFLICT DO NOTHING`.
5. **Retention prune** (Postgres side, best-effort): deletes `node_logs`/`node_events`
   older than `SHIPPER_LOG_RETENTION_DAYS` and `status_snapshots` older than
   `SHIPPER_STATUS_RETENTION_DAYS`.

### Exactly-once, anchored in Postgres

Cursors live in the `shipper_state` table, **not** on local disk, so a process restart
or a freshly rebuilt host resumes from the true high-water mark. Critically, the insert
and the cursor advance commit in the **same transaction**: a Postgres failure rolls both
back, so the un-acked batch re-ships on the next cycle and a row is **never skipped**.
The unique constraints make the replay idempotent (`ON CONFLICT DO NOTHING` → no dupes).

- SQLite tables: cursor = `{"rowid": <max rowid shipped>, "sha": <sha256 of the spec
  columns at that rowid>}` (a legacy bare-int cursor is still read and upgrades on the
  next advance). The fingerprint distinguishes a **rebuilt** source db from a
  **pruned** one: stored rowid above the source max, or a fingerprint mismatch at the
  stored rowid, means the db was rebuilt and shipping restarts from 0 (`ON CONFLICT`
  dedups the replay); a missing cursor row with higher rowids surviving is retention
  pruning and shipping resumes as-is. Without the identity check a rebuilt db whose
  rowids restart below the high-water mark would be silently skipped.
- Log files: cursor = `{"offset": <bytes>, "seq": <last seq>}` keyed by
  `log:<node>|<session>|<filename>`.

Log rotation/truncation is handled: if a file shrinks below the stored offset the offset
restarts at 0 while `seq` keeps climbing monotonically, so the `(node, session, seq)`
uniqueness never collides.

### Resilience (the cardinal property)

Any psycopg/connection error in any step logs a warning, rolls back that transaction,
leaves cursors/offsets untouched, and retries next cycle — the process never crashes on a
DB outage and never advances a high-water mark past uncommitted data. A malformed
`status.json` or a non-JSON `events.jsonl` line is skipped with a warning, never fatal.
One failing node directory does not block the others.

## Configuration (env)

Connection — `SHIPPER_DATABASE_URL` / `DATABASE_URL`, or the discrete
`SHIPPER_PG_HOST` / `PGHOST`, `SHIPPER_PG_PORT` / `PGPORT`, `SHIPPER_PG_DATABASE` /
`PGDATABASE`, `SHIPPER_PG_USER` / `PGUSER`, `SHIPPER_PG_PASSWORD` / `PGPASSWORD`,
`SHIPPER_PG_SSLMODE` / `PGSSLMODE` (use `require` for RDS).

| Var | Default | Meaning |
| --- | --- | --- |
| `NODEOPS_DB` | `/opt/cloudbet/nodeops/nodeops.db` | source SQLite DB (read-only) |
| `NODES_ROOT` | `/opt/cloudbet/strategy-nodes` | node directory root |
| `SHIPPER_INTERVAL_SECS` | `30` | cycle interval |
| `SHIPPER_MAX_LINE_LEN` | `8192` | per-log-line truncation |
| `SHIPPER_LOG_RETENTION_DAYS` | `14` | prune `node_logs`/`node_events` older than |
| `SHIPPER_STATUS_RETENTION_DAYS` | `30` | prune `status_snapshots` older than |
| `SHIPPER_BATCH_ROWS` | `1000` | SQLite read batch size |
| `SHIPPER_LOG_LEVEL` | `INFO` | log level |

## Deploy

```sh
sudo tools/shipper/install.sh
```

The installer creates `/opt/cloudbet/shipper/venv`, installs `psycopg[binary]`, copies
`shipper.py` + `schema.sql`, and installs + enables + starts the `shipper` systemd unit.
It is idempotent and re-runnable (an existing unit is kept so local overrides survive).

**Credentials** live in `/opt/cloudbet/shipper/shipper.env` (mode `600`, root-owned) and
are **not committed**. Create it before first start with the `SHIPPER_PG_*` values (see
the header of `install.sh` for a template). The systemd unit runs as root because the
node files and `nodeops.db` are root-owned on the deploy host.

```sh
systemctl status shipper
journalctl -u shipper -f
```

## Tests

```sh
cd tools/shipper && .venv/bin/python -m pytest tests/ -q
```

Hermetic — no network DB. They exercise cursor advance/replay-safety, idempotency, log
byte-offset resume + partial-line + truncation handling, status content-hash dedup,
resilience under injected DB errors, and the read-only SQLite open. If a real Postgres is
available (`pg_ctl` / `pytest-postgresql` / `testing.postgresql`), a schema + full-cycle
integration test can be added; it is otherwise deferred to on-host validation.
