"""Hermetic tests for the nodeops -> Postgres shipper. No network DB is used."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import insert_odds, insert_sample

import shipper as mod


def _config(nodeops_db: Path, nodes_root: Path) -> mod.Config:
    env = {
        "NODEOPS_DB": str(nodeops_db),
        "NODES_ROOT": str(nodes_root),
        "SHIPPER_BATCH_ROWS": "1000",
        "SHIPPER_MAX_LINE_LEN": "50",
    }
    return mod.Config(env)


def _make(nodeops_db, nodes_root, fake_writer):
    config = _config(nodeops_db, nodes_root)
    return mod.Shipper(config, fake_writer, mod.SqliteSource(nodeops_db)), config


# --- 1. cursor logic ------------------------------------------------------


def test_new_rows_shipped_once_and_cursor_advances(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)

    shipper._ship_sqlite()

    assert len(fake_writer.tables["samples"]) == 2
    assert fake_writer.cursors["sqlite:samples"] == "2"

    # Second cycle with no new rows ships nothing further.
    shipper._ship_sqlite()
    assert len(fake_writer.tables["samples"]) == 2


def test_pg_failure_does_not_advance_cursor_and_reships(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)

    fake_writer.fail_ship_predicate = lambda n: n == 1  # fail first ship only
    shipper._ship_sqlite()  # swallowed per-table
    assert "sqlite:samples" not in fake_writer.cursors
    assert fake_writer.tables.get("samples", []) == []

    # Next cycle succeeds: the same row ships, no skip.
    fake_writer.fail_ship_predicate = None
    shipper._ship_sqlite()
    assert len(fake_writer.tables["samples"]) == 1
    assert fake_writer.cursors["sqlite:samples"] == "1"


def test_cursor_resume_from_stored_high_water_mark(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    insert_sample(nodeops_db, "nodeB", "2026-07-16T00:01:00Z")
    fake_writer.cursors["sqlite:samples"] = "1"  # rowid 1 already shipped
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)

    shipper._ship_sqlite()
    assert len(fake_writer.tables["samples"]) == 1
    assert fake_writer.tables["samples"][0]["node"] == "nodeB"


# --- 2. idempotency -------------------------------------------------------


def test_replay_is_idempotent_on_conflict(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()
    # Simulate a restart that lost its cursor and re-reads rowid 1.
    fake_writer.cursors.pop("sqlite:samples")
    shipper._ship_sqlite()
    assert len(fake_writer.tables["samples"]) == 1


def test_odds_payload_parsed_to_jsonb(tmp_path, nodeops_db, fake_writer):
    insert_odds(nodeops_db, "nodeA", "2026-07-16T00:00:00Z", "topPositiveCandidates", '[{"x":1}]')
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper._ship_sqlite()
    row = fake_writer.tables["odds_samples"][0]
    assert row["payload"] == [{"x": 1}]  # parsed, not the raw string


# --- 3. log tailing -------------------------------------------------------


def _make_session(nodes_root: Path, node: str, session: str) -> Path:
    session_dir = nodes_root / node / "sessions" / session
    session_dir.mkdir(parents=True)
    return session_dir


def test_log_offset_resume_and_partial_trailing_line(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    session_dir = _make_session(nodes_root, "nodeA", "sess1")
    log = session_dir / "node.log"
    log.write_text("line one\nline two\npartial")
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    shipper._ship_status_and_logs()
    rows = fake_writer.tables["node_logs"]
    assert [r["line"] for r in rows] == ["line one", "line two"]  # partial withheld
    assert [r["seq"] for r in rows] == [1, 2]

    # Complete the partial line + append; resume from the stored byte offset.
    with log.open("a") as handle:
        handle.write(" done\nline four\n")
    shipper._ship_status_and_logs()
    rows = fake_writer.tables["node_logs"]
    assert [r["line"] for r in rows] == ["line one", "line two", "partial done", "line four"]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]  # monotonic, unique


def test_log_truncation_restarts_offset_seq_monotonic(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    session_dir = _make_session(nodes_root, "nodeA", "sess1")
    log = session_dir / "node.log"
    log.write_text("aaaa\nbbbb\n")
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)
    shipper._ship_status_and_logs()
    assert [r["seq"] for r in fake_writer.tables["node_logs"]] == [1, 2]

    # Rotate in place: file shrinks below stored offset.
    log.write_text("cccc\n")
    shipper._ship_status_and_logs()
    seqs = [r["seq"] for r in fake_writer.tables["node_logs"]]
    assert seqs == [1, 2, 3]  # seq kept monotonic across truncation
    assert len(seqs) == len(set(seqs))


def test_line_truncated_to_max_len(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    session_dir = _make_session(nodes_root, "nodeA", "sess1")
    (session_dir / "node.log").write_text("x" * 200 + "\n")
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)  # max_line_len=50
    shipper._ship_status_and_logs()
    assert len(fake_writer.tables["node_logs"][0]["line"]) == 50


def test_events_jsonl_parsed_and_bad_line_skipped(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    session_dir = _make_session(nodes_root, "nodeA", "sess1")
    (session_dir / "events.jsonl").write_text('{"a":1}\nnot json\n{"b":2}\n')
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)
    shipper._ship_status_and_logs()
    payloads = [r["payload"] for r in fake_writer.tables["node_events"]]
    assert payloads == [{"a": 1}, {"b": 2}]  # malformed middle line skipped, not fatal


# --- 4. status dedup ------------------------------------------------------


def test_status_dedup_unchanged_then_changed(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "nodeA"
    node_dir.mkdir(parents=True)
    status = node_dir / "status.json"
    status.write_text(json.dumps({"runtimeProbe": {"graphNodes": 10}}))
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    shipper._ship_status_and_logs()
    assert len(fake_writer.status) == 1

    shipper._ship_status_and_logs()  # unchanged -> no new snapshot
    assert len(fake_writer.status) == 1

    status.write_text(json.dumps({"runtimeProbe": {"graphNodes": 20}}))
    shipper._ship_status_and_logs()  # changed -> new snapshot
    assert len(fake_writer.status) == 2


def test_status_dedup_stable_across_key_order(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "nodeA"
    node_dir.mkdir(parents=True)
    status = node_dir / "status.json"
    status.write_text('{"a":1,"b":2}')
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)
    shipper._ship_status_and_logs()
    status.write_text('{"b":2,"a":1}')  # same content, reordered keys
    shipper._ship_status_and_logs()
    assert len(fake_writer.status) == 1  # canonical hash matches


# --- 5. resilience --------------------------------------------------------


def test_db_error_never_raises_out_of_cycle(tmp_path, nodeops_db, fake_writer):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    nodes_root = tmp_path / "nodes"
    _make_session(nodes_root, "nodeA", "sess1")
    (nodes_root / "nodeA" / "sessions" / "sess1" / "node.log").write_text("l1\n")
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    fake_writer.fail_ship_predicate = lambda n: True  # every ship raises

    shipper.run_cycle()  # must not raise
    assert fake_writer.tables.get("samples", []) == []
    assert "sqlite:samples" not in fake_writer.cursors


def test_malformed_status_json_skipped(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / "nodeA"
    node_dir.mkdir(parents=True)
    (node_dir / "status.json").write_text("{not valid json")
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)
    shipper._ship_status_and_logs()  # must not raise
    assert len(fake_writer.status) == 0


def test_one_bad_node_dir_does_not_block_others(tmp_path, nodeops_db, fake_writer):
    nodes_root = tmp_path / "nodes"
    bad = nodes_root / "bad"
    bad.mkdir(parents=True)
    (bad / "status.json").write_text("garbage{")
    good = nodes_root / "good"
    good.mkdir(parents=True)
    (good / "status.json").write_text(json.dumps({"ok": True}))
    shipper, _ = _make(nodeops_db, nodes_root, fake_writer)

    shipper._ship_status_and_logs()
    stored_nodes = {rec["node"] for rec in fake_writer.status.values()}
    assert stored_nodes == {"good"}


def test_missing_nodes_root_is_not_fatal(tmp_path, nodeops_db, fake_writer):
    shipper, _ = _make(nodeops_db, tmp_path / "does-not-exist", fake_writer)
    shipper.run_cycle()  # must not raise
    assert fake_writer.status == {}


def test_missing_nodeops_db_is_not_fatal(tmp_path, fake_writer):
    missing = tmp_path / "absent.db"
    config = mod.Config({"NODEOPS_DB": str(missing), "NODES_ROOT": str(tmp_path / "nodes")})
    shipper = mod.Shipper(config, fake_writer, mod.SqliteSource(missing))
    shipper.run_cycle()  # must not raise
    assert fake_writer.tables.get("samples", []) == []
    assert fake_writer.status == {}


# --- 6. sqlite RO open ----------------------------------------------------


def test_sqlite_ro_open_sets_query_only_and_needs_no_write(tmp_path, nodeops_db):
    insert_sample(nodeops_db, "nodeA", "2026-07-16T00:00:00Z")
    os.chmod(nodeops_db, 0o444)  # read-only file perms
    try:
        source = mod.SqliteSource(nodeops_db)
        rows = source.read_new_rows("samples", mod.SAMPLE_COLUMNS, 0, 100)
        assert len(rows) == 1

        # query_only is enforced: a write on the RO connection is refused.
        conn = source._connect()
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO samples (ts_utc, node) VALUES ('x','y')")
        finally:
            conn.close()
    finally:
        os.chmod(nodeops_db, 0o644)


def test_schema_ensure_reads_schema_file(tmp_path, nodeops_db, fake_writer):
    shipper, _ = _make(nodeops_db, tmp_path / "nodes", fake_writer)
    shipper.ensure_schema()
    assert fake_writer.schema_sql is not None
    assert "CREATE TABLE IF NOT EXISTS status_snapshots" in fake_writer.schema_sql
    assert "shipper_state" in fake_writer.schema_sql
