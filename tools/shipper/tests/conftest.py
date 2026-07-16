"""
Shared fixtures and the in-memory fake PgWriter used across the shipper tests.

No test touches a network database. The fake mirrors the exactly-once contract of
:class:`PsycopgWriter`: ``ship_batch`` inserts + advances the cursor atomically, so an
injected failure leaves both untouched (the row re-ships next cycle) and a replay
collides on the unique key (no duplicate). ``insert_status`` dedups on
(node, content_sha).

"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

_CONFLICT_RE = re.compile(r"\(([^)]*)\)")


def _unwrap(value: Any) -> Any:
    """Mimic what Postgres stores for a jsonb column: the underlying object."""
    if type(value).__name__ in ("Jsonb", "Json"):
        return value.obj
    return value


class FailNow(Exception):
    """
    Injected DB error, mimicking a psycopg failure inside a transaction.
    """


class FakePgWriter:
    def __init__(self) -> None:
        self.schema_sql: str | None = None
        self.cursors: dict[str, str] = {}
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.table_keys: dict[str, set[tuple[Any, ...]]] = {}
        self.status: dict[tuple[str, str], dict[str, Any]] = {}
        self.connect_calls = 0
        # Injected failures: a callable count->bool deciding whether ship_batch raises.
        self.fail_ship_predicate: Any = None
        self._ship_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def close(self) -> None:
        pass

    def ensure_schema(self, sql: str) -> None:
        self.schema_sql = sql

    def read_cursor(self, source: str) -> str | None:
        return self.cursors.get(source)

    def ship_batch(
        self,
        table: str,
        columns: Any,
        conflict: str,
        rows: Any,
        cursor_source: str,
        cursor_value: str,
    ) -> int:
        self._ship_calls += 1
        if self.fail_ship_predicate is not None and self.fail_ship_predicate(self._ship_calls):
            # Transaction rolls back: NOTHING is stored and the cursor is NOT advanced.
            raise FailNow(f"injected ship failure #{self._ship_calls}")
        match = _CONFLICT_RE.search(conflict)
        if match is None:
            raise ValueError(f"no conflict columns in {conflict!r}")
        conflict_cols = [c.strip() for c in match.group(1).split(",")]
        store = self.tables.setdefault(table, [])
        keys = self.table_keys.setdefault(table, set())
        inserted = 0
        for row in rows:
            record = {col: _unwrap(val) for col, val in zip(columns, row, strict=True)}
            key = tuple(record[c] for c in conflict_cols)
            if key in keys:
                continue  # ON CONFLICT DO NOTHING
            keys.add(key)
            store.append(record)
            inserted += 1
        self.cursors[cursor_source] = cursor_value
        return inserted

    def insert_status(
        self,
        node: str,
        ts_utc: str,
        updated_at: str | None,
        payload: Any,
        content_sha: str,
    ) -> bool:
        key = (node, content_sha)
        if key in self.status:
            return False
        self.status[key] = {
            "node": node,
            "ts_utc": ts_utc,
            "updated_at": updated_at,
            "payload": payload,
            "content_sha": content_sha,
        }
        return True

    def prune_older_than(self, table: str, column: str, cutoff_iso: str) -> int:
        store = self.tables.get(table, [])
        keep = [r for r in store if str(r.get(column, "")) >= cutoff_iso]
        removed = len(store) - len(keep)
        self.tables[table] = keep
        return removed


@pytest.fixture
def fake_writer() -> FakePgWriter:
    return FakePgWriter()


@pytest.fixture
def nodeops_db(tmp_path: Path) -> Path:
    """
    Build a real temp SQLite DB shaped like the nodeops one (WAL mode, seeded).
    """
    db_path = tmp_path / "nodeops.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL, node TEXT NOT NULL,
            container_state TEXT, heartbeat_age_secs REAL, image TEXT,
            subscribed_instruments INTEGER, graph_nodes INTEGER, graph_edges INTEGER,
            quoted_edges INTEGER, semantic_match_instruments INTEGER,
            cross_venue_candidate_count INTEGER, rag_green INTEGER, rag_amber INTEGER,
            rag_red INTEGER, raw_detections INTEGER, valid_opportunities INTEGER,
            executable_candidates INTEGER, executed INTEGER, pending_approvals INTEGER,
            mem_mb REAL, cpu_pct REAL, started_at TEXT, uptime_secs REAL
        );
        CREATE TABLE odds_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL, node TEXT NOT NULL, kind TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL, username TEXT, action TEXT NOT NULL,
            node TEXT, params_summary TEXT, status TEXT NOT NULL
        );
        """,
    )
    conn.commit()
    conn.close()
    return db_path


def insert_sample(db_path: Path, node: str, ts_utc: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO samples (ts_utc, node, container_state, graph_nodes) VALUES (?, ?, ?, ?)",
        (ts_utc, node, "running", 42),
    )
    conn.commit()
    conn.close()


def insert_odds(db_path: Path, node: str, ts_utc: str, kind: str, payload_json: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO odds_samples (ts_utc, node, kind, payload_json) VALUES (?, ?, ?, ?)",
        (ts_utc, node, kind, payload_json),
    )
    conn.commit()
    conn.close()
