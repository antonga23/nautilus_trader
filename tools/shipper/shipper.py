#!/usr/bin/env python3
"""
Host-side data shipper: nodeops SQLite + node dirs -> AWS RDS Postgres.

Purely additive and zero-restart. It never opens the nodeops SQLite database for
writing and never touches the nodeops service or the trading nodes; it opens the DB
read-only (``mode=ro`` + ``PRAGMA query_only=ON``) so the concurrent WAL writer is
undisturbed, tails the node directory artefacts, and streams everything into Postgres.

Exactly-once is anchored in Postgres, not on local disk: high-water marks
(per-table SQLite rowid + row fingerprint; per-logfile byte offset + last seq) live in the
``shipper_state`` table and advance only inside the same transaction that commits the
data they describe. A Postgres outage therefore re-ships the un-acked batch on the next
cycle and never skips a row. Any psycopg/connection error is caught, the transaction is
rolled back, cursors are left untouched, and the next cycle retries: the process does
not crash on a DB outage.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("shipper")

# ---------------------------------------------------------------------------
# Source-table definitions (mirror tools/nodeops/server.py Store._create_schema).
# ---------------------------------------------------------------------------

SAMPLE_COLUMNS: tuple[str, ...] = (
    "ts_utc",
    "node",
    "container_state",
    "heartbeat_age_secs",
    "image",
    "subscribed_instruments",
    "graph_nodes",
    "graph_edges",
    "quoted_edges",
    "semantic_match_instruments",
    "cross_venue_candidate_count",
    "rag_green",
    "rag_amber",
    "rag_red",
    "raw_detections",
    "valid_opportunities",
    "executable_candidates",
    "executed",
    "pending_approvals",
    "mem_mb",
    "cpu_pct",
    "started_at",
    "uptime_secs",
)
ODDS_COLUMNS: tuple[str, ...] = ("ts_utc", "node", "kind", "payload_json")
AUDIT_COLUMNS: tuple[str, ...] = (
    "ts_utc",
    "username",
    "action",
    "node",
    "params_summary",
    "status",
)


class SqliteTableSpec:
    """
    One SQLite source table and how it maps into Postgres.
    """

    def __init__(
        self,
        name: str,
        columns: tuple[str, ...],
        pg_columns: tuple[str, ...],
        conflict: str,
        jsonb_source: str | None = None,
    ) -> None:
        self.name = name
        self.columns = columns
        self.pg_columns = pg_columns
        self.conflict = conflict
        self.jsonb_source = jsonb_source

    @property
    def cursor_source(self) -> str:
        return f"sqlite:{self.name}"


SQLITE_TABLES: tuple[SqliteTableSpec, ...] = (
    SqliteTableSpec("samples", SAMPLE_COLUMNS, SAMPLE_COLUMNS, "(node, ts_utc)"),
    SqliteTableSpec(
        "odds_samples",
        ODDS_COLUMNS,
        ("ts_utc", "node", "kind", "payload"),
        "(node, ts_utc, kind)",
        jsonb_source="payload_json",
    ),
    SqliteTableSpec(
        "audit_log",
        AUDIT_COLUMNS,
        AUDIT_COLUMNS,
        "(ts_utc, action, status, node, username, params_summary)",
    ),
)

# ---------------------------------------------------------------------------
# Flat trade tables flattened from status.json at ship time.
# ---------------------------------------------------------------------------

ARB_PNL_COLUMNS: tuple[str, ...] = (
    "node",
    "snapshot_ts",
    "pairs_tracked",
    "pairs_open",
    "pairs_settled",
    "open_exposure",
    "open_guaranteed_pnl",
    "realized_pnl",
    "settlements_received",
    "settlements_unmatched",
)
LIVE_EXECUTION_COLUMNS: tuple[str, ...] = (
    "node",
    "snapshot_ts",
    "kill_switch_active",
    "halt_reason",
    "realized_loss",
    "notional_used",
    "max_daily_notional",
    "max_daily_loss",
    "attempts",
    "blocks",
    "submissions",
    "block_reasons",
    "submissions_by_venue",
)
APPROVAL_COLUMNS: tuple[str, ...] = (
    "node",
    "approval_id",
    "canonical_pair_id",
    "created_at",
    "expires_at",
    "match_type",
    "venue_a",
    "venue_b",
    "instrument_id_a",
    "instrument_id_b",
    "market_a",
    "market_b",
    "outcome_a",
    "outcome_b",
    "odds_a",
    "odds_b",
    "stake_a",
    "stake_b",
    "fee_adjusted_profit_margin",
    "raw_profit_margin",
    "expected_profit",
    "last_seen",
)
APPROVAL_UPDATE_COLUMNS: tuple[str, ...] = (
    "expires_at",
    "odds_a",
    "odds_b",
    "stake_a",
    "stake_b",
    "fee_adjusted_profit_margin",
    "raw_profit_margin",
    "expected_profit",
    "last_seen",
)
APPROVAL_STATS_COLUMNS: tuple[str, ...] = (
    "node",
    "snapshot_ts",
    "mode",
    "ttl_secs",
    "max_pending",
    "staged",
    "approved_executed",
    "approved_blocked",
    "rejected",
    "expired",
    "evicted",
    "commands_processed",
    "commands_invalid",
    "pending_count",
    "recent_decisions",
)
ARB_PAIR_COLUMNS: tuple[str, ...] = (
    "node",
    "pair_id",
    "settled",
    "void",
    "fully_hedged",
    "cross_currency",
    "base_currency",
    "winning_outcome",
    "exposure",
    "guaranteed_pnl",
    "best_case_pnl",
    "realized_pnl",
    "last_seen",
)
ARB_PAIR_UPDATE_COLUMNS: tuple[str, ...] = (
    "settled",
    "void",
    "fully_hedged",
    "cross_currency",
    "winning_outcome",
    "exposure",
    "guaranteed_pnl",
    "best_case_pnl",
    "realized_pnl",
    "last_seen",
)
TRADE_LEG_COLUMNS: tuple[str, ...] = (
    "node",
    "pair_id",
    "client_order_id",
    "venue",
    "outcome",
    "side",
    "currency",
    "stake",
    "exposure",
    "fill_count",
    "fills",
    "settlement_result",
    "last_seen",
)
TRADE_LEG_UPDATE_COLUMNS: tuple[str, ...] = (
    "venue",
    "outcome",
    "side",
    "currency",
    "stake",
    "exposure",
    "fill_count",
    "fills",
    "settlement_result",
    "last_seen",
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class Config:
    """
    Runtime configuration resolved from ``SHIPPER_*`` / ``PG*`` env vars.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.dsn = _resolve_dsn(env)
        self.db_path = Path(env.get("NODEOPS_DB", "/opt/cloudbet/nodeops/nodeops.db"))
        self.nodes_root = Path(env.get("NODES_ROOT", "/opt/cloudbet/strategy-nodes"))
        self.interval_secs = int(env.get("SHIPPER_INTERVAL_SECS", "30"))
        self.max_line_len = int(env.get("SHIPPER_MAX_LINE_LEN", "8192"))
        self.log_retention_days = int(env.get("SHIPPER_LOG_RETENTION_DAYS", "14"))
        self.status_retention_days = int(env.get("SHIPPER_STATUS_RETENTION_DAYS", "30"))
        self.batch_rows = int(env.get("SHIPPER_BATCH_ROWS", "1000"))


def _resolve_dsn(env: Mapping[str, str]) -> str:
    """
    Build a psycopg connection string from SHIPPER_PG_* / PG* / DATABASE_URL.
    """
    url = env.get("SHIPPER_DATABASE_URL") or env.get("DATABASE_URL")
    if url:
        return url
    parts: dict[str, str] = {}
    mapping = {
        "host": ("SHIPPER_PG_HOST", "PGHOST"),
        "port": ("SHIPPER_PG_PORT", "PGPORT"),
        "dbname": ("SHIPPER_PG_DATABASE", "PGDATABASE"),
        "user": ("SHIPPER_PG_USER", "PGUSER"),
        "password": ("SHIPPER_PG_PASSWORD", "PGPASSWORD"),
        "sslmode": ("SHIPPER_PG_SSLMODE", "PGSSLMODE"),
    }
    for key, (primary, fallback) in mapping.items():
        value = env.get(primary) or env.get(fallback)
        if value:
            parts[key] = value
    return " ".join(f"{key}={value}" for key, value in parts.items())


# ---------------------------------------------------------------------------
# PgWriter interface + psycopg implementation. All DB access lives behind this so
# the cycle is unit-testable against a fake.
# ---------------------------------------------------------------------------


class PgWriter(Protocol):
    def connect(self) -> None: ...

    def close(self) -> None: ...

    def ensure_schema(self, sql: str) -> None: ...

    def read_cursor(self, source: str) -> str | None: ...

    def ship_batch(
        self,
        table: str,
        columns: Sequence[str],
        conflict: str,
        rows: Sequence[Sequence[Any]],
        cursor_source: str,
        cursor_value: str,
    ) -> int: ...

    def upsert_rows(
        self,
        table: str,
        columns: Sequence[str],
        conflict: str,
        rows: Sequence[Sequence[Any]],
        update_columns: Sequence[str] = (),
    ) -> int: ...

    def insert_status(
        self,
        node: str,
        ts_utc: str,
        updated_at: str | None,
        payload: Any,
        content_sha: str,
    ) -> bool: ...

    def prune_older_than(self, table: str, column: str, cutoff_iso: str) -> int: ...


class PsycopgWriter:
    """
    Concrete :class:`PgWriter` over a psycopg (v3) connection.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None

    def connect(self) -> None:
        import psycopg

        if self._conn is not None and not self._conn.closed:
            return
        self._conn = psycopg.connect(self._dsn, autocommit=False)

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            with contextlib.suppress(Exception):  # best-effort teardown
                self._conn.close()
        self._conn = None

    def ensure_schema(self, sql: str) -> None:
        self.connect()
        with self._conn.transaction():
            self._conn.execute(sql)

    def read_cursor(self, source: str) -> str | None:
        self.connect()
        with self._conn.transaction():
            row = self._conn.execute(
                "SELECT cursor FROM shipper_state WHERE source = %s",
                (source,),
            ).fetchone()
        return None if row is None else row[0]

    def ship_batch(
        self,
        table: str,
        columns: Sequence[str],
        conflict: str,
        rows: Sequence[Sequence[Any]],
        cursor_source: str,
        cursor_value: str,
    ) -> int:
        self.connect()
        placeholders = ", ".join(["%s"] * len(columns))
        col_sql = ", ".join(columns)
        insert_sql = (
            # table/columns/conflict come from the fixed SQLITE_TABLES / log specs,
            # never user input; values are bound parameters.
            f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT {conflict} DO NOTHING"
        )
        inserted = 0
        with self._conn.transaction():
            if rows:
                with self._conn.cursor() as cur:
                    cur.executemany(insert_sql, rows)
                    inserted = cur.rowcount if cur.rowcount is not None else 0
            self._upsert_cursor(cursor_source, cursor_value)
        return inserted

    def upsert_rows(
        self,
        table: str,
        columns: Sequence[str],
        conflict: str,
        rows: Sequence[Sequence[Any]],
        update_columns: Sequence[str] = (),
    ) -> int:
        if not rows:
            return 0
        self.connect()
        placeholders = ", ".join(["%s"] * len(columns))
        col_sql = ", ".join(columns)
        if update_columns:
            action = "DO UPDATE SET " + ", ".join(
                f"{column} = EXCLUDED.{column}" for column in update_columns
            )
        else:
            action = "DO NOTHING"
        upsert_sql = (
            # table/columns/conflict come from the fixed flat-table specs, never user
            # input; values are bound parameters.
            f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT {conflict} {action}"
        )
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.executemany(upsert_sql, rows)
            return cur.rowcount if cur.rowcount is not None else 0

    def insert_status(
        self,
        node: str,
        ts_utc: str,
        updated_at: str | None,
        payload: Any,
        content_sha: str,
    ) -> bool:
        from psycopg.types.json import Jsonb

        self.connect()
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO status_snapshots "
                "(node, ts_utc, updated_at, payload, content_sha) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (node, content_sha) DO NOTHING",
                (node, ts_utc, updated_at, Jsonb(payload), content_sha),
            )
            return bool(cur.rowcount)

    def prune_older_than(self, table: str, column: str, cutoff_iso: str) -> int:
        self.connect()
        with self._conn.transaction(), self._conn.cursor() as cur:
            # table/column are fixed retention targets, not user input; cutoff is bound.
            cur.execute(f"DELETE FROM {table} WHERE {column} < %s", (cutoff_iso,))  # noqa: S608
            return cur.rowcount if cur.rowcount is not None else 0

    def _upsert_cursor(self, source: str, cursor_value: str) -> None:
        self._conn.execute(
            "INSERT INTO shipper_state (source, cursor, updated_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (source) DO UPDATE SET cursor = EXCLUDED.cursor, "
            "updated_at = EXCLUDED.updated_at",
            (source, cursor_value),
        )


# ---------------------------------------------------------------------------
# SQLite read-only source
# ---------------------------------------------------------------------------


class SqliteSource:
    """
    Read-only, non-intrusive reader over the live nodeops WAL database.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def available(self) -> bool:
        return self._db_path.exists()

    def _connect(self) -> sqlite3.Connection:
        # mode=ro never creates or writes the file and takes no write lock, so the
        # concurrent nodeops WAL writer is undisturbed. query_only hard-guarantees it.
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def read_new_rows(
        self,
        table: str,
        columns: Sequence[str],
        after_rowid: int,
        limit: int,
    ) -> list[tuple[int, dict[str, Any]]]:
        col_sql = ", ".join(columns)
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"SELECT rowid AS _rowid, {col_sql} FROM {table} "  # noqa: S608 - columns from fixed spec
                "WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
                (after_rowid, limit),
            )
            out: list[tuple[int, dict[str, Any]]] = []
            for record in cursor.fetchall():
                mapping = {column: record[column] for column in columns}
                out.append((int(record["_rowid"]), mapping))
            return out
        finally:
            conn.close()

    def max_rowid(self, table: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(f"SELECT max(rowid) FROM {table}").fetchone()  # noqa: S608 - table from fixed spec
            return 0 if row is None or row[0] is None else int(row[0])
        finally:
            conn.close()

    def read_row(self, table: str, columns: Sequence[str], rowid: int) -> dict[str, Any] | None:
        col_sql = ", ".join(columns)
        conn = self._connect()
        try:
            record = conn.execute(
                f"SELECT {col_sql} FROM {table} WHERE rowid = ?",  # noqa: S608 - columns from fixed spec
                (rowid,),
            ).fetchone()
            if record is None:
                return None
            return {column: record[column] for column in columns}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Shipper orchestration
# ---------------------------------------------------------------------------


class Shipper:
    def __init__(self, config: Config, writer: PgWriter, source: SqliteSource) -> None:
        self._config = config
        self._writer = writer
        self._source = source

    def ensure_schema(self) -> None:
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        self._writer.ensure_schema(schema_path.read_text(encoding="utf8"))

    def run_cycle(self) -> None:
        """
        One full cycle.

        Each step is isolated so one failure never blocks the rest.

        """
        self._safely(self._ship_sqlite, "sqlite replication")
        self._safely(self._ship_status_and_logs, "node directory shipping")
        self._safely(self._prune, "retention prune")

    def _safely(self, func: Any, label: str) -> None:
        try:
            func()
        except Exception as exc:
            logger.warning("%s failed this cycle (will retry): %s", label, exc)

    # -- SQLite tables ------------------------------------------------------

    def _ship_sqlite(self) -> None:
        if not self._source.available():
            logger.debug("nodeops db %s not present yet; skipping", self._config.db_path)
            return
        for spec in SQLITE_TABLES:
            try:
                self._ship_one_table(spec)
            except Exception as exc:
                logger.warning("shipping %s failed (will retry): %s", spec.name, exc)

    def _ship_one_table(self, spec: SqliteTableSpec) -> None:
        stored_rowid, stored_sha = _parse_sqlite_cursor(
            self._writer.read_cursor(spec.cursor_source),
        )
        after_rowid = self._verified_start_rowid(spec, stored_rowid, stored_sha)
        total = 0
        while True:
            batch = self._source.read_new_rows(
                spec.name,
                spec.columns,
                after_rowid,
                self._config.batch_rows,
            )
            if not batch:
                break
            max_rowid = batch[-1][0]
            rows = [self._pg_row(spec, mapping) for _, mapping in batch]
            cursor_value = json.dumps(
                {"rowid": max_rowid, "sha": _row_sha(spec.columns, batch[-1][1])},
            )
            # The insert and the cursor advance commit in ONE transaction: a PG failure
            # rolls both back, so the same rows re-ship next cycle (never skipped).
            self._writer.ship_batch(
                spec.name,
                spec.pg_columns,
                spec.conflict,
                rows,
                spec.cursor_source,
                cursor_value,
            )
            after_rowid = max_rowid
            total += len(batch)
            if len(batch) < self._config.batch_rows:
                break
        if total == 0 and after_rowid < stored_rowid:
            # Rebuild detected but the fresh db had nothing to ship: persist the reset
            # so the next cycle does not re-detect. A cursor retreat with no data is
            # safe (never an advance past unshipped rows).
            self._writer.ship_batch(
                spec.name,
                spec.pg_columns,
                spec.conflict,
                [],
                spec.cursor_source,
                json.dumps({"rowid": 0, "sha": None}),
            )
            return
        if total:
            logger.info("shipped %d %s rows (cursor -> %d)", total, spec.name, after_rowid)

    def _verified_start_rowid(
        self,
        spec: SqliteTableSpec,
        stored_rowid: int,
        stored_sha: str | None,
    ) -> int:
        """
        Decide where to resume, distinguishing a rebuilt source db (rowids restarted;
        the bare high-water mark would silently skip everything below it) from retention
        pruning (same db, low rowids deleted; resuming is correct).
        """
        if stored_rowid <= 0:
            return 0
        max_rowid = self._source.max_rowid(spec.name)
        if stored_rowid > max_rowid:
            logger.warning(
                "cursor %s at rowid %d but source max rowid is %d: source db was "
                "rebuilt; re-shipping from 0 (ON CONFLICT dedups)",
                spec.cursor_source,
                stored_rowid,
                max_rowid,
            )
            return 0
        row = self._source.read_row(spec.name, spec.columns, stored_rowid)
        if row is None:
            # Cursor row deleted but higher rowids survive: retention prune, same db.
            return stored_rowid
        if stored_sha is None:
            # Legacy bare-int cursor: no fingerprint to check; upgrades on next advance.
            return stored_rowid
        if _row_sha(spec.columns, row) != stored_sha:
            logger.warning(
                "cursor %s fingerprint mismatch at rowid %d: source db was rebuilt; "
                "re-shipping from 0 (ON CONFLICT dedups)",
                spec.cursor_source,
                stored_rowid,
            )
            return 0
        return stored_rowid

    def _pg_row(self, spec: SqliteTableSpec, mapping: dict[str, Any]) -> list[Any]:
        if spec.jsonb_source is None:
            return [mapping[column] for column in spec.columns]
        # odds_samples: payload_json (text) -> payload (jsonb).
        row: list[Any] = []
        for column in spec.pg_columns:
            if column == "payload":
                row.append(_wrap_jsonb(_parse_json(mapping[spec.jsonb_source])))
            else:
                row.append(mapping[column])
        return row

    # -- node dirs: status + logs ------------------------------------------

    def _ship_status_and_logs(self) -> None:
        for node_dir in self._node_dirs():
            node = node_dir.name
            self._safely(lambda nd=node_dir, n=node: self._ship_status(n, nd), f"status {node}")
            self._safely(lambda nd=node_dir, n=node: self._ship_logs(n, nd), f"logs {node}")

    def _node_dirs(self) -> list[Path]:
        try:
            return sorted(
                path
                for path in self._config.nodes_root.iterdir()
                if path.is_dir() and path.name != "archives"
            )
        except OSError as exc:
            logger.warning("nodes root %s not readable: %s", self._config.nodes_root, exc)
            return []

    def _ship_status(self, node: str, node_dir: Path) -> None:
        status_path = node_dir / "status.json"
        payload = _read_json_file(status_path)
        if payload is None:
            return
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        content_sha = hashlib.sha256(canonical.encode("utf8")).hexdigest()
        ts_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated_at = _status_updated_at(payload)
        inserted = self._writer.insert_status(node, ts_utc, updated_at, payload, content_sha)
        if inserted:
            logger.info("status snapshot stored for %s (sha=%s)", node, content_sha[:12])
        self._ship_flat_trades(node, payload)

    # -- flat trade tables from status.json ----------------------------------

    def _ship_flat_trades(self, node: str, payload: dict[str, Any]) -> None:
        """
        Flatten the already-parsed status payload into the queryable trade tables.

        Every row is keyed on snapshot_ts = the probe's own updatedAt, so re-shipping an
        unchanged status collides on the PK and the upserts stay idempotent.

        """
        probe = payload.get("runtimeProbe")
        if not isinstance(probe, dict):
            return
        snapshot_ts = _status_updated_at(payload)
        if snapshot_ts is None or not _is_parseable_ts(snapshot_ts):
            logger.debug(
                "status for %s has no parseable updatedAt; skipping flat trade tables",
                node,
            )
            return
        flatteners: tuple[tuple[str, Any], ...] = (
            ("arb_pnl_samples", self._flatten_arb_pnl),
            ("live_execution_samples", self._flatten_live_execution),
            ("arb_approvals", self._flatten_approvals),
            ("arb_approval_stats", self._flatten_approval_stats),
            ("arb_pairs/trade_legs", self._flatten_pairs_and_legs),
        )
        for label, flatten in flatteners:
            try:
                flatten(node, snapshot_ts, probe)
            except Exception as exc:
                logger.warning("flattening %s for %s failed (will retry): %s", label, node, exc)

    def _flatten_arb_pnl(self, node: str, snapshot_ts: str, probe: dict[str, Any]) -> None:
        block = probe.get("arbPositionPnl")
        if not isinstance(block, dict) or not block:
            return
        row = [node, snapshot_ts, *(block.get(column) for column in ARB_PNL_COLUMNS[2:])]
        self._writer.upsert_rows("arb_pnl_samples", ARB_PNL_COLUMNS, "(node, snapshot_ts)", [row])

    def _flatten_live_execution(self, node: str, snapshot_ts: str, probe: dict[str, Any]) -> None:
        stats = probe.get("strategyStats")
        if not isinstance(stats, dict):
            return
        block = stats.get("live_execution")
        if block is None:
            return
        if not isinstance(block, dict):
            raise TypeError(f"live_execution block is {type(block).__name__}, expected dict")
        row: list[Any] = [node, snapshot_ts]
        for column in LIVE_EXECUTION_COLUMNS[2:]:
            value = block.get(column)
            if column in ("block_reasons", "submissions_by_venue") and value is not None:
                value = _wrap_jsonb(value)
            row.append(value)
        self._writer.upsert_rows(
            "live_execution_samples",
            LIVE_EXECUTION_COLUMNS,
            "(node, snapshot_ts)",
            [row],
        )

    def _flatten_approvals(self, node: str, snapshot_ts: str, probe: dict[str, Any]) -> None:
        envelope = probe.get("executionApprovals")
        if not isinstance(envelope, dict):
            return
        pending = envelope.get("pending")
        if not isinstance(pending, list):
            return
        rows: list[list[Any]] = []
        skipped = 0
        for entry in pending:
            if not isinstance(entry, dict) or not entry.get("approval_id"):
                skipped += 1
                continue
            rows.append(
                [node, *(entry.get(column) for column in APPROVAL_COLUMNS[1:-1]), snapshot_ts],
            )
        if skipped:
            logger.warning("skipped %d malformed pending approvals for %s", skipped, node)
        if rows:
            self._writer.upsert_rows(
                "arb_approvals",
                APPROVAL_COLUMNS,
                "(node, approval_id)",
                rows,
                APPROVAL_UPDATE_COLUMNS,
            )

    def _flatten_approval_stats(self, node: str, snapshot_ts: str, probe: dict[str, Any]) -> None:
        envelope = probe.get("executionApprovals")
        if not isinstance(envelope, dict) or not envelope:
            return
        pending = envelope.get("pending")
        row: list[Any] = [node, snapshot_ts]
        for column in APPROVAL_STATS_COLUMNS[2:]:
            if column == "pending_count":
                row.append(len(pending) if isinstance(pending, list) else None)
            elif column == "recent_decisions":
                value = envelope.get(column)
                row.append(None if value is None else _wrap_jsonb(value))
            else:
                row.append(envelope.get(column))
        self._writer.upsert_rows(
            "arb_approval_stats",
            APPROVAL_STATS_COLUMNS,
            "(node, snapshot_ts)",
            [row],
        )

    def _flatten_pairs_and_legs(self, node: str, snapshot_ts: str, probe: dict[str, Any]) -> None:
        block = probe.get("arbPositionPnl")
        if not isinstance(block, dict):
            return
        pairs = block.get("pairs")
        if pairs is None:
            # The per-pair breakdown ships in a separate node-side change; until every
            # node carries it, absence is expected and not a warning.
            logger.debug("arbPositionPnl.pairs absent for %s; skipping pair/leg tables", node)
            return
        if not isinstance(pairs, list):
            raise TypeError(f"arbPositionPnl.pairs is {type(pairs).__name__}, expected list")
        pair_rows: list[list[Any]] = []
        leg_rows: list[list[Any]] = []
        for pair in pairs:
            if not isinstance(pair, dict) or not pair.get("pair_id"):
                continue
            pair_id = pair["pair_id"]
            pair_rows.append(
                [
                    node,
                    pair_id,
                    *(pair.get(column) for column in ARB_PAIR_COLUMNS[2:-1]),
                    snapshot_ts,
                ],
            )
            legs = pair.get("legs")
            if not isinstance(legs, list):
                continue
            leg_rows.extend(
                _leg_row(node, pair_id, leg, snapshot_ts)
                for leg in legs
                if isinstance(leg, dict) and leg.get("client_order_id")
            )
        if pair_rows:
            self._writer.upsert_rows(
                "arb_pairs",
                ARB_PAIR_COLUMNS,
                "(node, pair_id)",
                pair_rows,
                ARB_PAIR_UPDATE_COLUMNS,
            )
        if leg_rows:
            self._writer.upsert_rows(
                "trade_legs",
                TRADE_LEG_COLUMNS,
                "(node, pair_id, client_order_id)",
                leg_rows,
                TRADE_LEG_UPDATE_COLUMNS,
            )

    def _ship_logs(self, node: str, node_dir: Path) -> None:
        sessions_dir = node_dir / "sessions"
        if not sessions_dir.is_dir():
            return
        try:
            session_dirs = sorted(p for p in sessions_dir.iterdir() if p.is_dir())
        except OSError as exc:
            logger.warning("sessions dir %s not readable: %s", sessions_dir, exc)
            return
        for session_dir in session_dirs:
            session_id = session_dir.name
            self._ship_logfile(node, session_id, session_dir / "node.log", "node_logs")
            self._ship_logfile(node, session_id, session_dir / "events.jsonl", "node_events")

    def _ship_logfile(self, node: str, session_id: str, path: Path, table: str) -> None:
        if not path.is_file():
            return
        filename = path.name
        source = f"log:{node}|{session_id}|{filename}"
        cursor_raw = self._writer.read_cursor(source)
        offset, seq = _parse_log_cursor(cursor_raw)

        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < offset:
            # Rotation/truncation: file shrank under us. Restart from the top but keep
            # seq monotonic so the unique (node, session, seq) namespace never collides.
            logger.info("log %s shrank (%d < %d); restarting offset", source, size, offset)
            offset = 0

        lines, new_offset = self._read_complete_lines(path, offset)
        if not lines:
            return

        rows: list[list[Any]] = []
        is_events = table == "node_events"
        ts_ingest = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        assigned_seq = seq
        for line in lines:
            assigned_seq += 1
            text = line[: self._config.max_line_len]
            if is_events:
                parsed = _parse_json(text)
                if parsed is None:
                    logger.warning("skipping non-JSON events line in %s", source)
                    continue
                rows.append([node, session_id, assigned_seq, ts_ingest, _wrap_jsonb(parsed)])
            else:
                rows.append([node, session_id, assigned_seq, ts_ingest, text])

        columns = ("node", "session_id", "seq", "ts_ingest", "payload" if is_events else "line")
        new_cursor = json.dumps({"offset": new_offset, "seq": assigned_seq})
        # Insert + offset/seq advance commit together: a PG failure re-ships the byte
        # range next cycle; ON CONFLICT keeps the replay idempotent.
        self._writer.ship_batch(
            table,
            columns,
            "(node, session_id, seq)",
            rows,
            source,
            new_cursor,
        )
        logger.info("shipped %d lines from %s (offset -> %d)", len(rows), source, new_offset)

    def _read_complete_lines(self, path: Path, offset: int) -> tuple[list[str], int]:
        """
        Read newline-terminated lines from ``offset``; a partial trailing line waits.
        """
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        if not data:
            return [], offset
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            # No complete line yet; hold the offset so the partial line is not shipped.
            return [], offset
        complete = data[: last_nl + 1]
        new_offset = offset + len(complete)
        lines = complete.decode("utf8", errors="replace").splitlines()
        return [line for line in lines if line], new_offset

    # -- retention ----------------------------------------------------------

    def _prune(self) -> None:
        now = datetime.now(UTC)
        log_cutoff = (now - timedelta(days=self._config.log_retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        status_cutoff = (now - timedelta(days=self._config.status_retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        for table, column, cutoff in (
            ("node_logs", "ts_ingest", log_cutoff),
            ("node_events", "ts_ingest", log_cutoff),
            ("status_snapshots", "ts_utc", status_cutoff),
        ):
            try:
                removed = self._writer.prune_older_than(table, column, cutoff)
                if removed:
                    logger.info("pruned %d rows from %s", removed, table)
            except Exception as exc:
                logger.warning("prune of %s failed (will retry): %s", table, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json(text: Any) -> Any:
    if text is None:
        return None
    if not isinstance(text, (str, bytes, bytearray)):
        return text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _wrap_jsonb(obj: Any) -> Any:
    """
    Wrap a python object for a jsonb column when psycopg is present.
    """
    try:
        from psycopg.types.json import Jsonb
    except Exception:  # pragma: no cover - fake writer path in tests
        return obj
    return Jsonb(obj)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except PermissionError:
        logger.warning("permission denied reading %s; shipper needs read access", path)
        return None
    except (OSError, ValueError):
        return None


def _status_updated_at(payload: dict[str, Any]) -> str | None:
    for key in ("updatedAt", "updated_at", "timestamp", "at"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _parse_sqlite_cursor(raw: str | None) -> tuple[int, str | None]:
    """
    Parse a sqlite-table cursor: JSON ``{"rowid": N, "sha": "..."}`` or a legacy bare
    int (pre-fingerprint format, read as sha=None).
    """
    if not raw:
        return 0, None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return 0, None
    if isinstance(data, dict):
        try:
            rowid = int(data.get("rowid", 0))
        except (ValueError, TypeError):
            return 0, None
        sha = data.get("sha")
        return rowid, sha if isinstance(sha, str) else None
    try:
        return int(data), None
    except (ValueError, TypeError):
        return 0, None


def _row_sha(columns: Sequence[str], mapping: Mapping[str, Any]) -> str:
    """
    Deterministic fingerprint of the stable column values at a rowid (rowid excluded):

    the identity check that tells a rebuilt source db from the one the cursor came from.

    """
    canonical = json.dumps(
        [mapping[column] for column in columns],
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf8")).hexdigest()


def _leg_row(node: str, pair_id: str, leg: dict[str, Any], snapshot_ts: str) -> list[Any]:
    fills = leg.get("fills")
    if isinstance(fills, list):
        fill_count: int | None = len(fills)
    elif isinstance(fills, int):
        fill_count = fills
    else:
        fill_count = None
    return [
        node,
        pair_id,
        leg["client_order_id"],
        leg.get("venue"),
        leg.get("outcome"),
        leg.get("side"),
        leg.get("currency"),
        leg.get("stake"),
        leg.get("exposure"),
        fill_count,
        _wrap_jsonb(fills) if isinstance(fills, list) else None,
        leg.get("settlement_result"),
        snapshot_ts,
    ]


def _is_parseable_ts(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_log_cursor(raw: str | None) -> tuple[int, int]:
    if not raw:
        return 0, 0
    try:
        data = json.loads(raw)
        return int(data.get("offset", 0)), int(data.get("seq", 0))
    except (ValueError, TypeError):
        return 0, 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


class _StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self, *_: Any) -> None:
        self.stopped = True


def build_shipper(config: Config) -> Shipper:
    writer = PsycopgWriter(config.dsn)
    source = SqliteSource(config.db_path)
    return Shipper(config, writer, source)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("SHIPPER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config()
    shipper = build_shipper(config)

    flag = _StopFlag()
    signal.signal(signal.SIGTERM, flag.stop)
    signal.signal(signal.SIGINT, flag.stop)

    logger.info(
        "shipper starting: db=%s nodes_root=%s interval=%ds",
        config.db_path,
        config.nodes_root,
        config.interval_secs,
    )
    schema_ready = False
    while not flag.stopped:
        if not schema_ready:
            try:
                shipper.ensure_schema()
                schema_ready = True
                logger.info("postgres schema ensured")
            except Exception as exc:
                logger.warning("schema ensure failed (will retry): %s", exc)
        if schema_ready:
            shipper.run_cycle()
        _sleep_interruptible(config.interval_secs, flag)
    logger.info("shipper stopped")
    return 0


def _sleep_interruptible(seconds: int, flag: _StopFlag) -> None:
    deadline = time.monotonic() + seconds
    while not flag.stopped and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    sys.exit(main())
